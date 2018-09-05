
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import collections
import json
import logging
import os
import re
import string
import sys
import urllib.parse

import git
import requests

from gerrit_scripts import gerrit_api


LOG = logging.getLogger('custom-patches')

CHANGE_ID_PATTERN = re.compile(r'\nChange-Id:\s(?P<changeid>I[a-f0-9]{40})\n')
DEFAULT_FILTER_REGEX = (
    '^(?!('
    'Updated from global requirements|'
    'Imported Translations from Zanata|'
    'Update UPPER_CONSTRAINTS_FILE for stable|'
    'Update .gitreview for stable|'
    'import zuul job settings from project-config'
    '))'
)


def build_commit_dict(commits):
    commit_dict = {}
    for c in commits:
        f = CHANGE_ID_PATTERN.search(c.message)
        # filter out merge commits
        if f and len(c.parents) == 1:
            commit_dict[f.groups()[0]] = c
    return commit_dict


def prepare_repo(repo_path):
    if (os.path.exists(repo_path) and
            os.path.isdir(repo_path) and
            os.path.isdir(os.path.join(repo_path, '.git'))):
        LOG.info('Repo %s exists, updating remotes' % repo_path)
        repo = git.Repo(repo_path)
    else:
        LOG.info('Creating repo %s' % repo_path)
        os.mkdir(repo_path)
        repo = git.Repo.init(repo_path)
    return repo


def update_remote(repo, remote_name, gerrit_uri, project):

    remote_uri = os.path.join(gerrit_uri, project.strip('/'))
    if remote_name in (r.name for r in repo.remotes):
        remote = repo.remotes[remote_name]
        remote.set_url(remote_uri)
    else:
        remote = repo.create_remote(remote_name, remote_uri)
    LOG.info("Fetching from remote %s" % remote_uri)
    remote.update(prune=True)
    remote.set_url('')


def update_remotes(repo, gerrit_uri, project,
                   new_gerrit_uri=None, new_project=None):
    source_remote = 'custom_patches_source'
    target_remote = 'custom_patches_target'
    if not new_gerrit_uri:
        new_gerrit_uri = gerrit_uri
        target_remote = source_remote
    if not new_project:
        new_project = project

    update_remote(repo, source_remote, gerrit_uri, project)
    if source_remote != target_remote:
        update_remote(repo, target_remote, new_gerrit_uri, new_project)
    return source_remote, target_remote


def is_sha(s):
    return (len(s) == 40 and all(c in string.hexdigits.lower() for c in s))


def commit_ident(branch, remote):
    return branch if is_sha(branch) else 'remotes/{remote}/{branch}'.format(
        remote=remote, branch=branch)


def find_missing_changes(repo, source_remote, target_remote,
                         old_branch, new_branch):

    old_commit_ident = commit_ident(old_branch, source_remote)
    new_commit_ident = commit_ident(new_branch, target_remote)
    common_ancestor = repo.merge_base(
        old_commit_ident, new_commit_ident)[0]
    old_commits = repo.iter_commits(
        common_ancestor.hexsha+'..'+old_commit_ident)
    new_commits = repo.iter_commits(
        common_ancestor.hexsha+'..'+new_commit_ident)
    old_commit_dict = build_commit_dict(old_commits)
    new_commit_dict = build_commit_dict(new_commits)
    return [old_commit_dict[i]
            for i in set(old_commit_dict) - set(new_commit_dict)]


def output_commits(all_commits, filter_regex_str, long_out=False,
                   json_out=None):
    filter_regex = re.compile(filter_regex_str)
    commit_dict = collections.defaultdict(lambda: {})

    for prj, commits in all_commits.items():
        header = "Project: {proj}".format(proj=prj)
        print('\n'+header+'\n'+'='*len(header))
        for c in commits:
            commit_lines = c.message.splitlines()
            title = commit_lines[0]
            message = commit_lines[1:]
            if filter_regex.match(title):
                print("{id} {title}".format(id=c.hexsha[:8],
                                            title=title))
                if long_out:
                    for l in message:
                        print(" " * 9 + l)
                    print("\n")
                if json_out:
                    commit_dict[prj][c.hexsha] = {'title': title,
                                                  'message': message}

    if commit_dict:
        LOG.info("Writing JSON output to %s" % json_out)
        with open(json_out, 'w') as out:
            json.dump(commit_dict, out, indent=4)


