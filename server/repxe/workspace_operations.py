"""File operations in /data using pinned directory descriptors, never host paths."""
from contextlib import contextmanager
import ctypes
import errno
import fcntl
import os
from pathlib import PurePosixPath
import stat
import uuid
from typing import Literal
from fastapi import HTTPException, Request, Query
from pydantic import Field

from .file_workspace import Input, directory, parts, regular_file


@contextmanager
def workspace_lock(store):
    fd = os.open(store.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


class Operation(Input):
    action: Literal['rename', 'move', 'copy', 'delete']
    paths: list[str] = Field(min_length=1, max_length=5000)
    destination: str = ''
    name: str | None = None
    tokens: dict[str, str] = Field(default_factory=dict)


def token(info):
    return f'{info.st_dev}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}'


def rename_exclusive(source, name, target, new_name):
    # Linux renameat2 keeps concurrent external changes from being overwritten.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.renameat2(source, os.fsencode(name), target, os.fsencode(new_name), 1):
        error = ctypes.get_errno()
        if error == errno.EEXIST: raise HTTPException(409, 'A file or folder with that name already exists')
        raise OSError(error, os.strerror(error))


def remove_entry(parent, name):
    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if stat.S_ISDIR(info.st_mode):
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            for child in os.listdir(fd): remove_entry(fd, child)
        finally: os.close(fd)
        os.rmdir(name, dir_fd=parent)
    else:
        os.unlink(name, dir_fd=parent)


def copy_entry(source, name, target, new_name):
    info = os.stat(name, dir_fd=source, follow_symlinks=False)
    if stat.S_ISDIR(info.st_mode):
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=source)
        try:
            os.mkdir(new_name, 0o755, dir_fd=target)
            dest = os.open(new_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=target)
            try:
                for child in os.listdir(fd): copy_entry(fd, child, dest, child)
                os.fsync(dest)
            finally: os.close(dest)
        finally: os.close(fd)
    elif stat.S_ISREG(info.st_mode):
        with regular_file(source, name) as (fd, _):
            dest = os.open(new_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=target)
            with os.fdopen(dest, 'wb') as out, os.fdopen(os.dup(fd), 'rb') as inp:
                while chunk := inp.read(1024 * 1024): out.write(chunk)
                out.flush(); os.fsync(out.fileno())
    else:
        raise HTTPException(403, 'Links and device files cannot be copied or opened')


