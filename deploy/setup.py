#!/usr/bin/env python3
"""Initialize and start re-pxe on a Linux Docker host."""
import argparse, getpass, json, os, re, subprocess, time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]

def command(*args, capture=False):
    return subprocess.run(args, cwd=ROOT, check=True, text=True, stdout=subprocess.PIPE if capture else None)

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--address', required=True, help='Static LAN IP or DNS hostname')
p.add_argument('--samba', action='store_true', help='Configure dedicated shares in an existing Samba service (requires sudo)')
p.add_argument('--portal-updates', action='store_true', help='Install the optional host updater service (requires sudo and systemd)')
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
# Create the read-only control mount as the installation user, before Docker.
control = ROOT/'.local/updater/control'
control.mkdir(parents=True, exist_ok=True, mode=0o750)
(control/'requests.lock').touch(exist_ok=True)
if args.portal_updates:
    if os.getuid() != 1000:
        raise SystemExit('Run setup as the installation Docker user with uid 1000; it invokes sudo only for the service installer.')
os.environ['REPXE_REVISION'] = command('git', 'rev-parse', 'HEAD', capture=True).stdout.strip()
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
if args.portal_updates:
    command('sudo', 'python3', 'deploy/install_updater.py', '--root', str(ROOT), '--user', getpass.getuser())
print('Portal: https://'+args.address+':8443 (or http://'+args.address+':8080)')
print('DHCP boot server: '+args.address+'; UEFI filename: pxe/snponly.efi')
print('ShredOS is included and is prepared automatically. Open the portal to create your first routine.')
if not args.portal_updates:
    print('Optional portal updates: re-run setup with --portal-updates to install the host updater service.')
