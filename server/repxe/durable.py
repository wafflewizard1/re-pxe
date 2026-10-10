"""Durable file projections with independent staging for simultaneous writers."""
import json
import os
from pathlib import Path
import tempfile


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_bytes(path, data, *, mode=0o644):
    path=Path(path);temporary=None
    try:
        with tempfile.NamedTemporaryFile(mode='wb',dir=path.parent,prefix='.'+path.name+'.',suffix='.tmp',delete=False) as stream:
            temporary=Path(stream.name);stream.write(data);stream.flush();os.fchmod(stream.fileno(),mode);os.fsync(stream.fileno())
        os.replace(temporary,path);temporary=None;sync_directory(path.parent)
    finally:
        if temporary is not None:temporary.unlink(missing_ok=True)


def atomic_json(path, data, *, mode=0o644):
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.' + path.name + '.', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, indent=2)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        sync_directory(path.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
