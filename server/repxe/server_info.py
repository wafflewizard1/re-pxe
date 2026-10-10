"""Read-only status for the application and its data volume, not physical disks."""
from datetime import datetime, timezone
from pathlib import Path
from . import file_workspace
import json
import os
import platform
import re
import shutil
import time

STARTED = time.monotonic()
DEFAULTS = {'site_name': 're-pxe', 'refresh_seconds': 3, 'driver_stale_days': 90, 'notes': ''}


def initialize(store):
    with store.db() as db:
        db.execute('CREATE TABLE IF NOT EXISTS website_settings(id INTEGER PRIMARY KEY CHECK(id=1),value TEXT NOT NULL)')
        db.execute('INSERT OR IGNORE INTO website_settings VALUES(1,?)', (json.dumps(DEFAULTS),))


def settings(store):
    with store.db() as db:
        return {**DEFAULTS, **json.loads(db.execute('SELECT value FROM website_settings WHERE id=1').fetchone()['value'])}


def number(path):
    try:
        return int(Path(path).read_text().strip())
    except (OSError, ValueError):
        return None


def snapshot(store):
    at = datetime.now(timezone.utc)
    address = os.environ.get('REPXE_ADDRESS', '127.0.0.1')
    storage = shutil.disk_usage(store.root)
    memory = {}
    try:
        memory = {k: int(v.split()[0]) * 1024 for k, v in (line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines()) if k in ('MemTotal', 'MemAvailable')}
    except (OSError, ValueError):
        pass
    network = None
    try:
        sample = json.loads((store.root / 'database/server-network.json').read_text())
        sample['age_seconds'] = max(0, (at - datetime.fromisoformat(sample['sampled_at'])).total_seconds())
        sample['stale'] = sample['age_seconds'] >= 15
        network = sample
    except (OSError, ValueError, KeyError, TypeError):
        pass
    media = None
    try:
        media = json.loads((store.root / 'boot/pxe/current.json').read_text())
    except (OSError, ValueError):
        pass
    release = None
    version = str((media or {}).get('version', ''))
    if re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', version):
        try:
            release = json.loads((store.root / 'boot/client' / version / 'release.json').read_text())
        except (OSError, ValueError):
            pass
    iso = (release or {}).get('bootIso') or {}
    downloads = []
    filename = iso.get('filename', '')
    if re.fullmatch(r'[A-Za-z0-9._-]+\.iso', filename) and (store.root / 'boot/client' / version / filename).is_file():
        downloads.append({'name': 'WinPE operator ISO', 'url': f'http://{address}:8089/client/{version}/{filename}', 'sha256': iso.get('sha256', '')})
    with store.db() as db:
        images = db.execute("SELECT count(*) count,coalesce(sum(size),0) bytes FROM artifacts WHERE kind='image'").fetchone()
        drivers = db.execute('SELECT metadata FROM driver_library WHERE id NOT IN (SELECT driver_id FROM driver_retired)').fetchall()
        files = {f['sha256']: f['size'] for row in drivers for f in json.loads(row['metadata'])['files']}
        logical = sum(json.loads(row['metadata'])['size'] for row in drivers)
        staging = db.execute("SELECT count(*) count,coalesce(sum(received),0) bytes FROM uploads WHERE status NOT IN ('published','cancelled')").fetchone()
        retired = db.execute('SELECT count(*) FROM driver_retired').fetchone()[0]
    blobs = [p.stat().st_size for p in (store.root / 'drivers/blobs').iterdir() if p.is_file() and not p.is_symlink()]
    return {'sampled_at': at.isoformat(), 'runtime': {'python': platform.python_version(), 'system': platform.system(),
            'kernel': platform.release(), 'architecture': platform.machine(), 'process_uptime_seconds': int(time.monotonic() - STARTED),
            'cpu_count': os.cpu_count(), 'load_average': list(os.getloadavg()) if hasattr(os, 'getloadavg') else None,
            'memory_total': memory.get('MemTotal'), 'memory_available': memory.get('MemAvailable'),
            'container_memory_limit': number('/sys/fs/cgroup/memory.max'), 'container_memory_used': number('/sys/fs/cgroup/memory.current')},
        'storage': {'total': storage.total, 'free': storage.free, 'used': storage.used, 'images': dict(images),
            'drivers': {'count': len(drivers), 'logical_bytes': logical, 'active_unique_bytes': sum(files.values()),
                        'stored_blob_bytes': sum(blobs), 'retired_count': retired}, 'staging': dict(staging)},
        'network': network, 'boot': media, 'custom_boot_menu': file_workspace.boot_settings(store), 'downloads': downloads, 'public_ca_sha256': iso.get('trustedServerCertificateSha256') or None, 'endpoints': {'address': address, 'website': f'https://{address}:8443',
            'website_http': f'http://{address}:8080' if os.environ.get('REPXE_ALLOW_HTTP', '').lower() == 'true' else None,
            'boot_http': f'http://{address}:8089', 'tftp_server': (media or {}).get('dhcp', {}).get('server', address),
            'boot_filename': 'pxe/snponly.efi' if (store.root / 'boot/pxe/snponly.efi').is_file() else (media or {}).get('dhcp', {}).get('filename', 'pxe/' + str((media or {}).get('directory', (media or {}).get('version', 'RELEASE'))) + '/snponly.efi')},
        'services': ([{'name': 'HTTP website', 'state': 'Configured; external reachability not probed', 'port': 'TCP 8080'}]
                     if os.environ.get('REPXE_ALLOW_HTTP', '').lower() == 'true' else []) + [{'name': 'API', 'state': 'Responding', 'port': '8088 / loopback only'},
                     {'name': 'HTTPS gateway', 'state': 'Configured; external reachability not probed', 'port': 'TCP 8443'},
                     {'name': 'Boot HTTP', 'state': 'Configured; external reachability not probed', 'port': 'TCP 8089'},
                     {'name': 'TFTP', 'state': 'Configured; external reachability not probed', 'port': 'UDP 69 + 30000–30031'},
                     {'name': 'SMB (optional)', 'state': 'Host service; not probed', 'port': 'TCP 445'}]}
