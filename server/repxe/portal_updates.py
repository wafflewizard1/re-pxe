"""Admin-only bridge to the separately installed, narrowly scoped host updater."""
import fcntl
import http.client
import json
import os
from pathlib import Path
import socket

from fastapi import HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field


def control():
    return Path(os.environ.get('REPXE_UPDATER_CONTROL', '/run/re-pxe-updater'))


def maintenance():
    return (control() / 'maintenance.json').exists()


class Connection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(str(control() / 'control.sock'))


def request(method, path, body=None):
    connection = Connection('localhost', timeout=3)
    try:
        connection.request(method, path, json.dumps(body) if body is not None else None,
                           {'Content-Type': 'application/json'})
        response = connection.getresponse()
        data = response.read(16385)
        if len(data) > 16384:
            raise ValueError('Invalid updater response')
        result = json.loads(data)
        if response.status >= 400:
            raise HTTPException(response.status, result.get('detail', 'Updater request rejected'))
        return result
    except (OSError, ValueError, http.client.HTTPException):
        if method == 'GET':
            return {'enabled': False, 'busy': False, 'can_update': False,
                    'maintenance': maintenance(), 'phase': 'unavailable',
                    'detail': 'Host updater unavailable. Enable portal updates with deploy/setup.py --portal-updates on the Linux server.'}
        raise HTTPException(503, 'The host updater is unavailable. Check the re-pxe-updater service on the server.') from None
    finally:
        connection.close()


class Apply(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: str = Field(pattern=r'^[a-f0-9]{40}$')
    confirm: bool = Field(strict=True)


class Empty(BaseModel):
    model_config = ConfigDict(extra='forbid')


def install(app, Role, allow):
    @app.get('/api/system-updates')
    def status(role: Role, response: Response):
        allow(role)
        response.headers['Cache-Control'] = 'no-store'
        return request('GET', '/status')

    @app.post('/api/system-updates/check', status_code=202)
    def check(body: Empty, role: Role):
        allow(role)
        return request('POST', '/check', {})

    @app.post('/api/system-updates/apply', status_code=202)
    def apply(body: Apply, role: Role):
        allow(role)
        if not body.confirm:
            raise HTTPException(422, 'Confirm the service restart before updating')
        return request('POST', '/apply', body.model_dump())

    @app.post('/api/system-updates/recover', status_code=202)
    def recover(body: Empty, role: Role):
        allow(role)
        return request('POST', '/recover', {})


class MaintenanceMiddleware:
    """Drain full requests/background tasks before the host can stop writers.

    A separate lock file remains accessible through the read-only control mount.
    Status and health stay available while ordinary API/boot requests are held.
    """
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get('path', '')
        guarded = scope['type'] == 'http' and path.startswith(('/api/', '/pxe/')) and path not in (
            '/api/system-updates', '/api/system-updates/check', '/api/system-updates/apply',
            '/api/system-updates/recover', '/api/session')
        if not guarded:
            return await self.app(scope, receive, send)
        stream = None
        try:
            if maintenance():
                raise BlockingIOError
            try:
                stream = (control() / 'requests.lock').open('rb')
            except FileNotFoundError:
                # The optional updater is not installed.
                pass
            if stream:
                fcntl.flock(stream, fcntl.LOCK_SH | fcntl.LOCK_NB)
            if maintenance():
                raise BlockingIOError
        except BlockingIOError:
            if stream:
                stream.close()
            payload = json.dumps({'detail': 'Server update in progress. Retry when maintenance finishes.'}).encode()
            await send({'type': 'http.response.start', 'status': 503,
                        'headers': [(b'content-type', b'application/json'), (b'retry-after', b'5'), (b'cache-control', b'no-store')]})
            await send({'type': 'http.response.body', 'body': payload})
            return
        try:
            await self.app(scope, receive, send)
        finally:
            if stream:
                stream.close()
