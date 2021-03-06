#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright (C) 2019 ScyllaDB
#

#
# This file is part of Scylla.
#
# Scylla is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Scylla is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Scylla.  If not, see <http://www.gnu.org/licenses/>.
#

import argparse
import io
import os
import pathlib
import subprocess
import tarfile
import pathlib
import shutil
import sys
import tarfile
from tempfile import mkstemp

def should_copy(f):
    '''Given a file, returns whether or not we are interested in copying this file.
    We want the actual python interepreter, and the files in /lib(64) and /usr/lib(64)
    All the stuff in /var and other paths is not useful for the relocatable package.
    The locale files take a lot of space and we won't use them, so we get rid of them as well.
    The build_id files will be symlinks to binaries and shared libraries, that we don't want to keep.
    '''

    if f == "": # package with no files
        return False

    if f.startswith("/usr/bin/python3."):
        return f[-1] != "m" # python ships with two binaries, one of them with a specialized malloc (python 3.xm). No need.

    if f.startswith("/lib64/ld-linux"): # the interpreter is copied by the binary fixup process
        return False

    parts = list(pathlib.PurePath(f).parts)
    el = parts.pop(0)
    if el != "/":
        raise RuntimeError("unexpected path: not absolute! {}".format(f))

    if len(parts) > 0 and parts[0] == "usr":
        parts.pop(0)

    if not parts:
        return False

    if parts[0] != "lib" and parts[0] != "lib64":
        return False
    parts.pop(0)

    if len(parts) > 0 and (parts[0] == "locale" or parts[0] == ".build-id"):
        return False
    return True

def fix_binary(ar, path, libpath):
    '''Makes one binary or shared library relocatable. To do that, we need to set RUNPATH to $ORIGIN/../lib64 so we get libraries
    from the relocatable directory and not from the system during runtime. We also want to copy the interpreter used so
    we can launch with it later.
    '''
    # it's a pity patchelf have to patch an actual binary.
    patched_elf = mkstemp()[1]
    shutil.copy2(path, patched_elf)

    subprocess.check_call(['patchelf',
                           '--set-rpath',
                           libpath,
                           patched_elf])
    return patched_elf

def fix_python_binary(ar, binpath):
    '''Makes the python binary relocatable. To do that, we need to set RUNPATH to $ORIGIN/../lib64 so we get libraries
    from the relocatable directory and not from the system during runtime. We also want to copy the interpreter used so
    we can launch with it later.
    '''
    pyname = os.path.basename(binpath)
    patched_binary = fix_binary(ar, binpath, '$ORIGIN/../lib64/')
    interpreter = subprocess.check_output(['patchelf',
                                           '--print-interpreter',
                                           patched_binary], universal_newlines=True).splitlines()[0]
    ar.add(os.path.realpath(interpreter), arcname=os.path.join("libexec", "ld.so"))
    ar.add(patched_binary, arcname=os.path.join("libexec", pyname + ".bin"))

def fix_dynload(ar, binpath, targetpath):
    patched_binary = fix_binary(ar, binpath, '$ORIGIN/../../')
    ar.add(patched_binary, arcname=targetpath, recursive=False)

def gen_python_thunk(ar, pybin):
    thunk=b'''\
#!/bin/bash
x="$(readlink -f "$0")"
b="$(basename "$x")"
d="$(dirname "$x")/.."
ldso="$d/libexec/ld.so"
realexe="$d/libexec/$b.bin"
exec -a "$0" "$ldso" "$realexe" "$@"
'''

    ti = tarfile.TarInfo(name=os.path.join("bin", pybin))
    ti.size = len(thunk)
    ti.mode = 0o755
    ar.addfile(ti, fileobj=io.BytesIO(thunk))

    ti = tarfile.TarInfo(name=os.path.join("bin", "python3"))
    ti.type = tarfile.SYMTYPE
    ti.linkname = pybin
    ar.addfile(ti)

