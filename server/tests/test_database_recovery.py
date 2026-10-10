"""Disposable catalog fixtures; never restore the live database or touch a NAS."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import hashlib
import json
import os
import shutil
import sqlite3
import zipfile
import pytest
from fastapi import HTTPException
from repxe.app import atomic_json, Store
from repxe import database_recovery as recovery
from test_api import auth
from test_pxe import lab


def test_snapshot_includes_committed_wal_and_certificate_bytes(tmp_path):
    store=Store(tmp_path)
    with store.db() as db:
        db.execute('CREATE TABLE evidence_test(id INTEGER PRIMARY KEY,value BLOB)')
        db.execute('INSERT INTO evidence_test VALUES(1,?)',(b'original evidence\x00\x01',))
    source=tmp_path/'database/catalog.sqlite'
    writer=sqlite3.connect(source)
    writer.execute('PRAGMA journal_mode=WAL');writer.execute('INSERT INTO evidence_test VALUES(2,?)',(b'committed WAL',));writer.commit()
    assert Path(str(source)+'-wal').stat().st_size
    entry=recovery.backup(tmp_path,send_nas=False);writer.close()
    archive=recovery.backup_dir(tmp_path)/entry['name'];manifest=recovery.verify(archive)
    assert manifest['database']['tables']['evidence_test']==2
    with zipfile.ZipFile(archive) as bundle:
        restored=tmp_path/'restored.sqlite';restored.write_bytes(bundle.read('catalog.sqlite'))
    with closing(sqlite3.connect(restored)) as db:
        assert db.execute('SELECT value FROM evidence_test ORDER BY id').fetchall()==[(b'original evidence\x00\x01',),(b'committed WAL',)]
    assert recovery.public(tmp_path)['status']['last_success']
    assert recovery.backup(tmp_path,due=True)=={'skipped':'Not due'}


def test_durable_projection_concurrent_writers_and_failed_publication(tmp_path,monkeypatch):
    path=tmp_path/'report.json';atomic_json(path,{'old':True})
    assert path.stat().st_mode&0o777==0o644
    private=tmp_path/'private.json';atomic_json(private,{'secret':'fixture'},mode=0o600)
    assert private.stat().st_mode&0o777==0o600
    stale=tmp_path/'report.tmp';stale.write_text('interrupted unrelated staging')
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda n:atomic_json(path,{'generation':n,'text':'x'*4096}),range(32)))
    assert json.loads(path.read_text())['generation'] in range(32)
    assert stale.read_text()=='interrupted unrelated staging'
    prior=path.read_bytes()
    monkeypatch.setattr(os,'replace',lambda *args:(_ for _ in ()).throw(OSError('simulated publication failure')))
    with pytest.raises(OSError):atomic_json(path,{'new':True})
    assert path.read_bytes()==prior and not list(tmp_path.glob('.report.json.*.tmp'))


def test_settings_private_credentials_and_scope_validation(lab):
    c,root,_=lab
    spec={'nas_enabled':True,'nas_destination':r'\\192.168.1.126\md1','username':'nas-user','password':'never-return-me'}
    saved=c.put('/api/backups/settings',headers=auth(),json=spec)
    assert saved.status_code==200,saved.text
    assert saved.json()['settings']['has_password'] and 'never-return-me' not in saved.text
    assert (root/'database/backup-credentials.json').stat().st_mode&0o777==0o600
    for unc in [r'\\192.168.1.126\md1\other',r'\\host\share\..',r'\\host\share;rm', 'http://server/share']:
        assert c.put('/api/backups/settings',headers=auth(),json={'nas_destination':unc}).status_code==422
    changed=c.put('/api/backups/settings',headers=auth(),json={**spec,'nas_destination':r'\\other\share','password':None})
    assert changed.status_code==200 and not changed.json()['settings']['has_password']
    for role in ('agent','deploy','capture'):
        assert c.get('/api/backups',headers=auth(role)).status_code==403
    c.post('/api/login',json={'username':'admin','password':'admin'})
    assert c.put('/api/backups/settings',json={}).status_code==403
    assert c.post('/api/backups/create').status_code==403
    assert c.post('/api/backups/restore',json={}).status_code==403


def test_corrupted_snapshot_refused_without_changing_database(tmp_path):
    store=Store(tmp_path);entry=recovery.backup(tmp_path,send_nas=False)
    archive=recovery.backup_dir(tmp_path)/entry['name'];prior=(tmp_path/'database/catalog.sqlite').read_bytes()
    with archive.open('ab') as stream:stream.write(b'changed-after-verification')
    with pytest.raises(HTTPException) as caught:
        recovery.restore(store,recovery.Restore(snapshot=entry['name'],confirmation=recovery.CONFIRM,acknowledge_data_replacement=True),'fixture')
    assert caught.value.status_code==422 and (tmp_path/'database/catalog.sqlite').read_bytes()==prior


def test_restore_requires_confirmation_blocks_active_work_and_preserves_previous(lab):
    c,root,p=lab;store=c.app.state.store
    # Completed/idle snapshot; fixtures own a synthetic publication file.
    with store.db() as db:db.execute('CREATE TABLE custom_evidence(id INTEGER PRIMARY KEY,value TEXT)');db.execute("INSERT INTO custom_evidence VALUES(1,'earlier')")
    entry=recovery.backup(root,send_nas=False)
    with store.db() as db:
        db.execute("UPDATE custom_evidence SET value='later'")
        db.execute("INSERT INTO jobs VALUES('restore-active','created',?,'serial:FIXTURE','Fixture',?,?,NULL)",(p['image_id'],datetime.now(timezone.utc).isoformat(),datetime.now(timezone.utc).isoformat()))
    body={'snapshot':entry['name'],'confirmation':'RESTORE DATABASE','acknowledge_data_replacement':True}
    assert c.post('/api/backups/restore',headers=auth(),json={**body,'confirmation':'wrong'}).status_code==422
    assert c.post('/api/backups/restore',headers=auth(),json=body).status_code==409
    with store.db() as db:db.execute("UPDATE jobs SET stage='cancelled' WHERE id='restore-active'")
    c.post('/api/login',json={'username':'admin','password':'admin'})
    response=c.post('/api/backups/restore',headers=auth(),json=body)
    assert response.status_code==200,response.text
    with store.db() as db:
        assert db.execute('SELECT value FROM custom_evidence').fetchone()[0]=='earlier'
        assert db.execute('SELECT count(*) FROM website_sessions').fetchone()[0]==0
        assert db.execute('SELECT count(*) FROM database_restore_events').fetchone()[0]==1
    previous=recovery.backup_dir(root)/response.json()['preserved_current_snapshot'];assert previous.exists()
    with zipfile.ZipFile(previous) as bundle:
        copy=root/'old.sqlite';copy.write_bytes(bundle.read('catalog.sqlite'))
    with closing(sqlite3.connect(copy)) as db:assert db.execute('SELECT value FROM custom_evidence').fetchone()[0]=='later'
    assert c.get('/health').json()['status']=='ok'


def test_restore_retires_snapshot_work_and_never_reissues_boot_credentials(lab):
    c,root,p=lab;store=c.app.state.store;at=datetime.now(timezone.utc).isoformat()
    with store.db() as db:
        db.execute("INSERT INTO jobs VALUES('snapshot-active','created',?,'serial:FIXTURE','Fixture',?,?,NULL)",(p['image_id'],at,at))
    entry=recovery.backup(root,send_nas=False)
    with store.db() as db:db.execute("UPDATE jobs SET stage='cancelled' WHERE id='snapshot-active'")
    response=c.post('/api/backups/restore',headers=auth(),json={'snapshot':entry['name'],'confirmation':recovery.CONFIRM,'acknowledge_data_replacement':True})
    assert response.status_code==200,response.text
    with store.db() as db:
        assert db.execute("SELECT stage FROM jobs WHERE id='snapshot-active'").fetchone()[0]=='cancelled'
        assert db.execute('SELECT count(*) FROM report_credentials').fetchone()[0]==0
        assert db.execute('SELECT count(*) FROM routine_tickets').fetchone()[0]==0


def test_failed_snapshot_preserves_prior_backup_and_does_not_claim_success(tmp_path,monkeypatch):
    Store(tmp_path);entry=recovery.backup(tmp_path,send_nas=False);before=recovery.public(tmp_path)['status']['last_success']
    monkeypatch.setattr(recovery,'make_snapshot',lambda root:(_ for _ in ()).throw(OSError('simulated no space')))
    with pytest.raises(OSError):recovery.backup(tmp_path)
    state=recovery.public(tmp_path)['status'];assert state['last_success']==before and 'no space' in state['error'] and not state['running']
    assert (recovery.backup_dir(tmp_path)/entry['name']).exists()


def test_nas_operations_and_retention_cannot_touch_other_folders(tmp_path,monkeypatch):
    Store(tmp_path);calls=[];nas={};unrelated=tmp_path/'unrelated.zip';unrelated.write_text('keep')
    def fake_smb(root,command,directory='',**kwargs):
        calls.append((command,directory));owned=recovery.nas_parts(root)[3]
        assert directory in ('','re-pxe-backups',owned)
        if command.startswith('put '):
            import shlex
            _,source,name=shlex.split(command);nas[name]=Path(source).read_bytes()
        elif command.startswith('get '):
            import shlex
            _,name,target=shlex.split(command);Path(target).write_bytes(nas[name])
        elif command.startswith('del '):
            name=command.split('"')[1];assert recovery.SNAPSHOT.fullmatch(name);nas.pop(name)
        else:assert command in ('pwd','mkdir re-pxe-backups','mkdir '+recovery.identity(root))
        return b''
    monkeypatch.setattr(recovery,'smb',fake_smb)
    recovery.save_settings(tmp_path,recovery.Settings(nas_enabled=True,retain=2))
    for _ in range(4):recovery.backup(tmp_path)
    assert len(nas)==2 and len(recovery.public(tmp_path)['snapshots'])==2
    assert unrelated.read_text()=='keep' and all('ls' not in command and 'rmdir' not in command for command,_ in calls)
    assert recovery.nas_test(tmp_path)['guest']
    assert all(directory==recovery.nas_parts(tmp_path)[3] for command,directory in calls if command.startswith(('put','get','del')))


def test_failed_nas_keeps_verified_local_snapshot_and_obeys_schedule(tmp_path,monkeypatch):
    Store(tmp_path);recovery.save_settings(tmp_path,recovery.Settings(nas_enabled=True))
    monkeypatch.setattr(recovery,'nas_upload',lambda *args:(_ for _ in ()).throw(RuntimeError('NAS backup: NT_STATUS_ACCESS_DENIED')))
    with pytest.raises(RuntimeError):recovery.backup(tmp_path)
    state=recovery.public(tmp_path);assert len(state['snapshots'])==1 and state['status']['last_local_success']
    assert 'last_success' not in state['status'] and 'ACCESS_DENIED' in state['status']['error']
    assert recovery.backup(tmp_path,due=True)=={'skipped':'Not due'}


def test_database_rolls_back_failed_transaction_and_requests_full_durability(tmp_path):
    store=Store(tmp_path)
    with pytest.raises(RuntimeError):
        with store.db() as db:
            assert db.execute('PRAGMA synchronous').fetchone()[0]==2
            assert db.execute('PRAGMA foreign_keys').fetchone()[0]==1
            db.execute("INSERT INTO schema_version VALUES(99)")
            raise RuntimeError('interrupted transaction')
    with store.db() as db:assert not db.execute('SELECT 1 FROM schema_version WHERE version=99').fetchone()

def test_original_certificate_survives_backup_restore_and_failed_replacement(lab,monkeypatch):
    from test_routine_activity import wipe_only,finish_erase
    from test_workflows import PDF
    c,root,_=lab;store=c.app.state.store;wipe_only(c,root);run=finish_erase(c)
    certificate=c.get('/api/workflow-runs/'+run['workflow_id']+'/certificate',headers=auth()).content
    assert certificate==PDF
    entry=recovery.backup(root,send_nas=False)
    prior=root/'database/catalog.sqlite'
    replace=os.replace
    def fail_database(source,target):
        if Path(target)==prior:raise OSError('simulated replace failure')
        return replace(source,target)
    monkeypatch.setattr(os,'replace',fail_database)
    with pytest.raises(OSError):recovery.restore(store,recovery.Restore(snapshot=entry['name'],confirmation=recovery.CONFIRM,acknowledge_data_replacement=True),'Fixture')
    assert c.get('/health').status_code==200
    assert c.get('/api/workflow-runs/'+run['workflow_id']+'/certificate',headers=auth()).content==certificate
    monkeypatch.setattr(os,'replace',replace)
    restored=recovery.restore(store,recovery.Restore(snapshot=entry['name'],confirmation=recovery.CONFIRM,acknowledge_data_replacement=True),'Fixture')
    assert restored['restored'] and c.get('/api/workflow-runs/'+run['workflow_id']+'/certificate',headers=auth()).content==certificate
    with store.db() as db:
        assert db.execute('SELECT count(*) FROM workflow_runs WHERE token_hash IS NOT NULL OR ticket_hash IS NOT NULL').fetchone()[0]==0
        assert db.execute('SELECT count(*) FROM workflow_intake WHERE token_hash IS NOT NULL OR ticket_hash IS NOT NULL').fetchone()[0]==0
    assert c.get('/api/job-reports/routine:'+run['id'],headers=auth()).status_code==200
    assert c.get('/api/job-reports/routine:'+run['id']+'/pdf',headers=auth()).status_code==200


def test_snapshot_hash_invalid_certificate_and_incompatible_schema_are_refused(lab):
    c,root,_=lab;store=c.app.state.store
    entry=recovery.backup(root,send_nas=False)
    with store.db() as db:db.execute('CREATE TABLE later_schema(id INTEGER PRIMARY KEY)')
    result=c.post('/api/backups/restore',headers=auth(),json={'snapshot':entry['name'],'confirmation':recovery.CONFIRM,'acknowledge_data_replacement':True})
    assert result.status_code==422 and 'schema differs' in result.text
    with store.db() as db:assert db.execute("SELECT 1 FROM sqlite_master WHERE name='later_schema'").fetchone()

def test_smb_guest_is_explicit_and_password_never_enters_argv(tmp_path,monkeypatch):
    Store(tmp_path);calls=[]
    monkeypatch.setattr(shutil,'which',lambda name:'/usr/bin/smbclient')
    def run(args,**kwargs):
        calls.append(args)
        if '-A' in args:
            path=Path(args[args.index('-A')+1]);assert path.stat().st_mode&0o777==0o600
            assert 'private-password' in path.read_text() and 'private-password' not in str(args)
        class Result:
            returncode=0;stdout=b'Current directory';stderr=b''
        return Result()
    monkeypatch.setattr(recovery.subprocess,'run',run)
    assert recovery.nas_test(tmp_path)['guest']
    assert calls[0][calls[0].index('-U')+1]=='%' and '-N' in calls[0]
    recovery.save_settings(tmp_path,recovery.Settings(username='nas-user',password='private-password'))
    assert not recovery.nas_test(tmp_path)['guest'] and '-A' in calls[1]