def make_gerrit_repo_url(gerrit_url, username=None, password=None):
    if not gerrit_url:
        return gerrit_url
    auth_string = ''
    if username and password:
        auth_string = '{}:{}@'.format(username,
                                      urllib.parse.quote(password, safe=''))
    url_parts = urllib.parse.urlparse(gerrit_url)
    new_parts = [url_parts[0], '{}{}'.format(auth_string, url_parts[1])]
    new_parts.extend(url_parts[2:])
    repo_url = urllib.parse.urlunparse(new_parts)
    return repo_url


def find_projects(gerrit_uri, project_prefix, old_branch, new_branch,
                  gerrit_password=None, gerrit_username=None):
    session = requests.Session()
    if gerrit_password:
        session.auth = requests.auth.HTTPDigestAuth(gerrit_username,
                                                    gerrit_password)
        gerrit_uri += '/a'

    LOG.info('Listing projects by prefix on Gerrit..')
    r = session.get('{url}/projects/?p={prefix}'.format(
        url=gerrit_uri, prefix=urllib.parse.quote(project_prefix, safe='')))
    if r.status_code != 200:
        LOG.error('Could not fetch list of projects with prefix {prefix} '
                  'from URI {url}'.format(url=gerrit_uri,
                                          prefix=project_prefix))
        sys.exit(1)
    projects = r.json(cls=gerrit_api.GerritJSONDecoder)
    found = []
    LOG.info("Filtering projects {p} by branches..".format(p=list(projects)))
    for proj in projects:
        LOG.info('Listing branches for project {}'.format(proj))
        r = session.get('{url}/projects/{project}/branches'.format(
            url=gerrit_uri, project=urllib.parse.quote(proj, safe='')))
        if r.status_code != 200:
            LOG.warning('Failed to list branches for project {project} '
                        'on remote {url}'.format(project=proj, url=gerrit_uri))
            continue

        if all('refs/heads/'+b in map(lambda x: x['ref'],
                                      r.json(cls=gerrit_api.GerritJSONDecoder))
               for b in (old_branch, new_branch)):
            found.append(proj)
    LOG.info('Projects to fetch: %s' % found)
    return found


def parse_packages_file(path_to_file):
    commits = []
    with open(path_to_file) as f:
        for line in f:
            if line.startswith('Private-Mcp-Code-Sha: '):
                commit = line.split('Private-Mcp-Code-Sha: ')[-1].strip()
                commits.append(commit)
    return set(commits)


def find_projects_by_commits(gerrit_uri, commits, new_branch,
                             gerrit_password=None, gerrit_username=None):
    session = requests.Session()
    if gerrit_password:
        session.auth = requests.auth.HTTPDigestAuth(gerrit_username,
                                                    gerrit_password)
        gerrit_uri += '/a'
    projects = []
    for commit in commits:
        LOG.info("Looking for commit {commit}...".format(commit=commit))
        r = session.get('{url}/changes/?q={commit}'.format(
            url=gerrit_uri, commit=commit))
        if r.status_code != 200:
            LOG.error('Could not find commit with SHA-1 {commit} '
                      'on Gerrit instance at {url}'.format(url=gerrit_uri,
                                                           commit=commit))
            sys.exit(1)
        changes = r.json(cls=gerrit_api.GerritJSONDecoder)
        if not changes:
            LOG.error('Could not find commit with SHA-1 {commit} '
                      'on Gerrit instance at {url}'.format(url=gerrit_uri,
                                                           commit=commit))
            sys.exit(1)
        if len(changes) > 2:
            LOG.error('Could not find commit unique with SHA-1 {commit} '
                      'on Gerrit instance at {url}'.format(url=gerrit_uri,
                                                           commit=commit))
            sys.exit(1)
        # deal with manual rebuilds when there is no change on gerrit
        # but the commmit is mentioned in commit message
        if len(changes) == 2:
            # both code and spec commit were found
            changes = [c for c in changes if '/sources/' in c['project']]
        project = changes[0]['project']
        if '/specs/' in project:
            project = project.replace('/specs/', '/sources/')
        projects.append((project, None, commit))
    found = []
    for proj, _, commit in projects:
        r = session.get('{url}/projects/{project}/branches'.format(
            url=gerrit_uri, project=urllib.parse.quote(proj, safe='')))
        if r.status_code != 200:
            LOG.warning('Failed to list branches for project {project} '
                        'on remote {url}'.format(project=proj, url=gerrit_uri))
            continue
        data = r.json(cls=gerrit_api.GerritJSONDecoder)
        if 'refs/heads/'+new_branch in map(lambda x: x['ref'], data):
            found.append((proj, None, commit))
    LOG.info('Projects to fetch: %s' % [f[0] for f in found])
    return found


