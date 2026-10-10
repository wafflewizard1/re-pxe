"""A single bounded, versioned raster background in the real boot asset folder."""
from datetime import datetime, timezone
from io import BytesIO
import os
import uuid
import warnings
from PIL import Image, ImageOps
from fastapi import HTTPException, Request, Response
from . import file_workspace as files

NAME = 'ipxe-background.png'
PATH = 'assets/' + NAME
LIMIT = 5 * 1024**2


def initialize(store):
    with store.db() as db:
        db.execute('CREATE TABLE IF NOT EXISTS boot_background_history(id TEXT PRIMARY KEY,png BLOB,revision TEXT,saved TEXT NOT NULL,actor TEXT NOT NULL)')


def read(store):
    try:
        with files.directory(store,'custom-boot','assets') as folder:
            with files.regular_file(folder,NAME) as (fd,info):
                if info.st_size>LIMIT: raise HTTPException(413,'Background exceeds 5 MiB')
                if info.st_nlink!=1: raise HTTPException(403,'Shared background files are not editable')
                with os.fdopen(os.dup(fd),'rb') as stream: return stream.read(LIMIT+1)
    except HTTPException as exc:
        if exc.status_code==404: return None
        raise


def normalize(payload):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error',Image.DecompressionBombWarning)
            with Image.open(BytesIO(payload)) as source:
                if source.format not in ('PNG','JPEG','WEBP') or source.width>4096 or source.height>4096 or source.width*source.height>16_000_000: raise ValueError()
                source.load();image=ImageOps.exif_transpose(source).convert('RGB')
                image.thumbnail((1920,1200),Image.Resampling.LANCZOS)
                output=BytesIO();image.save(output,format='PNG');data=output.getvalue()
                if len(data)>LIMIT: raise ValueError()
                return data
    except (OSError,ValueError,Image.DecompressionBombError,Image.DecompressionBombWarning):
        raise HTTPException(422,'Choose a valid PNG, JPEG or WebP up to 4096 pixels per side and 5 MiB') from None


def metadata(store):
    data=read(store)
    with store.db() as db:
        history=[dict(row) for row in db.execute('SELECT id,revision,saved,actor FROM boot_background_history ORDER BY saved DESC LIMIT 20')]
    return {'path':'boot/custom/'+PATH,'revision':files.digest(data) if data else None,'custom':data is not None,'preview_url':'/pxe/routines/background.png','history':history}


def save(store,data,expected,actor):
    with store.lock('file-workspace'),files.directory(store,'custom-boot','assets') as folder:
        previous=read(store);revision=files.digest(previous) if previous else None
        if revision!=expected: raise HTTPException(409,'Background changed. Reload before replacing it.')
        if data==previous: return metadata(store)
        with store.db() as db:
            db.execute('INSERT INTO boot_background_history VALUES(?,?,?,?,?)',(str(uuid.uuid4()),previous,revision,datetime.now(timezone.utc).isoformat(),actor))
        temporary='.background-'+str(uuid.uuid4())
        try:
            if data is None:
                os.unlink(NAME,dir_fd=folder)
            else:
                fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o644,dir_fd=folder)
                with os.fdopen(fd,'wb') as stream: stream.write(data);stream.flush();os.fsync(stream.fileno())
                os.replace(temporary,NAME,src_dir_fd=folder,dst_dir_fd=folder)
            os.fsync(folder)
        finally:
            try: os.unlink(temporary,dir_fd=folder)
            except FileNotFoundError: pass
    return metadata(store)


def install(app,store,Role,allow):
    @app.get('/api/files/boot-background')
    def current(role: Role):
        allow(role);return metadata(store())

    @app.put('/api/files/boot-background')
    async def upload(request: Request,role: Role,expected_revision: str|None=None):
        allow(role);payload=bytearray()
        async for chunk in request.stream():
            if len(payload)+len(chunk)>LIMIT: raise HTTPException(413,'Background must be at most 5 MiB')
            payload.extend(chunk)
        return save(store(),normalize(payload),expected_revision,getattr(request.state,'web_user',{}).get('username','admin API'))

    @app.delete('/api/files/boot-background')
    def reset(request: Request,role: Role,expected_revision: str|None=None):
        allow(role);return save(store(),None,expected_revision,getattr(request.state,'web_user',{}).get('username','admin API'))

    @app.post('/api/files/boot-background/restore/{uid}')
    def restore(uid: str,request: Request,role: Role,expected_revision: str|None=None):
        allow(role)
        with store().db() as db: row=db.execute('SELECT png FROM boot_background_history WHERE id=?',(uid,)).fetchone()
        if not row: raise HTTPException(404,'Background version not found')
        return save(store(),bytes(row['png']) if row['png'] is not None else None,expected_revision,getattr(request.state,'web_user',{}).get('username','admin API'))
