#!/usr/bin/env python3
from subprocess import Popen, PIPE
import argparse
from shutil import which, rmtree
import re, os
from tempfile import mkdtemp, _get_candidate_names as get_candidate_names
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

def fetch_package(user, api_url, project, package, output_dir):
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
        new_vers[vers]['log'] = out.strip()
    os.chdir(cwd)

    # Delete the package unless we have generated an update
    Popen([which('osc'), '-A', api_url, 'rdelete', home_proj, home_pkg, '-m', 'Deleting package %s as part of automated update' % home_pkg]).wait()
    print('Deleted branch target %s/%s' % (home_proj, home_pkg))
    rmtree(proj_dir)
    print('Deleted project directory %s' % proj_dir)
    rmtree(clone_dir)
    print('Deleted samba shallow clone %s' % clone_dir)

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
        user = re.match(b'(\w+): .*', out).group(1).decode()

    fetch_package(user, args.apiurl, args.SOURCEPROJECT, args.SOURCEPACKAGE, args.output_dir)
