"""Single-instance catalog and resumable uploads. No disk imaging occurs here."""
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal
import fcntl
import hashlib
import hmac
import json
import os
import re
import shutil
import sqlite3
import secrets
import stat
import subprocess
import uuid
import xml.etree.ElementTree as ET
import zipfile

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import field_validator, model_validator, BaseModel, ConfigDict, Field
from . import pxe, driver_library, driver_sessions, driver_management, server_info, file_workspace, workflows, capabilities, routines
from . import job_reports, branding, report_settings, database_recovery
from . import portal_updates
from .durable import atomic_json
from .auth import WebsiteAuth
from .driver_inf import amd64_catalog, MAX_INF_BYTES

CHUNK = 16 * 1024 * 1024
STAGES = ['created', 'preflight', 'disk-prepared', 'image-applied', 'boot-configured',
          'drivers-staged', 'agent-staged', 'awaiting-windows', 'completing',
          'ready-for-oobe', 'complete']
TERMINAL = {'failed', 'cancelled', 'complete'}


def now():
    return datetime.now(timezone.utc).isoformat()


class Model(BaseModel):
    model_config = ConfigDict(extra='forbid')


class DriverSelection(Model):
    ids: list[str] = Field(min_length=1, max_length=500)

    @field_validator('ids')
    @classmethod
    def selected_ids(cls, value):
        if any(not re.fullmatch(r'[a-f0-9-]{36}', uid) for uid in value):
            raise ValueError('Invalid driver ID')
        return list(dict.fromkeys(value))


class DriverGroup(DriverSelection):
    name: str = Field(default='', max_length=80)


class WebsiteSettings(Model):
    site_name: str = Field(default='re-pxe', min_length=1, max_length=80)
    refresh_seconds: int = Field(default=3, ge=3, le=60)
    driver_stale_days: int = Field(default=90, ge=1, le=3650)
    notes: str = Field(default='', max_length=2000)


class ModelTag(Model):
    manufacturer: str = Field(min_length=1, max_length=120)
    model: str = Field(min_length=1, max_length=120)


class ReviewedTag(ModelTag):
    windows_build: str = Field(pattern=r'^[0-9]{4,6}$')
    hardware_test_passed: bool
    validation_note: str = Field(min_length=10, max_length=2000)


class Upload(Model):
    kind: Literal['image', 'drivers']
    name: str = Field(min_length=1, max_length=120)
    filename: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$')
    size: int = Field(gt=0)
    sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    architecture: Literal['amd64'] = 'amd64'
    image_index: int = Field(default=1, ge=1, le=100)
    make: str = Field(default='', max_length=120)
    model: str = Field(default='', max_length=120)
    windows_version: str = Field(default='', max_length=120)
    reference_preparation: str = Field(default='', max_length=1000)
    boot_critical_inf_paths: list[str] = Field(default_factory=list, max_length=256)
    model_tags: list[ModelTag] = Field(default_factory=list, max_length=64)

    @model_validator(mode='after')
    def image_size(self):
        if self.kind == 'image' and self.size > 100 * 1024**3:
            raise ValueError('Windows images must be at most 100 GiB')
        return self


class Publication(Model):
    validation_note: str = Field(min_length=10, max_length=2000)
    hardware_test_passed: bool = False


class NewJob(Model):
    image_id: str
    target_identity: str = Field(min_length=1, max_length=300)
    device_model: str = Field(default='', max_length=200)


class JobEvent(Model):
    stage: Literal['preflight', 'disk-prepared', 'image-applied', 'boot-configured',
                   'drivers-staged', 'agent-staged', 'awaiting-windows', 'completing',
                   'ready-for-oobe', 'complete', 'failed', 'cancelled', 'needs-operator']
    detail: str = Field(default='', max_length=2000)
    event_id: str | None = Field(default=None, pattern=r'^[a-z0-9-]{1,64}$')


class JobReport(Model):
    result: Literal['complete', 'failed', 'cancelled']
    summary: str = Field(min_length=1, max_length=4000)
    driver_problems: list[str] = Field(default_factory=list, max_length=100)
    recovery_available: bool


class ReportCredential(Model):
    lifetime_hours: int = Field(default=24, ge=1, le=168)


class DeploymentProfile(Model):
    name: str = Field(min_length=1, max_length=120)
    windows_label: str = Field(min_length=1, max_length=120)
    image_id: str = Field(pattern=r'^[a-f0-9-]{36}$')
    driver_ids: list[str] = Field(default_factory=list, max_length=32)
    driver_source: Literal['explicit', 'automatic'] = 'explicit'
    windows_update_timeout_seconds: int = Field(default=900, ge=60, le=1800, strict=True)
    windows_update_max_attempts: int = Field(default=2, ge=1, le=3, strict=True)
    # Generic images remain selectable on other hardware; only a matching
    # package is attached for the installed Windows Audit Mode agent.
    hardware_neutral: bool = False
    selection_mode: Literal['choose-version', 'assigned-device'] = 'choose-version'
    recovery_mib: int = Field(default=1536, ge=1024, le=8192)
    boot_ex: bool = False


class ProfileAvailability(Model):
    enabled: bool


