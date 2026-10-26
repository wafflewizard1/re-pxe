"""Job-bound Windows commands and streamed browser-to-desktop transfers."""
from datetime import datetime, timedelta, timezone
from typing import Literal
import hashlib
import hmac
import json
import re
import uuid

from fastapi import HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from . import client_management

MAX_FILE = 100 * 1024**3
UUID = r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$'


def now():
    return datetime.now(timezone.utc)


def initialize(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS windows_agents(job_id TEXT PRIMARY KEY,
            client_id TEXT NOT NULL, version TEXT NOT NULL, seen TEXT NOT NULL, busy INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS windows_commands(id TEXT PRIMARY KEY,
            client_id TEXT NOT NULL, job_id TEXT NOT NULL, action TEXT NOT NULL,
            status TEXT NOT NULL, created TEXT NOT NULL, expires TEXT NOT NULL,
            claimed TEXT, filename TEXT NOT NULL DEFAULT '', size INTEGER NOT NULL DEFAULT 0,
            sha256 TEXT NOT NULL DEFAULT '', result TEXT NOT NULL DEFAULT '{}');
        CREATE INDEX IF NOT EXISTS windows_commands_job ON windows_commands(job_id,created);
    ''')


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Power(Input):
    action: Literal['restart', 'shutdown']
    expected_job_id: str = Field(pattern=UUID)


class Poll(Input):
    client_version: str = Field(pattern=r'^[0-9]{1,5}\.[0-9]{1,5}\.[0-9]{1,5}$')
    busy: bool = Field(strict=True)
    accept_commands: bool = Field(default=True, strict=True)


class Result(Input):
    status: Literal['completed', 'failed']
    path: str = Field(default='', max_length=2048)
    error: str = Field(default='', max_length=1000)


def filename_valid(value):
    return (value not in ('.', '..') and 0 < len(value.encode('utf-16-le')) <= 400
            and not re.search(r'[\x00-\x1f\x7f<>:"/\\|?*]', value)
            and not value.endswith((' ', '.'))
            and not re.fullmatch(r'(CON|PRN|AUX|NUL|COM[0-9¹²³]|LPT[0-9¹²³])(?:\..*)?', value, re.I))


def client_for(db, uid, job_id=None):
    row = db.execute('SELECT * FROM clients WHERE id=?', (uid,)).fetchone()
    if not row:
        raise HTTPException(404, 'Client not found')
    latest = db.execute('SELECT id FROM clients WHERE mac=? ORDER BY created DESC,rowid DESC LIMIT 1', (row['mac'],)).fetchone()
    cap = db.execute('SELECT * FROM report_credentials WHERE job_id=?', (row['job_id'],)).fetchone()
    job = db.execute('SELECT stage FROM jobs WHERE id=?', (row['job_id'],)).fetchone()
    if (not row['job_id'] or job_id and job_id != row['job_id'] or latest['id'] != uid
            or db.execute('SELECT 1 FROM client_retirements WHERE client_id=?', (uid,)).fetchone()
            or row['stage'] not in client_management.COMPLETION or not job
            or job['stage'] in client_management.TERMINAL or not cap or cap['payload_hash']
            or cap['expires'] <= now().isoformat()):
        raise HTTPException(409, 'This client has no active Windows completion session')
    return row


def authenticate(db, job_id, token):
    cap = db.execute('SELECT * FROM report_credentials WHERE job_id=?', (job_id,)).fetchone()
    if (not cap or not hmac.compare_digest(cap['token_hash'], hashlib.sha256(token.encode()).hexdigest())
            or cap['expires'] <= now().isoformat() or cap['payload_hash']):
        raise HTTPException(401, 'Active job credential required')
    row = db.execute('SELECT id FROM clients WHERE job_id=?', (job_id,)).fetchone()
    if not row:
        raise HTTPException(404, 'Client not found')
    return client_for(db, row['id'], job_id)


def expire(db, root=None):
    db.execute("UPDATE windows_commands SET status='expired' WHERE status IN ('queued','running') AND expires<=?", (now().isoformat(),))
    if root:
        for row in db.execute("SELECT id FROM windows_commands WHERE action='file' AND status='expired'"):
            (root/'windows-controls'/row['id']).unlink(missing_ok=True)


def projection(row):
    out = {k: row[k] for k in ('id', 'job_id', 'action', 'status', 'created', 'expires', 'filename', 'size', 'sha256')}
    out['result'] = json.loads(row['result'])
    return out


def available_agent(db, row):
    agent = db.execute('SELECT * FROM windows_agents WHERE job_id=?', (row['job_id'],)).fetchone()
    if not agent or agent['seen'] < (now() - timedelta(seconds=60)).isoformat():
        raise HTTPException(409, 'Windows controls require an online Windows client version 0.14.0 or newer')
    return agent


def concurrent_files(version):
    return tuple(map(int, version.split('.'))) >= (0,14,1)


def file_agent(db, row):
    agent = available_agent(db, row)
    if agent['busy'] and not concurrent_files(agent['version']):
        raise HTTPException(409, 'Update this Windows client to 0.14.1 to transfer files during driver work')
    return agent


def queue(db, row, action, filename='', size=0, sha256=''):
    expire(db)
    available_agent(db, row)
    if db.execute("SELECT count(*) FROM windows_commands WHERE job_id=? AND status IN ('queued','running')", (row['job_id'],)).fetchone()[0] >= 32:
        raise HTTPException(409, 'Wait for pending Windows commands to finish')
    uid = str(uuid.uuid4())
    db.execute('INSERT INTO windows_commands(id,client_id,job_id,action,status,created,expires,filename,size,sha256) VALUES(?,?,?,?,?,?,?,?,?,?)',
               (uid, row['id'], row['job_id'], action, 'queued', now().isoformat(), (now()+timedelta(hours=2)).isoformat(), filename, size, sha256))
    return db.execute('SELECT * FROM windows_commands WHERE id=?', (uid,)).fetchone()


def install(app, store, Role, allow, Credential):
    @app.get('/api/clients/{uid}/windows')
    def status(uid: str, role: Role):
        allow(role)
        with store().db() as db:
            row = db.execute('SELECT job_id FROM clients WHERE id=?', (uid,)).fetchone()
            if not row: raise HTTPException(404, 'Client not found')
            expire(db, store().root)
            reason = ''
            agent = None
            try: agent = available_agent(db, client_for(db, uid))
            except HTTPException as exc: reason = exc.detail
            commands = [projection(r) for r in db.execute('SELECT * FROM windows_commands WHERE client_id=? ORDER BY created DESC LIMIT 50', (uid,))]
            return dict(available=agent is not None, busy=bool(agent and agent['busy']),
                        files_during_drivers=bool(agent and concurrent_files(agent['version'])), job_id=row['job_id'],
                        reason=reason, commands=commands)

    @app.post('/api/clients/{uid}/windows/commands', status_code=201)
    def power(uid: str, spec: Power, role: Role):
        allow(role)
        with store().db() as db:
            row = client_for(db, uid, spec.expected_job_id)
            return projection(queue(db, row, spec.action))

    @app.post('/api/clients/{uid}/windows/files', status_code=201)
    async def upload(uid: str, request: Request, role: Role,
                     filename: str = Query(min_length=1, max_length=200), expected_job_id: str = Query(pattern=UUID)):
        allow(role)
        if not filename_valid(filename): raise HTTPException(422, 'Choose a valid Windows desktop filename')
        if request.headers.get('content-type', '').split(';')[0] != 'application/octet-stream':
            raise HTTPException(415, 'Binary file upload required')
        with store().db() as db:
            row = client_for(db, uid, expected_job_id)
            file_agent(db, row)
        directory = store().root/'windows-controls'
        directory.mkdir(exist_ok=True)
        partial = directory/(str(uuid.uuid4())+'.part')
        size = 0
        sha = hashlib.sha256()
        destination = None
        try:
            with partial.open('xb') as stream:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > MAX_FILE: raise HTTPException(413, 'File exceeds 100 GiB')
                    sha.update(chunk)
                    stream.write(chunk)
            if not size: raise HTTPException(422, 'Choose a nonempty file')
            with store().db() as db:
                row = client_for(db, uid, expected_job_id)
                file_agent(db, row)
                command = queue(db, row, 'file', filename, size, sha.hexdigest())
                destination = directory/command['id']
                partial.replace(destination)
                response = projection(command)
            return response
        except BaseException:
            partial.unlink(missing_ok=True)
            if destination: destination.unlink(missing_ok=True)
            raise

    @app.post('/api/client-jobs/{job_id}/windows/poll')
    def poll(job_id: str, spec: Poll, token: Credential):
        if tuple(map(int, spec.client_version.split('.'))) < (0,14,0):
            raise HTTPException(409, 'Update the Windows client to version 0.14.0 or newer')
        with store().db() as db:
            row = authenticate(db, job_id, token)
            expire(db, store().root)
            db.execute('INSERT INTO windows_agents VALUES(?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET version=excluded.version,seen=excluded.seen,busy=excluded.busy',
                       (job_id, row['id'], spec.client_version, now().isoformat(), int(spec.busy)))
            command = None
            if spec.accept_commands:
                command = db.execute("""SELECT * FROM windows_commands WHERE job_id=? AND status IN ('queued','running')
                    AND (?=0 OR (action='file' AND ?=1))
                    ORDER BY CASE WHEN action='file' THEN 0 ELSE 1 END,
                             CASE WHEN status='running' THEN 0 ELSE 1 END, created LIMIT 1""",
                    (job_id, int(spec.busy), int(concurrent_files(spec.client_version)))).fetchone()
                if command:
                    db.execute("UPDATE windows_commands SET status='running',claimed=? WHERE id=?", (now().isoformat(), command['id']))
            result = projection(command) if command else None
            if result and result['action'] == 'file': result['download_path'] = '/api/client-jobs/'+job_id+'/windows/files/'+result['id']
            return dict(job_id=job_id, command=result)

    @app.get('/api/client-jobs/{job_id}/windows/files/{command_id}')
    def download(job_id: str, command_id: str, token: Credential):
        with store().db() as db:
            authenticate(db, job_id, token)
            expire(db, store().root)
            command = db.execute("SELECT * FROM windows_commands WHERE id=? AND job_id=? AND action='file' AND status='running'", (command_id, job_id)).fetchone()
            if not command: raise HTTPException(404, 'Active file transfer not found')
        return FileResponse(store().root/'windows-controls'/command['id'], media_type='application/octet-stream')

    @app.post('/api/client-jobs/{job_id}/windows/commands/{command_id}/result')
    def result(job_id: str, command_id: str, spec: Result, token: Credential):
        with store().db() as db:
            authenticate(db, job_id, token)
            command = db.execute('SELECT * FROM windows_commands WHERE id=? AND job_id=?', (command_id, job_id)).fetchone()
            if not command: raise HTTPException(404, 'Command not found')
            payload = json.dumps(spec.model_dump(), sort_keys=True)
            if command['status'] in ('completed', 'failed'):
                if command['status'] != spec.status or command['result'] != payload: raise HTTPException(409, 'Command result already recorded')
            elif command['status'] == 'running':
                db.execute('UPDATE windows_commands SET status=?,result=? WHERE id=?', (spec.status, payload, command_id))
            else: raise HTTPException(409, 'Command was not claimed')
        if command['action'] == 'file': (store().root/'windows-controls'/command['id']).unlink(missing_ok=True)
        return dict(accepted=True, id=command_id)
