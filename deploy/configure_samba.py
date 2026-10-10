"""Add isolated re-pxe shares to an existing Samba host and reload configuration."""
from datetime import datetime, timezone
from pathlib import Path
import os
import pwd
import shutil
import subprocess

root = Path(__file__).resolve().parents[1]
env = dict(line.split('=',1) for line in (root/'deploy/.env').read_text().splitlines() if '=' in line)
data = Path(env['REPXE_DATA_ROOT'])
owner = pwd.getpwuid(data.stat().st_uid).pw_name
if os.geteuid() != 0:
    raise SystemExit('Run with sudo')
if not shutil.which('smbd'):
    raise SystemExit('Existing Samba service required; use the host setup instructions')
for role in ('deploy','capture'):
    username = 're-pxe-'+role
    try:
        pwd.getpwnam(username)
    except KeyError:
        subprocess.run(['useradd','--system','--no-create-home','--shell','/usr/sbin/nologin',username],check=True)
    password = env['REPXE_SMB_'+role.upper()+'_PASSWORD']
    subprocess.run(['smbpasswd','-s','-a',username],input=password+'\n'+password+'\n',text=True,check=True,
                   stdout=subprocess.DEVNULL)
config = Path('/etc/samba/smb.conf')
original = config.read_text()
addition = Path('/etc/samba/re-pxe.conf')
previous_addition = addition.read_bytes() if addition.exists() else None
stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
backup = config.with_name('smb.conf.re-pxe-backup-'+stamp)
shutil.copy2(config,backup)
parts = []
for name,relative,role,writable in [('images','images/published','deploy',False),
                                   ('drivers','drivers','deploy',False),
                                   ('capture','images/staging','capture',True)]:
    parts.append(f'''[re-pxe-{name}]
    path = {data/relative}
    valid users = re-pxe-{role}
    force user = {owner}
    read only = {'no' if writable else 'yes'}
    guest ok = no
    browseable = yes
    create mask = 0644
    directory mask = 0755
    veto files = /.*/
    follow symlinks = no
''')
try:
    addition.write_text('\n'.join(parts))
    if 'include = /etc/samba/re-pxe.conf' not in original:
        config.write_text(original+'\n# re-pxe dedicated shares\n[global]\n    include = /etc/samba/re-pxe.conf\n')
    subprocess.run(['testparm','-s'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    subprocess.run(['smbcontrol','all','reload-config'],check=True)
except BaseException:
    config.write_text(original)
    if previous_addition is None:
        addition.unlink(missing_ok=True)
    else:
        addition.write_bytes(previous_addition)
    subprocess.run(['smbcontrol','all','reload-config'],check=False)
    raise
print(f're-pxe shares configured; previous Samba config backed up to {backup}.')
