"""Library lifecycle and usage; completed reports remain immutable audit evidence."""
from datetime import datetime, timezone
import json
import re
import shutil
from fastapi import HTTPException


def now():
    return datetime.now(timezone.utc).isoformat()


def initialize(db):
    db.executescript('''
      CREATE TABLE IF NOT EXISTS driver_retired(driver_id TEXT PRIMARY KEY,deleted TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS driver_groups(driver_id TEXT PRIMARY KEY,name TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS driver_archive_members(archive_id TEXT NOT NULL,driver_id TEXT NOT NULL,PRIMARY KEY(archive_id,driver_id));
      CREATE TABLE IF NOT EXISTS driver_archives_purged(archive_id TEXT PRIMARY KEY,purged TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS driver_job_pins(job_id TEXT NOT NULL,driver_id TEXT NOT NULL,PRIMARY KEY(job_id,driver_id));
      CREATE TABLE IF NOT EXISTS driver_usage(job_id TEXT NOT NULL,driver_id TEXT NOT NULL,last_used TEXT NOT NULL,confirmed INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(job_id,driver_id));
      CREATE TABLE IF NOT EXISTS driver_history_imported(job_id TEXT PRIMARY KEY);
    ''')
    for row in db.execute('SELECT * FROM driver_archive_imports').fetchall():
        members(db, row['archive_id'], json.loads(row['result'])['driver_ids'])


def members(db, archive_id, ids):
    db.executemany('INSERT OR IGNORE INTO driver_archive_members VALUES(?,?)', [(archive_id, uid) for uid in ids])


def archive_available(db, archive_id):
    return not db.execute('SELECT 1 FROM driver_archives_purged WHERE archive_id=?', (archive_id,)).fetchone() and not db.execute('''SELECT 1 FROM driver_archive_members m JOIN driver_retired r ON r.driver_id=m.driver_id WHERE m.archive_id=?''', (archive_id,)).fetchone()


def used(db, ids, job_id, timestamp=None, confirmed=False):
    at = timestamp or now()
    for uid in set(ids):
        db.execute('''INSERT INTO driver_usage VALUES(?,?,?,?) ON CONFLICT(job_id,driver_id) DO UPDATE SET last_used=max(last_used,excluded.last_used),confirmed=max(confirmed,excluded.confirmed)''', (job_id, uid, at, int(confirmed)))


def archive_used(db, archive_id, job_id):
    used(db, [r['driver_id'] for r in db.execute('SELECT driver_id FROM driver_archive_members WHERE archive_id=?', (archive_id,))], job_id)


def report_used(db, job_id, snapshot, timestamp):
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get('driverActions', []), list):
        return
    for action in snapshot.get('driverActions', []):
        if not isinstance(action, dict) or not isinstance(action.get('packageId'), str):
            continue
        if type(action.get('exitCode')) is not int or action['exitCode'] not in (0, 3010):
            continue
        package = action.get('packageId')
        ids = [r['driver_id'] for r in db.execute('SELECT driver_id FROM driver_archive_members WHERE archive_id=?', (package,))]
        if db.execute('SELECT 1 FROM driver_library WHERE id=?', (package,)).fetchone():
            ids.append(package)
        # Legacy archives may contain more than one INF. Match the installed INF.
        name = str(action.get('inf', '')).replace('\\', '/').rsplit('/', 1)[-1].casefold()
        matching = []
        for uid in ids:
            row = db.execute('SELECT metadata FROM driver_library WHERE id=?', (uid,)).fetchone()
            if row and json.loads(row['metadata'])['inf_path'].casefold() == name:
                matching.append(uid)
        used(db, matching, job_id, timestamp, True)


def backfill(store):
    with store.db() as db:
        rows = db.execute('''SELECT id,report,updated FROM jobs WHERE report IS NOT NULL AND id NOT IN (SELECT job_id FROM driver_history_imported)''').fetchall()
        for row in rows:
            report = json.loads(row['report'])
            report_used(db, row['id'], report.get('native', {}), row['updated'])
            db.execute('INSERT OR IGNORE INTO driver_history_imported VALUES(?)', (row['id'],))


def enrich(db, entry):
    usage = db.execute('SELECT max(last_used) last_used,count(*) use_count,sum(confirmed) confirmed_count FROM driver_usage WHERE driver_id=?', (entry['id'],)).fetchone()
    group = db.execute('SELECT name FROM driver_groups WHERE driver_id=?', (entry['id'],)).fetchone()
    return {**entry, **dict(usage), 'confirmed_count': usage['confirmed_count'] or 0, 'group': group['name'] if group else ''}


