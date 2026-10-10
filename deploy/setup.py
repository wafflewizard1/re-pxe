#!/usr/bin/env python3
"""Initialize and start re-pxe on a Linux Docker host."""
import argparse, json, os, re, subprocess, time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]

def command(*args, capture=False):
    return subprocess.run(args, cwd=ROOT, check=True, text=True, stdout=subprocess.PIPE if capture else None)

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--address', required=True, help='Static LAN IP or DNS hostname')
p.add_argument('--samba', action='store_true', help='Configure dedicated shares in an existing Samba service (requires sudo)')
args = p.parse_args()
if not re.fullmatch(r'[A-Za-z0-9.-]+', args.address):
    p.error('Use a LAN address or hostname without a scheme or port')
command('python3', 'deploy/initialize.py', args.address)
env = dict(line.split('=',1) for line in (ROOT/'deploy/.env').read_text().splitlines() if '=' in line)
if env['REPXE_ADDRESS'] != args.address:
    raise SystemExit('Existing deployment address differs; review deploy/.env before changing it.')
# The API container uses uid 1000. Change ownership only inside this installation’s data folder.
data = Path(env['REPXE_DATA_ROOT'])
if data.stat().st_uid != 1000:
    raise SystemExit('Set this installation data folder owner to uid 1000 before starting the API.')
compose = ['docker', 'compose', '--project-directory', str(ROOT/'deploy'), '--env-file', str(ROOT/'deploy/.env')]
command(*compose, 'config', '--quiet')
command(*compose, 'up', '-d', '--build')
ca = data/'config/server-public-ca.crt'; ca.parent.mkdir(exist_ok=True)
for attempt in range(60):
    result = subprocess.run(compose + ['exec','-T','gateway','cat','/data/caddy/pki/authorities/local/root.crt'],cwd=ROOT,text=True,capture_output=True)
    if result.returncode == 0 and 'BEGIN CERTIFICATE' in result.stdout and 'PRIVATE KEY' not in result.stdout:
        pending = ca.with_suffix('.tmp'); pending.write_text(result.stdout); os.chmod(pending,0o644); pending.replace(ca); break
    time.sleep(2)
else:
    raise SystemExit('Gateway public certificate was not ready. Re-run setup after checking docker compose logs.')
# Publish the current loader automatically, embedding this server’s address.
command('python3','tools/build_pxe_bootloader.py','--address',args.address)
if args.samba:
    command('sudo','python3','deploy/configure_samba.py')
print('Portal: https://'+args.address+':8443 (or http://'+args.address+':8080)')
print('DHCP boot server: '+args.address+'; UEFI filename: pxe/snponly.efi')
print('ShredOS is included and is prepared automatically. Open the portal to create your first routine.')