def parse_args():
    parser = argparse.ArgumentParser(
        description=('Using Geriit Change-Id, report patches in the history '
                     'leading to <old branch> which are missing '
                     'in the history of <new branch>. ')
    )
    parser.add_argument(
        '--gerrit',
        default=os.getenv('CUSTOM_PATCHES_GERRIT_LOC'),
        help=('Gerrit location (full HTTP(S) URL). '
              'Defaults to CUSTOM_PATCHES_GERRIT_LOC shell var')
    )
    parser.add_argument(
        '--gerrit-username',
        default=os.getenv('CUSTOM_PATCHES_GERRIT_USERNAME'),
        help=('Gerrit HTTP user name to access Gerrit HTTP API/repos. '
              'Defaults to CUSTOM_PATCHES_GERRIT_USERNAME shell var')
    )
    parser.add_argument(
        '--gerrit-password',
        default=os.getenv('CUSTOM_PATCHES_GERRIT_HTTP_PASSWORD'),
        help=('Gerrit HTTP password. '
              'Defaults to CUSTOM_PATCHES_GERRIT_HTTP_PASSWORD shell var.')
    )
    parser.add_argument(
        '--project',
        default=os.getenv('CUSTOM_PATCHES_GERRIT_PROJECT'),
        help=('Gerrit project name. '
              'Defaults to CUSTOM_PATCHES_GERRIT_PROJECT shell var.')
    )
    parser.add_argument(
        '--new-gerrit',
        default=os.getenv('CUSTOM_PATCHES_NEW_GERRIT_LOC'),
        help=('New Gerrit location (full HTTP(S) URL).'
              'Defaults to CUSTOM_PATCHES_NEW_GERRIT_LOC shell var. '
              'If empty, falls back to Gerrit location.')
    )
    parser.add_argument(
        '--new-gerrit-username',
        default=os.getenv('CUSTOM_PATCHES_NEW_GERRIT_USERNAME'),
        help=('New Gerrit HTTP user name to access Gerrit HTTP API/repos.'
              'Defaults to CUSTOM_PATCHES_NEW_GERRIT_USERNAME shell var. '
              'If empty, falls back to Gerrit username.')
    )
    parser.add_argument(
        '--new-gerrit-password',
        default=os.getenv('CUSTOM_PATCHES_GERRIT_HTTP_PASSWORD'),
        help=('Gerrit HTTP password. '
              'Defaults to CUSTOM_PATCHES_GERRIT_HTTP_PASSWORD shell var.')
    )
    parser.add_argument(
        '--new-project',
        default=os.getenv('CUSTOM_PATCHES_NEW_GERRIT_PROJECT'),
        help=('New Gerrit project name. '
              'Defaults to CUSTOM_PATCHES_NEW_GERRIT_PROJECT shell var. '
              'If empty, falls back to Gerrit project name.')
    )
    parser.add_argument(
        '--project-prefix',
        default=os.getenv('CUSTOM_PATCHES_GERRIT_PROJECT_PREFIX'),
        help=('Gerrit project prefix, to fetch all projects starting with it. '
              'Defaults to CUSTOM_PATCHES_GERRIT_PROJECT_PREFIX shell var.')
    )
    parser.add_argument(
        '--old-branch',
        default=os.getenv('CUSTOM_PATCHES_OLD_BRANCH'),
        help=('Old branch (typically, previous release). '
              'If resembling a full-length  SHA, will be considered as '
              'commit SHA instead of a branch name. '
              'Defaults to CUSTOM_PATCHES_OLD_BRANCH shell var')
    )
    parser.add_argument(
        '--new-branch',
        default=os.getenv('CUSTOM_PATCHES_NEW_BRANCH'),
        help=('New branch (typically, current release). '
              'If resembling a full-length  SHA, will be considered as '
              'commit SHA instead of a branch name. '
              'Defaults to CUSTOM_PATCHES_NEW_BRANCH shell var')
    )
    parser.add_argument(
        '--long',
        action='store_true',
        help='Print full commit messages'
    )
    parser.add_argument(
        '--json',
        default=None,
        help=('Path to JSON output file. '
              'Default is not to generate JSON output.')
    )
    parser.add_argument(
        '--regex',
        default=DEFAULT_FILTER_REGEX,
        help=("Output only commits with title matching "
              "this regular expression. "
              "Default \"%s\" is mostly suitable for OpenStack projects "
              "and their stable branches. "
              "To output all missing commits, set it to '.*'."
              % DEFAULT_FILTER_REGEX)
    )
    parser.add_argument(
        '--mcp-packages-file',
        help=('Path to debian Packages file to parse, overrides old_branch '
              'and project / project-prefix. '
              'Can be used to find commit diff between code in MCP debian '
              'package repo and Gerrit. '
              'WARNING: Very MCP specific as it relies on '
              'private package metadata fields added by MCP package build '
              'procedure to find commits from which packages were built.')
    )

    args = parser.parse_args()
    # validate required args
    # TODO: more logic for validation needed with mcp-packages-file
    if not (args.gerrit and args.new_branch and
            (args.project or args.project_prefix or args.mcp_packages_file)):
        parser.error('gerrit, project or project-prefix, '
                     'old-branew-branch are required')
    # eithe no auth or auth with both username and password
    if bool(args.gerrit_password) != bool(args.gerrit_username):
        parser.error('gerrit-username and gerrit-password must be '
                     'both definded or undefined')
    if bool(args.new_gerrit_password) != bool(args.new_gerrit_username):
        parser.error('new-gerrit-username and new-gerrit-password must be '
                     'both definded or undefined')
    if args.gerrit_password or args.new_gerrit_password:
        LOG.warning(
            'The cloned/updated repos will contain sensitive information '
            '(your password) in clear text while in process of fetching. '
            'The remote URL will be reset after fetching.')

    return args


