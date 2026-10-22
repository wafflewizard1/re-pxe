"""Explicit erasure runs, retained evidence, and one-use PXE handoffs.

Nothing in this module opens host disks or executes erasure commands.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import secrets
import stat
import uuid
from typing import Annotated, Literal

from fastapi import Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from . import erase_policy

DDL = '''
CREATE TABLE IF NOT EXISTS workflow_templates(
 id TEXT PRIMARY KEY,spec TEXT NOT NULL,created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS workflow_runs(
 id TEXT PRIMARY KEY,template_id TEXT NOT NULL,spec TEXT NOT NULL,mac TEXT NOT NULL,
 target TEXT NOT NULL,size INTEGER NOT NULL,asset_tag TEXT NOT NULL,stage TEXT NOT NULL,
 created TEXT NOT NULL,updated TEXT NOT NULL,inventory TEXT,result TEXT,
 ticket_hash TEXT,ticket_expires TEXT,token_hash TEXT,expires TEXT,
 certificate BLOB,certificate_sha256 TEXT,report_hash TEXT,job_id TEXT,error TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS workflow_progress(run_id TEXT PRIMARY KEY,percent INTEGER,detail TEXT NOT NULL,updated TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS workflow_phase_certificates(run_id TEXT NOT NULL,phase TEXT NOT NULL,payload BLOB NOT NULL,sha256 TEXT NOT NULL,PRIMARY KEY(run_id,phase));
CREATE INDEX IF NOT EXISTS workflow_mac ON workflow_runs(mac,created);
CREATE TABLE IF NOT EXISTS workflow_events(
 seq INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT NOT NULL,stage TEXT NOT NULL,
 detail TEXT NOT NULL,created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS workflow_boots(client_id TEXT PRIMARY KEY,run_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS workflow_intake_settings(id INTEGER PRIMARY KEY CHECK(id=1),enabled INTEGER NOT NULL);
INSERT OR IGNORE INTO workflow_intake_settings VALUES(1,0);
CREATE TABLE IF NOT EXISTS workflow_intake(
 mac TEXT PRIMARY KEY,inventory TEXT,ticket_hash TEXT,ticket_expires TEXT,
 token_hash TEXT,expires TEXT,seen TEXT NOT NULL);
'''
CERT_LIMIT = 8 * 1024**2
MEDIA_MARKER = 're-pxe-shredos-11'


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def initialize(store):
    with store.db() as db:
        db.executescript(DDL)


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')


class WipeControls(Input):
    verification: Literal['all', 'last', 'off'] = 'all'
    rounds: int = Field(default=1, ge=1, le=16, strict=True)
    final_blank: bool = Field(default=True, strict=True)
    prng: Literal[*erase_policy.PRNG] = 'auto'
    io_mode: Literal['auto', 'direct', 'cached'] = 'auto'
    direction: Literal['forward', 'reverse', 'scatter'] = 'forward'
    sync_blocks: int | None = Field(default=None, ge=0, le=2147483647, strict=True)
    retry_io_errors: bool = Field(default=True, strict=True)
    abort_on_block_errors: bool = Field(default=True, strict=True)
    # Neutral legacy fields keep stored draft/fingerprint compatibility only.
    unfreeze: Literal[False] = False
    crypto_approved: bool = Field(default=False, strict=True)
    readback: Literal['none'] = 'none'
    readback_pattern: Literal['readable'] = 'readable'
    overwrite_pattern: Literal[0] = 0
    invert_pattern: Literal[False] = False


class EraseOptions(WipeControls):
    method: erase_policy.Method = 'zero'
    fallback_method: erase_policy.Method | None = None
    fallback_options: WipeControls | None = None
    execution_method: Literal[*erase_policy.NWIPE, *erase_policy.NATIVE] | None = None
    crypto_assurance: str = Field(default='', max_length=1000)
    prng_followup: bool = Field(default=True,strict=True)

    @model_validator(mode='after')
    def no_implicit_fallback(self):
        if self.fallback_method or self.fallback_options:
            raise ValueError('Use the routine Failure connection instead of a fallback method')
        return self


class Template(EraseOptions):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default='', max_length=1000)
    requested_method: erase_policy.Method | None = None
    allow_usb: bool = Field(default=False, strict=True)
    after_erasure: Literal['stop', 'approval', 'automatic'] = 'stop'
    profile_id: str | None = Field(default=None, pattern=r'^[a-f0-9-]{36}$')

    @model_validator(mode='after')
    def windows_choice(self):
        if (self.after_erasure != 'stop') != bool(self.profile_id):
            raise ValueError('Choose a Windows configuration when installation follows erasure')
        normalized = erase_policy.settings(self.model_dump())
        for key in erase_policy.authorized_keys(normalized):
            if key in normalized: setattr(self, key, normalized[key])
        return self


class NewRun(Input):
    template_id: str = Field(pattern=r'^[a-f0-9-]{36}$')
    mac: str = Field(pattern=r'^[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}$')
    target_identity: str = Field(pattern=r'^serial:[^\x00-\x1f]{1,293}$')
    size_bytes: int = Field(gt=0, le=1024**5, strict=True)
    asset_tag: str = Field(default='', max_length=120)
    confirmation: str = Field(max_length=300)


class DriveCapabilities(Input):
    methods: list[Annotated[str, Field(max_length=64, pattern=r'^[a-z0-9_]+$')]] = Field(default_factory=list, max_length=24)
    frozen: bool = False
    security_enabled: bool = False
    security_locked: bool = False
    scope: Literal['disk', 'controller', 'unknown'] = 'unknown'
    detail: str = Field(default='', max_length=500)
    engine_version: str = Field(default='', max_length=80)
    sanitize_status: Literal['ready','unsupported','uncertain'] = 'uncertain'
    controller_id: str = Field(default='', max_length=300)
    scope_targets: list[dict] = Field(default_factory=list, max_length=32)

    @field_validator('scope_targets')
    @classmethod
    def bounded_targets(cls, targets):
        for target in targets:
            if set(target)!={'identity','size_bytes'} or not isinstance(target['identity'],str) or not re.fullmatch(r'serial:[^\x00-\x1f]{1,293}',target['identity']) or type(target['size_bytes']) is not int or not 0<target['size_bytes']<=1024**5:
                raise ValueError('Native scope requires exact disk serial and capacity')
        return targets


from .device_inventory import DriveHealth


class Disk(Input):
    identity: str = Field(pattern=r'^serial:[^\x00-\x1f]{1,293}$')
    size_bytes: int = Field(gt=0, le=1024**5, strict=True)
    model: str = Field(default='', max_length=200)
    transport: str = Field(default='', max_length=30)
    rotational: bool = Field(default=False, strict=True)
    capabilities: DriveCapabilities = Field(default_factory=DriveCapabilities)
    health: DriveHealth | None = None


class Inventory(Input):
    manufacturer: str = Field(default='', max_length=120)
    model: str = Field(default='', max_length=120)
    system_serial: str = Field(min_length=1, max_length=200)
    cpu: str = Field(default='', max_length=300)
    memory_bytes: int = Field(default=0, ge=0, le=1024**5, strict=True)
    firmware: str = Field(default='', max_length=200)
    disks: list[Disk] = Field(min_length=1, max_length=32)

    @field_validator('disks')
    @classmethod
    def unique_disks(cls, value):
        if len({d.identity for d in value}) != len(value):
            raise ValueError('Disk serials must be unique')
        return value


class Claim(Input):
    mac: str = Field(pattern=r'^[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}$')
    agent_version: Literal['re-pxe-shredos-1', 're-pxe-shredos-2', 're-pxe-shredos-3', 're-pxe-shredos-4', 're-pxe-shredos-5', 're-pxe-shredos-6', 're-pxe-shredos-7', 're-pxe-shredos-8', 're-pxe-shredos-9', 're-pxe-shredos-10']
    inventory: Inventory


class Result(Input):
    target_identity: str = Field(pattern=r'^serial:[^\x00-\x1f]{1,293}$')
    size_bytes: int = Field(gt=0, strict=True)
    method: erase_policy.Method
    verification: Literal['all', 'last', 'off']
    rounds: int = Field(ge=1, le=16, strict=True)
    final_blank: bool = Field(strict=True)
    result: Literal['passed', 'failed']
    exit_code: int = Field(ge=0, le=255, strict=True)
    errors: int = Field(ge=0, strict=True)
    started: datetime
    finished: datetime
    certificate_sha256: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')
    log: str = Field(default='', max_length=64000)
    run_id: str | None = Field(default=None, pattern=r'^[a-f0-9-]{36}$')
    system_serial: str = Field(default='', max_length=200)
    settings_sha256: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')
    evidence: dict = Field(default_factory=dict)

    @field_validator('evidence')
    @classmethod
    def bounded_evidence(cls, value):
        if len(json.dumps(value)) > 32000: raise ValueError('Evidence exceeds 32 KB')
        return value

    @model_validator(mode='after')
    def times(self):
        if not self.started.tzinfo or not self.finished.tzinfo or self.finished < self.started:
            raise ValueError('Timezone-aware, ordered timestamps required')
        if self.result == 'passed' and (self.exit_code or self.errors or not self.certificate_sha256):
            raise ValueError('Success requires zero errors, exit code zero and a certificate hash')
        return self


class Progress(Input):
    percent: int | None = Field(default=None, ge=0, le=100, strict=True)
    detail: str = Field(default='', max_length=500)


class Failure(Input):
    error: str = Field(min_length=1, max_length=1000)


class UnsupportedInput(Failure):
    evidence: dict

    @field_validator('evidence')
    @classmethod
    def bounded(cls,value):
        if len(json.dumps(value))>32000:raise ValueError('Evidence exceeds 32 KB')
        return value


class IntakeSetting(Input):
    enabled: bool = Field(strict=True)


def intake_enabled(store):
    with store.db() as db:
        return bool(db.execute('SELECT enabled FROM workflow_intake_settings WHERE id=1').fetchone()['enabled'])


class Action(Input):
    action: Literal['approve-install', 'cancel']


def event(db, uid, stage, detail=''):
    at = now()
    db.execute('UPDATE workflow_runs SET stage=?,updated=?,error=? WHERE id=?',
               (stage, at, detail if stage in ('failed', 'cancelled', 'operation-pending') else '', uid))
    db.execute('INSERT INTO workflow_events(run_id,stage,detail,created) VALUES(?,?,?,?)',
               (uid, stage, detail, at))


def check_profile(db, spec):
    if not spec['profile_id']:
        return
    row = db.execute('SELECT * FROM deployment_profiles WHERE id=?', (spec['profile_id'],)).fetchone()
    if not row or not row['enabled'] or json.loads(row['spec'])['selection_mode'] != 'choose-version':
        raise HTTPException(409, 'Choose an enabled Windows configuration with dashboard selection')


def media(store):
    path = store.root / 'boot/pxe/shredos/current.json'
    if not path.is_file() or path.is_symlink():
        raise HTTPException(503, 're-pxe ShredOS media has not been published and validated')
    try:
        value = json.loads(path.read_text())
        if value['agent_version'] not in ('re-pxe-shredos-1', 're-pxe-shredos-2', 're-pxe-shredos-3', 're-pxe-shredos-4', 're-pxe-shredos-5', 're-pxe-shredos-6', 're-pxe-shredos-7', 're-pxe-shredos-8', 're-pxe-shredos-9', 're-pxe-shredos-10', MEDIA_MARKER) or value['validated'] is not True:
            raise ValueError()
        if not re.fullmatch(r'[a-f0-9]{64}', value['release']):
            raise ValueError()
        base = path.parent / value['release']
        for name in ('shredos', 'companion.cpio.gz'):
            f = base / name
            if f.is_symlink() or not stat.S_ISREG(f.stat().st_mode) or f.stat().st_size != value['files'][name]['size']:
                raise ValueError()
        return value
    except (ValueError, KeyError, OSError, TypeError):
        raise HTTPException(503, 'ShredOS media manifest or files are invalid') from None


def sync_jobs(db):
    for row in db.execute("SELECT r.id,j.stage FROM workflow_runs r JOIN jobs j ON j.id=r.job_id WHERE r.stage='installing' AND j.stage IN ('complete','failed','cancelled')").fetchall():
        event(db, row['id'], row['stage'], 'Windows deployment ended: ' + row['stage'])


def progress_snapshot(row):
    """Compatibility with existing companions: ETA is measured by nwipe."""
    if not row: return None
    value = dict(row)
    match = re.search(r'ETA (\d+):(\d{2}):(\d{2})', value['detail'])
    value['remaining_seconds'] = sum(int(n)*factor for n,factor in zip(match.groups(),(3600,60,1))) if match else None
    value['stale'] = (datetime.now(timezone.utc)-datetime.fromisoformat(value['updated'])).total_seconds() > 30
    return value


def require_native_media(value):
    if value['agent_version'] not in ('re-pxe-shredos-9', 're-pxe-shredos-10', MEDIA_MARKER) or value.get('nwipe_version')!=erase_policy.NATIVE_VERSION:
        raise HTTPException(409,'Native sanitize requires the validated nwipe 0.43 re-pxe ShredOS release')


def projection(row, db, detail=False):
    value = {k: row[k] for k in ('id', 'template_id', 'mac', 'target', 'size', 'asset_tag',
                                 'stage', 'created', 'updated', 'job_id', 'error', 'certificate_sha256')}
    value['progress'] = progress_snapshot(db.execute('SELECT percent,detail,updated FROM workflow_progress WHERE run_id=?',(row['id'],)).fetchone())
    value['workflow'] = json.loads(row['spec'])
    value['inventory'] = json.loads(row['inventory']) if row['inventory'] else None
    value['certificate_url'] = f"/api/workflow-runs/{row['id']}/certificate" if row['certificate'] else None
    value['phase_certificates']=[{'phase':phase['phase'],'sha256':phase['sha256'],'url':f"/api/workflow-runs/{row['id']}/certificate/{phase['phase']}"} for phase in db.execute('SELECT phase,sha256 FROM workflow_phase_certificates WHERE run_id=? ORDER BY phase',(row['id'],))]
    if row['job_id']:
        job = db.execute('SELECT stage,report FROM jobs WHERE id=?', (row['job_id'],)).fetchone()
        value['deployment_stage'] = job['stage'] if job else None
    if detail:
        value['result'] = json.loads(row['result']) if row['result'] else None
        value['events'] = [dict(r) for r in db.execute('SELECT stage,detail,created FROM workflow_events WHERE run_id=? ORDER BY seq', (row['id'],))]
    return value


def validate_batch_inventory(db, workflow_id, inventory):
    parent=db.execute('SELECT r.* FROM routine_runs r JOIN routine_erase_disks d ON d.run_id=r.id WHERE d.workflow_id=?',(workflow_id,)).fetchone()
    if not parent:return
    binding=db.execute('SELECT step_index FROM routine_erase_disks WHERE workflow_id=?',(workflow_id,)).fetchone()
    step=json.loads(parent['spec'])['steps'][binding['step_index']]
    if step['target_policy']!='all-internal':return
    original=json.loads(parent['inventory'])
    allowed=lambda disks:{(d['identity'],d['size_bytes']) for d in disks if d.get('transport')!='usb' or step['allow_usb']}
    if allowed(original['disks'])!=allowed(inventory['disks']):raise HTTPException(409,'The complete routine drive set changed since discovery')


def validate_policy_result(db,current,policy,spec):
    """A preset name cannot stand in for completed, bound phase evidence."""
    if spec.result!='passed':return
    if policy['method'] in erase_policy.PRESETS or erase_policy.native(policy['method']):
        if spec.run_id!=current['id'] or spec.system_serial!=json.loads(current['inventory'])['system_serial'] or spec.settings_sha256!=erase_policy.fingerprint(policy):
            raise HTTPException(409,'Preset evidence must bind the PC, run and immutable policy')
    evidence=spec.evidence
    if policy['method']=='nist_clear':
        if evidence.get('execution_method')!='zero' or evidence.get('achieved_classification')!='clear':
            raise HTTPException(409,'NIST Clear requires a verified full zero overwrite')
        return
    if not erase_policy.native(policy['method']):return
    scope=evidence.get('scope_targets')
    if evidence.get('engine_version')!=erase_policy.NATIVE_VERSION or evidence.get('operation_started') is not True or evidence.get('controller_state')!='completed' or evidence.get('native_method')!=policy['execution_method'] or evidence.get('target_identity')!=current['target'] or evidence.get('size_bytes')!=current['size'] or scope!=[{'identity':current['target'],'size_bytes':current['size']}]:
        raise HTTPException(409,'Native sanitize requires confirmed controller completion and exact disk scope')
    if evidence.get('native_certificate_sha256')!=spec.certificate_sha256:
        raise HTTPException(409,'The original native sanitize PDF must be the primary certificate')
    phases=evidence.get('phases',[])
    if not isinstance(phases,list) or not all(isinstance(phase,dict) for phase in phases):raise HTTPException(409,'Native phase evidence is invalid')
    native_phase=next((phase for phase in phases if phase.get('phase')=='native'),{})
    if type(native_phase.get('exit_code')) is not int or type(native_phase.get('errors')) is not int or any(native_phase.get(k)!=v for k,v in {'result':'passed','exit_code':0,'errors':0,'certificate_sha256':spec.certificate_sha256}.items()):
        raise HTTPException(409,'Native phase evidence or its certificate is incomplete')
    if policy['method']=='nist_purge':
        if evidence.get('achieved_classification')!='purge':
            raise HTTPException(409,'NIST Purge requires confirmed native sanitize; Clear cannot be labelled Purge')
        if evidence.get('prng_followup') is not policy['prng_followup']:
            raise HTTPException(409,'PRNG follow-up evidence differs from the authorized policy')
        if not policy['prng_followup'] and any(phase.get('phase')=='followup' for phase in phases):
            raise HTTPException(409,'PRNG follow-up was disabled; unexpected phase evidence is refused')
    if policy['method']=='nist_purge' and policy['prng_followup']:
        followup=next((phase for phase in phases if phase.get('phase')=='followup'),{})
        certificate=db.execute("SELECT sha256 FROM workflow_phase_certificates WHERE run_id=? AND phase='followup'",(current['id'],)).fetchone()
        required={'method':'prng','verification':'all','rounds':1,'final_blank':False,'result':'passed','exit_code':0,'errors':0}
        if not certificate or type(followup.get('exit_code')) is not int or type(followup.get('errors')) is not int or type(followup.get('rounds')) is not int or followup.get('final_blank') is not False or followup.get('certificate_sha256')!=certificate['sha256'] or any(followup.get(k)!=v for k,v in required.items()) or evidence.get('achieved_classification')!='purge':
            raise HTTPException(409,'NIST Purge requires confirmed native sanitize and the verified PRNG follow-up PDF')


def boot_decision(store, mac, server):
    """Return a managed ShredOS script or a hold, never a generic erase menu."""
    with store.db() as db:
        sync_jobs(db)
        row=db.execute("SELECT w.* FROM workflow_runs w JOIN routine_runs r ON r.workflow_id=w.id WHERE r.mac=? AND r.stage='erase-running' ORDER BY r.created DESC LIMIT 1",(mac.lower(),)).fetchone()
        if not row:row=db.execute('SELECT * FROM workflow_runs WHERE mac=? ORDER BY created DESC LIMIT 1',(mac.lower(),)).fetchone()
        if not row:
            if not db.execute('SELECT enabled FROM workflow_intake_settings WHERE id=1').fetchone()['enabled']:
                return None, None
            release=media(store)
            ticket=secrets.token_urlsafe(48)
            expiry=(datetime.now(timezone.utc)+timedelta(minutes=15)).isoformat()
            db.execute('''INSERT INTO workflow_intake(mac,ticket_hash,ticket_expires,seen) VALUES(?,?,?,?)
                ON CONFLICT(mac) DO UPDATE SET ticket_hash=excluded.ticket_hash,ticket_expires=excluded.ticket_expires,token_hash=NULL,expires=NULL''',
                (mac.lower(),digest(ticket),expiry,now()))
            base=f'http://{server}:8089/pxe/shredos/{release["release"]}'
            return f'#!ipxe\nkernel {base}/shredos console=tty3 loglevel=3 repxe_ticket={ticket} repxe_mac={mac.lower()} repxe_mode=intake\ninitrd {base}/companion.cpio.gz\nboot\n',None
        if row['stage'] == 'install-ready':
            check_profile(db, json.loads(row['spec']))
            return None, dict(row)
        if row['stage'] == 'installing':
            return '#!ipxe\necho re-pxe: workflow installation already started. Boot the installed disk or inspect the job.\nexit\n', None
        if row['stage'] != 'queued':
            return '#!ipxe\necho re-pxe: workflow is on hold or complete. Review Workflows before another operation.\nexit\n', None
        release = media(store)
        ticket = secrets.token_urlsafe(48)
        expiry = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
        db.execute('UPDATE workflow_runs SET ticket_hash=?,ticket_expires=? WHERE id=?', (digest(ticket), expiry, row['id']))
        path = f"http://{server}:8089/pxe/shredos/{release['release']}/shredos"
        known_serial = (json.loads(row['inventory']).get('system_serial') if row['inventory'] else '') or ''
        binding = (' repxe_serial_sha256=' + digest(known_serial)) if known_serial and known_serial != 'unreported' else ''
        # The media embeds the HTTPS origin and pinned CA; only a short-lived,
        # single-run enrollment ticket crosses the existing boot network.
        return f'#!ipxe\nkernel {path} console=tty3 loglevel=3 repxe_ticket={ticket} repxe_mac={mac.lower()}{binding}\ninitrd http://{server}:8089/pxe/shredos/{release["release"]}/companion.cpio.gz\nboot\n', None


def bind_boot(db, client_id, run):
    current = db.execute('SELECT * FROM workflow_runs WHERE id=?', (run['id'],)).fetchone()
    if not current or current['stage'] != 'install-ready':
        raise HTTPException(409, 'Workflow is no longer ready for Windows')
    db.execute('INSERT INTO workflow_boots VALUES(?,?)', (client_id, run['id']))
    db.execute('UPDATE clients SET profile_override=? WHERE id=?', (run['spec']['profile_id'] if isinstance(run['spec'], dict) else json.loads(run['spec'])['profile_id'], client_id))


def for_boot(db, client_id):
    return db.execute('SELECT r.* FROM workflow_runs r JOIN workflow_boots b ON r.id=b.run_id WHERE b.client_id=?', (client_id,)).fetchone()


def enforce_boot(db, client_id, targets):
    row = for_boot(db, client_id)
    if not row:
        return None
    if row['stage'] != 'install-ready':
        raise HTTPException(409, 'Workflow installation is locked')
    matches = [t for t in targets if t.identity == row['target'] and t.size_bytes == row['size']]
    if len(matches) != 1:
        raise HTTPException(409, 'The erased disk identity or capacity changed')
    check_profile(db, json.loads(row['spec']))
    return row


def install(app, store, Role, allow):
    def credential(authorization: Annotated[str | None, Header()] = None):
        if not authorization or not re.fullmatch(r'Bearer [A-Za-z0-9_-]{32,256}', authorization):
            raise HTTPException(401, 'Run credential required')
        return authorization[7:]

    def run_session(token: Annotated[str, Depends(credential)]):
        with store().db() as db:
            row = db.execute('SELECT * FROM workflow_runs WHERE token_hash=?', (digest(token),)).fetchone()
        if not row or row['expires'] <= now():
            raise HTTPException(401, 'Run credential expired')
        return dict(row)

    Session = Annotated[dict, Depends(run_session)]
    Credential = Annotated[str, Depends(credential)]

    @app.get('/api/workflows')
    def templates(role: Role):
        allow(role)
        with store().db() as db:
            rows = db.execute('SELECT * FROM workflow_templates ORDER BY created DESC').fetchall()
        try:
            info = media(store())
            readiness = {'ready': True, 'release': info['release'], 'upstream_commit': info['upstream_commit']}
        except HTTPException as exc:
            readiness = {'ready': False, 'reason': exc.detail}
        return {'templates': [{'id': r['id'], **json.loads(r['spec'])} for r in rows], 'media': readiness, 'intake_enabled': intake_enabled(store())}

    @app.post('/api/workflows', status_code=201)
    def create_template(spec: Template, role: Role):
        allow(role)
        uid = str(uuid.uuid4())
        with store().db() as db:
            check_profile(db, spec.model_dump())
            db.execute('INSERT INTO workflow_templates VALUES(?,?,?)', (uid, spec.model_dump_json(), now()))
        return {'id': uid, **spec.model_dump()}

    @app.put('/api/workflow-intake')
    def intake_setting(spec: IntakeSetting, role: Role):
        allow(role)
        if spec.enabled:media(store())
        with store().db() as db:
            db.execute('UPDATE workflow_intake_settings SET enabled=? WHERE id=1',(int(spec.enabled),))
        return {'enabled':spec.enabled}

    @app.post('/api/intake/claim',status_code=201)
    def intake_claim(spec: Claim,ticket: Credential):
        from . import routines
        token=secrets.token_urlsafe(48)
        with store().db() as db:
            row=db.execute('SELECT * FROM workflow_intake WHERE ticket_hash=?',(digest(ticket),)).fetchone()
            if not row or row['ticket_expires']<=now() or row['token_hash']:
                raise HTTPException(401,'Intake ticket expired or consumed')
            if row['mac']!=spec.mac.lower():raise HTTPException(403,'PC is outside this intake session')
            db.execute('UPDATE workflow_intake SET inventory=?,token_hash=?,expires=?,ticket_hash=NULL,seen=? WHERE mac=?',
                (spec.inventory.model_dump_json(),digest(token),(datetime.now(timezone.utc)+timedelta(hours=12)).isoformat(),now(),row['mac']))
            routines.queue_from_inventory(db, row['mac'], spec.inventory.model_dump())
        return {'token':token,'mode':'inventory-only'}

    @app.get('/api/intake/status')
    def intake_status(token: Credential):
        with store().db() as db:
            row=db.execute('SELECT * FROM workflow_intake WHERE token_hash=?',(digest(token),)).fetchone()
            if not row or row['expires']<=now():raise HTTPException(401,'Intake session expired')
            db.execute('UPDATE workflow_intake SET seen=? WHERE mac=?',(now(),row['mac']))
            run=db.execute("SELECT stage FROM workflow_runs WHERE mac=? AND stage='queued' ORDER BY created,id LIMIT 1",(row['mac'],)).fetchone()
            from . import routines
            routines.sync(db)
            routine = routines.latest(db, row['mac'])
        return {'reboot_for_workflow':bool(run and run['stage']=='queued' or routine and routine['stage'] in ('custom-boot','checkpoint')),
                'routine_stage':routine['stage'] if routine else None,
                'inventory_needed':bool(routine and routine['stage']=='discovering'),
                'erase_ready':bool(run and run['stage']=='queued' and (not routine or routine['stage']=='erase-running'))}

    @app.post('/api/intake/inventory')
    def refresh_inventory(spec: Claim,token: Credential):
        from . import routines
        with store().db() as db:
            intake=db.execute('SELECT * FROM workflow_intake WHERE token_hash=?',(digest(token),)).fetchone()
            if not intake or intake['expires']<=now():raise HTTPException(401,'Intake session expired')
            original=json.loads(intake['inventory'])
            if spec.mac.lower()!=intake['mac'] or original['system_serial']!=spec.inventory.system_serial:raise HTTPException(409,'PC binding changed since discovery')
            routines.sync(db);routine=routines.latest(db,intake['mac'])
            if not routine or routine['stage']!='discovering' or routine['workflow_id']:raise HTTPException(409,'This routine is not awaiting fresh inventory')
            original_set={(d['identity'],d['size_bytes']) for d in original['disks']}
            fresh=spec.inventory.model_dump()
            if original_set!={(d['identity'],d['size_bytes']) for d in fresh['disks']}:raise HTTPException(409,'The complete drive set changed before fallback')
            db.execute('UPDATE workflow_intake SET inventory=?,seen=? WHERE mac=?',(spec.inventory.model_dump_json(),now(),intake['mac']))
            routines.queue_from_inventory(db,intake['mac'],fresh)
            routine=routines.latest(db,intake['mac'])
        return {'accepted':True,'routine_stage':routine['stage'],'erase_ready':routine['stage']=='erase-running'}

    @app.post('/api/intake/erase-ticket')
    def intake_erase_ticket(spec: Claim, token: Credential):
        # Upgrade only this existing read-only RAM session. A lost authorization
        # response holds rather than issuing a second destructive ticket.
        if spec.agent_version not in ('re-pxe-shredos-4', 're-pxe-shredos-5', 're-pxe-shredos-6', 're-pxe-shredos-7', 're-pxe-shredos-8', 're-pxe-shredos-9', 're-pxe-shredos-10', MEDIA_MARKER): raise HTTPException(409, 'Update ShredOS for an in-place erase handoff')
        from . import routines
        with store().db() as db:
            intake = db.execute('SELECT * FROM workflow_intake WHERE token_hash=?', (digest(token),)).fetchone()
            if not intake or intake['expires']<=now(): raise HTTPException(401, 'Intake session expired')
            original = json.loads(intake['inventory'])
            if spec.mac.lower()!=intake['mac'] or original['system_serial']!=spec.inventory.system_serial:
                raise HTTPException(409, 'PC binding changed since discovery')
            routines.sync(db)
            routine = routines.latest(db,intake['mac'])
            if routine and routine['stage']!='erase-running': raise HTTPException(409, 'Routine is not ready for erasure')
            row = db.execute("SELECT * FROM workflow_runs WHERE id=? AND stage='queued'",(routine['workflow_id'],)).fetchone() if routine else db.execute("SELECT * FROM workflow_runs WHERE mac=? AND stage='queued' ORDER BY created,id LIMIT 1", (intake['mac'],)).fetchone()
            if not row or row['ticket_hash']: raise HTTPException(409, 'Erase authorization is unavailable or already issued')
            if routine and row['id']!=routine['workflow_id']: raise HTTPException(409, 'Erase is outside this routine')
            validate_batch_inventory(db,row['id'],spec.inventory.model_dump())
            expected = db.execute('SELECT w.target,w.size,w.spec FROM routine_erase_disks d JOIN workflow_runs w ON w.id=d.workflow_id WHERE d.run_id=? AND d.step_index=?', (routine['id'],routine['step'])).fetchall() if routine else [row]
            for disk in expected:
                matches = [d for d in spec.inventory.disks if d.identity==disk['target'] and d.size_bytes==disk['size']]
                if len(matches)!=1: raise HTTPException(409, 'Routine drive set changed since discovery')
            ticket = secrets.token_urlsafe(48)
            db.execute('UPDATE workflow_runs SET ticket_hash=?,ticket_expires=? WHERE id=?', (digest(ticket),(datetime.now(timezone.utc)+timedelta(minutes=15)).isoformat(),row['id']))
        return {'ticket':ticket,'targets':[{'identity':d['target'],'size_bytes':d['size'],'allow_usb':json.loads(d['spec'])['allow_usb']} for d in expected]}

    @app.get('/api/workflow-devices')
    def devices(role: Role):
        allow(role)
        with store().db() as db:
            found={}
            for r in db.execute("SELECT * FROM clients c WHERE c.id=(SELECT x.id FROM clients x WHERE x.mac=c.mac ORDER BY x.created DESC LIMIT 1) AND c.targets!='[]' ORDER BY c.seen DESC LIMIT 200"):
                found[r['mac']]={'mac':r['mac'],'manufacturer':r['manufacturer'],'model':r['model'],
                                 'disks':json.loads(r['targets']),'seen':r['seen'],'inventory':None}
            for r in db.execute('SELECT * FROM workflow_intake WHERE inventory IS NOT NULL ORDER BY seen DESC LIMIT 200'):
                inv=json.loads(r['inventory'])
                found[r['mac']]={'mac':r['mac'],'manufacturer':inv['manufacturer'],'model':inv['model'],
                                  'disks':inv['disks'],'seen':r['seen'],'inventory':inv}
            return sorted(found.values(),key=lambda r:r['seen'],reverse=True)


    @app.get('/api/workflow-runs')
    def runs(role: Role):
        allow(role)
        with store().db() as db:
            sync_jobs(db)
            return [projection(r, db) for r in db.execute('SELECT * FROM workflow_runs ORDER BY created DESC LIMIT 200')]

    @app.post('/api/workflow-runs', status_code=201)
    def create_run(spec: NewRun, role: Role):
        allow(role)
        if spec.confirmation != spec.target_identity:
            raise HTTPException(422, 'Type the exact disk identity to authorize erasure')
        release=media(store())
        uid = str(uuid.uuid4())
        with store().db() as db:
            sync_jobs(db)
            template = db.execute('SELECT spec FROM workflow_templates WHERE id=?', (spec.template_id,)).fetchone()
            if not template:
                raise HTTPException(404, 'Workflow not found')
            value = json.loads(template['spec'])
            if value['method'] not in erase_policy.METHODS: raise HTTPException(409, 'Only nwipe methods are supported; replace this old workflow')
            if erase_policy.extended(value['method']):require_native_media(release)
            check_profile(db, value)
            if value['after_erasure'] != 'stop':
                path = store().root / 'boot/pxe/current.json'
                if not path.is_file() or not json.loads(path.read_text()).get('validated'):
                    raise HTTPException(409, 'Validated Windows PXE media is required')
            active = db.execute("SELECT id FROM workflow_runs WHERE mac=? AND stage NOT IN ('complete','failed','cancelled','unsupported')", (spec.mac.lower(),)).fetchone()
            pending = db.execute("SELECT j.id FROM clients c JOIN jobs j ON j.id=c.job_id WHERE c.mac=? AND j.stage NOT IN ('complete','failed','cancelled')", (spec.mac.lower(),)).fetchone()
            if active or pending:
                raise HTTPException(409, 'This PC has an active workflow or installation; inspect it first')
            at = now()
            db.execute('''INSERT INTO workflow_runs(id,template_id,spec,mac,target,size,asset_tag,stage,created,updated)
                          VALUES(?,?,?,?,?,?,?,?,?,?)''', (uid, spec.template_id, template['spec'], spec.mac.lower(),
                          spec.target_identity, spec.size_bytes, spec.asset_tag, 'queued', at, at))
            event(db, uid, 'queued', 'Operator authorized erasure of the named disk')
            # Invalidate pre-existing Windows enrollment tickets/sessions for this MAC.
            db.execute('UPDATE clients SET ticket_expires=?,expires=? WHERE mac=? AND job_id IS NULL', (at, at, spec.mac.lower()))
            return projection(db.execute('SELECT * FROM workflow_runs WHERE id=?', (uid,)).fetchone(), db, True)

    @app.get('/api/workflow-runs/{uid}')
    def details(uid: str, role: Role):
        allow(role)
        with store().db() as db:
            row = db.execute('SELECT * FROM workflow_runs WHERE id=?', (uid,)).fetchone()
            if not row:
                raise HTTPException(404, 'Run not found')
            return projection(row, db, True)

    @app.post('/api/workflow-runs/{uid}/action')
    def action(uid: str, spec: Action, role: Role):
        allow(role)
        with store().db() as db:
            row = db.execute('SELECT * FROM workflow_runs WHERE id=?', (uid,)).fetchone()
            if not row:
                raise HTTPException(404, 'Run not found')
            if spec.action == 'approve-install':
                if row['stage'] != 'awaiting-approval' or not row['certificate']:
                    raise HTTPException(409, 'A verified erase and certificate must be ready first')
                check_profile(db, json.loads(row['spec']))
                event(db, uid, 'install-ready', 'Operator approved Windows installation')
            else:
                if row['stage'] not in ('queued', 'claimed', 'awaiting-approval', 'install-ready'):
                    raise HTTPException(409, 'An active operation cannot be cancelled or retried from the dashboard')
                event(db, uid, 'cancelled', 'Operator cancelled the queued or waiting run')
                db.execute('UPDATE workflow_runs SET ticket_hash=NULL,token_hash=NULL WHERE id=?', (uid,))
        return {'accepted': True}

    @app.get('/api/workflow-runs/{uid}/certificate')
    def certificate(uid: str, role: Role, download: bool=True):
        allow(role)
        with store().db() as db:
            row = db.execute('SELECT certificate FROM workflow_runs WHERE id=?', (uid,)).fetchone()
        if not row or not row['certificate']:
            raise HTTPException(404, 'Certificate not received')
        from fastapi import Response
        return Response(bytes(row['certificate']), media_type='application/pdf', headers={
            'Content-Disposition': ('attachment' if download else 'inline')+'; filename="erasure-certificate.pdf"',
            'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', 'Content-Security-Policy': "default-src 'none'; frame-ancestors 'self'"})

    @app.get('/api/workflow-runs/{uid}/certificate/{phase}')
    def phase_certificate(uid: str, phase: Literal['followup'], role: Role, download: bool=True):
        allow(role)
        with store().db() as db:
            row=db.execute('SELECT payload FROM workflow_phase_certificates WHERE run_id=? AND phase=?',(uid,phase)).fetchone()
        if not row:raise HTTPException(404,'Phase certificate not received')
        from fastapi import Response
        return Response(bytes(row['payload']),media_type='application/pdf',headers={'Content-Disposition':('attachment' if download else 'inline')+'; filename="nwipe-followup.pdf"','Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Content-Security-Policy':"default-src 'none'; frame-ancestors 'self'"})

    @app.post('/api/erase/claim', status_code=201)
    def claim(spec: Claim, ticket: Credential):
        token = secrets.token_urlsafe(48)
        with store().db() as db:
            row = db.execute('SELECT * FROM workflow_runs WHERE ticket_hash=?', (digest(ticket),)).fetchone()
            if not row or row['ticket_expires'] <= now() or row['stage'] != 'queued':
                raise HTTPException(401, 'Erase ticket expired or consumed')
            if spec.mac.lower() != row['mac']:
                raise HTTPException(403, 'PC is outside this run')
            previous=json.loads(row['inventory']) if row['inventory'] else None
            if previous and previous['system_serial'] != spec.inventory.system_serial:raise HTTPException(409,'PC serial changed since discovery')
            validate_batch_inventory(db,row['id'],spec.inventory.model_dump())
            target = next((d for d in spec.inventory.disks if d.identity == row['target']), None)
            value = json.loads(row['spec'])
            if value['method'] not in erase_policy.METHODS: raise HTTPException(409, 'Only nwipe methods are supported; republish this routine')
            if erase_policy.extended(value['method']) and spec.agent_version not in ('re-pxe-shredos-9', 're-pxe-shredos-10', MEDIA_MARKER):
                raise HTTPException(409,'Native sanitize requires the validated nwipe 0.43 re-pxe ShredOS release')
            if target and target.transport == 'usb' and not value['allow_usb']:
                raise HTTPException(409, 'This workflow excludes USB disks')
            if not target or target.size_bytes != row['size']:
                raise HTTPException(409, 'Named disk or capacity differs from the authorized target')
            batch=[d.model_dump() for d in spec.inventory.disks]
            try:checked,_=erase_policy.resolve(value,target.capabilities.model_dump(),target.model_dump(),batch)
            except erase_policy.Unsupported:
                # Give the bound companion a credential to report unsupported;
                # begin still refuses writes and /unsupported verifies the batch
                # never started before authorizing its configured Failure route.
                checked=erase_policy.settings(value)
            except ValueError as exc:raise HTTPException(409,str(exc)) from None
            if erase_policy.native(value['method']) and checked['execution_method']!=erase_policy.settings(value)['execution_method']:
                routine=db.execute('SELECT 1 FROM routine_erase_disks WHERE workflow_id=?',(row['id'],)).fetchone()
                if routine:raise HTTPException(409,'Native sanitize method changed since authorization')
                # Standalone Purge is first resolved by this read-only inventory;
                # the returned claim pins its actual technique before begin.
                db.execute('UPDATE workflow_runs SET spec=? WHERE id=?',(json.dumps(checked),row['id']))
            db.execute('UPDATE workflow_runs SET inventory=?,token_hash=?,expires=?,ticket_hash=NULL WHERE id=?',
                       (spec.inventory.model_dump_json(), digest(token), (datetime.now(timezone.utc) + timedelta(hours=48)).isoformat(), row['id']))
            event(db, row['id'], 'claimed', 'PC specs recorded; erase has not started')
            row=db.execute('SELECT * FROM workflow_runs WHERE id=?',(row['id'],)).fetchone()
            binding=db.execute('SELECT run_id,step_index FROM routine_erase_disks WHERE workflow_id=?',(row['id'],)).fetchone()
            batch_rows=db.execute('SELECT w.target,w.size,w.spec FROM routine_erase_disks d JOIN workflow_runs w ON w.id=d.workflow_id WHERE d.run_id=? AND d.step_index=? ORDER BY d.ordinal',(binding['run_id'],binding['step_index'])).fetchall() if binding else [row]
            batch_targets=[]
            for bound in batch_rows:
                bound_policy=erase_policy.settings(json.loads(bound['spec']))
                batch_targets.append({'target_identity':bound['target'],'size_bytes':bound['size'],'allow_usb':bool(bound_policy['allow_usb']),
                                      'execution_method':bound_policy.get('execution_method') or bound_policy['method']})
        value = json.loads(row['spec'])
        policy = erase_policy.settings(value)
        from . import branding
        logo = branding.claim_logo(store()) if spec.agent_version in ('re-pxe-shredos-6','re-pxe-shredos-7','re-pxe-shredos-8','re-pxe-shredos-9','re-pxe-shredos-10',MEDIA_MARKER) else None
        return {'branding_logo': logo, 'id': row['id'], 'token': token, 'target_identity': row['target'], 'size_bytes': row['size'],'batch_targets':batch_targets,
                **{k: policy.get(k, False) for k in erase_policy.authorized_keys(policy)},
                'system_serial': spec.inventory.system_serial, 'settings_sha256': erase_policy.fingerprint(value),

                'allow_usb': value['allow_usb'], 'asset_tag': row['asset_tag']}

    @app.post('/api/erase/begin')
    def begin(row: Session):
        with store().db() as db:
            current = db.execute('SELECT id,stage,spec,inventory,target,size FROM workflow_runs WHERE id=?', (row['id'],)).fetchone()
            if json.loads(current['spec'])['method'] not in erase_policy.METHODS: raise HTTPException(409, 'Only nwipe methods are supported; cancel this unstarted legacy job')
            if current['stage'] != 'claimed':
                raise HTTPException(409, 'Erase has already started or was cancelled; automatic retries are refused')
            inventory=json.loads(current['inventory'])
            binding=db.execute('SELECT run_id,step_index FROM routine_erase_disks WHERE workflow_id=?',(current['id'],)).fetchone()
            bound=db.execute('SELECT w.spec,w.target,w.size FROM routine_erase_disks d JOIN workflow_runs w ON w.id=d.workflow_id WHERE d.run_id=? AND d.step_index=?',(binding['run_id'],binding['step_index'])).fetchall() if binding else [current]
            for disk in bound:
                target=next((d for d in inventory['disks'] if d['identity']==disk['target'] and d['size_bytes']==disk['size']),None)
                if not target:raise HTTPException(409,'Complete erase batch changed before beginning')
                try:erase_policy.resolve(json.loads(disk['spec']),target.get('capabilities',{}),target,inventory['disks'])
                except ValueError as exc:raise HTTPException(409,str(exc)) from None
            event(db, row['id'], 'erasing', 'Erasure started on the authorized disk')
        return {'accepted': True}

    @app.post('/api/erase/unsupported')
    def unsupported(spec: UnsupportedInput, row: Session):
        evidence=spec.evidence
        if evidence.get('operation_started') is not False or evidence.get('controller_state')!='unsupported' or evidence.get('engine_version')!=erase_policy.NATIVE_VERSION:
            raise HTTPException(409,'Only confirmed unsupported capability before starting can use the Failure route')
        with store().db() as db:
            current=db.execute('SELECT * FROM workflow_runs WHERE id=?',(row['id'],)).fetchone()
            if current['stage']!='claimed' or current['certificate'] or not erase_policy.native(json.loads(current['spec'])['method']):
                raise HTTPException(409,'An operation was already started; fallback is refused')
            if evidence.get('target_identity')!=current['target'] or evidence.get('size_bytes')!=current['size']:
                raise HTTPException(409,'Unsupported evidence is outside the authorized disk')
            binding=db.execute('SELECT run_id,step_index FROM routine_erase_disks WHERE workflow_id=?',(row['id'],)).fetchone()
            started=db.execute('''SELECT 1 FROM workflow_events e JOIN routine_erase_disks d ON d.workflow_id=e.run_id
                WHERE d.run_id=? AND d.step_index=? AND e.stage='erasing' LIMIT 1''',(binding['run_id'],binding['step_index'])).fetchone() if binding else None
            if started or db.execute("SELECT 1 FROM workflow_events WHERE run_id=? AND stage='erasing'",(row['id'],)).fetchone():
                raise HTTPException(409,'This erase batch already started; automatic fallback is refused')
            db.execute('UPDATE workflow_runs SET result=? WHERE id=?',(json.dumps({'result':'unsupported','evidence':evidence}),row['id']))
            event(db,row['id'],'unsupported',spec.error)
            db.execute('UPDATE workflow_runs SET token_hash=NULL,ticket_hash=NULL WHERE id=?',(row['id'],))
            from . import routines
            routines.sync(db)
            routine=db.execute('SELECT stage FROM routine_runs WHERE id=?',(binding['run_id'],)).fetchone() if binding else None
        return {'accepted':True,'routine_stage':routine['stage'] if routine else None,'inventory_needed':bool(routine and routine['stage']=='discovering')}

    @app.post('/api/erase/progress')
    def progress(spec: Progress, row: Session):
        with store().db() as db:
            current=db.execute('SELECT stage FROM workflow_runs WHERE id=?',(row['id'],)).fetchone()
            if current['stage']!='erasing': raise HTTPException(409, 'Erasure is not running')
            db.execute('UPDATE workflow_runs SET expires=? WHERE id=?',((datetime.now(timezone.utc)+timedelta(hours=48)).isoformat(),row['id']))
            db.execute("UPDATE workflow_intake SET expires=? WHERE mac=? AND json_extract(inventory,'$.system_serial')=?",((datetime.now(timezone.utc)+timedelta(hours=48)).isoformat(),row['mac'],json.loads(row['inventory'])['system_serial']))
            db.execute('INSERT INTO workflow_progress VALUES(?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET percent=excluded.percent,detail=excluded.detail,updated=excluded.updated', (row['id'],spec.percent,spec.detail,now()))
        return {'accepted':True}

    @app.put('/api/erase/certificate')
    async def receive_certificate(request: Request, row: Session):
        if request.headers.get('content-type', '').split(';')[0] != 'application/pdf':
            raise HTTPException(415, 'PDF certificate required')
        payload = bytearray()
        async for chunk in request.stream():
            if len(payload) + len(chunk) > CERT_LIMIT:
                raise HTTPException(413, 'Certificate exceeds 8 MiB')
            payload.extend(chunk)
        if not payload.startswith(b'%PDF-') or b'%%EOF' not in payload[-1024:]:
            raise HTTPException(422, 'Invalid PDF certificate')
        sha = hashlib.sha256(payload).hexdigest()
        with store().db() as db:
            current = db.execute('SELECT * FROM workflow_runs WHERE id=?', (row['id'],)).fetchone()
            if current['certificate']:
                if current['certificate_sha256'] == sha:
                    return {'accepted': True, 'sha256': sha, 'duplicate': True}
                raise HTTPException(409, 'Certificate already received; evidence cannot be replaced')
            if current['stage'] != 'erasing':
                raise HTTPException(409, 'Certificate cannot be attached at this stage')
            db.execute('UPDATE workflow_runs SET certificate=?,certificate_sha256=?,updated=? WHERE id=?', (bytes(payload), sha, now(), row['id']))
        return {'accepted': True, 'sha256': sha, 'duplicate': False}

    @app.put('/api/erase/certificate/{phase}')
    async def receive_phase_certificate(phase: Literal['followup'], request: Request,row: Session):
        if request.headers.get('content-type','').split(';')[0]!='application/pdf':raise HTTPException(415,'PDF certificate required')
        payload=bytearray()
        async for chunk in request.stream():
            if len(payload)+len(chunk)>CERT_LIMIT:raise HTTPException(413,'Certificate exceeds 8 MiB')
            payload.extend(chunk)
        if not payload.startswith(b'%PDF-') or b'%%EOF' not in payload[-1024:]:raise HTTPException(422,'Invalid PDF certificate')
        sha=hashlib.sha256(payload).hexdigest()
        with store().db() as db:
            current=db.execute('SELECT stage,spec FROM workflow_runs WHERE id=?',(row['id'],)).fetchone()
            existing=db.execute('SELECT sha256 FROM workflow_phase_certificates WHERE run_id=? AND phase=?',(row['id'],phase)).fetchone()
            if existing:
                if existing['sha256']==sha:return {'accepted':True,'sha256':sha,'duplicate':True}
                raise HTTPException(409,'Phase certificate already received; evidence cannot be replaced')
            policy=erase_policy.settings(json.loads(current['spec']))
            if current['stage']!='erasing' or policy['method']!='nist_purge' or not policy['prng_followup']:raise HTTPException(409,'This run cannot accept a follow-up certificate')
            db.execute('INSERT INTO workflow_phase_certificates VALUES(?,?,?,?)',(row['id'],phase,bytes(payload),sha))
        return {'accepted':True,'sha256':sha,'duplicate':False}

    @app.post('/api/erase/result')
    def result(spec: Result, row: Session):
        payload = spec.model_dump_json()
        sha = digest(payload)
        with store().db() as db:
            current = db.execute('SELECT * FROM workflow_runs WHERE id=?', (row['id'],)).fetchone()
            if current['report_hash']:
                if current['report_hash'] == sha:
                    return {'accepted': True, 'duplicate': True, 'stage': current['stage']}
                raise HTTPException(409, 'Result is immutable')
            expected = json.loads(current['spec'])
            if current['stage'] != 'erasing':
                raise HTTPException(409, 'Erase is not running')
            policy = erase_policy.settings(expected)
            if (spec.target_identity, spec.size_bytes, spec.method, spec.verification, spec.rounds, spec.final_blank) != (current['target'], current['size'], policy['method'], policy['verification'], policy['rounds'], policy['final_blank']):
                raise HTTPException(409, 'Result does not match the authorized erase')
            if spec.run_id and (spec.run_id != current['id'] or spec.system_serial != json.loads(current['inventory'])['system_serial'] or spec.settings_sha256 != erase_policy.fingerprint(expected)):
                raise HTTPException(409, 'Result is outside this PC, run or immutable erase policy')
            if spec.result == 'passed' and (not current['certificate'] or spec.certificate_sha256 != current['certificate_sha256']):
                raise HTTPException(409, 'Matching certificate must be saved before Windows can start')
            validate_policy_result(db,current,policy,spec)
            pending = spec.evidence.get('safe_to_reboot') is False or erase_policy.native(policy['method']) and spec.result=='failed'
            next_stage = ('operation-pending' if pending else 'failed') if spec.result == 'failed' else {
                'stop': 'complete', 'approval': 'awaiting-approval', 'automatic': 'install-ready'}[expected['after_erasure']]
            db.execute('UPDATE workflow_runs SET result=?,report_hash=? WHERE id=?', (payload, sha, row['id']))
            db.execute('INSERT INTO workflow_progress VALUES(?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET percent=excluded.percent,detail=excluded.detail,updated=excluded.updated', (row['id'],100 if spec.result=='passed' else None,'Verified erasure complete' if spec.result=='passed' else 'Erasure failed',now()))
            event(db, row['id'], next_stage, 'Erasure passed; verification: '+spec.verification+'; certificate retained' if spec.result == 'passed' else str(spec.evidence.get('error') or 'Erasure failed; inspect retained evidence')[:1000])
            from . import routines
            routines.sync(db)
            routine = db.execute('SELECT r.stage FROM routine_runs r WHERE r.workflow_id=? OR r.id IN (SELECT run_id FROM routine_erase_disks WHERE workflow_id=?)', (row['id'],row['id'])).fetchone()
        return {'accepted': True, 'duplicate': False, 'stage': next_stage, 'reboot_for_routine': bool(routine and routine['stage'] in ('custom-boot', 'windows-boot', 'checkpoint'))}

    @app.post('/api/erase/failure')
    def failure(spec: Failure, row: Session):
        with store().db() as db:
            current = db.execute('SELECT stage,spec FROM workflow_runs WHERE id=?', (row['id'],)).fetchone()
            if current['stage'] not in ('claimed', 'erasing'):
                raise HTTPException(409, 'Run already ended')
            event(db,row['id'],'operation-pending' if erase_policy.native(json.loads(current['spec'])['method']) else 'failed',spec.error)
            from . import routines
            routines.sync(db)
            routine = db.execute('SELECT r.stage FROM routine_runs r WHERE r.workflow_id=? OR r.id IN (SELECT run_id FROM routine_erase_disks WHERE workflow_id=?)', (row['id'],row['id'])).fetchone()
        return {'accepted': True, 'reboot_for_routine': bool(routine and routine['stage'] in ('custom-boot', 'windows-boot', 'checkpoint'))}

    @app.get('/api/erase/status')
    def status(row: Session):
        with store().db() as db:
            current = db.execute('SELECT * FROM workflow_runs WHERE id=?', (row['id'],)).fetchone()
            if current['stage'] in ('claimed', 'erasing', 'operation-pending', 'awaiting-approval', 'install-ready'):
                # Long multipass wipes and approval waits retain a bounded,
                # renewable credential; idle/disconnected sessions still expire.
                db.execute('UPDATE workflow_runs SET expires=? WHERE id=?',
                           ((datetime.now(timezone.utc) + timedelta(hours=48)).isoformat(), row['id']))
            from . import routines
            routines.sync(db)
            routine = db.execute('SELECT r.stage FROM routine_runs r WHERE r.workflow_id=? OR r.id IN (SELECT run_id FROM routine_erase_disks WHERE workflow_id=?)', (row['id'],row['id'])).fetchone()
        return {'stage': current['stage'], 'certificate_saved': bool(current['certificate']),
                'reboot_for_windows': current['stage'] == 'install-ready',
                'reboot_for_routine': bool(routine and routine['stage'] in ('custom-boot', 'windows-boot', 'checkpoint'))}