def preview(db, ids):
    entries = []
    at = now()
    for uid in ids:
        row = db.execute('''SELECT d.* FROM driver_library d LEFT JOIN driver_retired r ON r.driver_id=d.id WHERE d.id=? AND r.driver_id IS NULL''', (uid,)).fetchone()
        if not row:
            raise HTTPException(404, 'A selected driver no longer exists in the library')
        entry = json.loads(row['metadata'])
        archives = {r['archive_id'] for r in db.execute('SELECT archive_id FROM driver_archive_members WHERE driver_id=?', (uid,))}
        blockers, retained = [], []
        for profile in db.execute('SELECT spec,enabled FROM deployment_profiles'):
            spec = json.loads(profile['spec'])
            if archives.intersection(spec.get('driver_ids', [])):
                if profile['enabled']:
                    blockers.append('Enabled configuration: ' + spec['name'])
                else:
                    retained.append('Disabled configuration: ' + spec['name'])
        pins = db.execute('''SELECT j.id FROM driver_job_pins p JOIN jobs j ON j.id=p.job_id WHERE p.driver_id=? AND j.report IS NULL AND j.stage NOT IN ('complete','failed','cancelled')''', (uid,)).fetchall()
        blockers.extend('Active driver job: ' + r['id'] for r in pins)
        for client in db.execute('''SELECT c.selection,c.expires,c.ticket_expires,c.job_id,j.stage,j.report FROM clients c LEFT JOIN jobs j ON j.id=c.job_id WHERE c.selection IS NOT NULL'''):
            selected = json.loads(client['selection'])
            package = (selected.get('driverManifest') or {}).get('packageId')
            if package not in archives:
                continue
            active = client['job_id'] and client['report'] is None and client['stage'] not in ('complete','failed','cancelled')
            waiting = not client['job_id'] and max(client['expires'] or '', client['ticket_expires'] or '') > at
            if active or waiting:
                blockers.append('Pinned PXE selection' + (': ' + client['job_id'] if client['job_id'] else ' awaiting installation'))
        unknown = db.execute("SELECT id FROM jobs WHERE report IS NULL AND stage NOT IN ('complete','failed','cancelled') AND id NOT IN (SELECT job_id FROM driver_job_pins) AND id NOT IN (SELECT job_id FROM clients WHERE selection IS NOT NULL AND job_id IS NOT NULL)").fetchall()
        blockers.extend('Active deployment with no recorded driver selection: ' + r['id'] for r in unknown)
        entries.append({'id': uid, 'name': entry['name'], 'size': entry['size'], 'blockers': sorted(set(blockers)), 'retained_references': sorted(set(retained))})
    return {'entries': entries, 'count': len(entries), 'logical_bytes': sum(e['size'] for e in entries), 'blocked': sum(bool(e['blockers']) for e in entries)}


def collect(store):
    """Reclaim only unreachable payloads; an unfinished legacy job defers GC."""
    freed = 0
    with store.db() as db:
        if db.execute("SELECT 1 FROM jobs WHERE report IS NULL AND stage NOT IN ('complete','failed','cancelled') LIMIT 1").fetchone():
            return {'freed_bytes': 0, 'cleanup_deferred': True}
        profile_archives = {uid for r in db.execute('SELECT spec FROM deployment_profiles') for uid in json.loads(r['spec']).get('driver_ids', [])}
        candidates = {r['archive_id'] for r in db.execute('SELECT DISTINCT m.archive_id FROM driver_archive_members m JOIN driver_retired d ON d.driver_id=m.driver_id')}
        reclaimable = [archive for archive in candidates - profile_archives if re.fullmatch(r'[a-f0-9-]{36}', archive)]
        # Persist withdrawal before changing files. Cleanup failure must never make
        # an archive with removed payload appear available after re-publication.
        db.executemany('INSERT OR IGNORE INTO driver_archives_purged VALUES(?,?)', [(archive, now()) for archive in reclaimable])
    for archive in reclaimable:
        folder = store.root / 'drivers/packages' / archive
        if folder.exists() and not folder.is_symlink():
            shutil.rmtree(folder)
        (store.root / 'drivers/manifests' / (archive + '.json')).unlink(missing_ok=True)
    with store.db() as db:
        referenced = {f['sha256'] for r in db.execute('''SELECT metadata FROM driver_library WHERE id NOT IN (SELECT driver_id FROM driver_retired)''') for f in json.loads(r['metadata'])['files']}
        for blob in (store.root / 'drivers/blobs').iterdir():
            if re.fullmatch(r'[a-f0-9]{64}', blob.name) and blob.name not in referenced and not blob.is_symlink() and blob.is_file() and blob.stat().st_nlink == 1:
                freed += blob.stat().st_size
                blob.unlink()
    return {'freed_bytes': freed, 'cleanup_deferred': False}


def remove(store, ids):
    with store.lock('driver-library'):
        with store.db() as db:
            result = preview(db, ids)
            if result['blocked']:
                raise HTTPException(409, {'message': 'Selected drivers are in use. Disable dependent configurations or finish the active jobs first.', **result})
            db.executemany('INSERT INTO driver_retired VALUES(?,?)', [(uid, now()) for uid in ids])
        try:
            cleanup = collect(store)
        except OSError:
            cleanup = {'freed_bytes': 0, 'cleanup_deferred': True}
    return {**result, **cleanup, 'status': 'deleted', 'note': 'Shared files and archives referenced by saved configurations remain stored. Deleted archives cannot be selected; reupload/review is required before reusing deleted content.'}