def main():
    logging.basicConfig(
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    LOG.setLevel(logging.INFO)
    args = parse_args()
    all_missing = {}
    if args.mcp_packages_file:
        commits = parse_packages_file(args.mcp_packages_file)
        projects = find_projects_by_commits(
            args.gerrit, commits, args.new_branch,
            gerrit_password=args.gerrit_password,
            gerrit_username=args.gerrit_username)
    elif args.project_prefix:
        if not args.project:
            found = find_projects(args.gerrit, args.project_prefix,
                                  args.old_branch, args.new_branch,
                                  gerrit_password=args.gerrit_password,
                                  gerrit_username=args.gerrit_username)
            projects = zip(found,
                           [None]*len(found),
                           [args.old_branch]*len(found))
        else:
            projects = [(args.project_prefix + args.project,
                         None,
                         args.old_branch)]
    else:
        projects = [(args.project, args.new_project, args.old_branch)]
    if projects:
        gerrit_uri = make_gerrit_repo_url(args.gerrit,
                                          username=args.gerrit_username,
                                          password=args.gerrit_password)
        new_gerrit_uri = make_gerrit_repo_url(
            args.new_gerrit,
            username=args.new_gerrit_username,
            password=args.new_gerrit_password)
        for project, new_project, old_branch in projects:
            repo = prepare_repo(os.path.basename(project))
            source_remote, target_remote = update_remotes(
                repo, gerrit_uri, project,
                new_gerrit_uri=new_gerrit_uri,
                new_project=args.new_project)
            all_missing[new_project or project] = find_missing_changes(
                repo, source_remote, target_remote, old_branch,
                args.new_branch)
        output_commits(all_missing, args.regex,
                       long_out=args.long, json_out=args.json)
