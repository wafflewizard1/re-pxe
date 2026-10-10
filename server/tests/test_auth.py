import hashlib
import time

import pytest
from fastapi.testclient import TestClient

from repxe.app import create_app
from repxe.auth import COOKIE

TOKENS = {r: r + '-' + 'x' * 40 for r in ('admin', 'capture', 'agent', 'deploy')}
ADMIN = {'Authorization': 'Bearer ' + TOKENS['admin']}


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path, TOKENS), base_url='https://testserver') as c:
        yield c


def login(client, username='admin', password='admin'):
    response = client.post('/api/login', json={'username': username, 'password': password})
    assert response.status_code == 200, response.text
    return response.json()


def csrf(session):
    return {'X-CSRF-Token': session['csrf_token'], 'Origin': 'https://testserver'}


def test_bootstrap_cookie_and_native_roles(client):
    assert client.get('/api/users').status_code == 401
    response = client.post('/api/login', json={'username': 'ADMIN', 'password': 'admin'})
    assert response.status_code == 200
    cookie = response.headers['set-cookie']
    assert all(flag in cookie for flag in ('HttpOnly', 'Secure', 'SameSite=strict', 'Path=/'))
    assert response.headers['cache-control'] == 'no-store'
    assert response.json()['user']['default_password'] is True
    assert client.get('/api/session').json()['user']['username'] == 'admin'
    users = client.get('/api/users').json()
    assert [u['username'] for u in users] == ['admin']
    assert 'password_hash' not in users[0]
    for role in TOKENS:
        headers = {'Authorization': 'Bearer ' + TOKENS[role]}
        assert client.get('/api/session', headers=headers).json() == {'role': role}
        assert client.get('/api/users', headers=headers).status_code == (200 if role == 'admin' else 403)
    assert client.get('/api/users', headers={'Authorization': 'Bearer invalid'}).status_code == 401


def test_csrf_and_cross_site_requests(client):
    session = login(client)
    payload = {'username': 'technician', 'password': 'longer-test-password'}
    assert client.post('/api/users', json=payload).status_code == 403
    assert client.post('/api/users', json=payload, headers={'X-CSRF-Token': 'bad'}).status_code == 403
    assert client.post('/api/users', json=payload, headers={**csrf(session), 'Origin': 'https://evil.test'}).status_code == 403
    assert client.post('/api/users', json=payload, headers={**csrf(session), 'Origin': 'http://testserver'}).status_code == 403
    assert client.post('/api/users', json=payload, headers={**csrf(session), 'Sec-Fetch-Site': 'cross-site'}).status_code == 403
    assert client.post('/api/users', json=payload, headers=csrf(session)).status_code == 201
    assert client.post('/api/login', json={'username': 'admin', 'password': 'admin'},
                       headers={'Origin': 'https://evil.test'}).status_code == 403


def test_password_change_persists_and_revokes_every_session(tmp_path):
    app = create_app(tmp_path, TOKENS)
    with TestClient(app, base_url='https://testserver') as first, TestClient(app, base_url='https://testserver') as second:
        session = login(first)
        login(second)
        user_id = session['user']['id']
        assert first.put(f'/api/users/{user_id}/password', json={'password': 'admin'}, headers=csrf(session)).status_code == 422
        assert first.put(f'/api/users/{user_id}/password', json={'password': 'updated-test-password'}, headers=csrf(session)).status_code == 204
        assert first.get('/api/session').status_code == 401
        assert second.get('/api/session').status_code == 401
        assert first.post('/api/login', json={'username': 'admin', 'password': 'admin'}).status_code == 401
    with TestClient(create_app(tmp_path, TOKENS), base_url='https://testserver') as restarted:
        session = login(restarted, password='updated-test-password')
        assert session['user']['id'] == user_id
        assert session['user']['default_password'] is False
        assert len(restarted.get('/api/users').json()) == 1


