"""Verified catalog snapshots and explicitly confirmed, quiescent database restores.

Only this server's generated re-pxe-backups/<UUID> NAS folder is used. Published
image/driver/boot payloads are outside these database recovery snapshots.
"""
from contextlib import contextmanager, closing
from datetime import datetime, timezone, timedelta
from pathlib import Path
import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import uuid
import zipfile

from fastapi import HTTPException, BackgroundTasks, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from .durable import atomic_json as write_json, sync_directory

SNAPSHOT = re.compile(r'^catalog-[0-9]{8}T[0-9]{12}Z-[a-f0-9]{12}\.zip$')
CONFIRM = 'RESTORE DATABASE'
MAX_ARCHIVE = 512 * 1024 * 1024
DEFAULT_NAS = '\\\\nas.example\\backups\\'


class Settings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool = True
    interval_hours: int = Field(default=1, ge=1, le=168, strict=True)
    retain: int = Field(default=72, ge=2, le=1000, strict=True)
    nas_enabled: bool = False
    nas_destination: str = DEFAULT_NAS
    username: str = Field(default='', max_length=128)
    password: str | None = Field(default=None, max_length=256, exclude=True)
    clear_credentials: bool = Field(default=False, exclude=True)

    @field_validator('nas_destination')
    @classmethod
    def unc(cls, value):
        # Accept a server/share only. Subfolders and arbitrary command strings
        # cannot widen the generated server-specific destination.
        if not re.fullmatch(r'\\\\[A-Za-z0-9][A-Za-z0-9.-]{0,252}\\[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\\?', value):
            raise ValueError('Use \\\\server\\share\\; re-pxe creates its own backup subfolder')
        return value.rstrip('\\')+'\\'

    @field_validator('username', 'password')
    @classmethod
    def single_line(cls, value):
        if value and any(c in value for c in ('\r', '\n', '\x00')):
            raise ValueError('Credentials must be a single line')
        return value


class Restore(BaseModel):
    model_config = ConfigDict(extra='forbid')
    snapshot: str = Field(pattern=SNAPSHOT.pattern)
    confirmation: str
    acknowledge_data_replacement: bool


def atomic_json(path, data):
    # NAS credentials, server identity and backup manifests are private controls.
    return write_json(path, data, mode=0o600)


def control_dir(root):
    path = Path(root)/'database'
    path.mkdir(parents=True, exist_ok=True)
    return path


def backup_dir(root):
    return Path(root)/'backups/catalog'


