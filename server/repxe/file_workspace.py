"""Administrator file explorer confined to the project data directory."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import PurePosixPath
import re
import stat
import uuid

from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel, ConfigDict, Field

MAX_TEXT = 256 * 1024
TEXT_EXTENSIONS = {'.ipxe', '.cfg', '.conf', '.ini', '.json', '.yaml', '.yml', '.txt', '.md', '.inf', '.ps1', '.cmd', '.bat', '.sh'}
# These mappings also drive the website, status and documentation. Existing
# publication paths remain unchanged, including URLs and SMB/native references.
ROOTS = {
    'configuration': ('Configuration', 'config', True, False, 'Boot menu settings and operator text configuration.'),
    'custom-boot': ('Custom boot', 'boot/custom', True, True, 'Editable iPXE scripts and a folder reserved for future boot assets.'),
    'boot-releases': ('Boot releases', 'boot/pxe', False, True, 'Published WinPE/PXE versions; managed and read-only.'),
    'client-releases': ('Client releases', 'boot/client', False, True, 'Published client packages and operator ISOs; read-only.'),
    'windows-images': ('Windows images', 'images/published', False, False, 'Reviewed WIMs and image metadata; use Images/Uploads to manage them.'),
    'drivers': ('Drivers', 'drivers/packages', False, False, 'Reviewed source archives; use the Drivers page for tags and cleanup.'),
    'reports': ('Reports', 'reports', False, False, 'Completed job evidence; read-only.'),
}
ROOTS = {uid: (spec[0], spec[1], True, spec[3], 'Files and folders in /data/' + spec[1]) for uid, spec in ROOTS.items()}
ROOTS['workspace'] = ('Server workspace', '', True, False, 'All server files and folders')

MENU_SETTINGS = 'boot-menu.json'
MENU_PATH = 'scripts/menu.ipxe'
DEFAULT_MENU = '''#!ipxe
# Variables are set by the server: re-pxe-server and re-pxe-mac.
menu re-pxe boot menu
item --key w windows Windows imaging
item --key l local Boot local disk
choose --default windows --timeout 10000 target || goto local
goto ${target}
:windows
chain ${re-pxe-server}/pxe/managed.ipxe?mac=${re-pxe-mac} || goto local
:local
exit
'''


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')


class TextSave(Input):
    root: str
    path: str = Field(min_length=1, max_length=800)
    content: str = Field(max_length=MAX_TEXT)
    expected_revision: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')


class Folder(Input):
    root: str
    path: str = Field(min_length=1, max_length=800)


class BootSettings(Input):
    enabled: bool = Field(strict=True)
    expected_revision: str = Field(pattern=r'^[a-f0-9]{64}$')


def workspace_lock(store):
    from .workspace_operations import workspace_lock as lock
    return lock(store)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def parts(path):
    if not path:
        return []
    if len(path) > 800 or path.startswith('/') or '\\' in path:
        raise HTTPException(422, 'Use a relative workspace path')
    result = path.split('/')
    if len(result) > 16 or any(not p or p in ('.', '..') or len(p.encode('utf-8')) > 255 or any(ord(c) < 32 or ord(c) == 127 for c in p) for p in result):
        raise HTTPException(422, 'Use valid file names without empty components or parent traversal')
    return result


@contextmanager
def directory(store, root, path=''):
    if root not in ROOTS:
        raise HTTPException(404, 'Workspace section not found')
    fd = os.open(store.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for name in [p for p in ROOTS[root][1].split('/') if p] + parts(path):
            try:
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                raise HTTPException(404, 'Folder not found')
            except OSError:
                raise HTTPException(403, 'Linked or non-directory paths are not accessible')
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


@contextmanager
def regular_file(parent, name):
    """Pin an inode without opening its contents, and reject devices/links first."""
    try:
        inode = os.open(name, os.O_PATH | os.O_NOFOLLOW, dir_fd=parent)
    except FileNotFoundError:
        raise HTTPException(404, 'File not found')
    try:
        info = os.fstat(inode)
        if not stat.S_ISREG(info.st_mode):
            raise HTTPException(403, 'Only regular files are accessible')
        fd = os.open('/proc/self/fd/' + str(inode), os.O_RDONLY)
        try:
            yield fd, info
        finally:
            os.close(fd)
    finally:
        os.close(inode)


def editable(root, path):
    return root in ROOTS


def read_bytes(store, root, path):
    names = parts(path)
    if not names:
        raise HTTPException(422, 'Choose a file')
    with directory(store, root, '/'.join(names[:-1])) as parent:
        with regular_file(parent, names[-1]) as (fd, info):
            if info.st_size > MAX_TEXT:
                raise HTTPException(413, 'Text preview is limited to 256 KiB; download larger files')
            with os.fdopen(os.dup(fd), 'rb') as stream:
                value = stream.read(MAX_TEXT + 1)
            if len(value) > MAX_TEXT:
                raise HTTPException(413, 'Text preview is limited to 256 KiB')
            return value, info


def text_file(store, root, path):
    parts(path)
    if root not in ROOTS:
        raise HTTPException(404, 'Workspace section not found')
    data, info = read_bytes(store, root, path)
    try:
        text = data.decode('utf-8')
        if '\x00' in text:
            raise ValueError()
    except (UnicodeError, ValueError):
        raise HTTPException(415, 'Editor supports UTF-8 text without NUL characters')
    return {'root': root, 'path': path, 'content': text, 'revision': digest(data), 'size': len(data),
            'modified': datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat(),
            'editable': editable(root, path), 'public': ROOTS[root][3]}


def boot_settings(store):
    try:
        result = text_file(store, 'configuration', MENU_SETTINGS)
        value = json.loads(result['content'])
        if set(value) != {'schemaVersion', 'enabled'} or type(value['schemaVersion']) is not int or value['schemaVersion'] != 1 or type(value['enabled']) is not bool:
            raise ValueError()
        return {**value, 'revision': result['revision'], 'valid': True, 'menu_root': 'custom-boot', 'menu_path': MENU_PATH}
    except (HTTPException, ValueError, TypeError):
        return {'schemaVersion': 1, 'enabled': False, 'valid': False, 'revision': None, 'menu_root': 'custom-boot', 'menu_path': MENU_PATH,
                'error': 'Boot-menu configuration is invalid or unavailable; managed imaging remains the fallback.'}


def validate_text(store, root, path, content):
    if len(content.encode('utf-8')) > MAX_TEXT or '\x00' in content:
        raise HTTPException(422, 'Text must be UTF-8 without NUL characters and at most 256 KiB')
    if root == 'configuration' and path == 'routines-appearance.ipxe':
        from .boot_appearance import appearance_lines
        try:appearance_lines(content)
        except ValueError as exc:raise HTTPException(422,str(exc)) from None
    if root == 'configuration' and path == 'routines-menu.json':
        from .routines import Menu
        try:Menu.model_validate_json(content)
        except ValueError:raise HTTPException(422,'Invalid routine menu settings; use supported labels, palette, scale and options') from None
    if PurePosixPath(path).suffix.lower() == '.json':
        try:
            value = json.loads(content)
        except ValueError:
            raise HTTPException(422, 'Invalid JSON; file was not saved')
        if root == 'configuration' and path == MENU_SETTINGS:
            if not isinstance(value, dict) or set(value) != {'schemaVersion', 'enabled'} or type(value['schemaVersion']) is not int or value['schemaVersion'] != 1 or type(value['enabled']) is not bool:
                raise HTTPException(422, 'Boot settings require schemaVersion: 1 and a boolean enabled')
            if value['enabled']:
                menu = text_file(store, 'custom-boot', MENU_PATH)
                if not menu['content'].replace('\r\n', '\n').startswith('#!ipxe\n'):
                    raise HTTPException(422, 'The custom menu must start with #!ipxe')
    if PurePosixPath(path).suffix.lower() == '.ipxe' and not content.replace('\r\n', '\n').startswith('#!ipxe\n'):
        raise HTTPException(422, 'iPXE scripts must begin with #!ipxe on their own line')


def save_text(store, body, actor):
    names = parts(body.path)
    if body.root not in ROOTS or not editable(body.root, body.path):
        raise HTTPException(403, 'Workspace section not found')
    with workspace_lock(store):
        if body.root == 'workspace':
            if len(body.content.encode('utf-8')) > MAX_TEXT or '\x00' in body.content:
                raise HTTPException(422, 'Text must be UTF-8 without NUL characters and at most 256 KiB')
        else:
            validate_text(store, body.root, body.path, body.content)
        with directory(store, body.root, '/'.join(names[:-1])) as parent:
            previous = None
            try:
                previous, info = read_bytes(store, body.root, body.path)
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise
            if previous is not None:
                try: previous.decode('utf-8')
                except UnicodeError: raise HTTPException(415, 'Existing file is not UTF-8 text')
            revision = digest(previous) if previous is not None else None
            if revision != body.expected_revision:
                raise HTTPException(409, 'File changed since it was opened. Reload or compare before saving your edits.')
            data = body.content.encode('utf-8')
            if data == previous:
                return text_file(store, body.root, body.path)
            # Store the previous version before replacing the live file. A
            # crash can leave an extra backup, never a missing old version.
            with store.db() as db:
                if previous is not None:
                    db.execute('INSERT INTO workspace_revisions VALUES(?,?,?,?,?,?,?)',
                               (str(uuid.uuid4()), body.root, body.path, revision, previous.decode('utf-8'), datetime.now(timezone.utc).isoformat(), actor))
                db.execute('INSERT INTO workspace_audit VALUES(?,?,?,?,?,?,?)',
                           (str(uuid.uuid4()), body.root, body.path, revision, digest(data), datetime.now(timezone.utc).isoformat(), actor))
            name = '.edit-' + str(uuid.uuid4())
            try:
                fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=parent)
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(name, names[-1], src_dir_fd=parent, dst_dir_fd=parent)
                os.fsync(parent)
            finally:
                try:
                    os.unlink(name, dir_fd=parent)
                except FileNotFoundError:
                    pass
    return text_file(store, body.root, body.path)


def initialize(store):
    from . import boot_background
    boot_background.initialize(store)
    for relative in ('config', 'boot/custom/scripts', 'boot/custom/assets'):
        # Initialize only the known project directories; never follow a link.
        with workspace_lock(store):
            fd = os.open(store.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                for name in relative.split('/'):
                    try: os.mkdir(name, 0o755, dir_fd=fd)
                    except FileExistsError: pass
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    os.close(fd);fd = child
            finally: os.close(fd)
    with store.db() as db:
        db.executescript('''CREATE TABLE IF NOT EXISTS workspace_revisions(id TEXT PRIMARY KEY,root TEXT NOT NULL,path TEXT NOT NULL,revision TEXT NOT NULL,content TEXT NOT NULL,saved TEXT NOT NULL,actor TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS workspace_audit(id TEXT PRIMARY KEY,root TEXT NOT NULL,path TEXT NOT NULL,previous_revision TEXT,revision TEXT NOT NULL,saved TEXT NOT NULL,actor TEXT NOT NULL);''')
    for root, path, content in [('configuration', MENU_SETTINGS, json.dumps({'schemaVersion': 1, 'enabled': False}, indent=2)+'\n'),
                                 ('custom-boot', MENU_PATH, DEFAULT_MENU),
                                 ('configuration', 'workshop-notes.md', '# Workshop notes\n\nAdd local operating instructions here.\n')]:
        try:
            names = parts(path)
            with directory(store, root, '/'.join(names[:-1])) as parent:
                with regular_file(parent, names[-1]):
                    pass
        except HTTPException as exc:
            if exc.status_code != 404: raise
            save_text(store, TextSave(root=root, path=path, content=content), 'initialization')


def install(app, store, Role, allow):
    from . import boot_background
    boot_background.install(app,store,Role,allow)
    from . import workspace_operations
    workspace_operations.install(app, store, Role, allow)
    def actor(request):
        user = getattr(request.state, 'web_user', None)
        return user['username'] if user else 'admin API'

    @app.get('/api/files/roots')
    def roots(role: Role):
        allow(role)
        return {'roots': [{'id': uid, 'name': spec[0], 'data_path': spec[1], 'writable': spec[2], 'public': spec[3], 'description': spec[4]} for uid, spec in ROOTS.items()],
                'max_text_bytes': MAX_TEXT, 'binary_uploads_available': True, 'workspace_path': '/data', 'background_upload_available': True, 'boot_menu': boot_settings(store()), 'boot_http': 'http://' + os.environ.get('REPXE_ADDRESS', '127.0.0.1') + ':8089'}

    @app.get('/api/files/list')
    def listing(role: Role, root: str, path: str = ''):
        allow(role)
        entries = []
        labels = {}
        physical = '/'.join(filter(None, [ROOTS.get(root, ('', ''))[1], path]))
        if physical in ('images/published', 'drivers/packages'):
            with store().db() as db:
                labels = {row['id']: row['name'] for row in db.execute(
                    'SELECT id,name FROM artifacts WHERE kind=?',
                    ('image' if physical == 'images/published' else 'drivers',))}
        try:
            with directory(store(), root, path) as parent:
                names = os.listdir(parent)
        except HTTPException as exc:
            if exc.status_code == 404 and root in ROOTS and not path:
                return {'root': root, 'path': path, 'entries': []}
            raise
        with directory(store(), root, path) as parent:
            for name in names:
                try: info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError: continue
                if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                    continue
                full = '/'.join(filter(None, [path, name]))
                entries.append({'name': name, 'display_name': labels.get(name, name), 'path': full, 'directory': stat.S_ISDIR(info.st_mode), 'size': info.st_size if stat.S_ISREG(info.st_mode) else None,
                                'modified': datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat(),
                                'text': PurePosixPath(name).suffix.lower() in TEXT_EXTENSIONS or not PurePosixPath(name).suffix,
                                'token': f'{info.st_dev}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}',
                                'editable': stat.S_ISREG(info.st_mode) and editable(root, full)})
                if len(entries) > 5000:
                    raise HTTPException(413, 'This folder is too large to display; use a narrower folder')
        return {'root': root, 'path': path, 'entries': sorted(entries, key=lambda e: (not e['directory'], e['name'].casefold()))}

    @app.get('/api/files/tree')
    def tree(role: Role, path: str=''):
        allow(role)
        result = listing(role, 'workspace', path)
        for entry in result['entries']:
            entry.update(root='workspace', relative_path=entry['path'], locked=False)
        result.update(relative_path=path, writable=True)
        return result

    @app.get('/api/files/text')
    def read_text(role: Role, root: str, path: str):
        allow(role)
        return text_file(store(), root, path)

    @app.put('/api/files/text')
    def write_text(body: TextSave, request: Request, role: Role):
        allow(role)
        return save_text(store(), body, actor(request))

    @app.post('/api/files/folders', status_code=201)
    def new_folder(body: Folder, role: Role):
        allow(role)
        names = parts(body.path)
        if not names: raise HTTPException(422, 'Choose a folder name')
        if body.root not in ROOTS:
            raise HTTPException(403, 'Folders can be created in editable configuration and scripts only')
        with workspace_lock(store()), directory(store(), body.root, '/'.join(names[:-1])) as parent:
            try: os.mkdir(names[-1], 0o755, dir_fd=parent)
            except FileExistsError: raise HTTPException(409, 'Folder already exists')
        return {'root': body.root, 'path': body.path}

    @app.get('/api/files/history')
    def history(role: Role, root: str, path: str):
        allow(role)
        text_file(store(), root, path)
        physical = '/'.join(filter(None, [ROOTS[root][1], path]))
        aliases = [(uid, physical[len(spec[1]):].lstrip('/')) for uid, spec in ROOTS.items()
                   if not spec[1] or physical.startswith(spec[1] + '/')]
        where = ' OR '.join('(root=? AND path=?)' for _ in aliases)
        with store().db() as db:
            return [dict(row) for row in db.execute(
                'SELECT id,revision,saved,actor,length(content) size FROM workspace_revisions WHERE ' + where + ' ORDER BY saved DESC LIMIT 50',
                tuple(value for pair in aliases for value in pair))]

    @app.get('/api/files/history/{uid}')
    def old_revision(uid: str, role: Role):
        allow(role)
        with store().db() as db:
            row = db.execute('SELECT * FROM workspace_revisions WHERE id=?', (uid,)).fetchone()
        if not row: raise HTTPException(404, 'Revision not found')
        return dict(row)

    @app.get('/api/files/download')
    def download(role: Role, root: str, path: str):
        allow(role)
        names = parts(path)
        if not names: raise HTTPException(422, 'Choose a file')
        with directory(store(), root, '/'.join(names[:-1])) as parent:
            with regular_file(parent, names[-1]) as (fd, info):
                stream = os.fdopen(os.dup(fd), 'rb')
        def chunks():
            try:
                while data := stream.read(1024*1024): yield data
            finally: stream.close()
        return StreamingResponse(chunks(), media_type='application/octet-stream', headers={'Content-Length': str(info.st_size), 'Content-Disposition': "attachment; filename*=UTF-8''"+__import__('urllib.parse',fromlist=['quote']).quote(names[-1]), 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'}, background=BackgroundTask(stream.close))

    @app.get('/api/boot-menu')
    def read_boot_settings(role: Role):
        allow(role)
        return boot_settings(store())

    @app.put('/api/boot-menu')
    def write_boot_settings(body: BootSettings, request: Request, role: Role):
        allow(role)
        save_text(store(), TextSave(root='configuration', path=MENU_SETTINGS, content=json.dumps({'schemaVersion':1,'enabled':body.enabled},indent=2)+'\n', expected_revision=body.expected_revision), actor(request))
        return boot_settings(store())