def test_users_availability_and_last_administrator(client):
    session = login(client)
    admin_id = session['user']['id']
    assert client.put(f'/api/users/{admin_id}/availability', json={'enabled': False}, headers=csrf(session)).status_code == 409
    new = client.post('/api/users', json={'username': 'Tech.User', 'password': 'initial-test-password'}, headers=csrf(session))
    assert new.status_code == 201
    user_id = new.json()['id']
    assert new.json()['username'] == 'tech.user'
    assert client.post('/api/users', json={'username': 'TECH.USER', 'password': 'another-password'}, headers=csrf(session)).status_code == 409
    tech_session = login(client, 'tech.user', 'initial-test-password')
    assert client.put(f'/api/users/{user_id}/availability', json={'enabled': False}, headers=csrf(tech_session)).status_code == 200
    assert client.get('/api/session').status_code == 401
    assert client.post('/api/login', json={'username': 'tech.user', 'password': 'initial-test-password'}).status_code == 401
    session = login(client)
    assert client.put(f'/api/users/{admin_id}/availability', json={'enabled': False}, headers=csrf(session)).status_code == 409
    assert client.put(f'/api/users/{user_id}/availability', json={'enabled': True}, headers=csrf(session)).status_code == 200
    assert login(client, 'tech.user', 'initial-test-password')['user']['enabled'] is True


def test_other_user_password_change_and_unknown_users(client):
    session = login(client)
    new = client.post('/api/users', json={'username': 'other', 'password': 'initial-test-password'}, headers=csrf(session)).json()
    assert client.put(f"/api/users/{new['id']}/password", json={'password': 'replacement-password'}, headers=csrf(session)).status_code == 204
    assert client.get('/api/session').status_code == 200
    assert client.put('/api/users/unknown/password', json={'password': 'replacement-password'}, headers=csrf(session)).status_code == 404
    assert client.put('/api/users/unknown/availability', json={'enabled': False}, headers=csrf(session)).status_code == 404
    assert client.post('/api/login', json={'username': 'other', 'password': 'initial-test-password'}).status_code == 401
    assert login(client, 'other', 'replacement-password')['user']['default_password'] is False


def test_logout_expiry_and_hashed_storage(client):
    session = login(client)
    raw = client.cookies.get(COOKIE)
    with client.app.state.store.db() as db:
        row = db.execute('SELECT * FROM website_sessions').fetchone()
        assert row['token_hash'] == hashlib.sha256(raw.encode()).hexdigest()
        assert raw not in str(dict(row))
        user = db.execute('SELECT * FROM website_users').fetchone()
        assert user['password_hash'].startswith('pbkdf2_sha256$600000$')
        assert user['password_hash'] != 'admin'
    assert client.post('/api/logout').status_code == 403
    assert client.post('/api/logout', headers=csrf(session)).status_code == 204
    assert client.cookies.get(COOKIE) is None
    client.cookies.set(COOKIE, raw, domain='testserver.local', path='/')
    assert client.get('/api/session').status_code == 401
    login(client)
    with client.app.state.store.db() as db:
        db.execute('UPDATE website_sessions SET expires=?', (time.time() - 1,))
    assert client.get('/api/session').status_code == 401


def test_sign_in_errors_and_persistent_throttle(client):
    for username in ('missing', 'admin'):
        wrong = client.post('/api/login', json={'username': username, 'password': 'wrong-password'})
        assert wrong.status_code == 401
        assert wrong.json()['detail'] == 'Username or password is incorrect'
    for _ in range(9):
        assert client.post('/api/login', json={'username': 'admin', 'password': 'wrong-password'}).status_code == 401
    blocked = client.post('/api/login', json={'username': 'ADMIN', 'password': 'admin'})
    assert blocked.status_code == 429
    assert blocked.headers['retry-after'] == '900'
    with client.app.state.store.db() as db:
        db.execute('UPDATE website_login_attempts SET started=?', (time.time() - 901,))
    assert login(client)['role'] == 'admin'


def test_account_input_limits(client):
    session = login(client)
    for username in ('bad name', '../admin', '<script>', 'a' * 65):
        assert client.post('/api/users', json={'username': username, 'password': 'long-test-password'}, headers=csrf(session)).status_code == 422
    assert client.post('/api/users', json={'username': 'tech', 'password': 'short'}, headers=csrf(session)).status_code == 422
    assert client.post('/api/login', json={'username': 'admin', 'password': 'a' * 257}).status_code == 422


