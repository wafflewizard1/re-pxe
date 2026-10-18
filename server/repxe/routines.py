"""Versioned routines and reboot-safe orchestration of the existing boot agents.

This module stores intent and evidence only. It never accesses host storage.
"""
from .activity import routine_stage, ATTENTION
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import secrets
import uuid
from typing import Literal

from fastapi import HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from . import workflows, file_workspace, driver_management, client_management, erase_policy

TERMINAL = ('complete', 'failed', 'cancelled')
STEP_LABELS = {'boot':'Boot','inventory':'Inventory','erase':'ShredOS','windows':'Windows install','checkpoint':'Technician checkpoint','ipxe':'Custom iPXE','finish':'Finish'}
MAC_PATTERN = r'^[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}$'
UUID_PATTERN = r'^[a-f0-9-]{36}$'
DDL = '''
CREATE TABLE IF NOT EXISTS routines(id TEXT PRIMARY KEY,draft TEXT NOT NULL,generation INTEGER NOT NULL,
 published_version INTEGER,created TEXT NOT NULL,updated TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS routine_versions(routine_id TEXT NOT NULL,version INTEGER NOT NULL,spec TEXT NOT NULL,
 created TEXT NOT NULL,PRIMARY KEY(routine_id,version));
CREATE TABLE IF NOT EXISTS routine_settings(id INTEGER PRIMARY KEY CHECK(id=1),enabled INTEGER NOT NULL,default_id TEXT);
INSERT OR IGNORE INTO routine_settings VALUES(1,1,NULL);
CREATE TABLE IF NOT EXISTS routine_assignments(mac TEXT PRIMARY KEY,routine_id TEXT NOT NULL,updated TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS routine_runs(id TEXT PRIMARY KEY,routine_id TEXT NOT NULL,version INTEGER NOT NULL,
 spec TEXT NOT NULL,mac TEXT NOT NULL,step INTEGER NOT NULL,stage TEXT NOT NULL,created TEXT NOT NULL,
 updated TEXT NOT NULL,workflow_id TEXT,client_id TEXT,job_id TEXT,target TEXT,size INTEGER,inventory TEXT,
 error TEXT NOT NULL DEFAULT '');
CREATE UNIQUE INDEX IF NOT EXISTS routine_active_mac ON routine_runs(mac) WHERE stage NOT IN ('complete','failed','cancelled');
CREATE TABLE IF NOT EXISTS routine_erase_disks(run_id TEXT NOT NULL,workflow_id TEXT NOT NULL UNIQUE,ordinal INTEGER NOT NULL,PRIMARY KEY(run_id,ordinal));
CREATE TABLE IF NOT EXISTS routine_removed(run_id TEXT PRIMARY KEY,removed TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS routine_events(seq INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT NOT NULL,
 step INTEGER NOT NULL,stage TEXT NOT NULL,detail TEXT NOT NULL,created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS routine_tickets(hash TEXT PRIMARY KEY,mac TEXT NOT NULL,expires TEXT NOT NULL,run_id TEXT);
CREATE TABLE IF NOT EXISTS routine_boots(client_id TEXT PRIMARY KEY,run_id TEXT NOT NULL);
'''


def now(): return datetime.now(timezone.utc).isoformat()
def digest(value): return hashlib.sha256(value.encode()).hexdigest()


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')


class FinishOptions(Input):
    cleanup_client: bool = Field(default=True, strict=True)
    save_report_desktop: bool = Field(default=False, strict=True)
    setup_mode: Literal['customer-oobe', 'local-account'] = 'customer-oobe'
    account_name: str = Field(default='Customer', pattern=r'^[A-Za-z][A-Za-z0-9 _-]{0,19}$')
    account_password: str = Field(default='', max_length=128)
    locale: str = Field(default='en-US', pattern=r'^[a-z]{2,3}-[A-Z]{2}$')
    keyboard: str = Field(default='0409:00000409', pattern=r'^[0-9a-fA-F]{4}:[0-9a-fA-F]{8}$')
    time_zone: str = Field(default='New Zealand Standard Time', pattern=r'^[A-Za-z0-9 ()+-]{1,64}$')

    @model_validator(mode='after')
    def account(self):
        if self.account_name.endswith(' '):raise ValueError('Account name cannot end with a space')
        if self.account_name.lower() in ('administrator','guest','defaultaccount','wdagutilityaccount','re-pxe','repxe'):
            raise ValueError('Choose a name other than a Windows built-in or re-pxe account')
        if any(ord(c) < 32 for c in self.account_password): raise ValueError('Account password cannot contain control characters')
        return self


class Step(workflows.EraseOptions):
    id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,64}$')
    kind: Literal['boot', 'inventory', 'erase', 'windows', 'checkpoint', 'ipxe', 'finish']
    profile_id: str | None = Field(default=None, pattern=UUID_PATTERN)
    verify_image: bool = Field(default=True, strict=True)
    finish_options: FinishOptions | None = None
    start_delay_seconds: int = Field(default=5, strict=True, ge=0, le=300)
    allow_usb: bool = Field(default=False, strict=True)
    target_policy: Literal['single-internal', 'all-internal', 'first-disk', 'manual', 'named-disk'] = 'single-internal'
    target_identity: str | None = Field(default=None, pattern=r'^serial:[^\x00-\x1f]{1,293}$')
    note: str = Field(default='', max_length=500)
    ipxe_path: str = Field(default='', max_length=800)
    x: float | None = Field(default=None, ge=0, le=12000)
    y: float | None = Field(default=None, ge=0, le=12000)


class Connection(Input):
    source: str = Field(pattern=r'^[A-Za-z0-9_-]{1,64}$')
    target: str = Field(pattern=r'^[A-Za-z0-9_-]{1,64}$')
    outcome: Literal['success', 'failure'] = 'success'


def connections(spec):
    if spec.get('graph') or spec.get('connections'): return spec.get('connections', [])
    return [dict(source=a['id'], target=b['id'], outcome='success') for a, b in zip(spec['steps'], spec['steps'][1:])]


def validate_graph(spec, complete=True):
    nodes = {s['id']: s for s in spec['steps']}; edges = connections(spec); outputs = set()
    for e in edges:
        source, target = e['source'], e['target']
        if source not in nodes or target not in nodes or source == target: raise ValueError('Connect two different existing nodes')
        if nodes[source]['kind'] == 'finish' or nodes[target]['kind'] == 'boot': raise ValueError('Boot is the entry and Finish is an endpoint')
        if (source, e['outcome']) in outputs: raise ValueError('Each output can connect to one node')
        if e['outcome'] == 'failure' and nodes[source]['kind'] not in ('erase', 'windows', 'ipxe'):
            raise ValueError('This node has no Failure output')
        outputs.add((source, e['outcome']))
    if not complete: return
    visited = set()
    def walk(uid, stack, failed='', erased=False, installed=False):
        if uid in stack: raise ValueError('Routine loops are not supported; no automatic wipe retries')
        node = nodes[uid]; kind = node['kind']; visited.add(uid)
        if failed and (kind=='windows' or kind=='erase' and failed!='unsupported'):
            raise ValueError('Failure branches cannot erase or install Windows unless native sanitize is confirmed unsupported before starting')
        if kind == 'erase' and installed: raise ValueError('Erasure must precede Windows installation')
        if kind == 'windows' and any(n['kind'] == 'erase' for n in nodes.values()) and not erased:
            raise ValueError('Windows must follow the successful erasure path')
        next_edges = [e for e in edges if e['source'] == uid]
        if kind != 'finish' and not any(e['outcome'] == 'success' for e in next_edges): raise ValueError('Connect the Success output of every nonterminal node')
        for e in next_edges:
            failure=failed
            if e['outcome']=='failure':failure='unsupported' if kind=='erase' and erase_policy.native(node['method']) and failed!='unsafe' else 'unsafe'
            elif kind=='erase':failure=''
            walk(e['target'],stack|{uid},failure,erased or kind=='erase' and e['outcome']=='success',installed or kind=='windows')
    walk(spec['steps'][0]['id'], set())
    if visited != set(nodes): raise ValueError('Every node must connect to Boot')


class Routine(Input):
    name: str = Field(min_length=1, max_length=120)
    steps: list[Step] = Field(min_length=3, max_length=24)
    connections: list[Connection] = Field(default_factory=list, max_length=48)
    graph: bool = Field(default=False, strict=True)

    @model_validator(mode='after')
    def chain(self):
        kinds = [s.kind for s in self.steps]
        if kinds[0] != 'boot' or kinds[-1] != 'finish' or kinds.count('boot') != 1 or kinds.count('finish') != 1:
            raise ValueError('A routine starts with Boot and ends with Finish')
        if len({s.id for s in self.steps}) != len(self.steps): raise ValueError('Step IDs must be unique')
        for kind in ('windows', 'inventory'):
            if kinds.count(kind) > 1: raise ValueError('Use at most one '+kind+' step per routine')
        if not any(k in kinds for k in ('erase', 'windows')): raise ValueError('Add ShredOS or Windows installation')
        validate_graph(self.model_dump(), complete=False)
        return self


