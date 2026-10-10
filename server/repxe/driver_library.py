"""Individual driver packages, shared content blobs and reviewed model tags."""
import hashlib
import json
import os
from pathlib import PurePosixPath
import re
import shutil
import uuid

from fastapi import HTTPException
from . import driver_management
from .driver_inf import MAX_INF_BYTES


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def initialize(store):
    (store.root / 'drivers/blobs').mkdir(exist_ok=True)
    with store.db() as db:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS driver_library(
                id TEXT PRIMARY KEY, content_hash TEXT UNIQUE NOT NULL,
                metadata TEXT NOT NULL, created TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS driver_model_tags(
                driver_id TEXT NOT NULL REFERENCES driver_library(id),
                manufacturer TEXT NOT NULL, model TEXT NOT NULL, windows_build TEXT NOT NULL,
                manufacturer_key TEXT NOT NULL, model_key TEXT NOT NULL,
                reviewed INTEGER NOT NULL, note TEXT NOT NULL,
                PRIMARY KEY(driver_id,manufacturer_key,model_key,windows_build));
            CREATE TABLE IF NOT EXISTS driver_archive_imports(
                archive_id TEXT PRIMARY KEY, result TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS driver_console_bindings(
                job_id TEXT PRIMARY KEY REFERENCES jobs(id), manufacturer TEXT NOT NULL,
                model TEXT NOT NULL, windows_build TEXT NOT NULL);
        ''')
        driver_management.initialize(db)
        old = [json.loads(row['metadata']) for row in db.execute("SELECT metadata FROM artifacts WHERE kind='drivers'")]
    for metadata in old:
        import_archive(store, metadata, store.root / 'drivers/packages' / metadata['id'] / 'files')
    driver_management.backfill(store)


def inf_details(path):
    with path.open('rb') as stream:
        raw = stream.read(MAX_INF_BYTES)
    text = raw.decode('utf-16' if raw.startswith((b'\xff\xfe', b'\xfe\xff')) else 'utf-8-sig', errors='replace')
    sections, current = {}, ''
    for line in text.splitlines():
        line = line.split(';', 1)[0].strip()
        if line.startswith('[') and line.endswith(']'):
            current = line[1:-1].casefold()
        elif '=' in line:
            key, value = line.split('=', 1)
            sections.setdefault(current, {})[key.strip().casefold()] = value.strip().strip('"')[:200]
    values = sections.get('version', {})
    strings = sections.get('strings', {})
    def value(key):
        item = values.get(key, '')
        return strings.get(item.strip('%').casefold(), item) if item.startswith('%') else item
    return {'provider': value('provider'), 'class': value('class'), 'version': value('driverver')}


def import_archive(store, metadata, directory):
    with store.lock('driver-library'):
        return _import_archive(store, metadata, directory)


def _import_archive(store, metadata, directory):
    with store.db() as db:
        previous = db.execute('SELECT result FROM driver_archive_imports WHERE archive_id=?', (metadata['id'],)).fetchone()
        if previous:
            return json.loads(previous['result'])
    files = metadata['files']
    # PnPUtil exports each package to its own folder. Retain all supporting files
    # below the INF folder; shared payloads and multiple INFs reuse content blobs.
    blobs = store.root / 'drivers/blobs'
    for entry in files:
        source = directory / entry['path']
        if source.is_symlink() or not source.is_file() or source.stat().st_size != entry['size'] or sha(source) != entry['sha256']:
            raise RuntimeError('Reviewed driver content differs from its manifest')
        blob = blobs / entry['sha256']
        if not blob.exists():
            temporary = blobs / ('.' + str(uuid.uuid4()))
            try:
                shutil.copyfile(source, temporary)
                if sha(temporary) != entry['sha256']:
                    raise RuntimeError('Driver blob copy differs')
                temporary.chmod(0o444)
                os.replace(temporary, blob)
            finally:
                temporary.unlink(missing_ok=True)
        elif blob.stat().st_size != entry['size'] or sha(blob) != entry['sha256']:
            raise RuntimeError('Stored driver content differs from its manifest')
        if source.stat().st_ino != blob.stat().st_ino:
            temporary = source.with_name('.' + str(uuid.uuid4()))
            try:
                os.link(blob, temporary)
                os.replace(temporary, source)
            finally:
                temporary.unlink(missing_ok=True)
    result = {'new': 0, 'reused': 0, 'driver_ids': []}
    tags = metadata.get('model_tags') or [{'manufacturer': metadata['make'], 'model': metadata['model']}]
    with store.db() as db:
        for inf in (entry for entry in files if entry['path'].lower().endswith('.inf')):
            parent = PurePosixPath(inf['path']).parent
            grouped = [{**entry, 'path': str(PurePosixPath(entry['path']).relative_to(parent))}
                       for entry in files if PurePosixPath(entry['path']).is_relative_to(parent)]
            grouped.sort(key=lambda entry: entry['path'].casefold())
            inf_path = PurePosixPath(inf['path']).name
            canonical = {'files': [{**entry, 'path': entry['path'].casefold()} for entry in grouped], 'inf': inf_path.casefold()}
            content_hash = hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            existing = db.execute('SELECT id FROM driver_library WHERE content_hash=?', (content_hash,)).fetchone()
            driver_id = existing['id'] if existing else str(uuid.uuid4())
            if not existing:
                info = {'id': driver_id, 'name': inf_path, 'content_hash': content_hash,
                        'architecture': metadata['architecture'], 'files': grouped, 'inf_path': inf_path,
                        'size': sum(entry['size'] for entry in grouped), **inf_details(directory / inf['path'])}
                db.execute('INSERT INTO driver_library VALUES(?,?,?,?)',
                           (driver_id, content_hash, json.dumps(info), metadata['published']))
            result['reused' if existing else 'new'] += 1
            result['driver_ids'].append(driver_id)
            db.execute('DELETE FROM driver_retired WHERE driver_id=?', (driver_id,))
            for tag in tags:
                manufacturer, model = tag['manufacturer'].strip(), tag['model'].strip()
                db.execute('''INSERT INTO driver_model_tags VALUES(?,?,?,?,?,?,?,?)
                    ON CONFLICT(driver_id,manufacturer_key,model_key,windows_build)
                    DO UPDATE SET reviewed=max(reviewed,excluded.reviewed),
                    note=CASE WHEN excluded.reviewed AND (? OR NOT reviewed) THEN excluded.note ELSE note END''',
                           (driver_id, manufacturer, model, metadata['windows_version'], manufacturer.casefold(),
                            model.casefold(), int(bool(metadata.get('hardware_test_passed') or metadata.get('installation_authorized'))), metadata['validation_note'],int(bool(metadata.get('hardware_test_passed')))))
        db.execute('INSERT OR IGNORE INTO driver_archive_imports VALUES(?,?)', (metadata['id'], json.dumps(result)))
        driver_management.members(db, metadata['id'], result['driver_ids'])
    return result


def catalog(db, manufacturer=None, model=None, build=None):
    result = []
    for row in db.execute('SELECT * FROM driver_library WHERE id NOT IN (SELECT driver_id FROM driver_retired) ORDER BY created DESC,id'):
        tags = [{'manufacturer': tag['manufacturer'], 'model': tag['model'], 'windows_build': tag['windows_build'],
                 'reviewed': bool(tag['reviewed']), 'note': tag['note']}
                for tag in db.execute('SELECT * FROM driver_model_tags WHERE driver_id=? ORDER BY manufacturer,model,windows_build', (row['id'],))]
        matching = [tag for tag in tags if tag['reviewed'] and
                    (manufacturer is None or tag['manufacturer'].casefold() == manufacturer.casefold()) and
                    (model is None or tag['model'].casefold() == model.casefold()) and
                    (build is None or tag['windows_build'] == build)]
        if any(value is not None for value in (manufacturer, model, build)) and not matching:
            continue
        result.append(driver_management.enrich(db, {**json.loads(row['metadata']), 'tags': tags, 'created': row['created']}))
    return result


def manifest(db, driver_id, manufacturer, model, build):
    matching = next((entry for entry in catalog(db, manufacturer, model, build) if entry['id'] == driver_id), None)
    if not matching:
        raise HTTPException(404, 'No reviewed driver matches this model and Windows build')
    tag = next(tag for tag in matching['tags'] if tag['reviewed'] and tag['manufacturer'].casefold() == manufacturer.casefold()
               and tag['model'].casefold() == model.casefold() and tag['windows_build'] == build)
    return {'schemaVersion': 1, 'packageId': driver_id, 'architecture': matching['architecture'],
            'manufacturer': manufacturer, 'model': model, 'windowsBuild': build,
            'review': {'hardwareTestPassed': True, 'note': tag['note']},
            'files': matching['files'], 'infPaths': [matching['inf_path']]}
