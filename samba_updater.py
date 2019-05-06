#!/usr/bin/env python3
from subprocess import Popen, PIPE
import argparse
from shutil import which, rmtree, copyfile
import re, os
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
from urllib import request
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

def cleanup(api_url, home_proj, home_pkg, proj_dir, clone_dir, updated=False):
    # Delete the package unless we have generated an update
    if not updated and home_proj and home_pkg:
        ret = Popen([which('osc'), '-A', api_url, 'rdelete', home_proj, home_pkg, '-m', 'Deleting package %s as part of automated update' % home_pkg]).wait()
        if ret == 0:
            print('Deleted branch target %s/%s' % (home_proj, home_pkg))
        else:
            print('Failed to delete branch target %s/%s' % (home_proj, home_pkg))
    if proj_dir:
        rmtree(proj_dir)
        print('Deleted project directory %s' % proj_dir)
    rmtree(clone_dir)
    print('Deleted samba shallow clone %s' % clone_dir)

def fetch_package(user, email, api_url, project, package, output_dir):
    global samba_git_url
    # Choose a random package name (to avoid name collisions)
    rpackage = '%s-%s' % (package, next(get_candidate_names()))
    nproject = 'home:%s:branches:%s' % (user, project)
    # Branch the package
    out, err = Popen([which('osc'), '-A', api_url, 'branch', project, package, nproject, rpackage], stdout=PIPE, stderr=PIPE).communicate()
    home_proj, home_pkg = (None, None)
    if out and b'package can be checked out with' in out:
        home_proj, home_pkg = [p.decode() for p in re.findall(b'A working copy of the branched package can be checked out with:\n\nosc co ([^/]*)/(.*)', out)[0]]
        print('Created branch target %s/%s' % (home_proj, home_pkg))
    else:
        raise Exception(err.decode())

    # Checkout the package in the current directory
    if not output_dir:
        output_dir = mkdtemp()
    else:
        output_dir = os.path.abspath(output_dir)
    proj_dir = os.path.join(output_dir, home_pkg)
    if not os.path.exists(proj_dir):
        _, err = Popen([which('osc'), '-A', api_url, 'co', home_proj, home_pkg, '-o', proj_dir], stdout=PIPE, stderr=PIPE).communicate()
        if err:
            raise Exception(err.decode())
    print('Checked out %s/%s at %s' % (home_proj, home_pkg, proj_dir))

    # Check the package version
    spec_file = list(set(glob(os.path.join(proj_dir, '*.spec'))) - set(glob(os.path.join(proj_dir, '*-man.spec'))))[-1]
    spec = Spec.from_file(spec_file)
    version = spec.version

    # Fetch upstream versions
    samba_url = 'https://www.samba.org/ftp/pub/%s' % package
    resp = request.urlopen(samba_url)
    page_data = resp.read().decode()
    versions = set(re.findall('<a href="%s-([\.\-\w]+)\.tar\.[^"]+">' % package, page_data))
    vv = [int(v) for v in version.split('.')]
    date = re.findall('href="%s\-%d\.%d\.%d\.tar\.gz"\>%s\-%d\.%d\.%d\.tar\.gz\</a\>\</td\>\<td align="right">(\d{4}\-\d{2}\-\d{2})' % (package, vv[0], vv[1], vv[2], package, vv[0], vv[1], vv[2]), page_data)[-1]

    print('Current version of %s is %s published on %s' % (package, version, date))

    # Check for newer package version
    new_vers = {}
    vers_mo = re.compile('%d\.%d\.(\d+)' % (vv[0], vv[1]))
    for upstream_vers in versions:
        m = vers_mo.match(upstream_vers)
        if m and int(m.group(1)) > vv[-1]:
            new_vers[upstream_vers] = {'vers': int(m.group(1))}

    # Generate a changelog entry
    git_tags = fetch_tags(package, new_vers.keys())
    rclone = 'samba-%s' % next(get_candidate_names())
    clone_dir = os.path.join(output_dir, rclone)
    print('Shallow cloning samba since %s' % date)
    Popen([which('git'), 'clone', '--shallow-since=%s' % date, samba_git_url, clone_dir], stdout=PIPE).wait()
    print('Reading changelog from git history')
    cwd = os.getcwd()
    os.chdir(clone_dir)
    for vers in new_vers.keys():
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
            line = re.sub(r'\(bug\s*#\s*(\d+)\)', r'(bso#\1)', line)
            line = line.replace('    * ', '  + ').replace('      ', '    ')
            line = line.replace('%s: version %s' % (package, vers), '- Update to %s' % vers)
            log += '%s\n' % line
        new_vers[vers]['log'] = log.strip()
    os.chdir(cwd)
    sorted_versions = sorted(new_vers.keys(), key=lambda k: new_vers[k]['vers'], reverse=True)
    latest_version = sorted_versions[0]
    changelog_file = None
    with NamedTemporaryFile('w', dir=output_dir, delete=False, suffix='.changes') as changelog:
        now = datetime.utcnow()
        changelog.write('-------------------------------------------------------------------\n')
        changelog.write('%s - %s\n\n' % (now.strftime('%a %b %d %X UTC %Y'), email if email else user))
        for vers in sorted_versions:
            changelog.write(new_vers[vers]['log'])
            changelog.write('\n')
        changelog.write('\n')
        changelog_file = changelog.name
    Popen([which('vim'), changelog_file]).wait()
    changelogs = glob(os.path.join(proj_dir, '*.changes'))
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
    tar_remote = '%s/%s' % (samba_url, tar)
    asc_remote = '%s/%s' % (samba_url, asc)
    with open(os.path.join(proj_dir, tar), 'wb') as w:
        resp = request.urlopen(tar_remote)
        w.write(resp.read())
    with open(os.path.join(proj_dir, asc), 'wb') as w:
        resp = request.urlopen(asc_remote)
        w.write(resp.read())
    os.chdir(proj_dir)
    Popen([which('osc'), 'add', tar], stdout=PIPE).wait()
    Popen([which('osc'), 'rm', '%s-%s.tar.gz' % (package, version)], stdout=PIPE).wait()
    Popen([which('osc'), 'add', asc], stdout=PIPE).wait()
    Popen([which('osc'), 'rm', '%s-%s.tar.asc' % (package, version)], stdout=PIPE).wait()
    os.chdir(cwd)
    print('Downloaded package sources')

    # Make sure we have the key to verify sources
    Popen([which('gpg'), '--keyserver', 'keyserver.ubuntu.com', '--recv-keys', '4793916113084025'], stdout=PIPE, stderr=PIPE).wait()

    # Verify the sources
    copyfile(os.path.join(proj_dir, tar), os.path.join(output_dir, tar))
    copyfile(os.path.join(proj_dir, asc), os.path.join(output_dir, asc))
    os.chdir(output_dir)
    Popen([which('gunzip'), tar], stdout=PIPE).wait()
    _, out = Popen([which('gpg'), '--verify', asc], stdout=PIPE, stderr=PIPE).communicate()
    mt = b'Good signature from "Samba Library Distribution Key \<samba\-bugs@samba\.org\>"'
    if len(re.findall(mt, out)) > 0:
        print('Verified package sources')
    else:
        print(out)
        cleanup(api_url, home_proj, home_pkg, proj_dir, clone_dir)
        return
    os.remove(os.path.join(output_dir, '%s-%s.tar' % (package, latest_version)))
    os.remove(os.path.join(output_dir, asc))
    os.chdir(cwd)

    # Update the spec file
    spec_files = glob(os.path.join(proj_dir, '*.spec'))
    for specfile in spec_files:
        data = open(specfile, 'r').read()
        with open(specfile, 'w') as w:
            data = re.sub(r'([Vv]ersion:\s+)%s' % version, r'\g<1>%s' % latest_version, data)
            w.write(data)
    print('Updated version in the spec file')

    # Run a test build
    ret = -1
    os.chdir(proj_dir)
    while ret != 0:
        p = Popen([which('osc'), 'build', '-j8', '--ccache', '--local-package', '--trust-all-projects', '--clean', spec_file], stdout=PIPE, stderr=PIPE)
        out, _ = p.communicate()
        ret = p.returncode
        if ret != 0:
            print('Build failed.')
            if out:
                data = out.decode().split('\n')
                if len(data) > 20:
                    for line in data[-20:]:
                        print(line)
                else:
                    print(out.decode())
            print('The project sources are found in %s.' % proj_dir)
            input('Fixup the package sources, then press enter to continue...')
        else:
            print('Build succeeded. Submitting sources to the build service.')

    # Checkin the changes
    Popen([which('osc'), 'ci']).wait()
    os.chdir(cwd)

    cleanup(api_url, home_proj, home_pkg, proj_dir, clone_dir)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Run against obs samba package to update to latest version. This will branch the target package into your home project, then check it out on the local machine.')
    parser.add_argument('-A', '--apiurl', help='osc URL/alias', action='store', default='https://api.opensuse.org')
    parser.add_argument('SOURCEPROJECT', help='The source project to branch from')
    parser.add_argument('SOURCEPACKAGE', help='The source package to update')
    parser.add_argument('-o', '--output-dir', help='Place the package directory in the specified directory instead of a temp directory', action='store', default=None)

    args = parser.parse_args()

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

    fetch_package(user, email, args.apiurl, args.SOURCEPROJECT, args.SOURCEPACKAGE, args.output_dir)
