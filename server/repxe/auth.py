"""Persistent website accounts. Native clients retain their separate role tokens."""
import hashlib
import hmac
import secrets
import sqlite3
import time
import uuid
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

COOKIE = 'repxe_session'
HTTP_COOKIE = 'repxe_http_session'
HTTP_TOKEN_PREFIX = 'http.'
SESSION_SECONDS = 12 * 60 * 60
ITERATIONS = 600_000


def password_hash(password):
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), ITERATIONS)
    return f'pbkdf2_sha256${ITERATIONS}${salt}${digest.hex()}'


def password_matches(password, encoded):
    _, iterations, salt, expected = encoded.split('$')
    actual = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), int(iterations))
    return hmac.compare_digest(actual.hex(), expected)


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


class Login(BaseModel):
    model_config = ConfigDict(extra='forbid')
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class NewUser(BaseModel):
    model_config = ConfigDict(extra='forbid')
    username: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')
    password: str = Field(min_length=8, max_length=256)


class NewPassword(BaseModel):
    model_config = ConfigDict(extra='forbid')
    password: str = Field(min_length=8, max_length=256)


class Availability(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool = Field(strict=True)


def public_user(row):
    return {key: bool(row[key]) if key in ('enabled', 'default_password') else row[key]
            for key in ('id', 'username', 'enabled', 'default_password', 'created')}


class WebsiteAuth:
    def __init__(self, store, secure_cookies=True, allow_http=False):
        self.store = store
        self.secure_cookies = secure_cookies
        self.allow_http = allow_http
        self.dummy_hash = password_hash(secrets.token_urlsafe(32))
        with store.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS website_auth_initialized(id INTEGER PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS website_users(
                    id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL, enabled INTEGER NOT NULL,
                    default_password INTEGER NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS website_sessions(
                    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL
                    REFERENCES website_users(id) ON DELETE CASCADE,
                    csrf_token TEXT NOT NULL, expires REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS website_session_user ON website_sessions(user_id);
                CREATE TABLE IF NOT EXISTS website_login_attempts(
                    key TEXT PRIMARY KEY, attempts INTEGER NOT NULL, started REAL NOT NULL);
            ''')
        # Serialize the one-time bootstrap, including concurrent process starts.
        initial_hash = password_hash('admin')
        with store.db() as db:
            if not db.execute('SELECT 1 FROM website_auth_initialized').fetchone():
                db.execute('INSERT INTO website_users VALUES(?,?,?,?,?,?)',
                           (str(uuid.uuid4()), 'admin', initial_hash, 1, 1, time.time()))
                db.execute('INSERT INTO website_auth_initialized VALUES(1)')

    def cookie_settings(self, request):
        # Separate names let both transports coexist on the same hostname.
        # Existing HTTPS tokens contain only URL-safe characters, never a dot.
        if self.secure_cookies and request.url.scheme == 'http':
            if not self.allow_http:
                raise HTTPException(403, 'HTTP website access is disabled')
            return HTTP_COOKIE, False
        return COOKIE, self.secure_cookies

    def check_origin(self, request):
        origin = request.headers.get('origin')
        if origin:
            parsed = urlsplit(origin)
            schemes = ('https',) if self.secure_cookies and not self.allow_http else ('http', 'https')
            if (parsed.scheme not in schemes or parsed.scheme != request.url.scheme
                    or parsed.netloc.lower() != request.headers.get('host', '').lower()):
                raise HTTPException(403, 'Request origin is not permitted')
        if request.headers.get('sec-fetch-site') == 'cross-site':
            raise HTTPException(403, 'Cross-site request is not permitted')

    def authorize(self, request):
        if self.secure_cookies and request.url.scheme == 'http' and not self.allow_http:
            raise HTTPException(401, 'Sign in required')
        cookie, _ = self.cookie_settings(request)
        raw = request.cookies.get(cookie, '')
        if raw and raw.startswith(HTTP_TOKEN_PREFIX) != (cookie == HTTP_COOKIE):
            raise HTTPException(401, 'Sign in required')
        with self.store.db() as db:
            row = db.execute('''SELECT u.*, s.csrf_token, s.token_hash FROM website_sessions s
                JOIN website_users u ON u.id=s.user_id
                WHERE s.token_hash=? AND s.expires>? AND u.enabled=1''',
                             (token_hash(raw), time.time())).fetchone() if raw else None
        if not row:
            raise HTTPException(401, 'Sign in required')
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            self.check_origin(request)
            if not hmac.compare_digest(request.headers.get('x-csrf-token', '').encode(), row['csrf_token'].encode()):
                raise HTTPException(403, 'Session security check failed; sign in again')
        request.state.web_user = dict(row)
        return 'admin'

    def session(self, request):
        user = request.state.web_user
        return {'role': 'admin', 'user': public_user(user), 'csrf_token': user['csrf_token']}

    def login(self, request, response, credentials):
        self.check_origin(request)
        cookie, secure = self.cookie_settings(request)
        username = credentials.username.strip().lower()
        ip = request.client.host if request.client else 'unknown'
        counters = [(token_hash('user:' + username), 10), (token_hash('ip:' + ip), 100)]
        timestamp = time.time()
        # Reserve an attempt before hashing so parallel requests cannot evade the limit.
        with self.store.db() as db:
            db.execute('DELETE FROM website_login_attempts WHERE started<?', (timestamp - 900,))
            for key, limit in counters:
                row = db.execute('SELECT attempts FROM website_login_attempts WHERE key=?', (key,)).fetchone()
                if row and row['attempts'] >= limit:
                    raise HTTPException(429, 'Too many sign-in attempts; try again later',
                                        headers={'Retry-After': '900'})
            for key, _ in counters:
                db.execute('''INSERT INTO website_login_attempts VALUES(?,1,?)
                    ON CONFLICT(key) DO UPDATE SET attempts=attempts+1''', (key, timestamp))
            user = db.execute('SELECT * FROM website_users WHERE username=?', (username,)).fetchone()
        matched = password_matches(credentials.password, user['password_hash'] if user else self.dummy_hash)
        if not matched or not user or not user['enabled']:
            raise HTTPException(401, 'Username or password is incorrect')
        raw, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
        if cookie == HTTP_COOKIE:
            raw = HTTP_TOKEN_PREFIX + raw
        with self.store.db() as db:
            # A concurrent password change or disable must win over an earlier login.
            current = db.execute('SELECT * FROM website_users WHERE id=?', (user['id'],)).fetchone()
            if not current or not current['enabled'] or current['password_hash'] != user['password_hash']:
                raise HTTPException(401, 'Username or password is incorrect')
            db.execute('DELETE FROM website_login_attempts WHERE key=?', (counters[0][0],))
            db.execute('DELETE FROM website_sessions WHERE expires<=? OR token_hash=?',
                       (timestamp, token_hash(request.cookies.get(cookie, ''))))
            db.execute('INSERT INTO website_sessions VALUES(?,?,?,?)',
                       (token_hash(raw), user['id'], csrf, timestamp + SESSION_SECONDS))
        response.headers['Cache-Control'] = 'no-store'
        response.set_cookie(cookie, raw, max_age=SESSION_SECONDS, secure=secure,
                            httponly=True, samesite='strict', path='/')
        return {'role': 'admin', 'user': public_user(user), 'csrf_token': csrf}

    @staticmethod
    def install(app, Role, allow):
        def active_auth():
            return app.state.website_auth
        @app.post('/api/login')
        def login(body: Login, request: Request, response: Response):
            return active_auth().login(request, response, body)

        @app.post('/api/logout', status_code=204)
        def logout(request: Request, role: Role):
            allow(role)
            user = getattr(request.state, 'web_user', None)
            if not user:
                raise HTTPException(401, 'Website session required')
            with active_auth().store.db() as db:
                db.execute('DELETE FROM website_sessions WHERE token_hash=?', (user['token_hash'],))
            response = Response(status_code=204, headers={'Cache-Control': 'no-store'})
            cookie, secure = active_auth().cookie_settings(request)
            response.delete_cookie(cookie, secure=secure, httponly=True, samesite='strict')
            return response

        @app.get('/api/users')
        def users(response: Response, role: Role):
            allow(role)
            response.headers['Cache-Control'] = 'no-store'
            with active_auth().store.db() as db:
                return [public_user(row) for row in db.execute('SELECT * FROM website_users ORDER BY username')]

        @app.post('/api/users', status_code=201)
        def create_user(body: NewUser, role: Role):
            allow(role)
            encoded = password_hash(body.password)
            user_id = str(uuid.uuid4())
            try:
                with active_auth().store.db() as db:
                    db.execute('INSERT INTO website_users VALUES(?,?,?,?,?,?)',
                               (user_id, body.username.lower(), encoded, 1, 0, time.time()))
                    return public_user(db.execute('SELECT * FROM website_users WHERE id=?', (user_id,)).fetchone())
            except sqlite3.IntegrityError:
                raise HTTPException(409, 'Username already exists') from None

        @app.put('/api/users/{user_id}/password', status_code=204)
        def change_password(user_id: str, body: NewPassword, role: Role):
            allow(role)
            encoded = password_hash(body.password)
            with active_auth().store.db() as db:
                if not db.execute('SELECT 1 FROM website_users WHERE id=?', (user_id,)).fetchone():
                    raise HTTPException(404, 'User not found')
                db.execute('UPDATE website_users SET password_hash=?, default_password=0 WHERE id=?', (encoded, user_id))
                db.execute('DELETE FROM website_sessions WHERE user_id=?', (user_id,))
            return Response(status_code=204, headers={'Cache-Control': 'no-store'})

        @app.put('/api/users/{user_id}/availability')
        def availability(user_id: str, body: Availability, role: Role):
            allow(role)
            with active_auth().store.db() as db:
                user = db.execute('SELECT * FROM website_users WHERE id=?', (user_id,)).fetchone()
                if not user:
                    raise HTTPException(404, 'User not found')
                if user['enabled'] and not body.enabled:
                    if db.execute('SELECT count(*) FROM website_users WHERE enabled=1').fetchone()[0] <= 1:
                        raise HTTPException(409, 'Keep at least one administrator enabled')
                    db.execute('DELETE FROM website_sessions WHERE user_id=?', (user_id,))
                db.execute('UPDATE website_users SET enabled=? WHERE id=?', (int(body.enabled), user_id))
                return public_user(db.execute('SELECT * FROM website_users WHERE id=?', (user_id,)).fetchone())