class Save(Input):
    spec: Routine
    expected_generation: int = Field(ge=0, strict=True)


class Publish(Input):
    expected_generation: int = Field(ge=1, strict=True)


class Settings(Input):
    enabled: bool = Field(default=True, strict=True)
    default_id: str | None = Field(default=None, pattern=UUID_PATTERN)


class Assignment(Input):
    mac: str = Field(pattern=MAC_PATTERN)
    routine_id: str | None = Field(default=None, pattern=UUID_PATTERN)


class RunAction(Input):
    action: Literal['continue', 'cancel', 'remove']
    stopped_locally: bool = Field(default=False, strict=True)
    expected_progress_updated: str | None = Field(default=None, max_length=80)
    expected_step: int = Field(ge=0, strict=True)


class Menu(Input):
    title: str = Field(default='re-pxe', min_length=1, max_length=80, pattern=r'^[A-Za-z0-9 .:/()_-]+$')
    start_label: str = Field(default='Start assigned', min_length=1, max_length=80, pattern=r'^[A-Za-z0-9 .:/()_-]+$')
    other_label: str = Field(default='Select other routine', min_length=1, max_length=80, pattern=r'^[A-Za-z0-9 .:/()_-]+$')
    next_boot: bool = Field(default=True, strict=True)
    shutdown: bool = Field(default=True, strict=True)
    firmware: bool = Field(default=True, strict=True)
    theme: Literal['teal','blue','amber'] = 'teal'
    text_size: Literal['large','standard','compact'] = 'large'
    graphics: bool = Field(default=True, strict=True)
    show_logo: bool = Field(default=True, strict=True) # Legacy setting; ASCII heading uses show_banner.
    show_banner: bool = Field(default=True, strict=True)


class MenuSave(Input):
    spec: Menu
    expected_revision: str = Field(pattern=r'^[a-f0-9]{64}$')


def initialize(store):
    with store.db() as db:
        db.executescript(DDL)
        run_columns={r['name'] for r in db.execute('PRAGMA table_info(routine_runs)')}
        if 'failure_kind' not in run_columns:db.execute("ALTER TABLE routine_runs ADD COLUMN failure_kind TEXT NOT NULL DEFAULT ''")
        disk_columns={r['name'] for r in db.execute('PRAGMA table_info(routine_erase_disks)')}
        if 'step_index' not in disk_columns:
            db.execute('ALTER TABLE routine_erase_disks ADD COLUMN step_index INTEGER NOT NULL DEFAULT 0')
            for run in db.execute('SELECT id,spec FROM routine_runs').fetchall():
                index=next((i for i,s in enumerate(json.loads(run['spec'])['steps']) if s['kind']=='erase'),0)
                db.execute('UPDATE routine_erase_disks SET step_index=? WHERE run_id=?',(index,run['id']))
        columns={r['name'] for r in db.execute('PRAGMA table_info(routine_tickets)')}
        for name in ('default_id','default_version'):
            if name not in columns:db.execute('ALTER TABLE routine_tickets ADD COLUMN '+name+' '+('INTEGER' if name=='default_version' else 'TEXT'))
        db.execute('CREATE TABLE IF NOT EXISTS routine_step_tickets(hash TEXT PRIMARY KEY,run_id TEXT NOT NULL,step INTEGER NOT NULL,expires TEXT NOT NULL)')
    p = store.root / 'config/routines-menu.json'
    if not p.exists():
        try:
            with p.open('x') as f: f.write(Menu().model_dump_json(indent=2)+'\n')
        except FileExistsError: pass

    from .boot_appearance import APPEARANCE_PATH, DEFAULT_APPEARANCE
    appearance=store.root/'config'/APPEARANCE_PATH
    try:
        with appearance.open('x') as output:output.write(DEFAULT_APPEARANCE)
    except FileExistsError:pass


def enabled(db): return bool(db.execute('SELECT enabled FROM routine_settings WHERE id=1').fetchone()[0])


def published(db, uid):
    row = db.execute('''SELECT v.* FROM routines r JOIN routine_versions v ON v.routine_id=r.id
                        AND v.version=r.published_version WHERE r.id=?''', (uid,)).fetchone()
    if not row: raise HTTPException(409, 'Choose a published routine')
    return row


def default_for(db, mac):
    row = db.execute('SELECT routine_id FROM routine_assignments WHERE mac=?', (mac,)).fetchone()
    uid = row['routine_id'] if row else db.execute('SELECT default_id FROM routine_settings WHERE id=1').fetchone()[0]
    if not uid: return None
    return published(db, uid)


def profile_snapshot(db, uid):
    row = db.execute('SELECT * FROM deployment_profiles WHERE id=?', (uid,)).fetchone()
    if not row or not row['enabled']: raise HTTPException(409, 'Enable the Windows configuration first')
    spec = json.loads(row['spec'])
    if spec['selection_mode'] != 'choose-version': raise HTTPException(409, 'Routine Windows steps use version-selection configurations')
    return {**spec, 'id': uid, 'created': row['created']}


def validate_runtime(store, db, spec, profile_artifacts):
    try: validate_graph(spec)
    except ValueError as exc: raise HTTPException(409, str(exc)) from None
    for s in spec['steps']:
        if s['kind'] in ('erase','windows') and s['target_policy']=='named-disk' and not s['target_identity']:raise HTTPException(409,'Specify the exact disk serial')
        if s['kind'] == 'erase':
            if s['target_policy'] in ('first-disk','manual'): raise HTTPException(409,'Erasure requires all internal drives, one internal drive or an exact serial')
            if s['target_policy']=='all-internal' and workflows.media(store)['agent_version'] not in ('re-pxe-shredos-4','re-pxe-shredos-5','re-pxe-shredos-6','re-pxe-shredos-7','re-pxe-shredos-8','re-pxe-shredos-9',workflows.MEDIA_MARKER): raise HTTPException(409,'Update ShredOS before publishing multi-drive erasure')
            try: erase_policy.settings(s)
            except ValueError as exc: raise HTTPException(409, str(exc)) from None
            media = workflows.media(store)
            if erase_policy.extended(s['method']):workflows.require_native_media(media)
            if (s.get('verification', 'all') != 'all' or s.get('prng','auto') != 'auto' or s.get('io_mode','auto') != 'auto' or s['rounds'] > 8) and media['agent_version'] not in ('re-pxe-shredos-2', 're-pxe-shredos-3', 're-pxe-shredos-4', 're-pxe-shredos-5', 're-pxe-shredos-6', 're-pxe-shredos-7', 're-pxe-shredos-8', 're-pxe-shredos-9', workflows.MEDIA_MARKER):
                raise HTTPException(409, 'Update the ShredOS capability before publishing these nwipe settings')
        if s['kind'] == 'ipxe' and not s.get('ipxe_content'):
            raise HTTPException(409, 'Select a custom iPXE script before publishing')
        if s['kind'] == 'windows':
            if s['target_policy']=='all-internal': raise HTTPException(409,'Windows installs to one selected disk')
            current = db.execute('SELECT enabled FROM deployment_profiles WHERE id=?', (s['profile_id'],)).fetchone()
            if not current or not current['enabled']: raise HTTPException(409, 'The routine Windows configuration has been withdrawn')
            profile_artifacts(db, s['profile'])
            p = store.root / 'boot/pxe/current.json'
            if not p.is_file() or not json.loads(p.read_text()).get('validated'):
                raise HTTPException(409, 'Windows PXE boot media is not ready. Uploading install.wim only supplies the Windows installation image. Build WinPE with the re-pxe client and publish it using tools/publish_pxe.py --validated after acceptance testing. See Guides > Windows PXE setup or docs/windows.md; your routine remains saved as a draft.')
            if s['target_policy'] in ('first-disk','manual'):
                version=tuple(map(int,json.loads(p.read_text()).get('version','0.0.0').split('.')))
                if version < (0,12,11): raise HTTPException(409,'Publish validated WinPE 0.12.11 or newer before enabling routine disk selection')
            if not s.get('verify_image',True) and tuple(map(int,json.loads(p.read_text()).get('version','0.0.0').split('.'))) < (0,12,16):raise HTTPException(409,'Publish validated WinPE 0.12.16 or newer before disabling Windows image verification')
            if s.get('finish_options') is not None and tuple(map(int,json.loads(p.read_text()).get('version','0.0.0').split('.'))) < (0,13,0):raise HTTPException(409,'Publish validated WinPE 0.13.0 or newer before enabling Finish cleanup, customer reports or local accounts')