def install(app, store, Role, allow):
    @app.post('/api/files/operations')
    def operation(body: Operation, role: Role):
        allow(role)
        paths = list(dict.fromkeys(body.paths))
        if body.action == 'rename' and (len(paths) != 1 or not body.name or len(parts(body.name)) != 1):
            raise HTTPException(422, 'Choose one item and a single new name')
        for path in paths:
            if not parts(path): raise HTTPException(422, 'Choose an item inside the workspace')
            if any(path.startswith(other + '/') for other in paths if path != other):
                raise HTTPException(422, 'Select a folder or its children, not both')
        parts(body.destination)
        targets = []
        with workspace_lock(store()):
            # Validate every source and destination before starting a batch.
            for path in paths:
                names = parts(path)
                with directory(store(), 'workspace', '/'.join(names[:-1])) as parent:
                    try: info = os.stat(names[-1], dir_fd=parent, follow_symlinks=False)
                    except FileNotFoundError: raise HTTPException(404, 'Item no longer exists; refresh the folder')
                    if path in body.tokens and token(info) != body.tokens[path]:
                        raise HTTPException(409, 'Item changed; refresh the folder before continuing')
                if body.action == 'delete': continue
                folder = '/'.join(names[:-1]) if body.action == 'rename' else body.destination
                name = body.name if body.action == 'rename' else names[-1]
                if stat.S_ISDIR(info.st_mode) and (folder == path or folder.startswith(path + '/')):
                    raise HTTPException(422, 'A folder cannot be placed inside itself')
                with directory(store(), 'workspace', folder) as dest:
                    if body.action == 'copy' and folder == '/'.join(names[:-1]):
                        stem, suffix = PurePosixPath(name).stem, PurePosixPath(name).suffix
                        if stat.S_ISDIR(info.st_mode): stem, suffix = name, ''
                        i = 1
                        while True:
                            name = stem + ' - Copy' + (f' ({i})' if i > 1 else '') + suffix
                            try: os.stat(name, dir_fd=dest, follow_symlinks=False)
                            except FileNotFoundError: break
                            i += 1
                    try: os.stat(name, dir_fd=dest, follow_symlinks=False)
                    except FileNotFoundError: pass
                    else: raise HTTPException(409, 'A file or folder with that name already exists')
                target = '/'.join(filter(None, [folder, name]))
                if target in targets: raise HTTPException(409, 'Selected items have conflicting destination names')
                targets.append(target)
            for index, path in enumerate(paths):
                names = parts(path)
                with directory(store(), 'workspace', '/'.join(names[:-1])) as parent:
                    if body.action == 'delete': remove_entry(parent, names[-1])
                    else:
                        dest_names = parts(targets[index])
                        with directory(store(), 'workspace', '/'.join(dest_names[:-1])) as dest:
                            if body.action == 'copy':
                                temporary = '.copy-' + uuid.uuid4().hex
                                try:
                                    copy_entry(parent, names[-1], dest, temporary)
                                    rename_exclusive(dest, temporary, dest, dest_names[-1])
                                finally:
                                    try: remove_entry(dest, temporary)
                                    except FileNotFoundError: pass
                            else: rename_exclusive(parent, names[-1], dest, dest_names[-1])
                            os.fsync(dest)
                    os.fsync(parent)
        return {'action': body.action, 'paths': paths, 'targets': targets}

    @app.put('/api/files/upload')
    async def upload(request: Request, role: Role, path: str, expected_token: str | None = None,
                     upload_id: str | None = Query(default=None, pattern=r'^[a-f0-9]{32}$'),
                     offset: int = Query(default=0, ge=0), finish: bool = True):
        allow(role)
        names = parts(path)
        if not names: raise HTTPException(422, 'Choose a file name')
        if upload_id is None and offset: raise HTTPException(422, 'Start an upload at offset zero')
        uid = upload_id or uuid.uuid4().hex
        temporary = '.upload-' + uid
        with directory(store(), 'workspace', '/'.join(names[:-1])) as parent:
            if upload_id is None:
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=parent)
            else:
                with regular_file(parent, temporary) as (pinned, info):
                    if info.st_nlink != 1: raise HTTPException(403, 'Upload staging must be a private regular file')
                    fd = os.open('/proc/self/fd/' + str(pinned), os.O_WRONLY | os.O_APPEND)
            complete = False
            started = False
            try:
                try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError: raise HTTPException(409, 'This upload is receiving another chunk')
                size = os.fstat(fd).st_size
                if size != offset: raise HTTPException(409, 'Upload offset changed; start a new upload')
                started = True
                async for chunk in request.stream():
                    view = memoryview(chunk)
                    while view:
                        written = os.write(fd, view)
                        view = view[written:]
                    size += len(chunk)
                os.fsync(fd)
                if not finish:
                    complete = True
                    return {'path': path, 'size': size, 'upload_id': uid, 'complete': False}
                with workspace_lock(store()):
                    try: info = os.stat(names[-1], dir_fd=parent, follow_symlinks=False)
                    except FileNotFoundError:
                        if expected_token is not None: raise HTTPException(409, 'File was removed; refresh before replacing it')
                        rename_exclusive(parent, temporary, parent, names[-1])
                    else:
                        if expected_token != token(info): raise HTTPException(409, 'File exists or changed; select it and use Replace file')
                        if not stat.S_ISREG(info.st_mode): raise HTTPException(403, 'Only regular files can be replaced')
                        os.replace(temporary, names[-1], src_dir_fd=parent, dst_dir_fd=parent)
                    os.fsync(parent)
                complete = True
            finally:
                os.close(fd)
                if not complete and (started or upload_id is None):
                    try: os.unlink(temporary, dir_fd=parent)
                    except FileNotFoundError: pass
        return {'path': path, 'size': size, 'complete': True}