def test_http_portal_sessions_coexist_and_are_transport_bound(tmp_path):
    from repxe.auth import HTTP_COOKIE
    app = create_app(tmp_path, TOKENS, allow_http=True)
    with TestClient(app, base_url='https://testserver:8443') as c:
        https_session = login(c)
        secure_token = c.cookies.get(COOKIE)
        response = c.post('http://testserver:8080/api/login',
                          json={'username': 'admin', 'password': 'admin'},
                          headers={'Origin': 'http://testserver:8080'})
        assert response.status_code == 200
        assert 'Secure' not in response.headers['set-cookie']
        assert 'HttpOnly' in response.headers['set-cookie']
        assert 'SameSite=strict' in response.headers['set-cookie']
        http_session = response.json()
        http_token = c.cookies.get(HTTP_COOKIE)
        assert secure_token == c.cookies.get(COOKIE)
        assert c.get('/api/session').json()['csrf_token'] == https_session['csrf_token']
        assert c.get('http://testserver:8080/api/session').json()['csrf_token'] == http_session['csrf_token']
        # A leaked HTTP session must not become an HTTPS session by renaming it.
        assert c.get('/api/session', headers={'Cookie': f'{COOKIE}={http_token}'}).status_code == 401
        assert c.get('http://testserver:8080/api/session',
                     headers={'Cookie': f'{HTTP_COOKIE}={secure_token}'}).status_code == 401
        assert c.post('http://testserver:8080/api/logout', headers={
            'X-CSRF-Token': http_session['csrf_token'], 'Origin': 'https://testserver:8080'
        }).status_code == 403
        assert c.post('http://testserver:8080/api/logout').status_code == 403
        response = c.post('http://testserver:8080/api/logout', headers={
            'X-CSRF-Token': http_session['csrf_token'], 'Origin': 'http://testserver:8080'})
        assert response.status_code == 204
        assert 'Secure' not in response.headers['set-cookie']
        assert c.cookies.get(HTTP_COOKIE) is None
        assert c.get('http://testserver:8080/api/session').status_code == 401
        assert c.get('/api/session').status_code == 200
        assert c.post('/api/logout', headers={
            'X-CSRF-Token': https_session['csrf_token'], 'Origin': 'https://testserver:8443'
        }).status_code == 204
        assert c.get('/api/session').status_code == 401


def test_http_access_requires_configuration_and_ignores_untrusted_forwarded_header(tmp_path):
    with TestClient(create_app(tmp_path, TOKENS), base_url='http://testserver') as c:
        response = c.post('/api/login', json={'username': 'admin', 'password': 'admin'})
        assert response.status_code == 403
        response = c.post('/api/login', json={'username': 'admin', 'password': 'admin'},
                          headers={'X-Forwarded-Proto': 'https'})
        assert response.status_code == 403
        assert 'set-cookie' not in response.headers
        # Native role authentication remains independent of website cookies.
        assert c.get('/api/session', headers=ADMIN).json() == {'role': 'admin'}
    with TestClient(create_app(tmp_path, TOKENS, secure_cookies=False)) as c:
        assert login(c)['role'] == 'admin'
        assert c.get('/api/session').status_code == 200


def test_http_configuration_and_sessions_survive_restart(tmp_path, monkeypatch):
    from repxe.auth import HTTP_COOKIE
    monkeypatch.setenv('REPXE_ALLOW_HTTP', 'true')
    with TestClient(create_app(tmp_path, TOKENS)) as c:
        login(c)
        token = c.cookies.get(HTTP_COOKIE)
        info = c.get('/api/server-info').json()
        assert info['endpoints']['website_http'] == 'http://127.0.0.1:8080'
        assert any(s['port'] == 'TCP 8080' for s in info['services'])
    with TestClient(create_app(tmp_path, TOKENS)) as restarted:
        restarted.cookies.set(HTTP_COOKIE, token)
        assert restarted.get('/api/session').status_code == 200
    monkeypatch.setenv('REPXE_ALLOW_HTTP', 'false')
    with TestClient(create_app(tmp_path, TOKENS), base_url='https://testserver') as c:
        info = c.get('/api/server-info', headers=ADMIN).json()
        assert info['endpoints']['website_http'] is None
        assert not any(s['port'] == 'TCP 8080' for s in info['services'])