def event(db, row, stage, detail, step=None):
    at = now(); index = row['step'] if step is None else step
    db.execute('UPDATE routine_runs SET stage=?,step=?,updated=?,error=? WHERE id=?',
               (stage, index, at, detail if stage == 'failed' else row['error'], row['id']))
    db.execute('INSERT INTO routine_events(run_id,step,stage,detail,created) VALUES(?,?,?,?,?)',
               (row['id'], index, stage, detail, at))


def latest(db, mac):
    return db.execute('SELECT * FROM routine_runs WHERE mac=? ORDER BY created DESC,rowid DESC LIMIT 1', (mac,)).fetchone()


def for_boot(db, client_id):
    return db.execute('SELECT r.* FROM routine_runs r JOIN routine_boots b ON b.run_id=r.id WHERE b.client_id=?', (client_id,)).fetchone()


def successor(spec, index, outcome='success'):
    node = spec['steps'][index]['id']
    edge = next((e for e in connections(spec) if e['source'] == node and e['outcome'] == outcome), None)
    return next((i for i, s in enumerate(spec['steps']) if edge and s['id'] == edge['target']), None)


def advance(db, row, outcome='success', detail='', unsupported=False):
    spec = json.loads(row['spec']); index = successor(spec, row['step'], outcome)
    if outcome == 'failure':
        for child in db.execute("SELECT w.id FROM routine_erase_disks d JOIN workflow_runs w ON w.id=d.workflow_id WHERE d.run_id=? AND d.step_index=? AND w.stage IN ('queued','batch-wait','claimed')",(row['id'],row['step'])).fetchall():
            workflows.event(db,child['id'],'cancelled','Batch stopped after failed drive; no automatic retries')
            db.execute('UPDATE workflow_runs SET token_hash=NULL,ticket_hash=NULL WHERE id=?',(child['id'],))
        event(db, row, 'failed', detail or 'Operation failed; evidence retained')
        db.execute('UPDATE routine_runs SET failure_kind=? WHERE id=?',('unsupported' if unsupported else 'unsafe',row['id']))
        row = db.execute('SELECT * FROM routine_runs WHERE id=?', (row['id'],)).fetchone()
    elif json.loads(row['spec'])['steps'][row['step']]['kind']=='erase' and row['failure_kind']=='unsupported':
        db.execute("UPDATE routine_runs SET error='',failure_kind='' WHERE id=?",(row['id'],))
        row=db.execute('SELECT * FROM routine_runs WHERE id=?',(row['id'],)).fetchone()
    if index is None:
        if outcome != 'failure': event(db, row, 'failed', 'Missing Success connection')
        return
    if outcome == 'failure':
        db.execute('INSERT INTO routine_events(run_id,step,stage,detail,created) VALUES(?,?,?,?,?)',
                   (row['id'], row['step'], 'failure-route', 'Confirmed unsupported before any write; following Failure connection' if unsupported else 'Following Failure connection; erasure outcome retained', now()))
    if spec['steps'][index]['kind']=='erase':
        db.execute('UPDATE routine_runs SET workflow_id=NULL,target=NULL,size=NULL WHERE id=?',(row['id'],))
        row=db.execute('SELECT * FROM routine_runs WHERE id=?',(row['id'],)).fetchone()
    move(db, row, index)


def move(db, row, index):
    spec = json.loads(row['spec']); steps = spec['steps']
    while steps[index]['kind'] in ('boot', 'inventory'):
        index = successor(spec, index)
        if index is None: event(db, row, 'failed', 'Missing Success connection'); return
    kind = steps[index]['kind']
    stage = {'erase': 'discovering', 'windows': 'windows-boot', 'checkpoint': 'checkpoint', 'ipxe': 'custom-boot', 'finish': 'failed' if row['error'] else 'complete'}[kind]
    if row['error'] and (kind=='windows' or kind=='erase' and row['failure_kind']!='unsupported'): event(db,row,'failed','Failed erasure cannot authorize a destructive step'); return
    if kind == 'finish' and row['error']: event(db, row, 'failed', row['error'], index); return
    event(db, row, stage, 'Step: '+kind, index)


def sync(db):
    workflows.sync_jobs(db)
    for row in db.execute("SELECT * FROM routine_runs WHERE stage NOT IN ('complete','failed','cancelled')").fetchall():
        if row['workflow_id'] and row['stage'] in ('discovering', 'erase-running'):
            w = db.execute('SELECT * FROM workflow_runs WHERE id=?', (row['workflow_id'],)).fetchone()
            if w['stage']=='unsupported':advance(db,row,'failure',w['error'],unsupported=True)
            elif w['stage'] == 'failed':
                result = json.loads(w['result']) if w['result'] else {}
                if result.get('evidence', {}).get('safe_to_reboot', True): advance(db, row, 'failure', w['error'] or 'Erasure failed; automatic restart refused')
                else: event(db, row, 'failed', 'Hardware operation remains uncertain or active; inspect locally before rebooting')
            elif w['stage'] == 'cancelled': event(db, row, 'cancelled', 'Erasure cancelled')
            elif w['stage'] in ('install-ready', 'complete'):
                if not w['certificate'] or not w['result'] or json.loads(w['result'])['result'] != 'passed':
                    event(db, row, 'failed', 'Verified certificate and result are required')
                else:
                    pending = db.execute('''SELECT w.* FROM routine_erase_disks d JOIN workflow_runs w ON w.id=d.workflow_id
                        WHERE d.run_id=? AND d.step_index=? AND w.stage='batch-wait' ORDER BY d.ordinal LIMIT 1''', (row['id'],row['step'])).fetchone()
                    if pending:
                        workflows.event(db, pending['id'], 'queued', 'Previous drive verified; next bound drive authorized')
                        db.execute('UPDATE routine_runs SET workflow_id=? WHERE id=?', (pending['id'],row['id']))
                    else: advance(db, row)
        if row['stage'] == 'windows-running' and row['job_id']:
            j = db.execute('SELECT stage FROM jobs WHERE id=?', (row['job_id'],)).fetchone()
            if j and j['stage'] == 'complete': advance(db, row)
            elif j and j['stage'] == 'failed': advance(db, row, 'failure', 'Windows failed; inspect the retained job report')
            elif j and j['stage'] == 'cancelled': event(db, row, 'cancelled', 'Windows cancelled')


