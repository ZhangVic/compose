from __future__ import absolute_import
from __future__ import print_function
from __future__ import unicode_literals

import argparse
import os
import sys
import time
from distutils.core import run_setup

import pypandoc
from jinja2 import Template
from release.bintray import BintrayAPI
from release.const import BINTRAY_ORG
from release.const import NAME
from release.const import REPO_ROOT
from release.downloader import BinaryDownloader
from release.images import ImageManager
from release.repository import delete_assets
from release.repository import get_contributors
from release.repository import Repository
from release.repository import upload_assets
from release.utils import branch_name
from release.utils import compatibility_matrix
from release.utils import read_release_notes_from_changelog
from release.utils import ScriptError
from release.utils import update_init_py_version
from release.utils import update_run_sh_version
from release.utils import yesno


def create_initial_branch(repository, args):
    release_branch = repository.create_release_branch(args.release, args.base)
    if args.base and args.cherries:
        print('Detected patch version.')
        cherries = input('Indicate (space-separated) PR numbers to cherry-pick then press Enter:\n')
        repository.cherry_pick_prs(release_branch, cherries.split())

    return create_bump_commit(repository, release_branch, args.bintray_user, args.bintray_org)


def create_bump_commit(repository, release_branch, bintray_user, bintray_org):
    with release_branch.config_reader() as cfg:
        release = cfg.get('release')
    print('Updating version info in __init__.py and run.sh')
    update_run_sh_version(release)
    update_init_py_version(release)

    input('Please add the release notes to the CHANGELOG.md file, then press Enter to continue.')
    proceed = None
    while not proceed:
        print(repository.diff())
        proceed = yesno('Are these changes ok? y/N ', default=False)

    if repository.diff():
        repository.create_bump_commit(release_branch, release)
    repository.push_branch_to_remote(release_branch)

    bintray_api = BintrayAPI(os.environ['BINTRAY_TOKEN'], bintray_user)
    print('Creating data repository {} on bintray'.format(release_branch.name))
    bintray_api.create_repository(bintray_org, release_branch.name, 'generic')


def monitor_pr_status(pr_data):
    print('Waiting for CI to complete...')
    last_commit = pr_data.get_commits().reversed[0]
    while True:
        status = last_commit.get_combined_status()
        if status.state == 'pending' or status.state == 'failure':
            summary = {
                'pending': 0,
                'success': 0,
                'failure': 0,
            }
            for detail in status.statuses:
                if detail.context == 'dco-signed':
                    # dco-signed check breaks on merge remote-tracking ; ignore it
                    continue
                summary[detail.state] += 1
            print('{pending} pending, {success} successes, {failure} failures'.format(**summary))
            if status.total_count == 0:
                # Mostly for testing purposes against repos with no CI setup
                return True
            elif summary['pending'] == 0 and summary['failure'] == 0:
                return True
            elif summary['failure'] > 0:
                raise ScriptError('CI failures detected!')
            time.sleep(30)
        elif status.state == 'success':
            print('{} successes: all clear!'.format(status.total_count))
            return True


def check_pr_mergeable(pr_data):
    if not pr_data.mergeable:
        print(
            'WARNING!! PR #{} can not currently be merged. You will need to '
            'resolve the conflicts manually before finalizing the release.'.format(pr_data.number)
        )
    return pr_data.mergeable


def create_release_draft(repository, version, pr_data, files):
    print('Creating Github release draft')
    with open(os.path.join(os.path.dirname(__file__), 'release.md.tmpl'), 'r') as f:
        template = Template(f.read())
    print('Rendering release notes based on template')
    release_notes = template.render(
        version=version,
        compat_matrix=compatibility_matrix(),
        integrity=files,
        contributors=get_contributors(pr_data),
        changelog=read_release_notes_from_changelog(),
    )
    gh_release = repository.create_release(
        version, release_notes, draft=True, prerelease='-rc' in version,
        target_commitish='release'
    )
    print('Release draft initialized')
    return gh_release


def print_final_instructions(args):
    print(
        "You're almost done! Please verify that everything is in order and "
        "you are ready to make the release public, then run the following "
        "command:\n{exe} -b {user} finalize {version}".format(
            exe=sys.argv[0], user=args.bintray_user, version=args.release
        )
    )


def resume(args):
    try:
        repository = Repository(REPO_ROOT, args.repo)
        br_name = branch_name(args.release)
        if not repository.branch_exists(br_name):
            raise ScriptError('No local branch exists for this release.')
        gh_release = repository.find_release(args.release)
        if gh_release and not gh_release.draft:
            print('WARNING!! Found non-draft (public) release for this version!')
            proceed = yesno(
                'Are you sure you wish to proceed? Modifying an already '
                'released version is dangerous! y/N ', default=False
            )
            if proceed.lower() is not True:
                raise ScriptError('Aborting release')

        release_branch = repository.checkout_branch(br_name)
        if args.cherries:
            cherries = input('Indicate (space-separated) PR numbers to cherry-pick then press Enter:\n')
            repository.cherry_pick_prs(release_branch, cherries.split())

        create_bump_commit(repository, release_branch, args.bintray_user, args.bintray_org)
        pr_data = repository.find_release_pr(args.release)
        if not pr_data:
            pr_data = repository.create_release_pull_request(args.release)
        check_pr_mergeable(pr_data)
        monitor_pr_status(pr_data)
        downloader = BinaryDownloader(args.destination)
        files = downloader.download_all(args.release)
        if not gh_release:
            gh_release = create_release_draft(repository, args.release, pr_data, files)
        delete_assets(gh_release)
        upload_assets(gh_release, files)
        img_manager = ImageManager(args.release)
        img_manager.build_images(repository, files)
    except ScriptError as e:
        print(e)
        return 1

    print_final_instructions(args)
    return 0


