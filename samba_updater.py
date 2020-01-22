#!/usr/bin/env python3
from subprocess import Popen, PIPE
import argparse
from shutil import which, rmtree, copyfile
import re, os, sys
from tempfile import mkdtemp, _get_candidate_names as get_candidate_names, NamedTemporaryFile
def install_package(package_name):
    print('%s needs to be installed:' % package_name)
    Popen(['sudo %s in -y %s' % (which('zypper'), package_name)], shell=True).wait()
try:
    from pyrpm.spec import Spec
except ImportError:
    install_package('python3-python-rpm-spec')
    from pyrpm.spec import Spec
from glob import glob
from configparser import ConfigParser
from urllib import request, error
from datetime import datetime

samba_git_url = 'https://gitlab.com/samba-team/samba.git'

def fetch_tags(package, versions):
    global samba_git_url
    out, _ = Popen([which('git'), 'ls-remote', '--tags', samba_git_url], stdout=PIPE, stderr=PIPE).communicate()
    tags = {}
    for version in versions:
        res = re.findall('(\w{40})\s+refs/tags/%s-%s\^\{\}\n' % (package, version), out.decode())
        if len(res) > 0:
            tags[version] = res[0]
    return tags

def cleanup(api_url, details, updated=False):
    # Delete the package unless we have generated an update
    if not updated and details['proj'] and details['pkg']:
        ret = Popen([which('osc'), '-A', api_url, 'rdelete', details['proj'], details['pkg'], '-m', 'Deleting package %s as part of automated update' % details['pkg']]).wait()
        if ret == 0:
            print('Deleted branch target %s/%s' % (details['proj'], details['pkg']))
        else:
            print('Failed to delete branch target %s/%s' % (details['proj'], details['pkg']))
    if details['proj_dir']:
        rmtree(details['proj_dir'])
        print('Deleted project directory %s' % details['proj_dir'])

def older_package(nvv, uvv):
    if uvv[0] < nvv[0]:
        return True
    elif uvv[0] == nvv[0] and uvv[1] < nvv[1]:
        return True
    elif uvv[0] == nvv[0] and uvv[1] == nvv[1] and uvv[2] < nvv[2]:
        return True
    return False

def newer_package(vv, uvv):
    if uvv[0] > vv[0]:
        return True
    elif uvv[0] == vv[0] and uvv[1] > vv[1]:
        return True
    elif uvv[0] == vv[0] and uvv[1] == vv[1] and uvv[2] > vv[2]:
        return True
    return False