def projection(db, row, detail=False):
    spec = json.loads(row['spec'])
    out = {k: row[k] for k in ('id', 'routine_id', 'version', 'mac', 'step', 'stage', 'created', 'updated', 'workflow_id', 'client_id', 'job_id', 'target', 'size', 'error')}
    out.update(name=spec['name'], connections=connections(spec), steps=[{k:v for k,v in s.items() if k not in ('profile', 'ipxe_content')} for s in spec['steps']])
    visited=[0,*[e['step'] for e in db.execute('SELECT step FROM routine_events WHERE run_id=? ORDER BY seq',(row['id'],))],row['step']]
    out['step_history']=list(dict.fromkeys(index for index in visited if 0<=index<len(spec['steps'])))
    for step in out['steps']:
        if step.get('finish_options'):
            step['finish_options']={k:v for k,v in step['finish_options'].items() if k!='account_password'}
    if row['workflow_id']:
        w = db.execute('SELECT stage,certificate_sha256,updated FROM workflow_runs WHERE id=?', (row['workflow_id'],)).fetchone()
        out['erase_disks'] = [dict(d) for d in db.execute('''SELECT w.id,w.target,w.size,w.stage,w.certificate_sha256,p.percent,p.detail,p.updated progress_updated
            FROM routine_erase_disks d JOIN workflow_runs w ON w.id=d.workflow_id LEFT JOIN workflow_progress p ON p.run_id=w.id
            WHERE d.run_id=? AND d.step_index=(SELECT step_index FROM routine_erase_disks WHERE workflow_id=?) ORDER BY d.ordinal''', (row['id'],row['workflow_id']))]
        for disk in out['erase_disks']:
            progress = workflows.progress_snapshot({'percent':disk['percent'],'detail':disk['detail'] or '', 'updated':disk['progress_updated']}) if disk['progress_updated'] else None
            disk.update(remaining_seconds=progress['remaining_seconds'] if progress else None,progress_stale=bool(progress and progress['stale']))
        out['erase_stage'] = w['stage']; out['certificate_url'] = '/api/workflow-runs/'+row['workflow_id']+'/certificate' if w['certificate_sha256'] else None
    inv=json.loads(row['inventory']) if row['inventory'] else {}
    disk_models={d['identity']:d.get('model','') for d in inv.get('disks',[])}
    for disk in out.get('erase_disks',[]):disk['model']=disk_models.get(disk['target'],'')
    client=db.execute('SELECT c.*,i.inventory hardware FROM clients c LEFT JOIN client_inventory i ON i.client_id=c.id WHERE c.id=?',(row['client_id'],)).fetchone() if row['client_id'] else None
    hardware=json.loads(client['hardware']) if client and client['hardware'] else {}
    system=hardware.get('system',{})
    out.update(model=system.get('model') or (client['model'] if client else '') or inv.get('model',''),
        manufacturer=system.get('manufacturer') or (client['manufacturer'] if client else '') or inv.get('manufacturer',''),
        system_serial=system.get('serial') or inv.get('system_serial',''),
        last_seen=client['seen'] if client else None,last_activity=max(row['updated'],client['seen'] if client else row['updated'],w['updated'] if row['workflow_id'] else row['updated']),
        client_stage=client['stage'] if client else None,report_id='routine:'+row['id'],
        can_cancel=row['stage'] not in ('complete','cancelled') and not any(d['stage'] in ('erasing','operation-pending') for d in out.get('erase_disks',[])) and out.get('erase_stage') not in ('erasing','operation-pending'),
        can_continue=row['stage'] in ('checkpoint', 'custom-boot'),step_label=STEP_LABELS[spec['steps'][row['step']]['kind']])
    job = db.execute('SELECT stage,report IS NOT NULL has_report FROM jobs WHERE id=?', (row['job_id'],)).fetchone() if row['job_id'] else None
    erase_stale = row['workflow_id'] and w['stage']=='erasing' and (datetime.now(timezone.utc)-datetime.fromisoformat(next((d['progress_updated'] for d in out.get('erase_disks',[]) if d['id']==row['workflow_id'] and d['progress_updated']),w['updated']))).total_seconds()>60
    progress_times=[d['progress_updated'] for d in out.get('erase_disks',[]) if d['progress_updated']]
    out['last_activity']=max([out['last_activity'],*progress_times]) if row['stage'] not in TERMINAL else row['updated']
    out['effective_stage'] = routine_stage(row['stage'], client['stage'] if client else None,
        job['stage'] if job else None, bool(job and job['has_report']), out.get('erase_stage'),erase_stale)
    out['removed'] = bool(db.execute('SELECT 1 FROM routine_removed WHERE run_id=?',(row['id'],)).fetchone())
    out['can_confirm_stopped'] = can_confirm_stopped(db,row)
    out['can_remove'] = not out['removed'] and ((row['stage'] in TERMINAL and out.get('erase_stage') not in ('erasing','operation-pending') and not any(d['stage'] in ('erasing','operation-pending') for d in out.get('erase_disks',[]))) or out['can_confirm_stopped'])
    out['needs_attention'] = out['effective_stage'] in ATTENTION
    if client:
        out['windows_targets'] = json.loads(client['targets'])
        progress=db.execute('SELECT percent FROM client_install_progress WHERE client_id=? AND job_id=?',(client['id'],client['job_id'])).fetchone()
        out['install_percent']=progress['percent'] if progress and client['stage']=='applying-image' else None
    if detail and client:
        from .device_inventory import facts_for
        out['device']=facts_for(db,client['id'],manufacturer=out['manufacturer'],model=out['model'],targets=json.loads(client['targets']))
    if detail and not client and inv:
        from .device_inventory import shredos_facts
        out['device']=shredos_facts(inv)
    if detail:
        from .job_reports import asset_attachments
        out['attachments'] = asset_attachments(db, out, [{'id':client['id']}] if client else [])
        if row['workflow_id']:
            result = db.execute('SELECT result FROM workflow_runs WHERE id=?', (row['workflow_id'],)).fetchone()['result']
            out['erase_result'] = json.loads(result) if result else None
        out['inventory'] = json.loads(row['inventory']) if row['inventory'] else None
        out['events'] = [dict(e) for e in db.execute('SELECT step,stage,detail,created FROM routine_events WHERE run_id=? ORDER BY seq', (row['id'],))]
    return out


def queue_from_inventory(db, mac, inventory):
    """Pin the complete allowed drive set using fresh RAM-boot inventory."""
    row = latest(db, mac)
    if not row or row['stage'] != 'discovering' or row['workflow_id']: return
    spec = json.loads(row['spec']); step = spec['steps'][row['step']]
    db.execute('UPDATE routine_runs SET inventory=? WHERE id=?', (json.dumps(inventory), row['id']))
    disks = [d for d in inventory['disks'] if d.get('transport') != 'usb' or step['allow_usb']]
    if step['target_policy'] == 'named-disk': disks = [d for d in disks if d['identity'] == step['target_identity']]
    elif step['target_policy'] != 'all-internal': disks = [d for d in disks if d.get('transport') != 'usb']
    if not disks or (step['target_policy'] != 'all-internal' and len(disks) != 1):
        advance(db, row, 'failure', 'Disk selection is missing or ambiguous; specify one disk serial in the routine')
        return
    windows = next((s for s in spec['steps'] if s['kind'] == 'windows'), None)
    if windows and windows['target_policy']=='named-disk' and not any(d['identity']==windows['target_identity'] for d in disks):
        advance(db,row,'failure','Windows target must be included in the erased drive set'); return
    # Resolve every drive before authorizing any write. One failed/unsupported
    # drive holds the entire batch and can never silently install Windows.
    prepared = []
    if erase_policy.native(step['method']):
        controllers=[d.get('capabilities',{}).get('controller_id') for d in disks if d.get('capabilities',{}).get('sanitize_status')=='ready' and d.get('capabilities',{}).get('scope')=='controller']
        if len(controllers)!=len(set(controllers)):
            event(db,row,'failed','Native controller topology is inconsistent; inspect the PC without erasing or rebooting')
            db.execute("UPDATE routine_runs SET failure_kind='unsafe' WHERE id=?",(row['id'],));return
    for disk in disks:
        capabilities = disk.get('capabilities', {})
        try: policy, fallback = erase_policy.resolve(step,capabilities,disk,disks)
        except erase_policy.Unsupported as exc:
            db.execute('INSERT INTO routine_events(run_id,step,stage,detail,created) VALUES(?,?,?,?,?)',(row['id'],row['step'],'unsupported',disk['identity']+': '+str(exc),now()))
            advance(db,row,'failure',str(exc),unsupported=True)
            following=latest(db,mac)
            if following and following['stage']=='discovering':queue_from_inventory(db,mac,inventory)
            return
        except ValueError as exc:
            if isinstance(exc,erase_policy.Uncertain) or erase_policy.native(step['method']):
                event(db,row,'failed',str(exc));db.execute("UPDATE routine_runs SET failure_kind='unsafe' WHERE id=?",(row['id'],))
            else:advance(db,row,'failure',str(exc))
            return
        method = policy['method']
        if windows and method.startswith('verify_'):
            advance(db, row, 'failure', 'Read-only verification cannot authorize an erased-disk Windows handoff'); return
        if fallback:
            db.execute('INSERT INTO routine_events(run_id,step,stage,detail,created) VALUES(?,?,?,?,?)',(row['id'],row['step'],'fallback','Requested method unsupported; technician-selected fallback: '+method,now()))
        erase = workflows.Template(name=spec['name'], requested_method=step['method'],
            **{k: policy[k] for k in workflows.EraseOptions.model_fields if k in policy},
            allow_usb=step['allow_usb'], after_erasure='stop').model_dump_json()
        prepared.append((disk,erase))
    ordinal_base=db.execute('SELECT COALESCE(MAX(ordinal),-1)+1 FROM routine_erase_disks WHERE run_id=?',(row['id'],)).fetchone()[0]
    for local_ordinal, (disk,erase) in enumerate(prepared):
        ordinal=ordinal_base+local_ordinal
        uid = str(uuid.uuid4()); at = now()
        db.execute('''INSERT INTO workflow_runs(id,template_id,spec,mac,target,size,asset_tag,stage,created,updated)
                          VALUES(?,?,?,?,?,?,?,?,?,?)''', (uid, row['routine_id'], erase, mac, disk['identity'], disk['size_bytes'], '', 'queued' if local_ordinal == 0 else 'batch-wait', at, at))
        db.execute('UPDATE workflow_runs SET inventory=? WHERE id=?', (json.dumps(inventory), uid))
        workflows.event(db, uid, 'queued' if local_ordinal == 0 else 'batch-wait', 'Published routine selected; freshly discovered disk pinned')
        db.execute('INSERT INTO routine_erase_disks(run_id,workflow_id,ordinal,step_index) VALUES(?,?,?,?)',(row['id'],uid,ordinal,row['step']))
    first=db.execute('SELECT workflow_id FROM routine_erase_disks WHERE run_id=? AND step_index=? ORDER BY ordinal LIMIT 1',(row['id'],row['step'])).fetchone()[0]
    db.execute('UPDATE routine_runs SET workflow_id=?,target=?,size=? WHERE id=?',
               (first, disks[0]['identity'] if len(disks)==1 else None, disks[0]['size_bytes'] if len(disks)==1 else None, row['id']))
    event(db, row, 'erase-running', 'Fresh inventory pinned to '+str(len(disks))+' drive(s)')


