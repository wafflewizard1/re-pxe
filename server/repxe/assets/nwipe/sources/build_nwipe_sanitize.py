#!/usr/bin/env python3
"""Build re-pxe's pinned unattended nwipe extension without opening any disk.

Inputs are SHA256-pinned upstream archives. Compilation, pure mocked lifecycle
checks and dependency inspection run on source/ELF regular files only. Output
is a RAM-filesystem bundle with a private dynamic loader/library closure, so
ShredOS' existing utilities keep their own libc. No packages are installed.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tarfile

ROOT=Path(__file__).resolve().parents[1]
EXT=ROOT/'tools/nwipe_sanitize'
NWIPE_SHA='b1e9d94e1879934db688bce9515111ef6b0599b2244fdca2625a2e87e2d8e221'
CONFIG_SHA='e95798d2992a66ecd547ce3651d7e10642ff2211427c43a7238186ff4c372627'


def digest(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def private(path):
    path=path.absolute()
    if not path.resolve().is_relative_to(ROOT/'.local'):
        raise ValueError('Private build/output paths must stay under project .local')
    return path

def archive(path, expected, dest):
    path=private(path)
    if not stat.S_ISREG(path.lstat().st_mode) or digest(path)!=expected:
        raise ValueError('Archive hash or regular-file check failed: '+str(path))
    with tarfile.open(path,'r:gz') as tar:
        tar.extractall(dest,filter='data')

def run(args, cwd, log, env=None):
    with log.open('ab') as stream:
        stream.write(('\n$ '+' '.join(map(str,args))+'\n').encode())
        subprocess.run(args,cwd=cwd,env=env,stdout=stream,stderr=subprocess.STDOUT,check=True)


def package(binary, source, config_archive, nwipe_archive, work, output, lifecycle, sysroot, scope_checks, binding_checks):
    output.mkdir(mode=0o700)
    tree=output/'rootfs'; native=tree/'opt/re-pxe-nwipe'; lib=native/'lib'
    lib.mkdir(parents=True)
    shutil.copy2(binary,native/'nwipe')
    subprocess.run(['strip','--strip-unneeded',str(native/'nwipe')],check=True)
    # ELF linker trace only; it does not enter nwipe main or enumerate devices.
    loader=sysroot/'lib/x86_64-linux-gnu/ld-linux-x86-64.so.2'
    libpath=str(sysroot/'lib/x86_64-linux-gnu')+':'+str(sysroot/'usr/lib/x86_64-linux-gnu')
    trace=subprocess.run([str(loader),'--library-path',libpath,'--list',str(binary)],check=True,capture_output=True,text=True).stdout
    if 'not found' in trace: raise RuntimeError('Missing ELF dependency')
    dependencies=[]
    for line in trace.splitlines():
        value=line.split('=>',1)[1].strip().split()[0] if '=>' in line else line.strip().split()[0]
        if not value.startswith('/'): continue
        path=Path(value).resolve(strict=True)
        if not stat.S_ISREG(path.stat().st_mode) or not path.is_relative_to(sysroot): raise RuntimeError('Dependency is not a private Debian baseline regular file')
        name=line.strip().split(' => ')[0] if ' => ' in line else path.name
        name=Path(name).name
        target=(native/'ld-linux-x86-64.so.2') if name.startswith('ld-linux-') else lib/name
        shutil.copy2(path,target)
        dependencies.append({'name':name,'source':str(path),'sha256':digest(path),'size':path.stat().st_size})
    if not (native/'ld-linux-x86-64.so.2').exists(): raise RuntimeError('Private loader missing')
    wrapper=tree/'usr/bin/nwipe'; wrapper.parent.mkdir(parents=True)
    wrapper.write_text('#!/bin/sh\nexec /opt/re-pxe-nwipe/ld-linux-x86-64.so.2 --library-path /opt/re-pxe-nwipe/lib /opt/re-pxe-nwipe/nwipe "$@"\n')
    wrapper.chmod(0o755)
    notices=output/'sources';notices.mkdir()
    shutil.copy2(nwipe_archive,notices/'nwipe-upstream-v0.43.tar.gz')
    shutil.copy2(config_archive,notices/'libconfig-v1.8.1.tar.gz')
    shutil.copytree(EXT,notices/'extension')
    shutil.copy2(Path(__file__),notices/'build_nwipe_sanitize.py')
    # Full modified source is included, excluding generated object/dependency files.
    def source_filter(info):
        if info.name.endswith(('.o','.a','.Po','.Tpo','.log')) or info.name.endswith('/src/nwipe') or '/.deps/' in info.name or '/autom4te.cache/' in info.name:
            return None
        return info
    with tarfile.open(notices/'nwipe-0.43-repxe1-source.tar.gz','w:gz') as tar:
        tar.add(source,arcname='nwipe-0.43-repxe1',filter=source_filter)
    shutil.copy2(source/'COPYING',notices/'COPYING-nwipe')
    elf=subprocess.run(['readelf','-l',str(native/'nwipe')],check=True,capture_output=True,text=True).stdout
    version='glibc 2.36 (Debian bookworm amd64 baseline)'
    # Preserve redistributable dependency notices and the verified package/source records.
    shutil.copy2(sysroot.parent/'packages.json',notices/'debian-packages.json')
    # Preserve matching dependency source records for the separate public archive.
    for source_name,archive_name in [('source-manifest.json','debian-source-manifest.json'),('Sources.gz','debian-Sources.gz')]:
        source_record=sysroot.parent/source_name
        if source_record.is_file():shutil.copy2(source_record,notices/archive_name)
    license_dir=notices/'dependency-notices';license_dir.mkdir()
    records=json.loads((sysroot.parent/'packages.json').read_text())
    for name in records:
        copyright_file=sysroot/'usr/share/doc'/name/'copyright'
        if copyright_file.is_file(): shutil.copy2(copyright_file,license_dir/(name+'-copyright'))
    for package_name,path in [('nwipe','COPYING'),('libconfig','LICENSE')]:
        license_path=source/path if package_name=='nwipe' else work/'libconfig-1.8.1'/path
        if license_path.is_file(): shutil.copy2(license_path,license_dir/(package_name+'-COPYING'))
    manifest={
        'schema':1,'engine_version':'0.43-repxe1','upstream_version':'0.43',
        'upstream_url':'https://github.com/martijnvanbrummelen/nwipe/releases/tag/v0.43',
        'upstream_archive_sha256':NWIPE_SHA,'libconfig_archive_sha256':CONFIG_SHA,
        'patch_sha256':digest(EXT/'nwipe-0.43-repxe1.patch'),
        'binary_sha256':digest(native/'nwipe'),'private_libc':version,
        'methods':['sanitize_block','sanitize_crypto','sanitize_overwrite'],
        'single_device':True,'refuses_shared_nvme_subsystems':True,'original_nwipe_pdf':True,
        'mock_lifecycle':lifecycle,'mock_nvme_scope':scope_checks,'mock_device_binding':binding_checks,'cpu_baseline':'x86-64','physical_firmware_validation':False,
        'dependencies':dependencies,
        'files':{str(p.relative_to(tree)):{'sha256':digest(p),'size':p.stat().st_size,'mode':stat.S_IMODE(p.stat().st_mode)} for p in sorted(tree.rglob('*')) if p.is_file()},
    }
    (output/'bundle.json').write_text(json.dumps(manifest,indent=2)+'\n')
    (work/'elf.txt').write_text(elf)
    print(json.dumps({'bundle':str(output),'engine_version':manifest['engine_version'],'binary_sha256':manifest['binary_sha256'],'mock_lifecycle':lifecycle,'physical_firmware_validation':False}))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--nwipe-archive',type=Path,required=True)
    parser.add_argument('--libconfig-archive',type=Path,required=True)
    parser.add_argument('--sysroot',type=Path,required=True,help='Private verified Debian bookworm amd64 compiler/library sysroot')
    parser.add_argument('--work-dir',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();work=private(args.work_dir);output=private(args.output);sysroot=private(args.sysroot)
    if not (sysroot.parent/'packages.json').is_file(): raise ValueError('Verified Debian package records are required')
    if work.exists() or output.exists(): raise ValueError('Use new work/output directories; previous evidence is retained')
    work.mkdir(parents=True,mode=0o700)
    archive(args.nwipe_archive,NWIPE_SHA,work);archive(args.libconfig_archive,CONFIG_SHA,work)
    source=work/'nwipe-0.43';config=work/'libconfig-1.8.1';deps=work/'deps'
    log=work/'build.log'
    wrappers=work/'wrappers';wrappers.mkdir()
    for name,compiler in [('cc','gcc'),('cxx','g++')]:
        path=wrappers/name;path.write_text('#!/bin/sh\nexec '+str(sysroot/'usr/bin'/('x86_64-linux-gnu-'+compiler+'-12'))+' --sysroot='+str(sysroot)+' -Wl,-rpath-link='+str(sysroot/'usr/lib/x86_64-linux-gnu')+' -Wl,-rpath-link='+str(sysroot/'lib/x86_64-linux-gnu')+' \"$@\"\n');path.chmod(0o755)
    env=dict(os.environ);env['CC']=str(wrappers/'cc');env['CXX']=str(wrappers/'cxx')
    run(['cmake','-S',str(config),'-B',str(work/'config-build'),'-DCMAKE_INSTALL_PREFIX='+str(deps),'-DBUILD_EXAMPLES=OFF','-DBUILD_TESTS=OFF','-DBUILD_SHARED_LIBS=OFF'],ROOT,log,env)
    run(['cmake','--build',str(work/'config-build'),'-j4'],ROOT,log)
    run(['cmake','--install',str(work/'config-build')],ROOT,log)
    run(['patch','--batch','--forward','-p1','-i',str(EXT/'nwipe-0.43-repxe1.patch')],source,log)
    for path in EXT.glob('*.[ch]'):
        if not path.name.startswith('test_'): shutil.copy2(path,source/'src'/path.name)
    run(['./autogen.sh'],source,log)
    env['PKG_CONFIG_SYSROOT_DIR']=str(sysroot)
    env['PKG_CONFIG_LIBDIR']=str(sysroot/'usr/lib/x86_64-linux-gnu/pkgconfig')+':'+str(sysroot/'usr/share/pkgconfig')
    env['LIBCONFIG_CFLAGS']='-I'+str(deps/'include');env['LIBCONFIG_LIBS']=str(deps/'lib/libconfig.a')
    env['NWIPE_GIT_HASH']='v0.43-repxe1'
    run(['./configure','--prefix=/opt/re-pxe-nwipe','CFLAGS=-O2 -g -D_GNU_SOURCE'],source,log,env)
    run(['make','-j4'],source,log,env)
    # Exercise the actual duration conversion with reused SIGUSR1 outputs.
    text=(source/'src/miscellaneous.c').read_text()
    duration=text.split('void convert_seconds_to_hours_minutes_seconds(',1)[1].split('void calculate_duration_string(',1)[0]
    duration_source=work/'duration.c'
    duration_source.write_text('#include <stdint.h>\ntypedef uint64_t u64;\nvoid convert_seconds_to_hours_minutes_seconds('+duration)
    duration_test=work/'duration-tests'
    run(['cc','-std=c11','-Wall','-Wextra','-Werror',str(duration_source),str(EXT/'test_duration.c'),'-o',str(duration_test)],ROOT,log)
    run([str(duration_test)],ROOT,log)
    test=work/'state-tests'
    run(['cc','-std=c11','-Wall','-Wextra','-Werror','-fsanitize=undefined,address','-I'+str(EXT),str(EXT/'repxe_sanitize_state.c'),str(EXT/'test_state.c'),'-o',str(test)],ROOT,log)
    test_env=dict(os.environ);test_env['ASAN_OPTIONS']='detect_leaks=0'
    result=subprocess.run([str(test)],check=True,capture_output=True,text=True,env=test_env)
    lifecycle=result.stdout.strip();(work/'state-tests.txt').write_text(lifecycle+'\n')
    scope_test=work/'scope-tests'
    run(['cc','-std=c11','-D_GNU_SOURCE','-DHAVE_CONFIG_H','-ffunction-sections','-fdata-sections','-I'+str(EXT),'-I'+str(source/'src'),'-I'+str(source),str(EXT/'test_scope.c'),'-Wl,--gc-sections','-o',str(scope_test)],ROOT,log)
    scope_result=subprocess.run([str(scope_test)],check=True,capture_output=True,text=True).stdout.strip()
    (work/'scope-tests.txt').write_text(scope_result+'\n')
    binding_test=work/'binding-tests'
    run(['cc','-std=c11','-Wall','-Wextra','-Werror','-fsanitize=undefined,address','-I'+str(EXT),str(EXT/'test_binding.c'),'-o',str(binding_test)],ROOT,log)
    binding_result=subprocess.run([str(binding_test)],check=True,capture_output=True,text=True,env=test_env).stdout.strip()
    (work/'binding-tests.txt').write_text(binding_result+'\n')
    package(source/'src/nwipe',source,args.libconfig_archive,args.nwipe_archive,work,output,lifecycle,sysroot,scope_result,binding_result)

if __name__=='__main__': main()