def read_json(path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def settings(root):
    return Settings.model_validate(read_json(control_dir(root)/'backup-settings.json', {}))


def identity(root):
    return read_json(control_dir(root)/'backup-identity.json', {})['id']


def initialize(root):
    base = control_dir(root)
    if not (base/'backup-identity.json').exists():
        atomic_json(base/'backup-identity.json', {'id':str(uuid.uuid4())})
    if not (base/'backup-settings.json').exists():
        atomic_json(base/'backup-settings.json', Settings().model_dump())
    backup_dir(root).mkdir(parents=True, exist_ok=True, mode=0o700)


@contextmanager
def operation(root):
    with (control_dir(root)/'backup-operation.lock').open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise HTTPException(409, 'A backup or restore is already running')
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def save_settings(root, body):
    initialize(root)
    with operation(root):
        old = settings(root)
        secret = control_dir(root)/'backup-credentials.json'
        if body.clear_credentials:
            atomic_json(secret, {'password':''})
        elif body.password is not None:
            atomic_json(secret, {'password':body.password})
        elif (old.nas_destination, old.username) != (body.nas_destination, body.username):
            # Never carry credentials to a newly configured server/user.
            atomic_json(secret, {'password':''})
        atomic_json(control_dir(root)/'backup-settings.json', body.model_dump())
    return public(root)


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def db_info(path):
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=30)) as db:
        if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ValueError('Database integrity verification failed')
        if db.execute('PRAGMA foreign_key_check').fetchall():
            raise ValueError('Database has invalid foreign-key references')
        tables = [name for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        counts = {name:db.execute('SELECT count(*) FROM "'+name.replace('"','""')+'"').fetchone()[0] for name in tables}
        schema = db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchall()
        # Original certificates have their own accepted digest; check their bytes.
        if 'workflow_runs' in tables:
            for value, sha in db.execute('SELECT certificate,certificate_sha256 FROM workflow_runs WHERE certificate IS NOT NULL'):
                if hashlib.sha256(value).hexdigest() != sha:
                    raise ValueError('Stored erasure certificate hash does not match')
        return {'tables':counts, 'schema_sha256':hashlib.sha256(json.dumps(schema).encode()).hexdigest()}


def extract_verified(archive, destination):
    if archive.stat().st_size > MAX_ARCHIVE:
        raise ValueError('Snapshot exceeds 512 MiB recovery limit')
    with zipfile.ZipFile(archive) as source:
        entries = source.infolist()
        if len(entries) != 2 or {i.filename for i in entries} != {'catalog.sqlite','manifest.json'}:
            raise ValueError('Not a re-pxe database recovery snapshot')
        if sum(i.file_size for i in entries) > MAX_ARCHIVE or any(i.flag_bits & 1 for i in entries):
            raise ValueError('Invalid or oversized snapshot')
        manifest = json.loads(source.read('manifest.json'))
        if manifest.get('format') != 1 or manifest.get('scope') != 'catalog':
            raise ValueError('Unsupported snapshot format')
        output = destination/'catalog.sqlite'
        with source.open('catalog.sqlite') as src, output.open('xb') as dst:
            shutil.copyfileobj(src, dst)
            dst.flush(); os.fsync(dst.fileno())
        if digest(output) != manifest['database_sha256']:
            raise ValueError('Snapshot database hash mismatch')
        info = db_info(output)
        if info != manifest['database']:
            raise ValueError('Snapshot contents do not match verified manifest')
        return manifest


def verify(archive):
    archive = Path(archive).resolve()
    with tempfile.TemporaryDirectory(prefix='.verify-', dir=archive.parent) as directory:
        return extract_verified(archive, Path(directory))


def make_snapshot(root):
    root = Path(root).resolve()
    folder = backup_dir(root)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = 'catalog-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'-'+uuid.uuid4().hex[:12]+'.zip'
    source = Path(root)/'database/catalog.sqlite'
    if source.is_symlink() or not source.is_file():
        raise ValueError('Catalog must be an existing regular project file')
    if source.stat().st_size > MAX_ARCHIVE//2 or shutil.disk_usage(folder).free < max(64*1024**2, source.stat().st_size*4):
        raise ValueError('Insufficient space or catalog exceeds snapshot size limit')
    with tempfile.TemporaryDirectory(prefix='.creating-', dir=folder) as directory:
        staging = Path(directory); copy = staging/'catalog.sqlite'
        started = time.monotonic()
        def progress(status, remaining, total):
            if time.monotonic()-started > 120:
                raise TimeoutError('Database snapshot exceeded two minutes')
        with closing(sqlite3.connect(source.as_uri()+'?mode=ro', uri=True, timeout=30)) as src, closing(sqlite3.connect(copy)) as dst:
            src.backup(dst, pages=256, progress=progress, sleep=0.05)
            dst.execute('PRAGMA journal_mode=DELETE')
        manifest = {'format':1, 'scope':'catalog', 'server_id':identity(root),
            'created':datetime.now(timezone.utc).isoformat(), 'database_sha256':digest(copy),
            'database':db_info(copy), 'includes':'Catalog, users, settings, reports, original erasure PDFs and branding',
            'excludes':'Published images, driver payloads, boot media, external credentials and text configuration files'}
        packed = staging/name
        with zipfile.ZipFile(packed, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(copy,'catalog.sqlite'); archive.writestr('manifest.json',json.dumps(manifest,indent=2))
        if packed.stat().st_size > MAX_ARCHIVE:
            raise ValueError('Snapshot exceeds size limit')
        verify(packed)
        with packed.open('rb') as stream: os.fsync(stream.fileno())
        os.chmod(packed,0o600)
        os.rename(packed,folder/name);sync_directory(folder)
    return name, manifest


def nas_parts(root):
    spec = settings(root); host, share = spec.nas_destination.strip('\\').split('\\')
    server_id = identity(root)
    if not re.fullmatch(r'[a-f0-9-]{36}', server_id):
        raise ValueError('Invalid backup server identity')
    return spec, host, share, 're-pxe-backups/'+server_id


def smb(root, command, directory=''):
    spec,host,share,owned = nas_parts(root)
    if directory not in ('', 're-pxe-backups', owned):
        raise ValueError('NAS operation is outside owned folder')
    binary = shutil.which('smbclient')
    if not binary: raise RuntimeError('SMB backup client is unavailable')
    args=[binary,'//'+host+'/'+share,'-m','SMB3','--option=client min protocol=SMB2','-t','15']
    password=read_json(control_dir(root)/'backup-credentials.json',{}).get('password','')
    with tempfile.TemporaryDirectory(prefix='re-pxe-smb-') as directory_auth:
        if spec.username:
            auth=Path(directory_auth)/'credentials';auth.write_text('username = '+spec.username+'\npassword = '+password+'\n');auth.chmod(0o600)
            args+=['-A',str(auth)]
        else:args+=['-N','-U','%']
        if directory:args+=['-D',directory]
        args+=['-c',command]
        result=subprocess.run(args,capture_output=True,timeout=90)
        if result.returncode:
            # Return safe SMB status codes, never raw output or credentials.
            codes=re.findall(rb'NT_STATUS_[A-Z0-9_]+',result.stdout+result.stderr)
            code=codes[-1].decode() if codes else 'SMB_CONNECTION_FAILED'
            raise RuntimeError('NAS backup: '+code)
        return result.stdout


def nas_test(root):
    # Authenticate without enumerating or touching the share's files/folders.
    smb(root,'pwd')
    return {'connected':True,'guest':not settings(root).username,'folder':nas_parts(root)[3]}


def nas_upload(root, name):
    if not SNAPSHOT.fullmatch(name):raise ValueError('Invalid snapshot name')
    spec,host,share,owned=nas_parts(root)
    # Only create the application folder and this server's UUID folder.
    for directory,command in [('', 'mkdir re-pxe-backups'),('re-pxe-backups','mkdir '+identity(root))]:
        try:smb(root,command,directory)
        except RuntimeError as error:
            if 'NT_STATUS_OBJECT_NAME_COLLISION' not in str(error):raise
    local=backup_dir(root)/name
    smb(root,'put "'+str(local)+'" "'+name+'"',owned)
    with tempfile.TemporaryDirectory(prefix='.nas-verify-',dir=backup_dir(root)) as directory:
        check=Path(directory)/name
        smb(root,'get "'+name+'" "'+str(check)+'"',owned)
        if digest(check)!=digest(local):raise ValueError('NAS read-back hash verification failed')
        verify(check)
    return owned+'/'+name


def nas_download(root,name):
    if not SNAPSHOT.fullmatch(name):raise ValueError('Invalid snapshot name')
    target=backup_dir(root)/name
    with tempfile.TemporaryDirectory(prefix='.nas-download-',dir=backup_dir(root)) as directory:
        staged=Path(directory)/name
        smb(root,'get "'+name+'" "'+str(staged)+'"',nas_parts(root)[3])
        manifest=verify(staged)
        if manifest['server_id']!=identity(root):raise ValueError('Snapshot belongs to another server')
        os.chmod(staged,0o600);os.rename(staged,target);sync_directory(target.parent)
    return target


def update_status(root,**values):
    path=control_dir(root)/'backup-status.json'
    atomic_json(path,{**read_json(path,{}),**values})


def prune(root):
    # Retention only applies to registered, verified snapshots generated here.
    index_path=control_dir(root)/'backup-index.json';index=read_json(index_path,[])
    records=sorted(index,key=lambda r:r['created'],reverse=True);retain=settings(root).retain
    kept=records[:retain]
    for record in records[retain:]:
        name=record['name']
        if record.get('pre_restore') or not SNAPSHOT.fullmatch(name) or record.get('server_id')!=identity(root):
            kept.append(record);continue
        path=backup_dir(root)/name
        if record.get('nas_path') and record.get('nas_destination')==settings(root).nas_destination:
            # The same known filename in the fixed owned UUID directory only.
            try:smb(root,'del "'+name+'"',nas_parts(root)[3])
            except (RuntimeError,subprocess.TimeoutExpired):
                kept.append(record);continue
        if path.exists() and not path.is_symlink():path.unlink()
    atomic_json(index_path,kept)


def backup(root,*,due=False,send_nas=True):
    initialize(root)
    with operation(root):
        spec=settings(root);state=read_json(control_dir(root)/'backup-status.json',{})
        if due:
            if not spec.enabled:return {'skipped':'Automatic backups disabled'}
            last_due = state.get('last_attempt') or state.get('last_success')
            if last_due and datetime.now(timezone.utc)-datetime.fromisoformat(last_due)<timedelta(hours=spec.interval_hours):
                return {'skipped':'Not due'}
        update_status(root,running=True,last_attempt=datetime.now(timezone.utc).isoformat(),error='')
        try:
            name,manifest=make_snapshot(root)
            entry={'name':name,'created':manifest['created'],'server_id':identity(root),'bytes':(backup_dir(root)/name).stat().st_size,
                   'sha256':digest(backup_dir(root)/name),'nas_path':None,'nas_destination':None}
            index_path=control_dir(root)/'backup-index.json';index=read_json(index_path,[]);index.append(entry);atomic_json(index_path,index)
            update_status(root,last_local_success=manifest['created'],latest=name)
            if spec.nas_enabled and send_nas:
                entry['nas_path']=nas_upload(root,name);entry['nas_destination']=spec.nas_destination
                atomic_json(index_path,index);update_status(root,last_nas_success=manifest['created'])
            update_status(root,last_success=manifest['created'],running=False,error='',latest=name)
            prune(root)
            return entry
        except Exception as error:
            update_status(root,running=False,error=str(error)[:300])
            prune(root)
            raise


def public(root):
    spec=settings(root);state=read_json(control_dir(root)/'backup-status.json',{})
    index=read_json(control_dir(root)/'backup-index.json',[])
    result=spec.model_dump();result['has_password']=bool(read_json(control_dir(root)/'backup-credentials.json',{}).get('password'))
    return {'settings':result,'status':state,'server_id':identity(root),'nas_folder':nas_parts(root)[3],
            'snapshots':sorted(index,key=lambda r:r['created'],reverse=True),
            'scope':'Database, settings, users, report evidence, original erasure PDFs and logo. Published image/driver/boot files and editable text files are excluded.'}


def active_work(db):
    for table in ('routine_runs','workflow_runs','jobs'):
        if db.execute('SELECT 1 FROM '+table+" WHERE stage NOT IN ('complete','failed','cancelled') LIMIT 1").fetchone():
            return True
    return bool(db.execute("SELECT 1 FROM clients WHERE stage NOT IN ('complete','failed','cancelled') AND expires>? LIMIT 1",(datetime.now(timezone.utc).isoformat(),)).fetchone())


def restore(store,body,actor):
    if body.confirmation!=CONFIRM or not body.acknowledge_data_replacement:
        raise HTTPException(422,'Acknowledge replacement and type RESTORE DATABASE')
    root=store.root;initialize(root)
    with operation(root):
        archive=backup_dir(root)/body.snapshot
        index=read_json(control_dir(root)/'backup-index.json',[])
        record=next((r for r in index if r['name']==body.snapshot),None)
        if not record:raise HTTPException(404,'Snapshot is not registered on this server')
        if not archive.exists():
            if not record.get('nas_path') or record['nas_destination']!=settings(root).nas_destination:
                raise HTTPException(404,'Snapshot unavailable locally or at current NAS destination')
            archive=nas_download(root,body.snapshot)
        if archive.is_symlink() or digest(archive)!=record['sha256']:
            raise HTTPException(422,'Snapshot archive hash mismatch')
        # Exclusive process lock is also observed by every Store transaction.
        with (control_dir(root)/'catalog.access.lock').open('a') as access:
            try:fcntl.flock(access,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise HTTPException(409,'Database is busy; retry after current requests finish')
            try:
                current=root/'database/catalog.sqlite'
                with closing(sqlite3.connect(current,timeout=30)) as db:
                    if active_work(db):raise HTTPException(409,'Stop all machine jobs and active sessions before restoring')
                    live_info=db_info(current)
                    if db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0]!=0:
                        raise HTTPException(409,'Database checkpoint is busy; restore not started')
                with tempfile.TemporaryDirectory(prefix='.restore-',dir=control_dir(root)) as directory:
                    prepared=Path(directory);manifest=extract_verified(archive,prepared)
                    if manifest['server_id']!=identity(root) or manifest['database']['schema_sha256']!=live_info['schema_sha256']:
                        raise HTTPException(422,'Snapshot server/schema differs; use a reviewed offline migration')
                    staged=prepared/'catalog.sqlite'
                    with closing(sqlite3.connect(staged)) as restored:
                        # Do not revert publications whose immutable payload is gone.
                        for uid,kind,sha in restored.execute('SELECT id,kind,sha256 FROM artifacts'):
                            if not re.fullmatch(r'[a-f0-9-]{36}',uid):
                                raise HTTPException(422,'Invalid publication identity in snapshot')
                            if kind=='image':
                                image=root/'images/published'/uid/'image.wim'
                                if not image.is_file() or image.is_symlink():
                                    raise HTTPException(409,'A snapshot image payload is missing; restore its files separately first')
                            elif not restored.execute('SELECT 1 FROM driver_archives_purged WHERE archive_id=?',(uid,)).fetchone():
                                package=root/'drivers/packages'/uid/'files'
                                if not package.is_dir() or package.is_symlink():
                                    raise HTTPException(409,'A snapshot driver archive is missing; restore its files separately first')
                        for (metadata,) in restored.execute('SELECT metadata FROM driver_library WHERE id NOT IN (SELECT driver_id FROM driver_retired)'):
                            for payload in json.loads(metadata)['files']:
                                blob=root/'drivers/blobs'/payload['sha256']
                                if not blob.is_file() or blob.is_symlink():
                                    raise HTTPException(409,'A snapshot driver blob is missing; restore its files separately first')
                        at=datetime.now(timezone.utc).isoformat()
                        restored.execute('DELETE FROM website_sessions')
                        restored.execute('DELETE FROM report_credentials')
                        for table in ('routine_tickets','routine_step_tickets','routine_boots','workflow_boots'):
                            restored.execute('DELETE FROM '+table)
                        restored.execute('UPDATE workflow_runs SET ticket_hash=NULL,token_hash=NULL,expires=?,ticket_expires=?',(at,at))
                        restored.execute('UPDATE workflow_intake SET ticket_hash=NULL,token_hash=NULL,expires=?,ticket_expires=?',(at,at))
                        # Historical queued/active work stays evidence, never resumes.
                        reason='Cancelled during an explicitly confirmed database restore; fresh technician authorization is required'
                        for uid,step in restored.execute("SELECT id,step FROM routine_runs WHERE stage NOT IN ('complete','failed','cancelled')").fetchall():
                            restored.execute('INSERT INTO routine_events(run_id,step,stage,detail,created) VALUES(?,?,?,?,?)',(uid,step,'cancelled',reason,at))
                        for (uid,) in restored.execute("SELECT id FROM workflow_runs WHERE stage NOT IN ('complete','failed','cancelled')").fetchall():
                            restored.execute('INSERT INTO workflow_events(run_id,stage,detail,created) VALUES(?,?,?,?)',(uid,'cancelled',reason,at))
                        for (uid,) in restored.execute("SELECT id FROM jobs WHERE stage NOT IN ('complete','failed','cancelled')").fetchall():
                            restored.execute('INSERT INTO events(job_id,stage,detail,timestamp) VALUES(?,?,?,?)',(uid,'cancelled',reason,at))
                        for table in ('routine_runs','workflow_runs','jobs'):
                            restored.execute("UPDATE "+table+" SET stage='cancelled',updated=? WHERE stage NOT IN ('complete','failed','cancelled')",(at,))
                        restored.execute("UPDATE clients SET token_hash=NULL,expires=?,ticket_expires=?,countdown_until=NULL,rate=0,stage=CASE WHEN stage IN ('complete','failed','cancelled') THEN stage ELSE 'cancelled' END",(at,at))
                        restored.execute('CREATE TABLE IF NOT EXISTS database_restore_events(id TEXT PRIMARY KEY,snapshot TEXT NOT NULL,actor TEXT NOT NULL,created TEXT NOT NULL)')
                        restored.execute('INSERT INTO database_restore_events VALUES(?,?,?,?)',(str(uuid.uuid4()),body.snapshot,actor,at))
                        restored.commit();restored.execute('PRAGMA journal_mode=DELETE')
                    db_info(staged)
                    # Always preserve a verified current generation before replacement.
                    before,unused=make_snapshot(root)
                    index.append({'name':before,'created':unused['created'],'server_id':identity(root),'bytes':(backup_dir(root)/before).stat().st_size,'sha256':digest(backup_dir(root)/before),'nas_path':None,'nas_destination':None,'pre_restore':True})
                    atomic_json(control_dir(root)/'backup-index.json',index)
                    with staged.open('rb') as stream:os.fsync(stream.fileno())
                    os.chmod(staged,current.stat().st_mode & 0o777)
                    # SQLite has closed every live connection under the exclusive lock.
                    for suffix in ('-wal','-shm'):
                        side=Path(str(current)+suffix)
                        if suffix=='-wal' and side.exists() and side.stat().st_size:
                            raise HTTPException(409,'Uncheckpointed WAL remains; restore refused')
                        side.unlink(missing_ok=True)
                    os.replace(staged,current);sync_directory(current.parent)
                update_status(root,last_restore=at,restored_snapshot=body.snapshot,pre_restore_snapshot=before)
                return {'restored':True,'snapshot':body.snapshot,'preserved_current_snapshot':before,'sign_in_required':True}
            finally:fcntl.flock(access,fcntl.LOCK_UN)


def install(app,store,Role,allow):
    @app.get('/api/backups')
    def listing(role:Role):
        allow(role);return public(store().root)

    @app.put('/api/backups/settings')
    def update(body:Settings,role:Role):
        allow(role);return save_settings(store().root,body)

    @app.post('/api/backups/test-nas')
    def test_nas(role:Role):
        allow(role)
        try:return nas_test(store().root)
        except Exception as error:raise HTTPException(502,str(error)[:300])

    @app.post('/api/backups/create',status_code=202)
    def create(tasks:BackgroundTasks,role:Role):
        allow(role)
        # The operation lock handles timer/API overlap; the background response
        # makes a slow NAS transfer visible through the polled status.
        tasks.add_task(background_backup,store().root)
        return {'accepted':True}

    @app.get('/api/backups/{name}/download')
    def download(name:str,role:Role):
        allow(role)
        if not SNAPSHOT.fullmatch(name):raise HTTPException(404,'Snapshot not found')
        record=next((r for r in public(store().root)['snapshots'] if r['name']==name),None)
        path=backup_dir(store().root)/name
        if not record or path.is_symlink() or not path.is_file():raise HTTPException(404,'Snapshot unavailable locally')
        if digest(path)!=record['sha256']:raise HTTPException(422,'Snapshot hash mismatch')
        return FileResponse(path,media_type='application/zip',filename=name)

    @app.post('/api/backups/restore')
    def restore_backup(body:Restore,request:Request,role:Role):
        allow(role)
        actor=getattr(request.state,'web_user',{}).get('username','Admin API')
        try:return restore(store(),body,actor)
        except HTTPException:raise
        except (ValueError,sqlite3.Error) as error:raise HTTPException(422,str(error)[:300])
        except (OSError,RuntimeError,subprocess.TimeoutExpired):raise HTTPException(503,'Restore could not finish. Inspect the preserved snapshot and server state before retrying.')


def background_backup(root):
    try:backup(root)
    except Exception:pass  # backup persists its safe failure status; no retry of machine work.


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['backup','verify','status','test-nas'])
    parser.add_argument('--data',default=os.environ.get('REPXE_DATA','/data'))
    parser.add_argument('--archive');parser.add_argument('--due',action='store_true')
    args=parser.parse_args();root=Path(args.data).resolve();initialize(root)
    if args.action=='backup':result=backup(root,due=args.due)
    elif args.action=='verify':result=verify(Path(args.archive))
    elif args.action=='test-nas':result=nas_test(root)
    else:result=public(root)
    print(json.dumps(result,indent=2))


if __name__=='__main__':main()