def discovery_script(store, mac, address):
    release = workflows.media(store); ticket = secrets.token_urlsafe(48)
    expiry = (datetime.now(timezone.utc)+timedelta(minutes=15)).isoformat()
    with store.db() as db:
        db.execute('''INSERT INTO workflow_intake(mac,ticket_hash,ticket_expires,seen) VALUES(?,?,?,?)
            ON CONFLICT(mac) DO UPDATE SET ticket_hash=excluded.ticket_hash,ticket_expires=excluded.ticket_expires,
            token_hash=NULL,expires=NULL''', (mac, digest(ticket), expiry, now()))
    base = f'http://{address}:8089/pxe/shredos/{release["release"]}'
    return f'#!ipxe\nkernel {base}/shredos console=tty3 loglevel=3 repxe_ticket={ticket} repxe_mac={mac} repxe_mode=intake\ninitrd {base}/companion.cpio.gz\nboot\n'


def routing(store, mac, address):
    """Return script, optional Windows run and a handled flag."""
    with store.db() as db:
        sync(db); row = latest(db, mac)
        if not row: return None, None, False
        stage = row['stage']
        if stage == 'custom-boot':
            step = json.loads(row['spec'])['steps'][row['step']]
            ticket = secrets.token_urlsafe(48)
            db.execute('DELETE FROM routine_step_tickets WHERE expires<? OR run_id=?', (now(), row['id']))
            db.execute('INSERT INTO routine_step_tickets VALUES(?,?,?,?)', (digest(ticket), row['id'], row['step'], (datetime.now(timezone.utc)+timedelta(minutes=15)).isoformat()))
            continuation = f'http://{address}:8089/pxe/routines/continue.ipxe?ticket={ticket}'
            inv = json.loads(row['inventory']) if row['inventory'] else {}
            values = {'repxe_run': row['id'], 'repxe_mac': mac, 'repxe_serial': inv.get('system_serial',''),
                'repxe_target': row['target'] or '', 'repxe_error': row['error'] or 'No failure',
                'repxe_continue': continuation, 'repxe_fail': continuation+'&outcome=failure'}
            preamble = '#!ipxe\n' + ''.join('set '+k+' '+re.sub(r'[^A-Za-z0-9 .:/()_-]', ' ', str(v))[:1000]+'\n' for k,v in values.items() if k not in ('repxe_continue', 'repxe_fail'))
            preamble += 'set repxe_continue '+continuation+'\nset repxe_fail '+values['repxe_fail']+'\n'
            return preamble + step['ipxe_content'].removeprefix('#!ipxe').lstrip('\r\n') + '\n', None, True
        if stage == 'windows-boot':
            c=db.execute('SELECT token_hash FROM clients WHERE id=?',(row['client_id'],)).fetchone() if row['client_id'] else None
            if c and c['token_hash']:return '#!ipxe\necho Windows enrollment has started. Inspect the routine before restarting.\nexit\n',None,True
            return None, dict(row), True
        if stage == 'discovering' and not row['workflow_id']: pass
        elif stage == 'erase-running':
            script, _ = workflows.boot_decision(store_proxy(store, db), mac, address)
            return script, None, True
        else:
            hold_label = {'windows-running':'Windows completion pending', 'checkpoint':'Technician checkpoint',
                     'complete':'Routine complete', 'failed':'Routine failed', 'cancelled':'Routine cancelled'}.get(stage,stage)
            return '#!ipxe\necho re-pxe: '+hold_label+'. Review Jobs on the portal.\nexit\n', None, True
    return discovery_script(store, mac, address), None, True


def store_proxy(store, db):
    # Existing workflow boot routing uses the same serialized transaction.
    from contextlib import contextmanager
    class Proxy:
        root = store.root
        @contextmanager
        def db(self): yield db
    return Proxy()


def bind_boot(db, client_id, row):
    current = db.execute('SELECT * FROM routine_runs WHERE id=?', (row['id'],)).fetchone()
    if not current or current['stage'] != 'windows-boot': raise HTTPException(409, 'Routine is no longer ready for Windows')
    old = db.execute('SELECT c.* FROM routine_boots b JOIN clients c ON c.id=b.client_id WHERE b.run_id=? ORDER BY c.created DESC LIMIT 1', (row['id'],)).fetchone()
    if old and (old['token_hash'] or old['job_id']): raise HTTPException(409, 'Windows has already enrolled; cancel the run before a fresh attempt')
    if old: db.execute('UPDATE clients SET ticket_expires=? WHERE id=?', (now(), old['id']))
    db.execute('INSERT INTO routine_boots VALUES(?,?)', (client_id,row['id']))
    step = json.loads(row['spec'])['steps'][row['step']]
    db.execute('UPDATE clients SET profile_override=? WHERE id=?', (step['profile_id'],client_id))
    db.execute('UPDATE routine_runs SET client_id=?,updated=? WHERE id=?', (client_id,now(),row['id']))


def windows_choices(db, row, target, profile_artifacts):
    run = for_boot(db, row['id'])
    if not run: return None
    if run['stage'] not in ('windows-boot', 'windows-running'): raise HTTPException(409, 'Routine Windows step is locked')
    if run['target'] and target and run['target'] != target: raise HTTPException(403, 'Only the routine disk is allowed')
    step = json.loads(run['spec'])['steps'][run['step']]; p = step['profile']
    current = db.execute('SELECT enabled FROM deployment_profiles WHERE id=?', (step['profile_id'],)).fetchone()
    if not current or not current['enabled']: raise HTTPException(409, 'Routine configuration withdrawn')
    image, drivers = profile_artifacts(db, p)
    automatic = p['driver_source'] == 'automatic'
    if automatic:
        drivers = [json.loads(r['metadata']) for r in db.execute("SELECT metadata FROM artifacts WHERE kind='drivers' ORDER BY published DESC,id DESC")
            if driver_management.archive_available(db,json.loads(r['metadata'])['id'])]
        drivers = [d for d in drivers if (d.get('hardware_test_passed') or d.get('installation_authorized')) and d.get('architecture') == 'amd64' and d.get('windows_version') == str(image['selected_image']['build'])]
    match = next((d for d in drivers if (d['make'].casefold(), d['model'].casefold()) == (row['manufacturer'].casefold(), row['model'].casefold())),None)
    if drivers and not match and not automatic and not p.get('hardware_neutral'): raise HTTPException(409, 'Routine drivers do not match this PC')
    choice = {**p,'enabled':True,'requires_disk_confirmation':True,'driver_policy':'server-library-then-windows-update' if automatic else 'stop-if-missing',
              'finish':'oobe-and-shutdown','image_sha256':image['sha256'],'image_index':image['image_index'],
              'windows_build':str(image['selected_image']['build']),'driver_id':match['id'] if match else None}
    return {'schemaVersion':1,'choices':[choice],'selectedProfileId':p['id'],'requiresDiskConfirmation':True}


def enforce_enroll(db, client_id, targets, managed, client_version):
    run = for_boot(db, client_id)
    if not run: return None
    if run['stage'] != 'windows-boot' or not managed: raise HTTPException(409, 'Routine requires managed Windows media and an unstarted Windows step')
    step = json.loads(run['spec'])['steps'][run['step']]
    candidates = list(targets)
    erased=db.execute('SELECT w.* FROM routine_erase_disks d JOIN workflow_runs w ON w.id=d.workflow_id WHERE d.run_id=? AND d.step_index=(SELECT step_index FROM routine_erase_disks WHERE workflow_id=?) ORDER BY d.ordinal',(run['id'],run['workflow_id'])).fetchall()
    if not erased and run['workflow_id']:
        erased = [db.execute('SELECT * FROM workflow_runs WHERE id=?', (run['workflow_id'],)).fetchone()]
    if erased:
        for disk in erased:
            result = json.loads(disk['result']) if disk['result'] else {}
            if result.get('result') != 'passed' or not disk['certificate']:
                raise HTTPException(409, 'Every routine drive must finish erasure and certification first')
        candidates = [t for t in targets if any(t.identity == d['target'] and t.size_bytes == d['size'] for d in erased)]
        if len(candidates) != len(erased): raise HTTPException(409, 'Routine disk identity or capacity changed; collect native NVMe controller serials')
    if step['target_policy'] == 'named-disk': candidates = [t for t in candidates if t.identity == step['target_identity']]
    elif step['target_policy'] not in ('first-disk', 'manual') and run['target']:
        candidates = [t for t in candidates if t.identity == run['target'] and t.size_bytes == run['size']]
    if not candidates or step['target_policy'] not in ('first-disk', 'manual') and len(candidates) != 1:
        raise HTTPException(409, 'Routine needs one eligible disk or a named disk serial')
    if step['target_policy'] in ('first-disk', 'manual') and client_version < (0,12,11):
        raise HTTPException(409, 'Routine disk selection requires WinPE 0.12.11 or newer')
    target = None if step['target_policy'] == 'manual' else candidates[0]
    if target and target.identity!=targets[0].identity and client_version < (0,12,8): raise HTTPException(409,'This WinPE release selects its first disk; update the target-selection media')
    db.execute('UPDATE routine_runs SET target=?,size=? WHERE id=?', (target.identity if target else None,target.size_bytes if target else None,run['id']))
    return db.execute('SELECT * FROM routine_runs WHERE id=?',(run['id'],)).fetchone()