class DeviceAssignment(Model):
    target_identity: str = Field(pattern=r'^serial:[^\s\x00-\x1f][^\x00-\x1f]{0,292}$')
    profile_id: str = Field(pattern=r'^[a-f0-9-]{36}$')


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        for part in ['database', 'images/published', 'images/staging', 'drivers/packages',
                     'drivers/manifests', 'uploads/staging', 'reports', 'boot']:
            (self.root / part).mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            if [row[0] for row in db.execute('PRAGMA quick_check')] != ['ok']:
                raise RuntimeError('Catalog integrity check failed; preserve files and restore a verified snapshot')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY);
                INSERT OR IGNORE INTO schema_version VALUES(1);
                CREATE TABLE IF NOT EXISTS uploads(
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, status TEXT NOT NULL,
                    received INTEGER NOT NULL, spec TEXT NOT NULL, created TEXT NOT NULL,
                    error TEXT, metadata TEXT);
                CREATE TABLE IF NOT EXISTS artifacts(
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL,
                    sha256 TEXT NOT NULL, size INTEGER NOT NULL, metadata TEXT NOT NULL,
                    published TEXT NOT NULL, validation_note TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs(
                    id TEXT PRIMARY KEY, stage TEXT NOT NULL, image_id TEXT NOT NULL,
                    target_identity TEXT NOT NULL, device_model TEXT NOT NULL,
                    created TEXT NOT NULL, updated TEXT NOT NULL, report TEXT);
                CREATE TABLE IF NOT EXISTS event_requests(
                    job_id TEXT NOT NULL, event_id TEXT NOT NULL,
                    payload_hash TEXT NOT NULL, response TEXT NOT NULL,
                    PRIMARY KEY(job_id,event_id));
                CREATE TABLE IF NOT EXISTS report_credentials(
                    job_id TEXT PRIMARY KEY, token_hash TEXT NOT NULL,
                    expires TEXT NOT NULL, payload_hash TEXT);
                CREATE TABLE IF NOT EXISTS events(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
                    stage TEXT NOT NULL, detail TEXT NOT NULL, timestamp TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS deployment_profiles(
                    id TEXT PRIMARY KEY, spec TEXT NOT NULL, enabled INTEGER NOT NULL,
                    created TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS device_assignments(
                    id TEXT PRIMARY KEY, target_identity TEXT NOT NULL UNIQUE,
                    profile_id TEXT NOT NULL, updated TEXT NOT NULL);
            ''')
        with self.db() as db:
            pxe.initialize(db)
        driver_library.initialize(self)
        server_info.initialize(self)
        branding.initialize(self)
        report_settings.initialize(self)
        database_recovery.initialize(self.root)
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS database_restore_events(id TEXT PRIMARY KEY,snapshot TEXT NOT NULL,actor TEXT NOT NULL,created TEXT NOT NULL)')
        file_workspace.initialize(self)
        workflows.initialize(self)
        capabilities.initialize(self)
        routines.initialize(self)
        # A killed validation is retryable. The file length is the durable upload offset.
        with self.db() as db:
            db.execute("UPDATE uploads SET status='receiving' WHERE status='validating'")
            rows = db.execute("SELECT id FROM uploads WHERE status='receiving'").fetchall()
            for row in rows:
                path = self.part(row['id'])
                db.execute('UPDATE uploads SET received=? WHERE id=?',
                           (path.stat().st_size if path.exists() else 0, row['id']))
            reports = db.execute('SELECT id,report FROM jobs WHERE report IS NOT NULL').fetchall()
            manifests = db.execute("SELECT id,metadata FROM artifacts WHERE kind='drivers' AND id NOT IN (SELECT archive_id FROM driver_archives_purged)").fetchall()
        # SQLite is authoritative if a process died before writing a file projection.
        for row in reports:
            atomic_json(self.root / 'reports' / f'{row["id"]}.json', json.loads(row['report']))
        for row in manifests:
            atomic_json(self.root / 'drivers/manifests' / f'{row["id"]}.json', json.loads(row['metadata']))

    @contextmanager
    def db(self):
        # All server transactions share this lock; restore acquires it exclusively.
        with (self.root / 'database/catalog.access.lock').open('a') as access:
            fcntl.flock(access, fcntl.LOCK_SH)
            db = sqlite3.connect(self.root / 'database/catalog.sqlite', timeout=30)
            try:
                db.row_factory = sqlite3.Row
                if db.execute('PRAGMA journal_mode=WAL').fetchone()[0] != 'wal':
                    raise RuntimeError('Catalog requires SQLite WAL journaling')
                db.execute('PRAGMA synchronous=FULL')
                db.execute('PRAGMA foreign_keys=ON')
                db.execute('BEGIN IMMEDIATE')
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()
                fcntl.flock(access, fcntl.LOCK_UN)

    def part(self, uid):
        return self.root / 'uploads/staging' / f'{uid}.part'

    @contextmanager
    def lock(self, uid):
        if uid == 'file-workspace':
            from .file_workspace import workspace_lock
            with workspace_lock(self):
                yield
            return
        # Only internally generated UUIDs reach this function.
        with (self.root / 'uploads/staging' / f'{uid}.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def upload(self, uid, role):
        with self.db() as db:
            row = db.execute('SELECT * FROM uploads WHERE id=?', (uid,)).fetchone()
        if not row:
            raise HTTPException(404, 'Upload not found')
        if role != 'admin' and row['owner'] != role:
            raise HTTPException(403, 'Upload belongs to a different role')
        return dict(row)


def public_upload(row):
    result = dict(row)
    result['spec'] = json.loads(result['spec'])
    result['metadata'] = json.loads(result['metadata']) if result['metadata'] else None
    return result


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def verify_wim(path, spec):
    """Verify all image resources, then obtain the real WIM index/OS metadata."""
    if not shutil.which('wimlib-imagex'):
        raise HTTPException(503, 'WIM validator unavailable')
    checked = subprocess.run(['wimlib-imagex', 'verify', str(path)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             timeout=3600)
    if checked.returncode:
        raise ValueError('WIM integrity validation failed')
    info = subprocess.run(['wimlib-imagex', 'info', str(path), '--xml'],
                          capture_output=True, check=True, timeout=60)
    root = ET.fromstring(info.stdout.decode('utf-16') if info.stdout.startswith(b'\xff\xfe')
                         else info.stdout)
    images = []
    selected = None
    for node in root.findall('IMAGE'):
        item = {'index': int(node.attrib['INDEX']), 'name': node.findtext('NAME', ''),
                'architecture': node.findtext('WINDOWS/ARCH'),
                'edition': node.findtext('WINDOWS/EDITIONID'),
                'build': node.findtext('WINDOWS/VERSION/BUILD'),
                'total_bytes': int(node.findtext('TOTALBYTES', '0'))}
        images.append(item)
        if item['index'] == spec['image_index']:
            selected = item
    if not selected:
        raise ValueError('Requested image index does not exist')
    if selected['architecture'] != '9':
        raise ValueError('Selected image must contain AMD64 Windows metadata')
    return {'images': images, 'selected_image': selected, 'verification': 'wimlib verify'}


def unpack_drivers(path, destination):
    """Bounded extraction of ordinary exported INF/CAT packages; never executes files."""
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir()
    files, seen = [], set()
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        if not entries or len(entries) > 10000:
            raise ValueError('Driver archive must contain 1 to 10000 entries')
        expanded = sum(entry.file_size for entry in entries if not entry.is_dir())
        if shutil.disk_usage(destination).free < expanded + 64 * 1024**2:
            raise HTTPException(507, 'Insufficient storage to extract driver archive')
        for entry in entries:
            name = entry.filename
            from .driver_paths import validate as validate_driver_path
            portable = validate_driver_path(name, entry.is_dir())
            key = portable.casefold()
            if key in seen:
                raise ValueError('Case-insensitive duplicate driver archive path: ' + repr(name))
            seen.add(key)
            mode = entry.external_attr >> 16
            if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)):
                raise ValueError('Driver archive contains special files')
            output = destination.joinpath(*PurePosixPath(name).parts)
            if entry.is_dir():
                output.mkdir(parents=True, exist_ok=True)
                continue
            output.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(entry) as src, output.open('xb') as dst:
                written = 0
                while chunk := src.read(1024 * 1024):
                    written += len(chunk)
                    if written > entry.file_size:
                        raise ValueError('Archive entry exceeds declared size')
                    dst.write(chunk)
                if written != entry.file_size:
                    raise ValueError('Archive entry differs from declared size')
            files.append({'path': name, 'size': output.stat().st_size, 'sha256': digest(output)})
    infs = [f for f in files if f['path'].lower().endswith('.inf')]
    if not infs:
        raise ValueError('No INF driver packages found')
    lookup = {f['path'].casefold() for f in files}
    for inf in infs:
        if inf['size'] > MAX_INF_BYTES:
            raise ValueError('INF exceeds 16 MiB limit')
        raw = (destination / inf['path']).read_bytes()
        text = raw.decode('utf-16') if raw.startswith((b'\xff\xfe', b'\xfe\xff')) else raw.decode('utf-8-sig', errors='replace')
        cat = amd64_catalog(text)
        if '/' in cat or '\\' in cat or ':' in cat or cat in ('.', '..'):
            raise ValueError('Unsafe catalog reference in INF')
        expected = str(PurePosixPath(inf['path']).parent / cat).casefold()
        if expected not in lookup:
            raise ValueError(f'INF references a missing AMD64 catalog: {inf["path"][:200]} → {cat[:120]}')
    return {'files': files, 'inf_count': len(infs),
            'verification': 'archive paths, sizes, hashes and INF catalog presence; signatures not verified'}


def create_app(data_root=None, tokens=None, *, secure_cookies=True, allow_http=None):
    root = data_root or os.environ.get('REPXE_DATA', './data')
    if allow_http is None:
        allow_http = os.environ.get('REPXE_ALLOW_HTTP', '').lower() == 'true'
    keys = tokens or {r: os.environ.get(f'REPXE_{r.upper()}_TOKEN', '')
                      for r in ('admin', 'capture', 'agent', 'deploy')}

    @asynccontextmanager
    async def lifespan(app):
        if any(len(keys.get(r, '')) < 32 for r in ('admin', 'capture', 'agent', 'deploy')):
            raise RuntimeError('Four independent tokens of at least 32 characters are required')
        if len(set(keys.values())) != 4:
            raise RuntimeError('Role tokens must be distinct')
        app.state.store = Store(root)
        app.state.website_auth = WebsiteAuth(app.state.store, secure_cookies, allow_http)
        builtin_stop = capabilities.start_builtin(app.state.store)
        try:
            yield
        finally:
            builtin_stop.set()

    app = FastAPI(title='re-pxe Server', version='0.1.0', lifespan=lifespan,
                  docs_url=None, redoc_url=None)
    app.add_middleware(portal_updates.MaintenanceMiddleware)

    def authorize(request: Request, authorization: Annotated[str | None, Header()] = None):
        value = authorization.removeprefix('Bearer ') if authorization and authorization.startswith('Bearer ') else ''
        for role, key in keys.items():
            if value and hmac.compare_digest(value.encode('utf-8'), key.encode('utf-8')):
                return role
        if authorization:
            raise HTTPException(401, 'Invalid bearer credential', headers={'WWW-Authenticate': 'Bearer'})
        return app.state.website_auth.authorize(request)

    Role = Annotated[str, Depends(authorize)]

    def allow(role, *roles):
        if role not in ('admin', *roles):
            raise HTTPException(403, 'Role is not permitted for this action')

    def store():
        return app.state.store

    WebsiteAuth.install(app, Role, allow)
    portal_updates.install(app, Role, allow)
    job_reports.install(app, store, Role, allow)
    from . import customer_reports
    customer_reports.install(app, store)

    @app.get('/health')
    def health():
        with store().db() as db:
            db.execute('SELECT 1').fetchone()
        return {'status': 'ok', 'version': '0.1.0', 'revision': os.environ.get('REPXE_REVISION', 'unknown')}

    @app.get('/api/session')
    def session(request: Request, response: Response, role: Role):
        response.headers['Cache-Control'] = 'no-store'
        if getattr(request.state, 'web_user', None):
            return app.state.website_auth.session(request)
        return {'role': role}

    @app.get('/api/overview')
    def overview(role: Role):
        allow(role)
        with store().db() as db:
            counts = {kind: db.execute('SELECT count(*) FROM artifacts WHERE kind=?', (kind,)).fetchone()[0]
                      for kind in ('image', 'drivers')}
            counts['drivers'] = db.execute('SELECT count(*) FROM driver_library WHERE id NOT IN (SELECT driver_id FROM driver_retired)').fetchone()[0]
            counts['jobs'] = db.execute('SELECT count(*) FROM jobs').fetchone()[0]
            counts['pending'] = db.execute("SELECT count(*) FROM uploads WHERE status='ready'").fetchone()[0]
        usage = shutil.disk_usage(store().root)
        return {'counts': counts, 'storage': {'free': usage.free, 'total': usage.total},
                'capabilities': {'server': True, 'winpe_runtime': True, 'windows_agent': True,
                                 'deployment_profiles': True, 'technician_ui': True,
                                 'simplified_pxe_client': True, 'pxe_boot': (store().root / 'boot/pxe/current.json').is_file() and json.loads((store().root / 'boot/pxe/current.json').read_text()).get('validated', False)}}

    @app.post('/api/uploads', status_code=201)
    def new_upload(spec: Upload, role: Role):
        allow(role, 'capture' if spec.kind == 'image' else 'agent')
        if spec.model_tags:
            spec = spec.model_copy(update={'make': spec.model_tags[0].manufacturer, 'model': spec.model_tags[0].model})
        if not spec.filename.lower().endswith('.wim' if spec.kind == 'image' else '.zip'):
            raise HTTPException(422, 'Images require .wim; driver uploads require .zip')
        if spec.kind == 'drivers' and (not spec.make or not spec.model or not spec.windows_version):
            raise HTTPException(422, 'Drivers require make, model and Windows version')
        if spec.kind == 'image' and not spec.reference_preparation.strip():
            raise HTTPException(422, 'Declare the reference preparation before uploading an image')
        if shutil.disk_usage(store().root).free < spec.size + 256 * 1024**2:
            raise HTTPException(507, 'Insufficient storage')
        uid = str(uuid.uuid4())
        store().part(uid).touch(exist_ok=False)
        with store().db() as db:
            db.execute('INSERT INTO uploads VALUES(?,?,?,?,?,?,?,?)',
                       (uid, role, 'receiving', 0, spec.model_dump_json(), now(), None, None))
        return public_upload(store().upload(uid, role))

    @app.get('/api/uploads')
    def list_uploads(role: Role):
        allow(role)
        with store().db() as db:
            return [public_upload(r) for r in db.execute('SELECT * FROM uploads ORDER BY created DESC LIMIT 200')]

    @app.get('/api/uploads/{uid}')
    def get_upload(uid: str, role: Role):
        return public_upload(store().upload(uid, role))

    @app.patch('/api/uploads/{uid}/content')
    async def upload_chunk(uid: str, request: Request, role: Role,
                           upload_offset: Annotated[int, Header(ge=0)]):
        row = store().upload(uid, role)
        if row['status'] != 'receiving':
            raise HTTPException(409, 'Upload is not receiving data')
        # Bound each chunk in memory before taking a blocking filesystem lock.
        payload = bytearray()
        async for chunk in request.stream():
            if len(payload) + len(chunk) > CHUNK:
                raise HTTPException(413, 'Maximum chunk size is 16 MiB')
            payload.extend(chunk)
        if not payload:
            raise HTTPException(422, 'Empty upload chunk')
        from starlette.concurrency import run_in_threadpool

        def append():
            with store().lock(uid):
                row = store().upload(uid, role)
                if row['status'] != 'receiving':
                    raise HTTPException(409, 'Upload is not receiving data')
                path = store().part(uid)
                length = path.stat().st_size
                if length != upload_offset:
                    raise HTTPException(409, f'Expected upload offset {length}')
                if length + len(payload) > json.loads(row['spec'])['size']:
                    raise HTTPException(413, 'Chunk exceeds declared file size')
                if shutil.disk_usage(store().root).free < len(payload) + 64 * 1024**2:
                    raise HTTPException(507, 'Insufficient storage')
                try:
                    with path.open('ab') as stream:
                        stream.write(payload)
                        stream.flush()
                        os.fsync(stream.fileno())
                except OSError:
                    with path.open('r+b') as stream:
                        stream.truncate(length)
                    raise HTTPException(507, 'Cannot persist upload chunk')
                with store().db() as db:
                    db.execute('UPDATE uploads SET received=? WHERE id=?', (length + len(payload), uid))
                return {'id': uid, 'received': length + len(payload)}
        return await run_in_threadpool(append)

    @app.post('/api/uploads/{uid}/finish')
    def finish_upload(uid: str, role: Role):
        store().upload(uid, role)
        with store().lock(uid):
            row = store().upload(uid, role)
            if row['status'] in ('ready', 'published'):
                if row['status']=='ready' and json.loads(row['spec'])['kind']=='drivers':
                    return publish_driver_locked(uid, role)
                return public_upload(row)
            if row['status'] != 'receiving':
                raise HTTPException(409, 'Upload cannot be finalized')
            spec = json.loads(row['spec'])
            path = store().part(uid)
            if path.stat().st_size != spec['size']:
                raise HTTPException(409, 'Upload is incomplete')
            with store().db() as db:
                db.execute("UPDATE uploads SET status='validating' WHERE id=?", (uid,))
            try:
                if digest(path) != spec['sha256']:
                    raise ValueError('SHA-256 does not match')
                meta = (verify_wim(path, spec) if spec['kind'] == 'image' else
                        unpack_drivers(path, store().root / 'uploads/staging' / f'{uid}.drivers'))
                critical=spec.get('boot_critical_inf_paths',[])
                infs={f['path'] for f in meta.get('files',[]) if f['path'].lower().endswith('.inf')}
                if critical and (spec['kind']!='drivers' or len(set(critical))!=len(critical) or not set(critical)<=infs):
                    raise ValueError('Boot-critical drivers must be selected INF paths from this package')
            except HTTPException:
                with store().db() as db:
                    db.execute("UPDATE uploads SET status='receiving' WHERE id=?", (uid,))
                raise
            except (ValueError, zipfile.BadZipFile, RuntimeError, OSError, subprocess.SubprocessError, ET.ParseError) as exc:
                # Do not expose server paths or command output in API errors.
                message = str(exc) if isinstance(exc, ValueError) else 'Artifact validation failed'
                with store().db() as db:
                    db.execute("UPDATE uploads SET status='rejected', error=? WHERE id=?", (message, uid))
                raise HTTPException(422, message)
            with store().db() as db:
                db.execute("UPDATE uploads SET status='ready', metadata=?, error=NULL WHERE id=?", (json.dumps(meta), uid))
            if spec['kind']=='drivers':
                return publish_driver_locked(uid, role)
            return public_upload(store().upload(uid, role))

    @app.delete('/api/uploads/{uid}', status_code=204)
    def cancel_upload(uid: str, role: Role):
        store().upload(uid, role)
        with store().lock(uid):
            row = store().upload(uid, role)
            if (row['status'] == 'published' or
                (store().root / 'images/published' / uid).exists() or
                (store().root / 'drivers/packages' / uid).exists()):
                raise HTTPException(409, 'Published artifacts are immutable')
            store().part(uid).unlink(missing_ok=True)
            shutil.rmtree(store().root / 'uploads/staging' / f'{uid}.drivers', ignore_errors=True)
            with store().db() as db:
                db.execute("UPDATE uploads SET status='cancelled' WHERE id=?", (uid,))

    def publish_locked(uid, decision, role, automatic=False):
        with store().lock('driver-library'):
            row = store().upload(uid, role)
            if row['status'] == 'published':
                with store().db() as db:
                    imported=db.execute('SELECT result FROM driver_archive_imports WHERE archive_id=?',(uid,)).fetchone()
                return {'id': uid, 'status': 'published', **({'drivers':json.loads(imported['result'])} if imported else {})}
            if row['status'] != 'ready':
                raise HTTPException(409, 'Only validated uploads can be published')
            spec = json.loads(row['spec'])
            critical=spec.get('boot_critical_inf_paths', [])
            if critical:
                infs={f['path'] for f in json.loads(row['metadata']).get('files',[]) if f['path'].lower().endswith('.inf')}
                if spec['kind']!='drivers' or len(set(critical))!=len(critical) or not set(critical)<=infs:
                    raise HTTPException(422, 'Boot-critical drivers must be selected INF paths from this reviewed package')
            # One immutable directory per publication; directory rename is the file visibility boundary.
            base = store().root / ('images/published' if spec['kind'] == 'image' else 'drivers/packages')
            dest, temp = base / uid, base / f'.{uid}.pending'
            if not dest.exists():
                if temp.exists():
                    shutil.rmtree(temp)
                temp.mkdir()
                source = store().part(uid) if spec['kind'] == 'image' else store().root / 'uploads/staging' / f'{uid}.drivers'
                if spec['kind'] == 'image':
                    os.link(source, temp / 'image.wim')
                else:
                    shutil.copytree(source, temp / 'files')
                metadata = {**spec, **json.loads(row['metadata']), 'id': uid,
                            'published': now(), **decision.model_dump(),
                            **({'automatic_publication': True, 'installation_authorized': True} if automatic else {})}
                atomic_json(temp / 'metadata.json', metadata)
                os.rename(temp, dest)
            metadata = json.loads((dest / 'metadata.json').read_text())
            library_result = driver_library._import_archive(store(), metadata, dest / 'files') if spec['kind'] == 'drivers' else None
            with store().db() as db:
                db.execute('INSERT OR IGNORE INTO artifacts VALUES(?,?,?,?,?,?,?,?)',
                           (uid, spec['kind'], spec['name'], spec['sha256'], spec['size'],
                            json.dumps(metadata), metadata['published'], metadata['validation_note']))
                db.execute("UPDATE uploads SET status='published' WHERE id=?", (uid,))
            if spec['kind'] == 'drivers':
                atomic_json(store().root / 'drivers/manifests' / f'{uid}.json', metadata)
            store().part(uid).unlink(missing_ok=True)
            shutil.rmtree(store().root / 'uploads/staging' / f'{uid}.drivers', ignore_errors=True)
            return {'id': uid, 'status': 'published', **({'drivers': library_result} if library_result else {})}

    @app.post('/api/uploads/{uid}/publish')
    def publish(uid: str, decision: Publication, role: Role):
        allow(role)
        store().upload(uid, role)
        with store().lock(uid):
            drivers=json.loads(store().upload(uid,role)['spec'])['kind']=='drivers'
            if drivers:
                decision=Publication(hardware_test_passed=False,validation_note='Automatically published after archive integrity and AMD64 catalog validation.')
            return publish_locked(uid, decision, role, automatic=drivers)

    def publish_driver_locked(uid, role):
        publish_locked(uid, Publication(hardware_test_passed=False,
            validation_note='Automatically published after archive integrity and AMD64 catalog validation.'), role, automatic=True)
        return public_upload(store().upload(uid, role))

    @app.get('/api/images')
    def images(role: Role):
        with store().db() as db:
            return [json.loads(r['metadata']) for r in db.execute("SELECT metadata FROM artifacts WHERE kind='image' ORDER BY published DESC")]

    def public_profile(row):
        spec=json.loads(row['spec'])
        # Older immutable profiles predate generic-image selection.
        spec.setdefault('hardware_neutral', False)
        spec.setdefault('driver_source', 'explicit')
        spec.setdefault('windows_update_timeout_seconds', 900)
        spec.setdefault('windows_update_max_attempts', 2)
        return {**spec, 'id': row['id'], 'enabled': bool(row['enabled']),
                'created': row['created'],
                'requires_disk_confirmation': True,
                'driver_policy': 'server-library-then-windows-update' if spec['driver_source']=='automatic' else 'stop-if-missing',
                'finish': 'oobe-and-shutdown'}

    def profile_artifacts(db, spec):
        if spec.get('driver_source') == 'automatic' and spec['driver_ids']:
            raise HTTPException(422, 'Automatic drivers use the server library; do not attach explicit packages')
        row = db.execute("SELECT metadata FROM artifacts WHERE id=? AND kind='image'", (spec['image_id'],)).fetchone()
        if not row:
            raise HTTPException(422, 'Choose a published Windows image')
        image = json.loads(row['metadata'])
        if image.get('architecture') != 'amd64' or not re.fullmatch(r'[0-9]{4,6}', str(image.get('selected_image', {}).get('build', ''))):
            raise HTTPException(422, 'Profile requires an AMD64 image with a verified Windows build')
        drivers, hardware = [], set()
        for uid in spec['driver_ids']:
            row = db.execute("SELECT metadata FROM artifacts WHERE id=? AND kind='drivers'", (uid,)).fetchone()
            if not row or not driver_management.archive_available(db, uid):
                raise HTTPException(422, 'Choose available published driver packages')
            driver = json.loads(row['metadata'])
            match = (driver['make'].casefold(), driver['model'].casefold())
            if (match in hardware or not (driver.get('hardware_test_passed') or driver.get('installation_authorized')) or
                    driver.get('architecture') != 'amd64' or
                    driver['windows_version'] != str(image['selected_image']['build'])):
                raise HTTPException(422, 'Driver sets must match the exact Windows build and have one reviewed set per model')
            hardware.add(match)
            drivers.append(driver)
        return image, drivers

    @app.get('/api/profiles')
    def profiles(role: Role):
        allow(role, 'deploy')
        with store().db() as db:
            return [public_profile(r) for r in db.execute('SELECT * FROM deployment_profiles ORDER BY created DESC')
                    if role == 'admin' or r['enabled']]

    @app.post('/api/profiles', status_code=201)
    def new_profile(spec: DeploymentProfile, role: Role):
        allow(role)
        uid = str(uuid.uuid4())
        with store().db() as db:
            profile_artifacts(db, spec.model_dump())
            db.execute('INSERT INTO deployment_profiles VALUES(?,?,?,?)', (uid, spec.model_dump_json(), 0, now()))
            row = db.execute('SELECT * FROM deployment_profiles WHERE id=?', (uid,)).fetchone()
        return public_profile(row)

    @app.post('/api/profiles/{uid}/availability')
    def profile_availability(uid: str, spec: ProfileAvailability, role: Role):
        allow(role)
        with store().db() as db:
            row = db.execute('SELECT * FROM deployment_profiles WHERE id=?', (uid,)).fetchone()
            if not row:
                raise HTTPException(404, 'Profile not found')
            if spec.enabled:
                profile_artifacts(db, json.loads(row['spec']))
            db.execute('UPDATE deployment_profiles SET enabled=? WHERE id=?', (spec.enabled, uid))
            row = db.execute('SELECT * FROM deployment_profiles WHERE id=?', (uid,)).fetchone()
        return public_profile(row)

    @app.get('/api/device-assignments')
    def device_assignments(role: Role):
        allow(role)
        with store().db() as db:
            return [dict(r) for r in db.execute('SELECT * FROM device_assignments ORDER BY updated DESC')]

    @app.post('/api/device-assignments')
    def assign_device(spec: DeviceAssignment, role: Role):
        allow(role)
        with store().db() as db:
            row = db.execute('SELECT * FROM deployment_profiles WHERE id=?', (spec.profile_id,)).fetchone()
            if not row or not row['enabled'] or json.loads(row['spec'])['selection_mode'] != 'assigned-device':
                raise HTTPException(422, 'Choose an enabled profile that uses dashboard device assignments')
            profile_artifacts(db, json.loads(row['spec']))
            db.execute('''INSERT INTO device_assignments VALUES(?,?,?,?)
                       ON CONFLICT(target_identity) DO UPDATE SET profile_id=excluded.profile_id,updated=excluded.updated''',
                       (str(uuid.uuid4()), spec.target_identity, spec.profile_id, now()))
            row = db.execute('SELECT * FROM device_assignments WHERE target_identity=?', (spec.target_identity,)).fetchone()
        return dict(row)

    @app.delete('/api/device-assignments/{uid}', status_code=204)
    def remove_assignment(uid: str, role: Role):
        allow(role)
        with store().db() as db:
            if not db.execute('SELECT 1 FROM device_assignments WHERE id=?', (uid,)).fetchone():
                raise HTTPException(404, 'Assignment not found')
            db.execute('DELETE FROM device_assignments WHERE id=?', (uid,))

    def boot_options_for(db, manufacturer, model, target_identity):
        choices = []
        assignment = db.execute('SELECT * FROM device_assignments WHERE target_identity=?', (target_identity,)).fetchone()
        rows = db.execute('SELECT * FROM deployment_profiles WHERE enabled=1 ORDER BY created DESC').fetchall()
        if assignment and not any(r['id'] == assignment['profile_id'] for r in rows):
            raise HTTPException(409, 'Assigned profile is disabled; review its dashboard assignment')
        for row in rows:
            spec = json.loads(row['spec'])
            if assignment and row['id'] != assignment['profile_id']:
                continue
            if not assignment and spec['selection_mode'] == 'assigned-device':
                continue
            image, drivers = profile_artifacts(db, spec)
            automatic = spec.get('driver_source') == 'automatic'
            if automatic:
                # Published packages are immutable. Prefer the latest reviewed
                # exact model/build package, independently of the Windows image.
                drivers = [json.loads(r['metadata']) for r in db.execute(
                    "SELECT metadata FROM artifacts WHERE kind='drivers' ORDER BY published DESC,id DESC")]
                drivers = [d for d in drivers if driver_management.archive_available(db, d['id']) and (d.get('hardware_test_passed') or d.get('installation_authorized')) and
                           d.get('architecture') == image['architecture'] and
                           d.get('windows_version') == str(image['selected_image']['build'])]
            matched = [d for d in drivers if (d['make'].casefold(), d['model'].casefold()) == (manufacturer.casefold(), model.casefold())]
            if drivers and not matched and not automatic and not spec.get('hardware_neutral', False):
                if assignment:
                    raise HTTPException(409, 'Assigned profile has no reviewed driver set for this device')
                continue
            choices.append({**public_profile(row), 'image_sha256': image['sha256'],
                            'image_index': image['image_index'], 'windows_build': str(image['selected_image']['build']),
                            'driver_id': matched[0]['id'] if matched else None})
        return {'schemaVersion': 1, 'choices': choices,
                'selectedProfileId': assignment['profile_id'] if assignment else None,
                'requiresDiskConfirmation': True}

    @app.get('/api/boot-options')
    def boot_options(role: Role, manufacturer: str = '', model: str = '', target_identity: str = ''):
        allow(role, 'deploy')
        if any(len(s) > 300 for s in (manufacturer, model, target_identity)):
            raise HTTPException(422, 'Device identity exceeds supported length')
        with store().db() as db:
            return boot_options_for(db, manufacturer, model, target_identity)

    @app.get('/api/drivers')
    def drivers(role: Role, make: str | None = None, model: str | None = None):
        with store().db() as db:
            rows = [json.loads(r['metadata']) for r in db.execute("SELECT metadata FROM artifacts WHERE kind='drivers' ORDER BY published DESC")]
            rows = [r for r in rows if driver_management.archive_available(db, r['id'])]
        return [r for r in rows if (make is None or r['make'].casefold() == make.casefold())
                and (model is None or r['model'].casefold() == model.casefold())]

    @app.get('/api/driver-library')
    def driver_catalog(role: Role):
        allow(role)
        with store().db() as db:
            return driver_library.catalog(db)

    @app.post('/api/driver-library/delete-preview')
    def driver_delete_preview(body: DriverSelection, role: Role):
        allow(role)
        with store().db() as db:
            return driver_management.preview(db, body.ids)

    @app.post('/api/driver-library/delete')
    def driver_delete(body: DriverSelection, role: Role):
        allow(role)
        return driver_management.remove(store(), body.ids)

    @app.put('/api/driver-library/group')
    def driver_group(body: DriverGroup, role: Role):
        allow(role)
        with store().db() as db:
            driver_management.preview(db, body.ids)
            db.executemany('INSERT INTO driver_groups VALUES(?,?) ON CONFLICT(driver_id) DO UPDATE SET name=excluded.name', [(uid, body.name.strip()) for uid in body.ids])
        return {'count': len(body.ids), 'group': body.name.strip()}

    @app.get('/api/server-info')
    def server_status(role: Role):
        allow(role)
        return server_info.snapshot(store())

    @app.get('/api/website-settings')
    def website_settings(role: Role):
        allow(role)
        return server_info.settings(store())

    @app.put('/api/website-settings')
    def save_website_settings(body: WebsiteSettings, role: Role):
        allow(role)
        if not body.site_name.strip():
            raise HTTPException(422, 'Website name is required')
        value = {**body.model_dump(), 'site_name': body.site_name.strip()}
        with store().db() as db:
            db.execute('UPDATE website_settings SET value=? WHERE id=1', (json.dumps(value),))
        return value

    @app.post('/api/driver-library/{uid}/tags')
    def tag_driver(uid: str, tag: ReviewedTag, role: Role):
        allow(role)
        if not tag.hardware_test_passed or not tag.manufacturer.strip() or not tag.model.strip():
            raise HTTPException(422, 'Model tags require a successful hardware test declaration')
        manufacturer, model = tag.manufacturer.strip(), tag.model.strip()
        with store().db() as db:
            if not db.execute('SELECT 1 FROM driver_library WHERE id=? AND id NOT IN (SELECT driver_id FROM driver_retired)', (uid,)).fetchone():
                raise HTTPException(404, 'Driver not found')
            db.execute('''INSERT INTO driver_model_tags VALUES(?,?,?,?,?,?,1,?)
                ON CONFLICT(driver_id,manufacturer_key,model_key,windows_build)
                DO UPDATE SET reviewed=1,note=excluded.note''',
                       (uid, manufacturer, model, tag.windows_build, manufacturer.casefold(), model.casefold(), tag.validation_note))
        return {'id': uid, 'status': 'tagged'}

    @app.get('/api/drivers/{uid}/client-manifest')
    def driver_client_manifest(uid: str, role: Role):
        with store().db() as db:
            row = db.execute("SELECT metadata FROM artifacts WHERE id=? AND kind='drivers'", (uid,)).fetchone()
            available = driver_management.archive_available(db, uid)
        if not row or not available:
            raise HTTPException(404, 'Published driver package unavailable')
        meta = json.loads(row['metadata'])
        if not re.fullmatch(r'[0-9]{4,6}', meta['windows_version']):
            raise HTTPException(422, 'Native packages require an exact numeric Windows build')
        manifest = {'schemaVersion': 1, 'packageId': uid, 'architecture': meta['architecture'],
                'manufacturer': meta['make'], 'model': meta['model'], 'windowsBuild': meta['windows_version'],
                'review': {'hardwareTestPassed': bool(meta.get('hardware_test_passed') or meta.get('installation_authorized')), 'note': meta['validation_note']},
                'files': meta['files'], 'infPaths': [f['path'] for f in meta['files'] if f['path'].lower().endswith('.inf')]}
        if meta.get('boot_critical_inf_paths'):
            manifest['bootCriticalInfPaths']=meta['boot_critical_inf_paths']
        return manifest

    @app.get('/api/images/{uid}/content')
    def image_content(uid: str, role: Role):
        with store().db() as db:
            row = db.execute("SELECT * FROM artifacts WHERE id=? AND kind='image'", (uid,)).fetchone()
        if not row:
            raise HTTPException(404, 'Image not found')
        return FileResponse(store().root / 'images/published' / row['id'] / 'image.wim', filename='image.wim')

    @app.post('/api/jobs', status_code=201)
    def create_job(spec: NewJob, role: Role):
        allow(role, 'deploy')
        uid = str(uuid.uuid4())
        timestamp = now()
        with store().db() as db:
            if not db.execute("SELECT 1 FROM artifacts WHERE id=? AND kind='image'", (spec.image_id,)).fetchone():
                raise HTTPException(422, 'Select a published image')
            db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?)',
                       (uid, 'created', spec.image_id, spec.target_identity, spec.device_model, timestamp, timestamp, None))
            db.execute('INSERT INTO events(job_id,stage,detail,timestamp) VALUES(?,?,?,?)', (uid, 'created', '', timestamp))
        return {'id': uid, 'stage': 'created'}

    @app.get('/api/jobs')
    def jobs(role: Role):
        allow(role, 'deploy', 'agent')
        with store().db() as db:
            return [dict(r) for r in db.execute('SELECT * FROM jobs ORDER BY created DESC LIMIT 200')]

    @app.get('/api/jobs/{uid}')
    def get_job(uid: str, role: Role):
        allow(role, 'deploy', 'agent')
        with store().db() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (uid,)).fetchone()
            if not row:
                raise HTTPException(404, 'Job not found')
            return {**dict(row), 'events': [dict(r) for r in db.execute('SELECT * FROM events WHERE job_id=? ORDER BY seq', (uid,))]}

    @app.post('/api/jobs/{uid}/events')
    def job_event(uid: str, event: JobEvent, role: Role):
        allow(role, 'deploy', 'agent')
        if role == 'agent' and event.stage in STAGES[:8]:
            raise HTTPException(403, 'Completion agent cannot perform WinPE stages')
        if role == 'deploy' and event.stage in STAGES[8:]:
            raise HTTPException(403, 'Deployment client cannot perform completion stages')
        timestamp = now()
        payload_hash = hashlib.sha256(event.model_dump_json().encode()).hexdigest()
        with store().db() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (uid,)).fetchone()
            if not row:
                raise HTTPException(404, 'Job not found')
            if event.event_id:
                previous = db.execute('SELECT * FROM event_requests WHERE job_id=? AND event_id=?', (uid, event.event_id)).fetchone()
                if previous:
                    if not hmac.compare_digest(previous['payload_hash'], payload_hash):
                        raise HTTPException(409, 'Event ID was already used for different content')
                    return json.loads(previous['response'])
            current = row['stage']
            if current in TERMINAL:
                raise HTTPException(409, 'Job is terminal')
            if event.stage == 'complete':
                raise HTTPException(409, 'Submit a completion report to complete the job')
            history = db.execute("SELECT stage FROM events WHERE job_id=? AND stage!='needs-operator' ORDER BY seq DESC LIMIT 1", (uid,)).fetchone()['stage']
            allowed = {'failed', 'cancelled', 'needs-operator'}
            if history in STAGES and STAGES.index(history) + 1 < len(STAGES):
                allowed.add(STAGES[STAGES.index(history) + 1])
            if event.stage not in allowed:
                raise HTTPException(409, 'Invalid stage transition')
            if role == 'agent' and event.stage in STAGES[:8]:
                raise HTTPException(403, 'Completion agent cannot perform WinPE stages')
            if role == 'deploy' and event.stage in STAGES[8:]:
                raise HTTPException(403, 'Deployment client cannot perform completion stages')
            db.execute('UPDATE jobs SET stage=?, updated=? WHERE id=?', (event.stage, timestamp, uid))
            db.execute('INSERT INTO events(job_id,stage,detail,timestamp) VALUES(?,?,?,?)', (uid, event.stage, event.detail, timestamp))
            if event.event_id:
                db.execute('INSERT INTO event_requests VALUES(?,?,?,?)',
                           (uid, event.event_id, payload_hash, json.dumps({'id': uid, 'stage': event.stage})))
        return {'id': uid, 'stage': event.stage}

    @app.post('/api/jobs/{uid}/report')
    def job_report(uid: str, report: JobReport, role: Role):
        allow(role, 'agent')
        with store().db() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (uid,)).fetchone()
            if not row:
                raise HTTPException(404, 'Job not found')
            if row['report']:
                raise HTTPException(409, 'Report already exists')
            if report.result == 'complete' and (row['stage'] != 'ready-for-oobe' or report.driver_problems):
                raise HTTPException(409, 'Completion requires ready-for-oobe with no driver problems')
            if row['stage'] in TERMINAL and row['stage'] != report.result:
                raise HTTPException(409, 'Report conflicts with terminal job state')
            image = db.execute('SELECT sha256,metadata FROM artifacts WHERE id=?', (row['image_id'],)).fetchone()
            payload = {**report.model_dump(), 'job_id': uid, 'target_identity': row['target_identity'],
                       'image_id': row['image_id'], 'image_sha256': image['sha256'],
                       'image_index': json.loads(image['metadata'])['image_index'], 'timestamp': now(),
                       'events': [dict(r) for r in db.execute('SELECT * FROM events WHERE job_id=? ORDER BY seq', (uid,))]}
            # Files are projections of the canonical SQLite report, reconciled on a retry.
            db.execute('UPDATE jobs SET stage=?, report=?, updated=? WHERE id=?', (report.result, json.dumps(payload), now(), uid))
            db.execute('INSERT INTO events(job_id,stage,detail,timestamp) VALUES(?,?,?,?)', (uid, report.result, 'Report submitted', now()))
        atomic_json(store().root / 'reports' / f'{uid}.json', payload)
        return payload

    @app.post('/api/jobs/{uid}/report-credential', status_code=201)
    def report_credential(uid: str, spec: ReportCredential, role: Role):
        allow(role, 'deploy')
        token = secrets.token_urlsafe(48)
        expires = (datetime.now(timezone.utc) + timedelta(hours=spec.lifetime_hours)).isoformat()
        with store().db() as db:
            job = db.execute('SELECT * FROM jobs WHERE id=?', (uid,)).fetchone()
            if not job:
                raise HTTPException(404, 'Job not found')
            if job['report'] or job['stage'] in TERMINAL:
                raise HTTPException(409, 'Job already ended')
            # Reissuing invalidates the previous credential; secrets are never stored in plaintext.
            db.execute('INSERT OR REPLACE INTO report_credentials VALUES(?,?,?,NULL)',
                       (uid, hashlib.sha256(token.encode()).hexdigest(), expires))
        return {'schemaVersion': 1, 'jobId': uid, 'token': token, 'expires': expires,
                'path': f'/api/client-jobs/{uid}/report'}

    @app.post('/api/client-jobs/{uid}/report')
    async def native_report(uid: str, request: Request):
        credential = request.headers.get('authorization', '')
        if not credential.startswith('Bearer ') or len(credential) > 300:
            raise HTTPException(401, 'Job report credential required')
        token_hash = hashlib.sha256(credential[7:].encode()).hexdigest()
        with store().db() as db:
            cap = db.execute('SELECT * FROM report_credentials WHERE job_id=?', (uid,)).fetchone()
        if not cap or not hmac.compare_digest(token_hash, cap['token_hash']):
            raise HTTPException(401, 'Invalid job report credential')
        if datetime.fromisoformat(cap['expires']) < datetime.now(timezone.utc):
            raise HTTPException(401, 'Job report credential expired')
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > 512 * 1024:
                raise HTTPException(413, 'Maximum native report size is 512 KiB')
            raw.extend(chunk)
        digest = hashlib.sha256(raw).hexdigest()
        try:
            snapshot = json.loads(raw)
            stage = snapshot['stage']
            if (snapshot['schemaVersion'] != 1 or stage not in {'complete', 'failed', 'needs-operator'}
                    or not isinstance(snapshot['target']['identity'], str)
                    or type(snapshot['imageIndex']) is not int
                    or not isinstance(snapshot['imageSha256'], str)
                    or snapshot['generalizationPerformed'] is not False
                    or not isinstance(snapshot['drivers']['problems'], list)):
                raise ValueError('Unsupported native report')
            final_specs = None
            hardware = snapshot.get('hardware', {})
            if isinstance(hardware, dict) and 'specs' in hardware:
                from .device_inventory import Inventory
                final_specs = Inventory.model_validate(hardware['specs']).model_dump()
            if stage == 'complete':
                boots = snapshot['bootObservations']
                history = [event['stage'] for event in snapshot['history']]
                maximum = snapshot.get('windowsUpdatePolicy', {}).get('maxAttempts', 0)
                if type(maximum) is not int or not 0 <= maximum <= 3:
                    raise ValueError('Invalid driver reboot bound')
                console = snapshot.get('driverConsole') is True
                if console:
                    attempts = snapshot.get('windowsUpdateAttempts', [])
                    if (not isinstance(attempts, list) or len(attempts) > 3 or
                            any(attempt.get('status') == 'running' for attempt in attempts) or
                            snapshot.get('driverRebootRequired') is not False or
                            snapshot.get('driverInstallationMayBeInProgress', False) is not False or
                            snapshot.get('driverVerifiedBoot') != boots[-1]['bootId'] or
                            snapshot.get('windowsUpdatePolicyRestored') is not True):
                        raise ValueError('Incomplete driver console checkpoint')
                finish = snapshot.get('technicianFinish')
                technician_finish = False
                if finish is not None:
                    if (not console or not isinstance(finish, dict) or finish.get('requested') is not True or
                            type(finish.get('unresolvedDeviceCount')) is not int or
                            finish['unresolvedDeviceCount'] != len(snapshot['drivers']['problems'])):
                        raise ValueError('Invalid technician finish evidence')
                    for field in ('requestedUtc', 'sealedUtc'):
                        value = finish.get(field)
                        if not isinstance(value, str) or len(value) > 64 or datetime.fromisoformat(value).tzinfo is None:
                            raise ValueError('Invalid technician finish timestamp')
                    technician_finish = True
                if maximum and not console:
                    attempts=snapshot.get('windowsUpdateAttempts', [])
                    if (not isinstance(attempts,list) or len(attempts)>maximum or len(boots)!=2+len(attempts) or
                            snapshot.get('windowsUpdatePolicyRestored') is not True or
                            any(a.get('status')!='succeeded' or a.get('result',{}).get('automaticReboot') is not False for a in attempts)):
                        raise ValueError('Incomplete Windows Update driver evidence')
                if ((snapshot['drivers']['problems'] and not technician_finish) or not 2 <= len(boots) <= (32 if console else 2 + maximum) or
                        len({b['bootId'] for b in boots}) != len(boots)
                        or snapshot['startupTriggerRemoved'] is not True
                        or snapshot['sealedImageState'] != 'IMAGE_STATE_SPECIALIZE_RESEAL_TO_OOBE'
                        or snapshot['oobeBootVerified'] is not False
                        or history[-3:] != ['ready-for-oobe', 'sealing-oobe', 'complete']):
                    raise ValueError('Incomplete native completion evidence')
        except (ValueError, KeyError, TypeError, IndexError):
            raise HTTPException(422, 'Invalid native report evidence')
        with store().db() as db:
            # Authenticate again under the transaction: rotation cannot race report acceptance.
            cap = db.execute('SELECT * FROM report_credentials WHERE job_id=?', (uid,)).fetchone()
            if not cap or not hmac.compare_digest(token_hash, cap['token_hash']):
                raise HTTPException(401, 'Invalid job report credential')
            row = db.execute('SELECT * FROM jobs WHERE id=?', (uid,)).fetchone()
            if not row:
                raise HTTPException(404, 'Job not found')
            customer_reports.validate_finish_evidence(db,uid,snapshot)
            image = db.execute('SELECT * FROM artifacts WHERE id=?', (row['image_id'],)).fetchone()
            meta = json.loads(image['metadata'])
            if (snapshot['target']['identity'] != row['target_identity'] or snapshot['imageSha256'] != image['sha256']
                    or snapshot['imageIndex'] != meta.get('image_index', 1)):
                raise HTTPException(409, 'Native report does not match assigned image and target')
            if cap['payload_hash']:
                if not hmac.compare_digest(cap['payload_hash'], digest):
                    raise HTTPException(409, 'A different native report is already accepted')
                return {'accepted': True, 'jobId': uid, 'sha256': digest, 'duplicate': True}
            if row['report'] or row['stage'] not in {'awaiting-windows', 'completing', 'ready-for-oobe', 'needs-operator'}:
                raise HTTPException(409, 'Job is not awaiting Windows completion')
            timestamp = now()
            if final_specs is not None:
                from .device_inventory import serial_key
                clients = db.execute('SELECT c.id,i.inventory FROM clients c LEFT JOIN client_inventory i ON i.client_id=c.id WHERE c.job_id=?', (uid,)).fetchall()
                for client in clients:
                    previous = json.loads(client['inventory']) if client['inventory'] else {}
                    expected = serial_key(previous.get('system', {}).get('serial'))
                    supplied = serial_key(final_specs['system']['serial'])
                    if expected and supplied != expected:
                        raise HTTPException(409, 'Completion inventory does not match the enrolled PC serial')
                for client in clients:
                    db.execute('INSERT INTO client_inventory VALUES(?,?,?) ON CONFLICT(client_id) DO UPDATE SET inventory=excluded.inventory,captured_at=excluded.captured_at',
                               (client['id'], json.dumps(final_specs), timestamp))
            payload = {'job_id': uid, 'target_identity': row['target_identity'], 'image_id': row['image_id'],
                       'image_sha256': image['sha256'], 'image_index': meta.get('image_index', 1),
                       'result': stage, 'reported_utc': timestamp, 'native_sha256': digest,
                       'native': snapshot, 'summary': (f"Finished by technician with {len(snapshot['drivers']['problems'])} unresolved devices" if stage == 'complete' and snapshot.get('technicianFinish') else snapshot.get('error') or f'Native completion: {stage}')[:4000],
                       'driver_problems': snapshot['drivers']['problems'],
                       'recovery_available': snapshot.get('recovery', {}).get('status') == 'enabled-online'}
            db.execute('UPDATE jobs SET stage=?,updated=?,report=? WHERE id=?',
                       (stage, timestamp, json.dumps(payload), uid))
            driver_management.report_used(db, uid, snapshot, timestamp)
            db.execute('INSERT OR IGNORE INTO driver_history_imported VALUES(?)', (uid,))
            db.execute('UPDATE report_credentials SET payload_hash=? WHERE job_id=?', (digest, uid))
            db.execute('INSERT INTO events(job_id,stage,detail,timestamp) VALUES(?,?,?,?)',
                       (uid, stage, 'Authenticated native report accepted', timestamp))
        if stage == 'complete' and snapshot.get('technicianFinish'):
            # Final reporting revokes the scoped uploader. Retire its unfinished transfers;
            # immutable published packages and local collection archives remain available.
            with store().db() as db:
                unfinished = [r['id'] for r in db.execute("SELECT id FROM uploads WHERE owner=? AND status NOT IN ('published','cancelled')", ('job:' + uid,))]
            for upload_id in unfinished:
                try:
                    cancel_upload(upload_id, 'job:' + uid)
                except (HTTPException, OSError):
                    pass  # An accepted completion must remain idempotent after cleanup failure.
        atomic_json(store().root / 'reports' / f'{uid}.json', payload)
        return {'accepted': True, 'jobId': uid, 'sha256': digest, 'duplicate': False}

    @app.get('/api/jobs/{uid}/report')
    def get_report(uid: str, role: Role):
        allow(role, 'agent', 'deploy')
        with store().db() as db:
            row = db.execute('SELECT report FROM jobs WHERE id=?', (uid,)).fetchone()
        if not row or not row['report']:
            raise HTTPException(404, 'Report not found')
        return json.loads(row['report'])

    database_recovery.install(app, store, Role, allow)
    report_settings.install(app, store, Role, allow)
    branding.install(app, store, Role, allow)
    capabilities.install(app, store, Role, allow)
    routines.install(app, store, Role, allow, profile_artifacts)
    workflows.install(app, store, Role, allow)
    file_workspace.install(app, store, Role, allow)
    driver_sessions.install(app, store, Upload, new_upload, upload_chunk, finish_upload, get_upload, cancel_upload)
    pxe.install(app, store, Role, allow, boot_options_for, profile_artifacts, create_job,
                report_credential, job_event, driver_client_manifest)

    static = Path(__file__).parent / 'static'
    app.mount('/static', StaticFiles(directory=static), name='static')

    @app.get('/', include_in_schema=False)
    def dashboard():
        return FileResponse(static / 'index.html')

    @app.get('/docs', include_in_schema=False)
    def api_reference():
        return FileResponse(static / 'api.html')

    return app


app = create_app()