def fetch_package(user, email, api_url, project, packages, output_dir, samba_vers, skip_test, clone_dir, remote, rproject, branch, dest_exists):
    global samba_git_url
    if not output_dir:
        output_dir = mkdtemp()
    else:
        output_dir = os.path.abspath(os.path.expanduser(output_dir))
    details = {}
    new_versions = {}
    # Choose a random project name (to avoid name collisions)
    if not rproject:
        rproject = 'home:%s:branches:%s:%s' % (user, project, next(get_candidate_names()))
    for package in packages:
        details[package] = {}
        if not dest_exists:
            # Branch the package
            out, err = Popen([which('osc'), '-A', api_url, 'branch', project, package, rproject, package], stdout=PIPE, stderr=PIPE).communicate()
            if out and b'package can be checked out with' in out:
                print('Created branch target %s/%s' % (rproject, package))
            else:
                raise Exception(err.decode())

        # Checkout the package in the current directory
        details[package]['proj_dir'] = os.path.join(output_dir, package)
        if not os.path.exists(details[package]['proj_dir']):
            _, err = Popen([which('osc'), '-A', api_url, 'co', rproject, package, '-o', details[package]['proj_dir']], stdout=PIPE, stderr=PIPE).communicate()
            if err:
                raise Exception(err.decode())
        print('Checked out %s/%s at %s' % (rproject, package, details[package]['proj_dir']))

        # Check the package version
        spec_file = list(set(glob(os.path.join(details[package]['proj_dir'], '*.spec'))) - set(glob(os.path.join(details[package]['proj_dir'], '*-man.spec'))))[-1]
        spec = Spec.from_file(spec_file)
        details[package]['version'] = spec.version

    # Clone a copy of samba
    cleanup_clone = False
    if not clone_dir:
        rclone = 'samba-%s' % next(get_candidate_names())
        clone_dir = os.path.join(output_dir, rclone)
        print('Cloning samba')
        Popen([which('git'), 'clone', samba_git_url, clone_dir], stdout=PIPE).wait()
        cleanup_clone = True
    else:
        cwd = os.getcwd()
        clone_dir = os.path.abspath(os.path.expanduser(clone_dir))
        os.chdir(clone_dir)
        Popen([which('git'), 'fetch', remote], stdout=PIPE).wait()
        os.chdir(cwd)

    for package in packages:
        # Check for newer package version
        cwd = os.getcwd()
        os.chdir(clone_dir)
        if not branch:
            branch = 'v%s-%s-stable' % tuple(samba_vers.split('.'))
        if Popen([which('git'), 'checkout', '--track', '%s/%s' % (remote, branch)], stdout=PIPE).wait() == 128:
            Popen([which('git'), 'checkout', branch], stdout=PIPE).wait()
            Popen([which('git'), 'pull', remote, branch], stdout=PIPE).wait()
        latest_version = None
        with open('lib/%s/wscript' % package, 'r') as r:
            res = re.findall("VERSION = '(.*)'", r.read())
            if len(res) == 1:
                latest_version = res[0]
        nvv = [int(v) for v in latest_version.split('.')]

        # Fetch upstream versions
        details[package]['url'] = 'https://www.samba.org/ftp/pub/%s' % package
        resp = request.urlopen(details[package]['url'])
        page_data = resp.read().decode()
        versions = set(re.findall('<a href="%s-([\.\-\w]+)\.tar\.[^"]+">' % package, page_data))
        vv = [int(v) for v in details[package]['version'].split('.')]
        details[package]['date'] = re.findall('href="%s\-%d\.%d\.%d\.tar\.gz"\>%s\-%d\.%d\.%d\.tar\.gz\</a\>\</td\>\<td [^>]+>(\d{4}\-\d{2}\-\d{2})' % (package, vv[0], vv[1], vv[2], package, vv[0], vv[1], vv[2]), page_data)[-1]

        print('Current version of %s is %s published on %s' % (package, details[package]['version'], details[package]['date']))
        print('New version of %s is %s' % (package, latest_version))
        lvv = [int(v) for v in latest_version.split('.')]
        if details[package]['version'] == latest_version or older_package(vv, lvv):
            print('Skipping upgrade because the package has no update')
            continue

        # Check for newer package version
        details[package]['new'] = {latest_version: {}}
        for upstream_vers in versions:
            uvv = [int(v) for v in upstream_vers.split('.')]
            if newer_package(vv, uvv) and older_package(nvv, uvv):
                details[package]['new'][upstream_vers] = {}

        # Generate a changelog entry
        git_tags = fetch_tags(package, details[package]['new'].keys())
        print('Reading changelog from git history')
        for vers in details[package]['new'].keys():
            if vers not in git_tags:
                details[package]['new'][vers]['log'] = '- Update to %s %s' % (package, vers)
                continue
            out, _ = Popen([which('git'), 'log', '-1', git_tags[vers]], stdout=PIPE).communicate()
            log = ''
            for line in out.decode().split('\n'):
                if not line.strip():
                    continue
                elif re.match('commit \w{40}', line):
                    continue
                elif re.match('Author:\s+.*', line):
                    continue
                elif re.match('Date:\s+.*', line):
                    continue
                elif re.match('\s+signed\-off\-by:\s+.*', line.lower()):
                    continue
                elif re.match('\s+reviewed\-by:\s+.*', line.lower()):
                    continue
                elif re.match('\s+autobuild\-user\(\w+\):\s+.*', line.lower()):
                    continue
                elif re.match('\s+autobuild\-date\(\w+\):\s+.*', line.lower()):
                    continue
                line = re.sub(r'https?://bugzilla.samba.org/show_bug.cgi\?id\=(\d+)', r'(bso#\1)', line)
                line = re.sub(r'\(bug\s*#?\s*(\d+)\)', r'(bso#\1)', line)
                line = line.replace('    * ', '  + ').replace('      ', '    ')
                line = line.replace('%s: version %s' % (package, vers), '- Update to %s' % vers)
                log += '%s\n' % line
            log = re.sub(r'\n\s*BUG:\s*', '; ', log)
            details[package]['new'][vers]['log'] = log.strip()
        os.chdir(cwd)
        sorted_versions = sorted(details[package]['new'].keys(), key=lambda s: list(map(int, s.split('.'))), reverse=True)
        new_versions[package] = latest_version
        print('Updating %s to latest version %s' % (package, latest_version))

        changelog_file = None
        with NamedTemporaryFile('w', dir=output_dir, delete=False, suffix='.changes') as changelog:
            now = datetime.utcnow()
            changelog.write('-------------------------------------------------------------------\n')
            changelog.write('%s - %s\n\n' % (now.strftime('%a %b %d %X UTC %Y'), email if email else user))
            for vers in sorted_versions:
                changelog.write(details[package]['new'][vers]['log'])
                changelog.write('\n')
            changelog.write('\n')
            changelog_file = changelog.name
        if 'EDITOR' in os.environ:
            editor = os.environ['EDITOR']
        else:
            editor = which('vim')
        Popen([editor, changelog_file]).wait()
        changelogs = glob(os.path.join(details[package]['proj_dir'], '*.changes'))
        changes = open(changelog_file, 'r').read()
        for changelog in changelogs:
            existing = open(changelog, 'r').read()
            with open(changelog, 'w') as w:
                w.write(changes)
                w.write(existing)
        os.remove(changelog_file)

        # Download the new package sources
        tar = '%s-%s.tar.gz' % (package, latest_version)
        asc = '%s-%s.tar.asc' % (package, latest_version)
        tar_remote = '%s/%s' % (details[package]['url'], tar)
        asc_remote = '%s/%s' % (details[package]['url'], asc)
        try:
            with open(os.path.join(details[package]['proj_dir'], tar), 'wb') as w:
                resp = request.urlopen(tar_remote)
                w.write(resp.read())
        except error.HTTPError: # The file doesn't exist
            # Build the distribution package on our own (this typically
            # happens if the version hasn't been released yet).
            os.chdir(os.path.join(clone_dir, 'lib', package))
            Popen([which('make'), 'dist'], stdout=PIPE).wait()
            os.remove(os.path.join(details[package]['proj_dir'], tar))
            copyfile(os.path.join(os.getcwd(), tar), os.path.join(details[package]['proj_dir'], tar))
            os.remove(os.path.join(os.getcwd(), tar))
            os.chdir(cwd)
        try:
            with open(os.path.join(details[package]['proj_dir'], asc), 'wb') as w:
                resp = request.urlopen(asc_remote)
                w.write(resp.read())
        except error.HTTPError: # The file doesn't exist
            os.remove(os.path.join(details[package]['proj_dir'], asc))
        os.chdir(details[package]['proj_dir'])
        Popen([which('osc'), 'add', tar], stdout=PIPE).wait()
        Popen([which('osc'), 'rm', '%s-%s.tar.gz' % (package, details[package]['version'])], stdout=PIPE).wait()
        if os.path.exists(asc):
            Popen([which('osc'), 'add', asc], stdout=PIPE).wait()
        Popen([which('osc'), 'rm', '%s-%s.tar.asc' % (package, details[package]['version'])], stdout=PIPE).wait()
        os.chdir(cwd)
        print('Downloaded package sources')

        # Make sure we have the key to verify sources
        Popen([which('gpg'), '--keyserver', 'keyserver.ubuntu.com', '--recv-keys', '4793916113084025'], stdout=PIPE, stderr=PIPE).wait()

        # Verify the sources
        if os.path.exists(os.path.join(details[package]['proj_dir'], asc)):
            copyfile(os.path.join(details[package]['proj_dir'], tar), os.path.join(output_dir, tar))
            copyfile(os.path.join(details[package]['proj_dir'], asc), os.path.join(output_dir, asc))
            os.chdir(output_dir)
            Popen([which('gunzip'), tar], stdout=PIPE).wait()
            _, out = Popen([which('gpg'), '--verify', asc], stdout=PIPE, stderr=PIPE).communicate()
            mt = b'Good signature from "Samba Library Distribution Key \<samba\-bugs@samba\.org\>"'
            if len(re.findall(mt, out)) > 0:
                print('Verified package sources')
            else:
                print(out)
                cleanup(api_url, details[package])
                return
            os.remove(os.path.join(output_dir, '%s-%s.tar' % (package, latest_version)))
            os.remove(os.path.join(output_dir, asc))
            os.chdir(cwd)
        else:
            print('\033[31m' + 'WARNING: Failed to verify sources!\nMaybe we generated them?' + '\033[0m')

        # Update the spec file
        spec_files = glob(os.path.join(details[package]['proj_dir'], '*.spec'))
        for specfile in spec_files:
            data = open(specfile, 'r').read()
            with open(specfile, 'w') as w:
                data = re.sub(r'([Vv]ersion:\s+)%s' % details[package]['version'], r'\g<1>%s' % latest_version, data)
                for pkg in new_versions.keys():
                    data = re.sub('%%define %s_version \d+\.\d+\.\d+' % pkg, '%%define %s_version %s' % (pkg, new_versions[pkg]), data)
                w.write(data)
        print('Updated version in the spec file')

        # Run a test build
        ret = -1
        os.chdir(details[package]['proj_dir'])
        if not skip_test:
            while ret != 0:
                print('Testing the build for %s' % package)
                p = Popen([which('osc'), 'build', '-j8', '--ccache', '--local-package', '--trust-all-projects', '--clean', spec_file], stdout=PIPE, stderr=PIPE)
                out, _ = p.communicate()
                ret = p.returncode
                if ret != 0:
                    print('Build failed.')
                    if out:
                        data = out.decode().split('\n')
                        if len(data) > 50:
                            for line in data[-50:]:
                                print(line)
                        else:
                            print(out.decode())
                    print('The project sources are found in %s.' % details[package]['proj_dir'])
                    print('Fixup the package sources, then exit the testing shell to continue...')
                    # Opens a shell in the project directory
                    env = os.environ
                    env['PS1'] = '%s> ' % package
                    Popen([os.environ['SHELL']], env=env).wait()
                else:
                    print('Build succeeded. Submitting sources to the build service.')

        # Checkin the changes
        Popen([which('osc'), 'ci', '--noservice']).wait()
        os.chdir(cwd)

        cleanup(api_url, details[package], updated=True)
    if cleanup_clone:
        rmtree(clone_dir)
        print('Deleted samba clone %s' % clone_dir)
    print('Results are posted in project %s' % rproject)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Run against obs samba package to update to latest version. This will branch the target package into your home project, then check it out on the local machine. It also downloads the latest package version from samba.org, then clones a shallow copy of samba and generates a changelog entry. Finally, an updated package will be checked into your home project on obs.')
    parser.add_argument('-A', '--apiurl', help='osc URL/alias', action='store', default='https://api.opensuse.org')
    parser.add_argument('--skip-test', action='store_true', default=False, help='Use to disable build testing of new sources (this will submit to the build service with testing!)')
    parser.add_argument('--samba', help='A git checkout of samba (this speeds up processing time)', action='store', default=None)
    parser.add_argument('--samba-remote', help='When paired with --samba, specifies the samba remote name (default=origin)', action='store', default='origin')
    parser.add_argument('--samba-branch', help='The branch to check for upstream versions (default=v$(SAMBA_VERSION)-stable)', default=None)
    parser.add_argument('SAMBA_VERSION', help='The relative samba version')
    parser.add_argument('SOURCEPROJECT', help='The source project to branch from')
    parser.add_argument('SOURCEPACKAGE', help='The source package[s] to update (default=[talloc, tdb, tevent, ldb])', nargs='*', default=['talloc', 'tdb', 'tevent', 'ldb'])
    parser.add_argument('--dest-project', help='The destination project to branch to', default=None)
    parser.add_argument('--dest-exists', help='Indicates if the destination branches already exist and skips branching. This should only be used in conjunction with --dest-project (otherwise this option says that a random project name already exists)', action='store_true', default=False)
    parser.add_argument('-o', '--output-dir', help='Place the package directory in the specified directory instead of a temp directory', action='store', default=None)

    args = parser.parse_args()

    # Parse the samba version
    if not re.match('\d+\.\d+$', args.SAMBA_VERSION):
        sys.stderr.write('Samba version should be in the form \'4.10\', being the first 2 digits of the version number.\n')
        exit(1)

    # Try to parse user from ~/.oscrc
    oscrc = None
    user = None
    if os.path.exists(os.path.expanduser('~/.config/osc/oscrc')):
        oscrc = os.path.expanduser('~/.config/osc/oscrc')
    elif os.path.exists(os.path.expanduser('~/.oscrc')):
        oscrc = os.path.expanduser('~/.oscrc')
    if oscrc:
        config = ConfigParser()
        config.read(oscrc)
        try:
            user = config[args.apiurl]['user']
        except KeyError:
            user = None
    # If no user is found, we need to initialize creds
    if not user:
        # Force user to input creds
        Popen([which('osc'), '-A', args.apiurl, 'whois']).wait()
    # Read the whois output for this user
    out, _ = Popen([which('osc'), '-A', args.apiurl, 'whois'], stdout=PIPE, stderr=PIPE).communicate()
    user_data_m = re.match(b'(\w+): (.*)', out)
    email = None
    if user_data_m:
        if not user:
            user = user_data_m.group(1).decode()
        email = user_data_m.group(2).decode().replace('"', '')

    fetch_package(user, email, args.apiurl, args.SOURCEPROJECT, args.SOURCEPACKAGE, args.output_dir, args.SAMBA_VERSION, args.skip_test, args.samba, args.samba_remote, args.dest_project, args.samba_branch, args.dest_exists)