def select_windows_target(db, client_id, identity):
    run = for_boot(db, client_id)
    if not run: return
    if run['stage'] != 'windows-boot' or run['job_id']: raise HTTPException(409, 'Routine installation is locked')
    if run['target'] and run['target'] != identity: raise HTTPException(409, 'Another drive has already been selected')
    client = db.execute('SELECT targets FROM clients WHERE id=?', (client_id,)).fetchone()
    targets = json.loads(client['targets'])
    matches = [t for t in targets if t['identity'] == identity]
    if len(matches) != 1: raise HTTPException(409, 'Choose one detected eligible drive')
    erased=db.execute('SELECT w.target,w.size,w.result,w.certificate FROM routine_erase_disks d JOIN workflow_runs w ON w.id=d.workflow_id WHERE d.run_id=? AND d.step_index=(SELECT step_index FROM routine_erase_disks WHERE workflow_id=?)',(run['id'],run['workflow_id'])).fetchall()
    if erased and not any(d['target']==identity and d['size']==matches[0]['size_bytes'] and d['certificate'] and d['result'] and json.loads(d['result'])['result']=='passed' for d in erased):
        raise HTTPException(409, 'Selected drive has no completed routine erasure')
    db.execute('UPDATE routine_runs SET target=?,size=? WHERE id=?', (identity,matches[0]['size_bytes'],run['id']))


def started(db, client_id, job_id):
    run = for_boot(db, client_id)
    if not run: return
    if run['stage'] != 'windows-boot' or run['job_id']: raise HTTPException(409, 'Routine deployment already started')
    db.execute('UPDATE routine_runs SET job_id=? WHERE id=?', (job_id,run['id']))
    if run['workflow_id']:
        db.execute('UPDATE workflow_runs SET job_id=? WHERE id=?', (job_id,run['workflow_id']))
        workflows.event(db,run['workflow_id'],'installing','Routine Windows deployment started on the erased disk')
    event(db,run,'windows-running','Windows deployment started')


def can_confirm_stopped(db, row):
    if row['stage'] in TERMINAL or not row['workflow_id']: return False
    children=db.execute('SELECT w.* FROM routine_erase_disks d JOIN workflow_runs w ON w.id=d.workflow_id WHERE d.run_id=?',(row['id'],)).fetchall()
    if not children: children=[db.execute('SELECT * FROM workflow_runs WHERE id=?',(row['workflow_id'],)).fetchone()]
    active=[w for w in children if w['stage'] in ('erasing','operation-pending')]
    if not active or any(w['stage']=='operation-pending' or json.loads(w['spec'])['method'] not in {*erase_policy.NWIPE,'nist_clear'} for w in active): return False
    for w in active:
        progress=db.execute('SELECT updated FROM workflow_progress WHERE run_id=?',(w['id'],)).fetchone()
        at=progress['updated'] if progress else w['updated']
        if (datetime.now(timezone.utc)-datetime.fromisoformat(at)).total_seconds()<60: return False
    return True


def remove_run(db,row,body):
    if db.execute('SELECT 1 FROM routine_removed WHERE run_id=?',(row['id'],)).fetchone(): return
    if row['stage'] in TERMINAL and row['workflow_id'] and db.execute('SELECT stage FROM workflow_runs WHERE id=?',(row['workflow_id'],)).fetchone()['stage'] in ('erasing','operation-pending'):raise HTTPException(409,'An erasure or uncertain controller operation remains active')
    if row['stage'] in TERMINAL and any(w['stage'] in ('erasing','operation-pending') for w in db.execute('SELECT w.stage FROM routine_erase_disks d JOIN workflow_runs w ON w.id=d.workflow_id WHERE d.run_id=?',(row['id'],))): raise HTTPException(409,'An erasure or uncertain controller operation remains active')
    if row['stage'] not in TERMINAL:
        if not body.stopped_locally or not can_confirm_stopped(db,row): raise HTTPException(409,'Stop the software wipe on the PC first. Fresh or uncertain hardware operations cannot be cleared.')
        progress=db.execute('SELECT updated FROM workflow_progress WHERE run_id=?',(row['workflow_id'],)).fetchone()
        observed=progress['updated'] if progress else None
        if body.expected_progress_updated != observed: raise HTTPException(409,'Erasure activity changed; refresh and inspect the PC again')
        children=db.execute('SELECT workflow_id FROM routine_erase_disks WHERE run_id=?',(row['id'],)).fetchall()
        for uid in [r['workflow_id'] for r in children] or [row['workflow_id']]:
            w=db.execute('SELECT stage FROM workflow_runs WHERE id=?',(uid,)).fetchone()
            if w['stage']=='erasing':
                workflows.event(db,uid,'failed','Technician confirmed software wipe stopped locally; erasure is incomplete and unverified')
                db.execute('UPDATE workflow_runs SET token_hash=NULL,ticket_hash=NULL WHERE id=?',(uid,))
        cancel(db,row)
    # Preserve run, inventory, certificates, events and report URLs. Clearing the
    # portal is distinct from deleting evidence or authorizing another wipe.
    db.execute('INSERT INTO routine_removed VALUES(?,?)',(row['id'],now()))
    db.execute('INSERT INTO routine_events(run_id,step,stage,detail,created) VALUES(?,?,?,?,?)',(row['id'],row['step'],'removed','Operator removed job from portal; reports retained',now()))
    if row['client_id']:
        db.execute('UPDATE clients SET deleted_at=?,token_hash=NULL,expires=?,ticket_expires=? WHERE id=?',(now(),now(),now(),row['client_id']))


def cancel(db, row, revoke_session=True):
    if row['stage'] in ('complete','cancelled'): raise HTTPException(409,'Routine has already ended')
    children = db.execute('SELECT w.* FROM routine_erase_disks d JOIN workflow_runs w ON w.id=d.workflow_id WHERE d.run_id=?',(row['id'],)).fetchall()
    if not children and row['workflow_id']: children=[db.execute('SELECT * FROM workflow_runs WHERE id=?',(row['workflow_id'],)).fetchone()]
    if any(w['stage'] in ('erasing','operation-pending') for w in children): raise HTTPException(409,'Erasure is running or its controller outcome is unknown')
    for w in children:
        if w['stage'] in ('erasing', 'operation-pending'): raise HTTPException(409,'An erasure is running or its controller outcome is unknown; inspect the PC locally before clearing its run')
        if w['stage'] not in TERMINAL:
            workflows.event(db,w['id'],'cancelled','Routine cancelled')
            db.execute('UPDATE workflow_runs SET token_hash=NULL,ticket_hash=NULL WHERE id=?',(w['id'],))
    if row['job_id'] and revoke_session:
        j = db.execute('SELECT stage FROM jobs WHERE id=?',(row['job_id'],)).fetchone()
        if j and j['stage'] not in TERMINAL:
            db.execute("UPDATE jobs SET stage='cancelled',updated=? WHERE id=?",(now(),row['job_id']))
            db.execute('DELETE FROM report_credentials WHERE job_id=?',(row['job_id'],))
            db.execute("INSERT INTO events(job_id,stage,detail,timestamp) VALUES(?,'cancelled','Routine cancelled',?)",(row['job_id'],now()))
    if row['client_id'] and revoke_session:
        db.execute("UPDATE clients SET token_hash=NULL,expires=?,ticket_expires=?,stage='cancelled',countdown_until=NULL WHERE id=?",(now(),now(),row['client_id']))
    event(db,row,'cancelled','Operator cancelled routine; evidence retained')


def label(value): return re.sub(r'[^A-Za-z0-9 .:/()_-]', ' ',value)[:120]


def menu_spec(store):
    text = file_workspace.text_file(store,'configuration','routines-menu.json')
    try: value = Menu.model_validate_json(text['content'])
    except ValueError: raise HTTPException(503,'Invalid routines-menu.json; repair it in the text editor') from None
    return value, text['revision']


