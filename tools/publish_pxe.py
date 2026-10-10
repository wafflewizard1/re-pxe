#!/usr/bin/env python3
"""Stage or publish versioned PXE media on the authorized test server."""
import argparse,getpass,hashlib,json,paramiko,shlex,time,uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
MASTER_BOOT_FILENAME='pxe/snponly.efi'


def with_master_bootfile(manifest):
 return {**manifest, 'dhcp': {**manifest['dhcp'], 'filename': MASTER_BOOT_FILENAME}}


def select_media(sftp,run,target,manifest):
 """Atomically replace the master loader only after verifying its staged copy.

 The embedded bootstrap follows current.json through the managed API. Versioned
 media remains immutable; DHCP always uses the same regular file path.
 """
 boot_root=str(Path(target).parent);nonce=uuid.uuid4().hex
 pending_loader=boot_root+'/snponly-'+nonce+'.tmp';pending_manifest=boot_root+'/current-'+nonce+'.tmp'
 try:
  run('cp -- '+shlex.quote(target+'/snponly.efi')+' '+shlex.quote(pending_loader))
  run('chmod 644 '+shlex.quote(pending_loader))
  digest=run('sha256sum '+shlex.quote(pending_loader)).split()[0]
  if digest!=manifest['files']['snponly.efi']['sha256']:raise RuntimeError('Master PXE loader verification failed')
  with sftp.open(pending_manifest,'w') as f:f.write(json.dumps(with_master_bootfile(manifest),indent=2)+'\n')
  sftp.chmod(pending_manifest,0o644)
  sftp.posix_rename(pending_loader,boot_root+'/snponly.efi')
  sftp.posix_rename(pending_manifest,boot_root+'/current.json')
 finally:
  for path in (pending_loader,pending_manifest):
   try:sftp.remove(path)
   except FileNotFoundError:pass


def sha(path):
 with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--media-manifest',type=Path,required=True);p.add_argument('--files',type=Path,default=ROOT/'.local/pxe');p.add_argument('--validated',action='store_true');p.add_argument('--stage-only',action='store_true',help='Upload and verify candidate files without selecting live boot media');p.add_argument('--host',required=True);p.add_argument('--user',required=True);p.add_argument('--server-directory',required=True);args=p.parse_args()
 media=json.loads(args.media_manifest.read_text());version=media['clientVersion']
 if media['architecture']!='amd64' or any(media[k] for k in ('labValidation','labStartup','labPowerShell')):raise RuntimeError('Normal AMD64 WinPE required')
 if sha(args.files/'boot.wim')!=media['bootWimSha256']:raise RuntimeError('Boot WIM differs from builder manifest')
 names=('boot.wim','BCD','boot.sdi','bootx64.efi','snponly.efi','wimboot',
        'COPYING-ipxe','COPYING-wimboot','ipxe-source-commit','ipxe-source.json','ipxe-source.tar.xz',
        'wimboot-source.json','wimboot-source.tar.xz')
 bundle=hashlib.sha256(''.join(name+sha(args.files/name) for name in names).encode()).hexdigest()[:12]
 directory=version if args.validated else 'candidate-'+version+'-'+bundle
 manifest={'schemaVersion':1,'version':version,'directory':directory,'architecture':'amd64','validated':args.validated,'clientSha256':media['clientSha256'],
           'files':{name:{'size':(args.files/name).stat().st_size,'sha256':sha(args.files/name)} for name in names},
           'dhcp':{'server':args.host,'filename':MASTER_BOOT_FILENAME},'secureBootValidated':False}
 local=args.files/('release-'+directory+'.json');local.write_text(json.dumps(manifest,indent=2)+'\n')
 password=getpass.getpass('Server SSH password: ');ssh=paramiko.SSHClient();ssh.load_system_host_keys();ssh.connect(args.host,username=args.user,password=password,timeout=10);sftp=ssh.open_sftp()
 stage=args.server_directory+'/client/artifacts/pxe-'+directory;target=args.server_directory+'/data/boot/pxe/'+directory
 def run(command):
  stdin,out,err=ssh.exec_command(command);stdin.channel.shutdown_write();result=out.read().decode();error=err.read().decode()
  if out.channel.recv_exit_status():raise RuntimeError(result+error)
  return result
 try:
  run('mkdir -p '+shlex.quote(stage))
  for name in names:
   path=args.files/name;last=[0.0]
   def progress(n,total):
    now=time.monotonic()
    if now-last[0]>=20 or n==total:print(f'Transferred {name}: {n}/{total}',flush=True);last[0]=now
   sftp.put(str(path),stage+'/'+name,callback=progress)
  sftp.put(str(local),stage+'/release.json')
  run('mkdir -p '+shlex.quote(args.server_directory+'/data/boot/pxe'))
  try:
   with sftp.open(target+'/release.json') as f:previous=json.load(f)
   if with_master_bootfile(previous)!=manifest:raise RuntimeError('PXE version exists with different files')
  except FileNotFoundError:run('chmod 755 '+shlex.quote(stage)+' && chmod 644 '+shlex.quote(stage)+'/* && mv '+shlex.quote(stage)+' '+shlex.quote(target))
  # Verify server copies independently before selecting the bootstrap directory.
  output=run('sha256sum '+' '.join(shlex.quote(target+'/'+n) for n in names))
  for line,name in zip(output.splitlines(),names):
   if line.split()[0]!=manifest['files'][name]['sha256']:raise RuntimeError('Remote PXE file differs')
  if args.stage_only:
   print('PXE candidate staged:',directory,'live media unchanged');return
  select_media(sftp,run,target,manifest)
  print('PXE media selected:',directory,'validated='+str(args.validated));print('UniFi boot filename:',manifest['dhcp']['filename'])
 finally:sftp.close();ssh.close()
if __name__=='__main__':main()