def copy_file_to_python_env(ar, f):
    if f.startswith("/usr/bin/python"):
        gen_python_thunk(ar, os.path.basename(f))
        fix_python_binary(ar, f)
    else:
        libfile = f
        # python tends to install in both /usr/lib and /usr/lib64, which doesn't mean it is
        # a package for the wrong arch. So we need to handle both /lib and /lib64. Copying files
        # blindly from /lib could be a problem, but we filtered out all the i686 packages during
        # the dependency generation.
        if libfile.startswith("/usr/"):
            libfile = libfile.replace("/usr/", "/", 1)
        if libfile.startswith("/lib/"):
            libfile = libfile.replace("/lib/", "lib64/", 1)
        elif libfile.startswith("/lib64/"):
            libfile = libfile.replace("/lib64/", "lib64/", 1)
        else:
            raise RuntimeError("unexpected path: don't know what to do with {}".format(f))

        # copy file instead of link unless we link to the current directory.
        # links to the current directory are usually safe, but because we are manipulating
        # the directory structure, very likely links that transverse paths will break.
        if os.path.islink(f) and os.readlink(f) != os.path.basename(os.readlink(f)):
            ar.add(os.path.realpath(f), arcname=libfile)
        elif os.path.dirname(f).endswith("lib-dynload"):
            fix_dynload(ar, f, libfile)
        else:
            # in case this is a directory that is listed, we don't want to include everything that is in that directory
            # for instance, the python3 package will own site-packages, but other packages that we are not packaging could have
            # filled it with stuff.
            ar.add(f, arcname=libfile, recursive=False)

def filter_basic_packages(package):
    '''Returns true if this package should be filtered out. We filter out packages that are too basic like the Fedora repos,
    or contains no files'''
    # The packages below are way too basic and are listed just because repoquery will, correctly, list
    # everything. We make our lives easier by filtering them out.
    too_basic_packages = ["filesystem",
                           "tzdata",
                           "chkconfig",
                           "basesystem",
                           "coreutils",
                           "fedora-release",
                           "fedora-repos",
                           "fedora-gpg-keys",
                           "glibc-minimal-langpack",
                           "glibc-all-langpacks"]
    return True in [package.startswith(x) for x in too_basic_packages]


def dependencies(package_list):
    '''Generates a list of RPM dependencies for the python interpreter and its modules'''
    output = subprocess.check_output(['repoquery',
                # Some architectures like x86_64 also carry packages for
                # their 32-bit versions. In thise cases, we won't want
                # to mix them since we will only install lib64/
                '--archlist=noarch,{machine}'.format(machine=os.uname().machine),
                # Don't look into the yum cache. Guarantees consistent builds
                '--cacheonly',
                '--installed',
                '--resolve',
                '--requires',
                '--recursive'] + package_list,
                universal_newlines=True).splitlines()

    output = [x for x in output if not filter_basic_packages(x)]
    return output + package_list

def generate_file_list(executables):
    '''Given the RPM files that we want to scan in this run, returns a list of all files in those packages that are of interest to us'''

    exclusions = []
    for exe in executables:
        exclusions += subprocess.check_output(['rpm', '-qd', exe], universal_newlines=True).splitlines()

    # we don't want to use --list the first time: For one, we want to be able to filter out some packages with files we don't want to copy
    # Second, repoquery --list do not include the actual package files when used with --resolve and --recursive (only its dependencies').
    # So we need a separate step in which all packages are added together.
    candidates = subprocess.check_output(['repoquery',
                                 '--installed',
                                 '--cacheonly',
                                 '--list' ] + executables, universal_newlines=True).splitlines()

    return [x for x in set(candidates) - set(exclusions) if should_copy(x)]

ap = argparse.ArgumentParser(description='Create a relocatable python3 interpreter.')
ap.add_argument('--output', required=True,
                help='Destination file (tar format)')
ap.add_argument('modules', nargs='*', help='list of python modules to add, separated by spaces')

args = ap.parse_args()
packages= ["python3"] + args.modules

file_list = generate_file_list(dependencies(packages))
ar = tarfile.open(args.output, mode='w|gz')

for f in file_list:
    copy_file_to_python_env(ar, f)

ar.close()