def menu_script(store, mac, address, alternatives=False, idle=False, preview=False):
    menu,_ = menu_spec(store); at = now(); ticket = secrets.token_urlsafe(48)
    with store.db() as db:
        chosen = default_for(db, mac)
        if not preview:
            db.execute('DELETE FROM routine_tickets WHERE expires<?',(at,))
            if db.execute('SELECT count(*) FROM routine_tickets').fetchone()[0] >= 1024: raise HTTPException(429,'Too many pending routine menus')
            db.execute('INSERT INTO routine_tickets(hash,mac,expires,run_id,default_id,default_version) VALUES(?,?,?,NULL,?,?)',(digest(ticket),mac,(datetime.now(timezone.utc)+timedelta(minutes=15)).isoformat(),chosen['routine_id'] if chosen else None,chosen['version'] if chosen else None))
        else:ticket='preview-only-not-a-boot-credential'
        records = db.execute('''SELECT v.* FROM routines r JOIN routine_versions v ON v.routine_id=r.id AND v.version=r.published_version ORDER BY r.created,r.id LIMIT 100''').fetchall()
    base = f'http://{address}:8089/pxe/routines'
    from .boot_appearance import commands, saved_appearance
    from .boot_background import read as read_background
    custom_background = read_background(store) is not None
    lines = ['#!ipxe', *commands(menu, address,custom_background=custom_background), *saved_appearance(store), 'menu '+menu.title]
    if menu.show_banner and not custom_background:
        lines.extend(['item --gap   [ re-pxe ]   PROVISION / ERASE / DELIVER', 'item --gap ========================================'])
    choices = []
    if alternatives:
        for i,r in enumerate(records):
            key='routine'+str(i); lines.append('item '+key+' '+label(json.loads(r['spec'])['name']))
            choices.append((key, f'{base}/start.ipxe?ticket={ticket}&routine={r["routine_id"]}&version={r["version"]}'))
        lines.append('item back Back')
        lines.append('choose --retimeout 0 re-pxe-choice || goto back')
    else:
        if chosen:
            lines.append('item assigned '+menu.start_label+' - '+label(json.loads(chosen['spec'])['name']))
            choices.append(('assigned',f'{base}/start.ipxe?ticket={ticket}&assigned=1'))
        else: lines.append('item --gap No assigned or default routine')
        lines.append('item other '+menu.other_label)
        choices.append(('other', f'{base}/menu.ipxe?mac={mac}&alternatives=1'))
        lines.append('item refresh Refresh menu')
        if menu.next_boot: lines.append('item next Next boot option')
        if menu.shutdown: lines.append('item shutdown Shut down')
        if menu.firmware: lines.append('item firmware Firmware setup (UEFI)')
        delay = json.loads(chosen['spec'])['steps'][0].get('start_delay_seconds',5) if chosen else 0
        if chosen and not idle:
            lines.append('item --gap '+('Automatic start in '+str(delay)+' seconds. Any key pauses.' if delay else 'Automatic start disabled. Select Start assigned.'))
        flags = '--timeout '+str(delay*1000)+' --default assigned ' if chosen and not idle and delay else ''
        lines.append('choose '+flags+'--retimeout 0 re-pxe-choice || goto idle')
    lines.append('goto ${re-pxe-choice}')
    for key,url in choices:
        lines.extend([':'+key, 'chain --replace --autofree '+url+' || goto idle'])
    lines.extend([':refresh',f'chain --replace --autofree {base}/menu.ipxe?mac={mac} || goto fail',
                  ':back',f'chain --replace --autofree {base}/menu.ipxe?mac={mac}&idle=1 || goto fail',
                  ':next','exit', ':shutdown','poweroff || goto unavailable',':firmware','reboot --setup || goto unavailable',
                  ':unavailable','echo This command is unavailable on this firmware.','prompt Press any key to return',
                  ':idle',f'chain --replace --autofree {base}/menu.ipxe?mac={mac}&idle=1 || goto fail',
                  ':fail','echo Cannot contact re-pxe. No routine was started.','prompt Press any key to exit','exit'])
    return '\n'.join(lines)+'\n'


