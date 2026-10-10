"""Shared, decoded raster branding. Original erasure certificates remain immutable."""
import base64
import hashlib
from io import BytesIO
import warnings

from fastapi import HTTPException, Request, Response
from PIL import Image, ImageOps, UnidentifiedImageError

MAX_UPLOAD = 5 * 1024**2


def initialize(store):
    with store.db() as db:
        db.execute('CREATE TABLE IF NOT EXISTS website_branding(id INTEGER PRIMARY KEY CHECK(id=1),png BLOB NOT NULL,jpeg BLOB NOT NULL,icon BLOB NOT NULL,sha256 TEXT NOT NULL)')


def read(store):
    with store.db() as db:
        row = db.execute('SELECT * FROM website_branding WHERE id=1').fetchone()
    return dict(row) if row else None


def normalize(payload):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(BytesIO(payload)) as source:
                if source.format not in ('PNG', 'JPEG', 'WEBP', 'ICO'):
                    raise ValueError('Unsupported image format')
                if source.width > 4096 or source.height > 4096 or source.width * source.height > 16_000_000:
                    raise ValueError('Image dimensions too large')
                source.load()
                image = ImageOps.exif_transpose(source).convert('RGBA')
                image.thumbnail((512, 512), Image.Resampling.LANCZOS)
        # Fresh encoders strip metadata and any content following the image.
        png = BytesIO(); image.save(png, format='PNG')
        square = Image.new('RGBA', (256, 256), (255, 255, 255, 255))
        fit = ImageOps.contain(image, (240, 240), Image.Resampling.LANCZOS)
        square.alpha_composite(fit, ((256-fit.width)//2, (256-fit.height)//2))
        jpeg = BytesIO(); square.convert('RGB').save(jpeg, format='JPEG', quality=90)
        icon = BytesIO(); ImageOps.pad(image, (64, 64), color=(0, 0, 0, 0)).save(icon, format='PNG')
        return png.getvalue(), jpeg.getvalue(), icon.getvalue()
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise HTTPException(422, 'Upload a valid PNG, JPEG, WebP or ICO image, at most 4096 pixels per side') from None


def claim_logo(store):
    row = read(store)
    if not row: return None
    return {'jpeg_base64': base64.b64encode(row['jpeg']).decode(),
            'sha256': hashlib.sha256(row['jpeg']).hexdigest()}


def install(app, store, Role, allow):
    @app.get('/api/branding')
    def metadata():
        row = read(store())
        return {'logo_url': '/branding/logo.png?v='+row['sha256'] if row else None,
                'favicon_url': '/branding/favicon.png'+('?v='+row['sha256'] if row else '')}

    @app.put('/api/branding/logo')
    async def upload(request: Request, role: Role):
        allow(role)
        payload = bytearray()
        async for chunk in request.stream():
            if len(payload)+len(chunk) > MAX_UPLOAD: raise HTTPException(413, 'Logo must be at most 5 MiB')
            payload.extend(chunk)
        png, jpeg, icon = normalize(payload)
        with store().db() as db:
            db.execute('INSERT INTO website_branding VALUES(1,?,?,?,?) ON CONFLICT(id) DO UPDATE SET png=excluded.png,jpeg=excluded.jpeg,icon=excluded.icon,sha256=excluded.sha256',
                       (png,jpeg,icon,hashlib.sha256(png).hexdigest()))
        return metadata()

    @app.delete('/api/branding/logo')
    def remove(role: Role):
        allow(role)
        with store().db() as db: db.execute('DELETE FROM website_branding WHERE id=1')
        return metadata()

    @app.get('/branding/{name}')
    def image(name: str):
        if name not in ('logo.png','favicon.png'): raise HTTPException(404, 'Image not found')
        row = read(store())
        headers = {'Cache-Control':'no-cache','X-Content-Type-Options':'nosniff'}
        if row: return Response(row['png' if name=='logo.png' else 'icon'],media_type='image/png',headers=headers)
        if name=='logo.png': raise HTTPException(404, 'No logo uploaded')
        return Response('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="12" fill="#176b5b"/><path d="M16 16h32v32H16zm8 8v16h16V24z" fill="white" fill-rule="evenodd"/></svg>',media_type='image/svg+xml',headers=headers)
