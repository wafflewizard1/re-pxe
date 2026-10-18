#!/usr/bin/env python3
"""One-time installation of the host updater; invoked with sudo by setup."""
import argparse
import json
import os
from pathlib import Path
import pwd
import re
import subprocess
import sys
import uuid
from update_agent import atomic


def install(root, username):
    if sys.version_info < (3, 12):
        raise SystemExit('Portal updates require Python 3.12 or newer on the host.')
    if os.geteuid() != 0:
        raise SystemExit('Run this installer with sudo.')
    root = root.resolve()
    account = pwd.getpwnam(username)
    if account.pw_uid != 1000:
        raise SystemExit('The updater must use the installation owner with uid 1000, matching the API container.')
    if not re.fullmatch(r'[A-Za-z0-9_./ -]+', str(root)) or not re.fullmatch(r'[a-z_][a-z0-9_-]*', username):
        raise SystemExit('Unsupported installation path or username.')
    if root.stat().st_uid != account.pw_uid or not (root / 'deploy/.env').is_file():
        raise SystemExit('Use the existing installation checkout owned by the Docker user.')
    base = root / '.local/updater'
    control = base / 'control'
    if (control / 'maintenance.json').exists():
        raise SystemExit('Recover the interrupted update before reinstalling the host service.')
    if (control / 'status.json').exists() and json.loads((control / 'status.json').read_text()).get('busy'):
        raise SystemExit('Wait for the current updater operation before reinstalling.')
    for folder in (root / '.local', base, control):
        folder.mkdir(exist_ok=True, mode=0o750)
        if folder.is_symlink():
            raise SystemExit('Updater directories must not be symbolic links.')
        os.chown(folder, account.pw_uid, account.pw_gid)
        os.chmod(folder, 0o750)
    lock = control / 'requests.lock'
    lock.touch(exist_ok=True)
    os.chown(lock, account.pw_uid, account.pw_gid)
    os.chmod(lock, 0o600)
    unit = f'''[Unit]
Description=re-pxe portal updater
After=docker.service network-online.target
Wants=network-online.target
Requires=docker.service

[Service]
Type=simple
User={username}
WorkingDirectory="{root}"
ExecStart=/usr/bin/python3 "{root}/deploy/update_agent.py" --root "{root}"
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
'''
    Path('/etc/systemd/system/re-pxe-updater.service').write_text(unit)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', 'enable', 're-pxe-updater.service'], check=True)
    subprocess.run(['systemctl', 'stop', 're-pxe-updater.service'], check=True)
    revision = subprocess.check_output(['git', '-c', f'safe.directory={root}', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
    for name in ('installed.json', 'state.json'):
        path = base / name
        if path.exists():
            path.rename(base / (name + '.before-setup-' + uuid.uuid4().hex))
    installed = base / 'installed.json'
    atomic(installed, {'revision': revision, 'config': None})
    os.chown(installed, account.pw_uid, account.pw_gid)
    subprocess.run(['systemctl', 'restart', 're-pxe-updater.service'], check=True)
    print('Portal updates enabled. Open Settings → Server to check GitHub.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--user', required=True)
    args = parser.parse_args()
    install(args.root, args.user)