def install(app, store, Role, allow, profile_artifacts):
    @app.get('/api/routines')
    def listing(role: Role):
        allow(role)
        with store().db() as db:
            sync(db)
            rows = [{'id':r['id'],'generation':r['generation'],'published_version':r['published_version'],'draft':json.loads(r['draft']),'published_name':json.loads(db.execute('SELECT spec FROM routine_versions WHERE routine_id=? AND version=?',(r['id'],r['published_version'])).fetchone()[0])['name'] if r['published_version'] else None} for r in db.execute('SELECT * FROM routines ORDER BY created')]
            settings = dict(db.execute('SELECT * FROM routine_settings WHERE id=1').fetchone());settings.pop('id');settings['enabled']=bool(settings['enabled'])
            assignments=[dict(r) for r in db.execute('SELECT * FROM routine_assignments ORDER BY updated DESC')]
            runs=[projection(db,r) for r in db.execute('SELECT * FROM routine_runs WHERE id NOT IN (SELECT run_id FROM routine_removed) ORDER BY created DESC LIMIT 200')]
            from .device_inventory import serial_key
            runs.sort(key=lambda r:(not bool(serial_key(r['system_serial'])),serial_key(r['system_serial'])))
        return {'routines':rows,'settings':settings,'assignments':assignments,'runs':runs,'timeout_seconds':5,
                'erase_methods': erase_policy.catalogue(), 'prngs': erase_policy.PRNG,
                'sanitize_notice': 'NIST Clear verifies a full zero overwrite. NIST Purge uses native nwipe sanitize; its optional verified PRNG follow-up is enabled by default. Confirmed unsupported capabilities before any write can use the Failure route; failed or uncertain operations stop for inspection. Presets record the actual technique and achieved outcome; device suitability and organizational validation remain required.',
                'sanitize_source': 'https://github.com/martijnvanbrummelen/nwipe/releases/tag/v0.43'}

    @app.post('/api/routines',status_code=201)
    def create(spec: Routine, role: Role):
        allow(role); uid=str(uuid.uuid4());at=now()
        with store().db() as db: db.execute('INSERT INTO routines VALUES(?,?,1,NULL,?,?)',(uid,spec.model_dump_json(),at,at))
        return {'id':uid,'generation':1}

    @app.put('/api/routines/{uid}')
    def save(uid: str, body: Save, role: Role):
        allow(role)
        with store().db() as db:
            if not db.execute('UPDATE routines SET draft=?,generation=generation+1,updated=? WHERE id=? AND generation=?',
                (body.spec.model_dump_json(),now(),uid,body.expected_generation)).rowcount: raise HTTPException(409,'Routine changed; reload before saving')
        return {'id':uid,'generation':body.expected_generation+1}

    @app.post('/api/routines/{uid}/publish')
    def publish(uid: str, body: Publish, role: Role):
        allow(role)
        with store().db() as db:
            row=db.execute('SELECT * FROM routines WHERE id=?',(uid,)).fetchone()
            if not row or row['generation']!=body.expected_generation: raise HTTPException(409,'Routine changed; reload before publishing')
            spec=json.loads(row['draft'])
            spec['erase_protocol'] = 2
            for s in spec['steps']:
                if s['kind']=='windows': s['profile']=profile_snapshot(db,s['profile_id'])
                if s['kind']=='ipxe':
                    if not s['ipxe_path'].endswith('.ipxe'): raise HTTPException(409, 'Choose an .ipxe file in Custom boot')
                    file = file_workspace.text_file(store(), 'custom-boot', s['ipxe_path'])
                    if not file['content'].startswith('#!ipxe'): raise HTTPException(409, 'Custom script must start with #!ipxe')
                    s['ipxe_content'], s['ipxe_sha256'] = file['content'], file['revision']
            validate_runtime(store(),db,spec,profile_artifacts)
            version=(db.execute('SELECT MAX(version) FROM routine_versions WHERE routine_id=?',(uid,)).fetchone()[0] or 0)+1
            db.execute('INSERT INTO routine_versions VALUES(?,?,?,?)',(uid,version,json.dumps(spec),now()))
            db.execute('UPDATE routines SET published_version=? WHERE id=?',(version,uid))
        return {'id':uid,'published_version':version}

    @app.post('/api/routines/migrate')
    def migrate(role: Role):
        allow(role)
        with store().db() as db:
            if db.execute('SELECT count(*) FROM routines').fetchone()[0]: raise HTTPException(409,'Routines already exist; configure them individually')
            settings=json.loads(db.execute('SELECT spec FROM pxe_settings WHERE id=1').fetchone()[0]);default=None;ids=[]
            for p in db.execute('SELECT * FROM deployment_profiles WHERE enabled=1 ORDER BY created DESC').fetchall():
                spec=json.loads(p['spec'])
                if spec['selection_mode']!='choose-version':continue
                uid=str(uuid.uuid4());at=now()
                draft=Routine(name=spec['name'],steps=[Step(id='boot',kind='boot'),Step(id='windows',kind='windows',profile_id=p['id']),Step(id='finish',kind='finish')]).model_dump()
                frozen=json.loads(json.dumps(draft));frozen['steps'][1]['profile']=profile_snapshot(db,p['id'])
                validate_runtime(store(),db,frozen,profile_artifacts)
                db.execute('INSERT INTO routines VALUES(?,?,1,1,?,?)',(uid,json.dumps(draft),at,at))
                db.execute('INSERT INTO routine_versions VALUES(?,1,?,?)',(uid,json.dumps(frozen),at));ids.append(uid)
                if p['id']==settings['default_profile_id']:default=uid
            if default is None and len(ids)==1:default=ids[0]
            db.execute('UPDATE routine_settings SET enabled=1,default_id=? WHERE id=1',(default,))
        return {'created':ids,'default_id':default}

    @app.put('/api/routine-settings')
    def update_settings(body: Settings, role: Role):
        allow(role)
        with store().db() as db:
            if body.default_id:published(db,body.default_id)
            db.execute('UPDATE routine_settings SET enabled=?,default_id=? WHERE id=1',(int(body.enabled),body.default_id))
        return body.model_dump()

    @app.put('/api/routine-assignments')
    def assign(body: Assignment, role: Role):
        allow(role)
        with store().db() as db:
            if body.routine_id:
                published(db,body.routine_id)
                db.execute('INSERT INTO routine_assignments VALUES(?,?,?) ON CONFLICT(mac) DO UPDATE SET routine_id=excluded.routine_id,updated=excluded.updated',(body.mac.lower(),body.routine_id,now()))
            else:db.execute('DELETE FROM routine_assignments WHERE mac=?',(body.mac.lower(),))
        return {'accepted':True}

    @app.get('/api/routine-jobs')
    def routine_jobs(role: Role,view: Literal['active','attention','all','complete','failed','cancelled']='active',
                     search: str=Query(default='',max_length=200),page: int=Query(default=1,ge=1),
                     page_size: int=Query(default=50,ge=1,le=200)):
        allow(role)
        from . import job_reports
        with store().db() as db:
            status={'active':'pending'}.get(view,view)
            data=job_reports.list_reports(db,search,status,page=page,page_size=page_size,source='routine',include_removed=False)
            runs=[projection(db,db.execute('SELECT * FROM routine_runs WHERE id=?',(r['run_id'],)).fetchone()) for r in data['reports']]
            for run,item in zip(runs,data['reports']):run['status_label']=job_reports.display(item['stage'])
            return {k:v for k,v in data.items() if k!='reports'}|{'runs':runs}

    @app.get('/api/routine-runs/{uid}')
    def details(uid: str, role: Role):
        allow(role)
        with store().db() as db:
            sync(db);row=db.execute('SELECT * FROM routine_runs WHERE id=?',(uid,)).fetchone()
            if not row:raise HTTPException(404,'Run not found')
            return projection(db,row,True)

    @app.post('/api/routine-runs/{uid}/action')
    def action(uid: str, body: RunAction, role: Role):
        allow(role)
        with store().db() as db:
            sync(db);row=db.execute('SELECT * FROM routine_runs WHERE id=?',(uid,)).fetchone()
            if not row or row['step']!=body.expected_step:raise HTTPException(409,'Run changed; refresh before continuing')
            if body.action=='remove':remove_run(db,row,body)
            elif body.action=='cancel':cancel(db,row)
            elif row['stage'] in ('checkpoint', 'custom-boot'):advance(db,row)
            else:raise HTTPException(409,'The run is not at a technician checkpoint')
        return {'accepted':True}

    @app.get('/api/routine-menu')
    def read_menu(role: Role,mac: str=Query(default='52:54:00:00:00:01',pattern=MAC_PATTERN)):
        allow(role);spec,revision=menu_spec(store());address=server_address()
        with store().db() as db: chosen=default_for(db,mac.lower())
        delay=json.loads(chosen['spec'])['steps'][0].get('start_delay_seconds',5) if chosen else 0
        return {'spec':spec.model_dump(),'revision':revision,'timeout_seconds':delay,'preview':menu_script(store(),mac.lower(),address,preview=True)}

    @app.get('/pxe/routines/background.png')
    def boot_background():
        from .boot_appearance import background
        from fastapi import Response
        menu,_=menu_spec(store())
        return Response(background(store(),menu),media_type='image/png',headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})

    @app.put('/api/routine-menu')
    def save_menu(body: MenuSave, request: Request, role: Role):
        allow(role)
        result=file_workspace.save_text(store(),file_workspace.TextSave(root='configuration',path='routines-menu.json',content=body.spec.model_dump_json(indent=2)+'\n',expected_revision=body.expected_revision),getattr(request.state,'web_user',{}).get('username','admin API'))
        return result

    @app.get('/pxe/routines/menu.ipxe')
    def boot_menu(mac: str=Query(pattern=MAC_PATTERN),alternatives: bool=False,idle: bool=False):
        mac=mac.lower()
        with store().db() as db:
            if not enabled(db):raise HTTPException(409,'Routine boot is disabled')
            sync(db);row=latest(db,mac)
            if client_management.blocking_job(db,mac) or row and row['stage'] not in TERMINAL:
                return script_response(f'#!ipxe\nchain --replace --autofree http://{server_address()}:8089/pxe/managed.ipxe?mac={mac}\n')
        return script_response(menu_script(store(),mac,server_address(),alternatives,idle))

    @app.get('/pxe/routines/start.ipxe')
    def start(ticket: str=Query(pattern=r'^[A-Za-z0-9_-]{32,128}$'),assigned: bool=False,
              routine: str | None=Query(default=None,pattern=UUID_PATTERN),version: int | None=Query(default=None,ge=1)):
        with store().db() as db:
            if not enabled(db):raise HTTPException(409,'Routine boot is disabled')
            t=db.execute('SELECT * FROM routine_tickets WHERE hash=?',(digest(ticket),)).fetchone()
            if not t or t['expires']<=now():raise HTTPException(401,'Routine menu expired; refresh the menu')
            sync(db);row=latest(db,t['mac'])
            if t['run_id']:
                if not row or row['id']!=t['run_id'] or row['stage'] in TERMINAL:raise HTTPException(409,'This menu choice has already been used')
            else:
                if row and row['stage'] not in TERMINAL:raise HTTPException(409,'A routine is already active')
                if db.execute("SELECT 1 FROM clients c JOIN jobs j ON j.id=c.job_id WHERE c.mac=? AND j.stage NOT IN ('complete','failed','cancelled')",(t['mac'],)).fetchone():raise HTTPException(409,'A Windows job is already active')
                if db.execute("SELECT 1 FROM workflow_runs WHERE mac=? AND stage NOT IN ('complete','failed','cancelled','unsupported')",(t['mac'],)).fetchone():raise HTTPException(409,'An erasure run already owns this PC')
                if assigned:
                    selected=default_for(db,t['mac'])
                    if not selected:raise HTTPException(409,'No routine is assigned; refresh the menu')
                    if (selected['routine_id'],selected['version'])!=(t['default_id'],t['default_version']):raise HTTPException(409,'Assigned routine changed; refresh the menu')
                elif routine and version:
                    selected=published(db,routine)
                    if selected['version']!=version:raise HTTPException(409,'Routine changed; refresh the menu')
                else:raise HTTPException(422,'Select assigned or a published routine')
                spec=json.loads(selected['spec']);validate_runtime(store(),db,spec,profile_artifacts)
                uid=str(uuid.uuid4());at=now()
                db.execute('''INSERT INTO routine_runs(id,routine_id,version,spec,mac,step,stage,created,updated)
                              VALUES(?,?,?,?,?,0,'queued',?,?)''',(uid,selected['routine_id'],selected['version'],selected['spec'],t['mac'],at,at))
                row=latest(db,t['mac']);advance(db,row)
                db.execute('UPDATE routine_tickets SET run_id=? WHERE hash=?',(uid,digest(ticket)))
                db.execute('UPDATE clients SET ticket_expires=?,expires=? WHERE mac=? AND job_id IS NULL',(at,at,t['mac']))
            mac=t['mac']
        return script_response(f'#!ipxe\nchain --replace --autofree http://{server_address()}:8089/pxe/managed.ipxe?mac={mac}\n')

    @app.get('/pxe/routines/continue.ipxe')
    def continue_step(ticket: str=Query(pattern=r'^[A-Za-z0-9_-]{32,128}$'), outcome: Literal['success', 'failure']='success'):
        with store().db() as db:
            t = db.execute('SELECT * FROM routine_step_tickets WHERE hash=?', (digest(ticket),)).fetchone()
            if not t or t['expires'] <= now(): raise HTTPException(401, 'Routine step ticket expired or consumed')
            row = db.execute('SELECT * FROM routine_runs WHERE id=?', (t['run_id'],)).fetchone()
            if not row or row['stage'] != 'custom-boot' or row['step'] != t['step']: raise HTTPException(409, 'Routine step has changed')
            db.execute('DELETE FROM routine_step_tickets WHERE hash=?', (digest(ticket),))
            advance(db, row, outcome, 'Custom iPXE step reported failure' if outcome == 'failure' else '')
            mac = row['mac']
        return script_response(f'#!ipxe\nchain --replace --autofree http://{server_address()}:8089/pxe/managed.ipxe?mac={mac}\n')


def server_address():
    import os
    address=os.environ.get('REPXE_ADDRESS','127.0.0.1')
    if not re.fullmatch(r'[A-Za-z0-9.-]+',address):raise HTTPException(503,'Invalid server address')
    return address


def script_response(script): return PlainTextResponse(script,headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})
