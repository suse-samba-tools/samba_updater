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

def fetch_package(user, api_url, project, package, output_dir):
    # Choose a random package name (to avoid name collisions)
    rpackage = '%s%s' % (package, next(get_candidate_names()))
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
        output_dir = mkdtemp(prefix=home_pkg)
    else:
        output_dir = os.path.abspath(os.path.join(output_dir, home_pkg))
    if not os.path.exists(output_dir):
        _, err = Popen([which('osc'), '-A', api_url, 'co', home_proj, home_pkg, '-o', output_dir], stdout=PIPE, stderr=PIPE).communicate()
        if err:
            raise Exception(err.decode())
    print('Checked out %s/%s at %s' % (home_proj, home_pkg, output_dir))

    # Check the package version
    spec_file = list(set(glob(os.path.join(output_dir, '*.spec'))) - set(glob(os.path.join(output_dir, '*-man.spec'))))[-1]
    spec = Spec.from_file(spec_file)

    # Delete the package unless we have generated an update
    Popen([which('osc'), '-A', api_url, 'rdelete', home_proj, home_pkg, '-m', 'Deleting package %s as part of automated update' % home_pkg]).wait()
    print('Deleted branch target %s/%s' % (home_proj, home_pkg))
    rmtree(output_dir)
    print('Deleted output directory %s' % output_dir)

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
