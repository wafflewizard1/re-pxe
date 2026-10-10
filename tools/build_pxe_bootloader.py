#!/usr/bin/env python3
"""Build and publish the current UEFI iPXE loader for this installation."""
import argparse, hashlib, json, os, re, shutil, subprocess, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
p = argparse.ArgumentParser(description=__doc__);p.add_argument('--address',required=True);args=p.parse_args()
if not re.fullmatch(r'[A-Za-z0-9.-]+',args.address):p.error('Invalid LAN address')
env=dict(line.split('=',1) for line in (ROOT/'deploy/.env').read_text().splitlines() if '=' in line)
out=Path(env['REPXE_DATA_ROOT'])/'boot/pxe';out.mkdir(parents=True,exist_ok=True)
with tempfile.TemporaryDirectory(prefix='re-pxe-ipxe-') as folder:
    work=Path(folder)
    for name in ('Dockerfile.ipxe','ignore-empty-input.patch'):
        shutil.copyfile(ROOT/'deploy/pxe'/name,work/name)
    template=(ROOT/'deploy/pxe/bootstrap.ipxe').read_text()
    (work/'bootstrap.ipxe').write_text(template.replace('@REPXE_ADDRESS@',args.address))
    subprocess.run(['docker','build','-f',str(work/'Dockerfile.ipxe'),'-t','re-pxe-ipxe-build',str(work)],check=True)
    container=subprocess.check_output(['docker','create','re-pxe-ipxe-build'],text=True).strip()
    try:
        artifacts=work/'artifacts';artifacts.mkdir()
        subprocess.run(['docker','cp',container+':/artifacts/.',str(artifacts)],check=True)
        for name in ('snponly.efi','ipxe-source.tar.xz','COPYING','ipxe-source-commit'):
            pending=out/(name+'.tmp');shutil.copyfile(artifacts/name,pending);os.chmod(pending,0o644);pending.replace(out/name)
        staged=ROOT/'.local/pxe';staged.mkdir(parents=True,exist_ok=True)
        for source,destination in [('snponly.efi','snponly.efi'),('ipxe-source.tar.xz','ipxe-source.tar.xz'),('COPYING','COPYING-ipxe'),('ipxe-source-commit','ipxe-source-commit')]:
            shutil.copyfile(artifacts/source,staged/destination)
        metadata={'sourceRevision':(artifacts/'ipxe-source-commit').read_text().strip(),'sourceUrl':'https://github.com/ipxe/ipxe','sourceArchive':'ipxe-source.tar.xz','sourceArchiveSha256':hashlib.sha256((artifacts/'ipxe-source.tar.xz').read_bytes()).hexdigest(),'binarySha256':hashlib.sha256((artifacts/'snponly.efi').read_bytes()).hexdigest(),'embeddedBootstrapSha256':hashlib.sha256((work/'bootstrap.ipxe').read_bytes()).hexdigest(),'patchSha256':hashlib.sha256((work/'ignore-empty-input.patch').read_bytes()).hexdigest(),'customization':'embedded bootstrap; console/PNG; empty-input countdown fix'}
        (staged/'ipxe-source.json').write_text(json.dumps(metadata,indent=2)+'\n')
    finally:
        subprocess.run(['docker','rm',container],check=True)
print('Published pxe/snponly.efi and its matching source.')
