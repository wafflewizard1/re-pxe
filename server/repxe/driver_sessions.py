"""Driver console endpoints restricted to one active deployment credential."""
from datetime import datetime, timezone
import hashlib
import hmac
import json

from fastapi import Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from typing import Annotated

from . import driver_library, driver_management


class Hardware(BaseModel):
    model_config = ConfigDict(extra='forbid')
    manufacturer: str = Field(min_length=1, max_length=120)
    model: str = Field(min_length=1, max_length=120)


def install(app, store, Upload, new_upload, upload_chunk, finish_upload, get_upload, cancel_upload):
    def credential(uid, request):
        authorization = request.headers.get('authorization', '')
        if not authorization.startswith('Bearer ') or not 32 <= len(authorization[7:]) <= 256:
            raise HTTPException(401, 'Active job credential required')
        digest = hashlib.sha256(authorization[7:].encode()).hexdigest()
        with store().db() as db:
            cap = db.execute('SELECT * FROM report_credentials WHERE job_id=?', (uid,)).fetchone()
            job = db.execute('SELECT * FROM jobs WHERE id=?', (uid,)).fetchone()
            if not cap or not hmac.compare_digest(cap['token_hash'], digest) or cap['payload_hash'] or datetime.fromisoformat(cap['expires']) <= datetime.now(timezone.utc):
                raise HTTPException(401, 'Active job credential required')
            if not job or job['stage'] not in ('awaiting-windows', 'completing', 'needs-operator', 'ready-for-oobe'):
                raise HTTPException(409, 'Deployment is not awaiting driver completion')
        return job

    def binding(uid):
        with store().db() as db:
            row = db.execute('SELECT * FROM driver_console_bindings WHERE job_id=?', (uid,)).fetchone()
        if not row:
            raise HTTPException(409, 'Connect the driver console first')
        return dict(row)

    @app.post('/api/client-jobs/{uid}/drivers/catalog')
    def catalog(uid: str, body: Hardware, request: Request):
        job = credential(uid, request)
        with store().db() as db:
            enrollment = db.execute('SELECT manufacturer,model FROM clients WHERE job_id=?', (uid,)).fetchone()
            manufacturer, model = body.manufacturer.strip(), body.model.strip()
            if not manufacturer or not model or (enrollment and (manufacturer.casefold(), model.casefold()) != (enrollment['manufacturer'].casefold(), enrollment['model'].casefold())):
                raise HTTPException(403, 'Hardware differs from this deployment')
            row = db.execute("SELECT metadata FROM artifacts WHERE id=? AND kind='image'", (job['image_id'],)).fetchone()
            if not row:
                raise HTTPException(409, 'Deployment image is unavailable')
            build = str(json.loads(row['metadata'])['selected_image']['build'])
            previous = db.execute('SELECT * FROM driver_console_bindings WHERE job_id=?', (uid,)).fetchone()
            if previous and (previous['manufacturer'].casefold(), previous['model'].casefold(), previous['windows_build']) != (manufacturer.casefold(), model.casefold(), build):
                raise HTTPException(403, 'Driver console binding differs')
            db.execute('INSERT OR IGNORE INTO driver_console_bindings VALUES(?,?,?,?)', (uid, manufacturer, model, build))
            entries = driver_library.catalog(db, manufacturer, model, build)
            db.executemany('INSERT OR IGNORE INTO driver_job_pins VALUES(?,?)', [(uid, entry['id']) for entry in entries])
            manifests = [driver_library.manifest(db, entry['id'], manufacturer, model, build) for entry in entries]
        return {'jobId': uid, 'hardware': {'manufacturer': manufacturer, 'model': model, 'build': build},
                'drivers': [{'name': entry['name'], 'provider': entry['provider'], 'class': entry['class'],
                             'version': entry['version'], 'manifest': manifest}
                            for entry, manifest in zip(entries, manifests)]}

    @app.get('/api/client-jobs/{uid}/drivers/{driver_id}/files/{index}')
    def content(uid: str, driver_id: str, index: int, request: Request):
        credential(uid, request)
        hardware = binding(uid)
        with store().db() as db:
            manifest = driver_library.manifest(db, driver_id, hardware['manufacturer'], hardware['model'], hardware['windows_build'])
        if index < 0 or index >= len(manifest['files']):
            raise HTTPException(404, 'Driver file not found')
        entry = manifest['files'][index]
        path = store().root / 'drivers/blobs' / entry['sha256']
        if not path.is_file() or path.stat().st_size != entry['size']:
            raise HTTPException(409, 'Driver content is unavailable')
        with store().db() as db:
            driver_management.used(db, [driver_id], uid)
        return FileResponse(path, media_type='application/octet-stream')

    @app.post('/api/client-jobs/{uid}/driver-uploads', status_code=201)
    def create_upload(uid: str, spec: Upload, request: Request):
        credential(uid, request)
        hardware = binding(uid)
        if spec.kind != 'drivers':
            raise HTTPException(403, 'This credential can upload only drivers')
        spec = spec.model_copy(update={'make': hardware['manufacturer'], 'model': hardware['model'],
                                      'windows_version': hardware['windows_build'], 'model_tags': [],
                                      'architecture': 'amd64', 'boot_critical_inf_paths': []})
        with store().lock('console-' + uid):
            with store().db() as db:
                count = db.execute("SELECT count(*) FROM uploads WHERE owner=? AND status NOT IN ('cancelled','published')", ('job:' + uid,)).fetchone()[0]
            if count >= 8:
                raise HTTPException(429, 'Review existing driver uploads before uploading more')
            uploaded = new_upload(spec, 'agent')
            with store().db() as db:
                db.execute('UPDATE uploads SET owner=? WHERE id=?', ('job:' + uid, uploaded['id']))
        return uploaded

    @app.get('/api/client-jobs/{uid}/driver-uploads/{upload_id}')
    def status(uid: str, upload_id: str, request: Request):
        credential(uid, request)
        return get_upload(upload_id, 'job:' + uid)

    @app.patch('/api/client-jobs/{uid}/driver-uploads/{upload_id}/content')
    async def chunk(uid: str, upload_id: str, request: Request, upload_offset: Annotated[int, Header(ge=0)]):
        credential(uid, request)
        return await upload_chunk(upload_id, request, 'job:' + uid, upload_offset)

    @app.post('/api/client-jobs/{uid}/driver-uploads/{upload_id}/finish')
    def finish(uid: str, upload_id: str, request: Request):
        credential(uid, request)
        return finish_upload(upload_id, 'job:' + uid)

    @app.post('/api/client-jobs/{uid}/driver-uploads/{upload_id}/cancel')
    def cancel(uid: str, upload_id: str, request: Request):
        credential(uid, request)
        role = 'job:' + uid
        row = store().upload(upload_id, role)
        if row['status'] == 'published':
            return {'id': upload_id, 'published': True}
        cancel_upload(upload_id, role)
        return {'id': upload_id, 'cancelled': True}
