"""Operator visibility and server-side retirement of deployment sessions."""
from datetime import datetime, timedelta, timezone
import json
from typing import Literal

from fastapi import HTTPException, Query
from .device_inventory import facts_for, asset_key, serial_key
from pydantic import BaseModel, ConfigDict, Field

TERMINAL = {'complete', 'failed', 'cancelled'}
COMPLETION = {'awaiting-windows', 'completing', 'needs-operator', 'ready-for-oobe'}
VIEWS = Literal['current', 'pending', 'attention', 'online', 'offline', 'all', 'history']


def initialize(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS client_install_progress(client_id TEXT PRIMARY KEY REFERENCES clients(id),
            job_id TEXT NOT NULL REFERENCES jobs(id), percent INTEGER NOT NULL, captured_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS client_retirements(client_id TEXT PRIMARY KEY, deleted_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS client_control_events(
            seq INTEGER PRIMARY KEY AUTOINCREMENT, client_id TEXT NOT NULL,
            mac TEXT NOT NULL, job_id TEXT, action TEXT NOT NULL,
            reason TEXT NOT NULL, timestamp TEXT NOT NULL);
    ''')


def active_session(db, uid, at=None):
    row = db.execute('SELECT * FROM clients WHERE id=?', (uid,)).fetchone()
    if not row or row['stage'] == 'cancelled' or not row['expires'] or row['expires'] <= (at or datetime.now(timezone.utc).isoformat()):
        raise HTTPException(401, 'Device session expired or cancelled')
    return row


def blocking_job(db, mac):
    # Match the network-first boot guard, including retired records. Retirement
    # never reveals an older completion job by physically deleting a session.
    row = db.execute('''SELECT j.id,j.stage,j.report FROM clients c JOIN jobs j ON j.id=c.job_id
        WHERE c.mac=? ORDER BY c.created DESC,c.rowid DESC LIMIT 1''', (mac,)).fetchone()
    return row['id'] if row and row['report'] is None and row['stage'] in COMPLETION else None


def project(db, settings, at):
    from . import routines
    routines.sync(db)
    routine_records={r['client_id']:routines.projection(db,r) for r in db.execute('SELECT * FROM routine_runs WHERE client_id IS NOT NULL')}
    labels = {r['id']: json.loads(r['spec']) for r in db.execute('SELECT id,spec FROM deployment_profiles')}
    rows = db.execute('''SELECT c.*,j.stage job_stage,j.created job_created,j.updated job_updated,
        j.report IS NOT NULL has_report,a.name image_name,rc.expires completion_expires,
        json_extract(j.report,'$.summary') report_summary,
        json_array_length(json_extract(j.report,'$.driver_problems')) problem_count,
        (SELECT detail FROM events e WHERE e.job_id=c.job_id ORDER BY seq DESC LIMIT 1) last_event,
        r.deleted_at,p.percent install_percent,p.captured_at progress_updated,c.rowid session_order,
        COALESCE(NULLIF(json_extract(i.inventory,'$.system.serial'),''),json_extract(j.report,'$.native.hardware.specs.system.serial'),'') system_serial,
        c.id=(SELECT x.id FROM clients x WHERE x.mac=c.mac ORDER BY x.created DESC,x.rowid DESC LIMIT 1) latest
        FROM clients c LEFT JOIN jobs j ON j.id=c.job_id
        LEFT JOIN artifacts a ON a.id=j.image_id LEFT JOIN report_credentials rc ON rc.job_id=j.id
        LEFT JOIN client_retirements r ON r.client_id=c.id LEFT JOIN client_inventory i ON i.client_id=c.id
        LEFT JOIN client_install_progress p ON p.client_id=c.id AND p.job_id=c.job_id ORDER BY c.seen DESC,c.rowid DESC''').fetchall()
    identities = {r['id']: asset_key(dict(r)) for r in rows}
    latest = {}
    for r in sorted(rows, key=lambda r: (r['created'], r['session_order']), reverse=True):
        latest.setdefault(identities[r['id']], r['id'])
    blockers = {}
    for r in rows:
        key = identities[r['id']]
        if latest[key] == r['id']:
            blockers[key] = r['job_id'] if not r['has_report'] and r['job_stage'] in COMPLETION else None
    result = []
    for r in rows:
        selected = json.loads(r['selection']) if r['selection'] else {}
        policy = json.loads(r['boot_policy']) if r['boot_policy'] else {}
        configuration = selected.get('profile', {}).get('id') or policy.get('profileId') or r['profile_override'] or settings.default_profile_id
        stage = 'cancelled' if r['stage']=='cancelled' else r['job_stage'] if r['job_stage'] in TERMINAL or r['has_report'] else r['stage']
        pending = bool(r['job_id'] and r['job_stage'] not in TERMINAL and not r['has_report'])
        completion = pending and (r['job_stage'] in COMPLETION or stage in COMPLETION)
        expires = r['completion_expires'] if completion and r['completion_expires'] else (r['expires'] or r['ticket_expires'])
        seen = datetime.fromisoformat(r['seen'])
        offline_at = min(seen + timedelta(seconds=90), datetime.fromisoformat(expires))
        online = at < offline_at and stage not in TERMINAL
        is_latest = latest[identities[r['id']]] == r['id']
        current = bool(is_latest and at < offline_at + timedelta(seconds=settings.offline_hide_seconds))
        attention = stage in ('failed', 'needs-operator', 'waiting-for-input') or bool(pending and (not online or expires <= at.isoformat()))
        result.append({
            'routine': routine_records.get(r['id']),
            'id': r['id'], 'system_serial': r['system_serial'], 'mac': r['mac'], 'manufacturer': r['manufacturer'], 'model': r['model'],
            'target': selected.get('target_identity', policy.get('targetIdentity', '')),
            'targets': json.loads(r['targets']), 'windows': labels.get(configuration, {}).get('windows_label', ''),
            'image_name': r['image_name'], 'client_version': policy.get('clientVersion', ''),
            'manual_disk_selection': bool(policy.get('manualDiskSelection')),'disk_selection_ready':stage=='waiting-for-input' and not policy.get('targetIdentity'),
            'configuration_id': configuration, 'configuration_name': labels.get(configuration, {}).get('name', ''),
            'configuration_override': r['profile_override'],
            'selection_locked': bool(r['token_hash'] or r['stage'] != 'booting' or r['ticket_expires'] <= at.isoformat() or not r['latest']),
            'stage': stage, 'job_stage': r['job_stage'], 'online': online, 'last_seen': r['seen'],
            'created': r['created'], 'job_updated': r['job_updated'], 'credential_expires': expires,
            'credential_expired': expires <= at.isoformat(), 'install_percent': r['install_percent'] if stage=='applying-image' else None,
            'progress_updated': r['progress_updated'], 'received_bytes': r['received'], 'total_bytes': r['total'],
            'bytes_per_second': r['rate'] if online and (at-seen).total_seconds() < 10 else 0,
            'job_id': r['job_id'], 'error': '' if stage=='complete' else (r['error'] or (r['report_summary'] if stage in ('failed','needs-operator') else '') or ''),
            'last_event': r['last_event'] or '', 'problem_count': r['problem_count'],
            'pending': pending, 'completion_pending': completion, 'needs_attention': attention,
            'boot_blocked': blockers[identities[r['id']]] == r['job_id'] and r['job_id'] is not None,
            'blocking_job_id': blockers[identities[r['id']]], 'has_report': bool(r['has_report']),
            'latest': is_latest, 'deleted_at': r['deleted_at'], 'current': current,
            'can_cancel': not r['deleted_at'] and (routine_records[r['id']]['can_cancel'] if r['id'] in routine_records else (pending or stage=='failed' or (not r['job_id'] and stage not in TERMINAL))),
            'can_reset': bool(is_latest and not r['deleted_at']),
            'can_delete': not r['deleted_at'] and (not pending and (bool(r['job_id']) or not online or stage in TERMINAL)),
        })
    return result


def listing(db, settings, at, view='current', search='', page=1, page_size=50):
    rows = [r for r in project(db, settings, at) if not r['deleted_at']]
    counts = {key: sum(bool(r[key]) for r in rows) for key in ('pending', 'needs_attention', 'online', 'completion_pending')}
    counts['standalone_attention'] = sum(r['needs_attention'] and not r['routine'] for r in rows)
    filters = {'current': lambda r: r['current'] or r['pending'] or (r['latest'] and r['needs_attention']),
               'pending': lambda r: r['pending'], 'attention': lambda r: r['needs_attention'],
               'online': lambda r: r['online'], 'offline': lambda r: not r['online'] and (r['latest'] or r['pending']),
               'all': lambda r: r['latest'] or r['pending'], 'history': lambda r: True}
    query = search.casefold().strip()
    matching = [r for r in rows if filters[view](r) and (not query or query in ' '.join(str(r.get(k) or '') for k in
        ('id', 'mac', 'manufacturer', 'model', 'system_serial', 'target', 'configuration_name', 'windows', 'job_id', 'stage', 'error')).casefold())]
    matching.sort(key=lambda r: (not bool(serial_key(r['system_serial'])), serial_key(r['system_serial'])))
    total = len(matching); pages = max(1, (total+page_size-1)//page_size); page = min(page, pages)
    return {'clients': matching[(page-1)*page_size:page*page_size], 'client_counts': counts,
            'client_total': total, 'client_page': page, 'client_pages': pages,
            'online_clients': len({asset_key(r) for r in rows if r['online']}), 'active_jobs': counts['pending']}


class Binding(BaseModel):
    model_config = ConfigDict(extra='forbid')
    client_id: str = Field(min_length=1,max_length=100)
    expected_job_id: str | None

class BulkControl(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['cancel','reset','delete']
    sessions: list[Binding] = Field(min_length=1,max_length=200)

class Control(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_job_id: str | None
    reason: str = Field(default='Operator requested a fresh start', min_length=1, max_length=500)


def install(app, store, Role, allow, settings_for):
    @app.get('/api/clients')
    def clients(role: Role, view: VIEWS='current', search: str=Query(default='', max_length=200),
                page: int=Query(default=1, ge=1), page_size: int=Query(default=50, ge=1, le=200)):
        allow(role)
        with store().db() as db:
            return listing(db, settings_for(db), datetime.now(timezone.utc), view, search, page, page_size)

    @app.get('/api/clients/{uid}')
    def detail(uid: str, role: Role):
        allow(role)
        with store().db() as db:
            rows = project(db, settings_for(db), datetime.now(timezone.utc))
            row = next((r for r in rows if r['id'] == uid), None)
            if not row: raise HTTPException(404, 'Client not found')
            history = [r for r in rows if asset_key(r) == asset_key(row)]
            job = db.execute('SELECT * FROM jobs WHERE id=?', (row['job_id'],)).fetchone()
            events = [dict(r) for r in db.execute('SELECT * FROM events WHERE job_id=? ORDER BY seq', (row['job_id'],))]
            controls = [dict(r) for r in db.execute('SELECT * FROM client_control_events WHERE client_id IN (' + ','.join('?' for _ in history) + ') ORDER BY seq', [r['id'] for r in history])]
            device=facts_for(db,uid,json.loads(job['report']) if job and job['report'] else None,row['manufacturer'],row['model'],row['targets'])
            from .job_reports import asset_attachments
            return {'device':device,'client': row, 'sessions': history, 'job': {**dict(job), 'events': events} if job else None, 'controls': controls,
                    'attachments': asset_attachments(db, row, history)}

    def control(uid, action, spec, role):
        allow(role)
        at = datetime.now(timezone.utc).isoformat()
        with store().db() as db:
            rows = project(db, settings_for(db), datetime.fromisoformat(at))
            row = next((r for r in rows if r['id'] == uid), None)
            if not row or row['deleted_at']: raise HTTPException(404, 'Client not found')
            if spec.expected_job_id != row['job_id']:
                raise HTTPException(409, 'Client changed; refresh its details before continuing')
            if action == 'reset' and not row['can_reset']:
                raise HTTPException(409, 'A newer session exists; reset the latest client instead')
            if action == 'delete' and not row['can_delete']:
                raise HTTPException(409, 'Cancel the active session before deleting it')
            if action == 'cancel' and not row['can_cancel'] and row['stage'] != 'cancelled':
                raise HTTPException(409, 'Client has already ended')
            from . import routines
            routine=routines.for_boot(db,uid)
            if routine and routine['stage'] not in ('complete','cancelled') and action in ('cancel','reset'):
                routines.cancel(db,routine,revoke_session=False)
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_runs'").fetchone():
                active = db.execute("SELECT inventory FROM workflow_runs WHERE mac=? AND stage NOT IN ('complete','failed','cancelled')", (row['mac'],)).fetchall()
                serial = serial_key(row['system_serial'])
                if any(not serial or not (other := serial_key(json.loads(w['inventory'] or '{}').get('system_serial'))) or other == serial for w in active):
                    raise HTTPException(409, 'An active erasure workflow owns this PC; stop it through Workflows first')
            affected = [r for r in rows if asset_key(r) == asset_key(row)] if action == 'reset' else [row]
            cancelled = []
            for item in affected:
                if item['pending']:
                    db.execute("UPDATE jobs SET stage='cancelled',updated=? WHERE id=?", (at, item['job_id']))
                    db.execute('DELETE FROM report_credentials WHERE job_id=?', (item['job_id'],))
                    db.execute('INSERT INTO events(job_id,stage,detail,timestamp) VALUES(?,?,?,?)',
                               (item['job_id'], 'cancelled', spec.reason, at))
                    cancelled.append(item['job_id'])
                if item['job_id'] and not item['has_report']:
                    db.execute('DELETE FROM report_credentials WHERE job_id=?', (item['job_id'],))
                # Preserve accepted reports and their duplicate-ack credentials.
                db.execute("UPDATE clients SET token_hash=NULL,expires=?,ticket_expires=?,countdown_until=NULL,rate=0,stage=? WHERE id=?",
                           (at, at, 'cancelled' if action=='cancel' or item['pending'] or not item['job_id'] else item['stage'], item['id']))
                if action == 'delete':
                    db.execute('INSERT OR IGNORE INTO client_retirements VALUES(?,?)', (item['id'], at))
                db.execute('INSERT INTO client_control_events(client_id,mac,job_id,action,reason,timestamp) VALUES(?,?,?,?,?,?)',
                           (item['id'], item['mac'], item['job_id'], action, spec.reason, at))
            return {'clientId': uid, 'action': action, 'cancelled_jobs': cancelled,
                    'next_step': 'The record is removed. Job reports remain in Reports.' if action=='delete' else 'Boot the client from the network to create a new deployment session. Select the assigned routine; reports from this attempt remain available.'}

    @app.post('/api/clients/{uid}/cancel')
    def cancel(uid: str, spec: Control, role: Role):
        return control(uid, 'cancel', spec, role)

    @app.post('/api/clients/{uid}/reset')
    def reset(uid: str, spec: Control, role: Role):
        return control(uid, 'reset', spec, role)

    @app.delete('/api/clients/{uid}')
    def delete(uid: str, spec: Control, role: Role):
        return control(uid, 'delete', spec, role)

    @app.post('/api/clients/bulk')
    def bulk(spec: BulkControl, role: Role):
        allow(role)
        if len({s.client_id for s in spec.sessions})!=len(spec.sessions):
            raise HTTPException(422,'Duplicate client selections')
        results=[]
        for session in spec.sessions:
            try:
                result=control(session.client_id,spec.action,Control(expected_job_id=session.expected_job_id,
                    reason='Operator requested bulk '+spec.action),role)
                results.append({'clientId':session.client_id,'ok':True,**result})
            except HTTPException as error:
                results.append({'clientId':session.client_id,'ok':False,'status':error.status_code,'error':error.detail})
        return {'results':results,'succeeded':sum(r['ok'] for r in results),'failed':sum(not r['ok'] for r in results)}
