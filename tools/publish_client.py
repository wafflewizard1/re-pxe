#!/usr/bin/env python3
"""Publish the original native client package to the authorized lab boot-file server."""
import argparse
import getpass
import hashlib
import json
import re
from pathlib import Path
import shlex
import zipfile
import time
import paramiko

ROOT=Path(__file__).resolve().parents[1]
VERSION='0.13.5'

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--iso',type=Path,help='Optional normal operator ISO to publish alongside the client')
    parser.add_argument('--media-manifest',type=Path,help='Builder manifest for --iso')
    parser.add_argument('--version',default=VERSION,help='Immutable release version')
    parser.add_argument('--artifacts',type=Path,default=ROOT/'.local/client',help='Directory containing validated client executables')
    parser.add_argument('--host',required=True)
    parser.add_argument('--user',required=True)
    parser.add_argument('--server-directory',required=True)
    args=parser.parse_args()
    if bool(args.iso)!=bool(args.media_manifest):parser.error('--iso and --media-manifest must be supplied together')
    version=args.version
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+',version):parser.error('Use a numeric release version')
    artifacts=args.artifacts
    package=artifacts/f're-pxe-client-{version}.zip'
    files=[(artifacts/name,name) for name in ('re-pxe.exe','repxe_core_tests.exe','repxe_wua_tests.exe')]
    if (artifacts/'repxe_hardware_tests.exe').exists():
        files.append((artifacts/'repxe_hardware_tests.exe','repxe_hardware_tests.exe'))
    if (artifacts/'repxe_progress_tests.exe').exists():
        files.append((artifacts/'repxe_progress_tests.exe','repxe_progress_tests.exe'))
    for name in ('repxe_battery_tests.exe','repxe_operation_lock_tests.exe','repxe_state_file_tests.exe','repxe_handoff_tests.exe'):
        if (artifacts/name).exists():files.append((artifacts/name,name))
    for folder in ('examples','media'):
        files += [(p,str(p.relative_to(ROOT/'client'))) for p in (ROOT/'client'/folder).rglob('*') if p.is_file()]
    files += [(ROOT/'docs/windows.md','README.md'),
              (ROOT/'client/third_party/README.md','third_party/README.md'),
              (ROOT/'client/third_party/nlohmann/LICENSE.MIT','third_party/nlohmann/LICENSE.MIT'),
              (ROOT/'client/third_party/PUGIXML-LICENSE.md','third_party/PUGIXML-LICENSE.md')]
    with zipfile.ZipFile(package,'w',zipfile.ZIP_DEFLATED) as archive:
        for path,name in sorted(files,key=lambda pair:pair[1]):
            info=zipfile.ZipInfo(name,(2026,10,5,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED
            archive.writestr(info,path.read_bytes())
    digest=hashlib.sha256(package.read_bytes()).hexdigest()
    manifest={'schemaVersion':1,'version':version,'package':package.name,'sha256':digest,
              'installationNamespace':'re-pxe','freshProvisioningForNamespaceTransition':True,
              'clientSha256':hashlib.sha256((artifacts/'re-pxe.exe').read_bytes()).hexdigest(),
              'deploymentExecutionAvailable':True,'deploymentTargetPolicy':'format-selected-disk',
              'configurableBlankRawOnlyPolicyAvailable':True,
              'technicianUiAvailable':True,'technicalDetailsVisibleByDefault':False,'simplifiedPxeClientAvailable':True,'pxeBootAvailable':True,'dashboardImageSelectionAvailable':True,'automaticPxeDiskSelectionAvailable':True,'configurablePxeCountdownAvailable':True,'configurableOfflineClientHidingAvailable':True,'temporaryShareAuthenticationAvailable':True,'serverJobOrchestration':'durable-idempotent-offline-events-explicit-retry',
              'windowsCompletionAvailable':True,'completionDriverInstallationAvailable':True,
              'automaticDriverSource':'reviewed-exact-model-build-library-then-windows-update',
              'automaticRestartAfterDeploymentAvailable':True,'driverConsoleAvailable':True,
              'driverConsoleCountdownSeconds':10,'driverConsoleReturnsToMainMenu':True,
              'driverConsoleUploadAvailable':True,'driverLibraryContentDeduplicationAvailable':True,
              'driverLibraryModelTagsAvailable':True,
              'windowsUpdateDriverCompletionAvailable':True,'windowsUpdateWaitSecondsBounds':[60,1800],
              'windowsUpdateAttemptBounds':[1,3],'offlineBootCriticalDriverStagingAvailable':True,
              'completionReportDelivery':'https-job-scoped-durable-explicit-retry'}
    if tuple(map(int,version.split('.'))) >= (0,13,0):
        manifest.update({'routineFinishOptionsAvailable':True,'acceptedReportClientCleanupAvailable':True,'customerDesktopPdfAvailable':True,'routineLocalAccountAvailable':True,'customerReportIntegrity':'sha256-and-size'})
    if tuple(map(int,version.split('.'))) >= (0,13,1):
        manifest.update({'keyboardDiskListAvailable':True,'diskSelectionKeys':['arrows','enter','tab','shift-tab'],'manualDiskReviewRequired':True})
    if tuple(map(int,version.split('.'))) >= (0,13,3):
        manifest.update({'manualDiskIdentityTypingRequired':False,'manualDiskInstallFocusedAfterReview':True})
    release_files=[package]
    if tuple(map(int,version.split('.'))) >= (0,12,4):
        manifest.update({'driverCollectionHealthChecksAvailable':True,
                         'driverUploadResumeAvailable':True,'driverPassSummaryAvailable':True,
                         'offlineReviewedDriverPreparationAvailable':True,
                         'driverInstallTimeoutSeconds':900})
    if tuple(map(int,version.split('.'))) >= (0,12,6):
        manifest.update({'hardwareInventoryAvailable':True,'hardwareInventorySchemaVersion':1})
    if tuple(map(int,version.split('.'))) >= (0,12,7):
        manifest.update({'installationProgressAvailable':True,'installationProgressSource':'dism-apply-image'})
    if tuple(map(int,version.split('.'))) >= (0,12,9):
        manifest.update({'portableDriverFilenamesAvailable':True,
                         'driverCollectionRequiresHealthyDevices':False,
                         'technicianFinishWithDeviceProblemsAvailable':True,
                         'abandonedDriverUploadCleanupAvailable':True})
    if tuple(map(int,version.split('.'))) >= (0,12,10):
        manifest.update({'completionHardwareRefreshAvailable':True,
                         'batteryHealthSource':'powercfg-batteryreport',
                         'batteryHealthCalculation':'full-charge-capacity/design-capacity*100',
                         'idleTechnicianFinishAvailable':True,
                         'driverOperationGuard':'mutex-ownership'})
    if tuple(map(int,version.split('.'))) >= (0,12,11):
        manifest.update({'routineManualDiskSelectionAvailable':True,'routineFirstDetectedDiskAvailable':True,'nvmeControllerSerialAvailable':True,'serverConflictDetailAvailable':True})
    if tuple(map(int,version.split('.'))) >= (0,12,12):
        manifest.update({'technicianRetryOobeFinishAvailable':True,'installedClientUpdateAvailable':True})
    if tuple(map(int,version.split('.'))) >= (0,12,13):
        manifest.update({'interruptedStateSaveRecoveryAvailable':True,'stateSaveStrategy':'unique-staging-atomic-replace'})
    if tuple(map(int,version.split('.'))) >= (0,12,16):
        manifest.update({'optionalDownloadedImageSha256VerificationAvailable':True,'imageHashPassCount':1,'tlsCertificateFailureDiagnosticsAvailable':True})
    if tuple(map(int,version.split('.'))) >= (0,12,17):
        manifest.update({'sealedFinalReportRetryAvailable':True,'automaticShutdownRequiresFinalReportAck':True,'reportDeliveryTlsDiagnosticsAvailable':True,'sealedPendingReportClientUpdateAvailable':True})
    if args.iso:
        media=json.loads(args.media_manifest.read_text())
        with args.iso.open('rb') as stream:iso_hash=hashlib.file_digest(stream,'sha256').hexdigest()
        if media['clientVersion']!=version or media['clientSha256']!=manifest['clientSha256'] or media['isoSha256']!=iso_hash:
            raise RuntimeError('Operator ISO does not match this client release')
        if media['architecture']!='amd64' or media['labValidation'] or media['labStartup'] or media['labPowerShell']:
            raise RuntimeError('Only normal AMD64 operator media may be published')
        manifest['bootIso']={'filename':args.iso.name,'sha256':iso_hash,'bootWimSha256':media['bootWimSha256'],'bootEx':media['bootEx'],'trustedServerCertificateSha256':media.get('trustedServerCertificateSha256','')}
        release_files.append(args.iso)
    manifest_path=artifacts/'release.json';manifest_path.write_text(json.dumps(manifest,indent=2)+'\n')
    release_files.append(manifest_path)
    password=getpass.getpass('Server SSH password: ')
    ssh=paramiko.SSHClient();ssh.load_system_host_keys()
    ssh.connect(args.host,username=args.user,password=password,timeout=10)
    sftp=ssh.open_sftp();stage=f'{args.server_directory}/client/artifacts/release-{version}'
    target=f'{args.server_directory}/data/boot/client/{version}'
    def run(command,sudo=False):
        stdin,out,err=ssh.exec_command('sudo -S -p "" sh -c '+shlex.quote(command) if sudo else command)
        if sudo:stdin.write(password+'\n');stdin.flush()
        stdin.channel.shutdown_write();output=out.read().decode();error=err.read().decode()
        if out.channel.recv_exit_status():raise RuntimeError(output+error)
        return output
    try:
        run('mkdir -p '+shlex.quote(stage))
        for path in release_files:
            last_progress=[0.0]
            def progress(received,total):
                now=time.monotonic()
                if now-last_progress[0]>=20 or received==total:
                    print(f'Transferred {path.name}: {received}/{total} bytes',flush=True);last_progress[0]=now
            sftp.put(str(path),stage+'/'+path.name,callback=progress)
        # Keep a published version immutable. Repeating an identical publication is safe.
        try:
            with sftp.open(target+'/release.json') as handle:existing=json.load(handle)
            if existing!=manifest:raise RuntimeError('Published version differs; choose a new version')
        except FileNotFoundError:
            run('mkdir -p '+shlex.quote(str(Path(target).parent)),sudo=True)
            run('chmod 755 '+shlex.quote(stage)+' && chmod 644 '+' '.join(shlex.quote(stage+'/'+p.name) for p in release_files)+' && mv '+shlex.quote(stage)+' '+shlex.quote(target),sudo=True)
        print(f'Published http://{args.host}:8089/client/{version}/{package.name}')
        if args.iso:print(f'Operator ISO: http://{args.host}:8089/client/{version}/{args.iso.name}')
        print('Package SHA-256: '+digest)
    finally:sftp.close();ssh.close()

if __name__=='__main__':main()