def cancel(args):
    try:
        repository = Repository(REPO_ROOT, args.repo)
        repository.close_release_pr(args.release)
        repository.remove_release(args.release)
        repository.remove_bump_branch(args.release)
        bintray_api = BintrayAPI(os.environ['BINTRAY_TOKEN'], args.bintray_user)
        print('Removing Bintray data repository for {}'.format(args.release))
        bintray_api.delete_repository(args.bintray_org, branch_name(args.release))
    except ScriptError as e:
        print(e)
        return 1
    print('Release cancellation complete.')
    return 0


def start(args):
    try:
        repository = Repository(REPO_ROOT, args.repo)
        create_initial_branch(repository, args)
        pr_data = repository.create_release_pull_request(args.release)
        check_pr_mergeable(pr_data)
        monitor_pr_status(pr_data)
        downloader = BinaryDownloader(args.destination)
        files = downloader.download_all(args.release)
        gh_release = create_release_draft(repository, args.release, pr_data, files)
        upload_assets(gh_release, files)
        img_manager = ImageManager(args.release)
        img_manager.build_images(repository, files)
    except ScriptError as e:
        print(e)
        return 1

    print_final_instructions(args)
    return 0


def finalize(args):
    try:
        repository = Repository(REPO_ROOT, args.repo)
        img_manager = ImageManager(args.release)
        pr_data = repository.find_release_pr(args.release)
        if not pr_data:
            raise ScriptError('No PR found for {}'.format(args.release))
        if not check_pr_mergeable(pr_data):
            raise ScriptError('Can not finalize release with an unmergeable PR')
        if not img_manager.check_images(args.release):
            raise ScriptError('Missing release image')
        br_name = branch_name(args.release)
        if not repository.branch_exists(br_name):
            raise ScriptError('No local branch exists for this release.')
        gh_release = repository.find_release(args.release)
        if not gh_release:
            raise ScriptError('No Github release draft for this version')

        repository.checkout_branch(br_name)

        pypandoc.convert_file(
            os.path.join(REPO_ROOT, 'README.md'), 'rst', outputfile=os.path.join(REPO_ROOT, 'README.rst')
        )
        run_setup(os.path.join(REPO_ROOT, 'setup.py'), script_args=['sdist', 'bdist_wheel'])

        merge_status = pr_data.merge()
        if not merge_status.merged:
            raise ScriptError('Unable to merge PR #{}: {}'.format(pr_data.number, merge_status.message))
        print('Uploading to PyPi')
        run_setup(os.path.join(REPO_ROOT, 'setup.py'), script_args=['upload'])
        img_manager.push_images(args.release)
        repository.publish_release(gh_release)
    except ScriptError as e:
        print(e)
        return 1

    return 0


ACTIONS = [
    'start',
    'cancel',
    'resume',
    'finalize',
]

EPILOG = '''Example uses:
    * Start a new feature release (includes all changes currently in master)
        release.py -b user start 1.23.0
    * Start a new patch release
        release.py -b user --patch 1.21.0 start 1.21.1
    * Cancel / rollback an existing release draft
        release.py -b user cancel 1.23.0
    * Restart a previously aborted patch release
        release.py -b user -p 1.21.0 resume 1.21.1
'''


def main():
    if 'GITHUB_TOKEN' not in os.environ:
        print('GITHUB_TOKEN environment variable must be set')
        return 1

    if 'BINTRAY_TOKEN' not in os.environ:
        print('BINTRAY_TOKEN environment variable must be set')
        return 1

    parser = argparse.ArgumentParser(
        description='Orchestrate a new release of docker/compose. This tool assumes that you have '
                    'obtained a Github API token and Bintray API key and set the GITHUB_TOKEN and '
                    'BINTRAY_TOKEN environment variables accordingly.',
        epilog=EPILOG, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument(
        'action', choices=ACTIONS, help='The action to be performed for this release'
    )
    parser.add_argument('release', help='Release number, e.g. 1.9.0-rc1, 2.1.1')
    parser.add_argument(
        '--patch', '-p', dest='base',
        help='Which version is being patched by this release'
    )
    parser.add_argument(
        '--repo', '-r', dest='repo', default=NAME,
        help='Start a release for the given repo (default: {})'.format(NAME)
    )
    parser.add_argument(
        '-b', dest='bintray_user', required=True, metavar='USER',
        help='Username associated with the Bintray API key'
    )
    parser.add_argument(
        '--bintray-org', dest='bintray_org', metavar='ORG', default=BINTRAY_ORG,
        help='Organization name on bintray where the data repository will be created.'
    )
    parser.add_argument(
        '--destination', '-o', metavar='DIR', default='binaries',
        help='Directory where release binaries will be downloaded relative to the project root'
    )
    parser.add_argument(
        '--no-cherries', '-C', dest='cherries', action='store_false',
        help='If set, the program will not prompt the user for PR numbers to cherry-pick'
    )
    args = parser.parse_args()

    if args.action == 'start':
        return start(args)
    elif args.action == 'resume':
        return resume(args)
    elif args.action == 'cancel':
        return cancel(args)
    elif args.action == 'finalize':
        return finalize(args)

    print('Unexpected action "{}"'.format(args.action), file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main())
