"""Windows chooses a lone eligible drive and waits for a technician on multi-drive PCs."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from test_api import auth
from test_pxe import lab
from test_routines import managed_ticket, select
from test_workflows import install_media, SIZE, TARGET


def setup(lab, count=1):
    c, root, profile = lab
    install_media(root)
    path = root / 'boot/pxe/current.json'
    path.write_text(json.dumps({'version': '0.14.1'}))
    spec = {'name': 'Automatic single drive', 'steps': [
        {'id': 'boot', 'kind': 'boot'},
        {'id': 'windows', 'kind': 'windows', 'profile_id': profile['id'], 'target_policy': 'auto-unless-multiple'},
        {'id': 'finish', 'kind': 'finish'}]}
    saved = c.post('/api/routines', headers=auth(), json=spec)
    assert saved.status_code == 201, saved.text
    uid = saved.json()['id']
    published = c.post('/api/routines/' + uid + '/publish', headers=auth(), json={'expected_generation': 1})
    assert published.status_code == 200, published.text
    assert c.put('/api/routine-settings', headers=auth(), json={'default_id': uid}).status_code == 200
    select(c)
    request = {'manufacturer': 'Example', 'model': 'VM', 'client_version': '0.14.1', 'managed_selection': True,
               'targets': [{'identity': TARGET if i == 0 else 'serial:DISK-' + str(i),
                            'size_bytes': SIZE * (i+1), 'model': 'SSD ' + str(i)} for i in range(count)]}
    return c, profile, managed_ticket(c), request


def enroll(c, ticket, request):
    response = c.post('/api/pxe/enroll', headers={'Authorization': 'Bearer ' + ticket}, json=request)
    assert response.status_code == 201, response.text
    session = response.json()
    return session, {'Authorization': 'Bearer ' + session['token']}


def expire_countdown(c, uid):
    with c.app.state.store.db() as db:
        db.execute('UPDATE clients SET countdown_until=? WHERE id=?',
                   ((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(), uid))


def test_one_drive_automatically_selects_and_requires_countdown(lab):
    c, profile, ticket, request = setup(lab)
    session, native = enroll(c, ticket, request)
    policy = session['automation']
    assert policy['automaticDiskSelection'] and not policy['manualDiskSelection']
    assert policy['targetIdentity'] == TARGET and policy['startDelaySeconds'] == 5
    row = c.get('/api/clients/' + session['clientId'], headers=auth()).json()['client']
    assert not row['disk_selection_ready']
    assert c.post('/api/pxe/start', headers=native, json={}).status_code == 409
    chosen = c.post('/api/pxe/select', headers=native, json={'profile_id': profile['id'], 'target_identity': TARGET})
    assert chosen.status_code == 200, chosen.text
    assert c.post('/api/pxe/start', headers=native, json={}).status_code == 409
    countdown = c.post('/api/pxe/countdown', headers=native, json={'action': 'start'})
    assert countdown.status_code == 200 and countdown.json()['startDelaySeconds'] == 5
    assert c.post('/api/pxe/start', headers=native, json={}).status_code == 409
    expire_countdown(c, session['clientId'])
    started = c.post('/api/pxe/start', headers=native, json={})
    assert started.status_code == 201, started.text
    run = c.get('/api/routines', headers=auth()).json()['runs'][0]
    assert run['target'] == TARGET and run['job_id'] == started.json()['id']


@pytest.mark.parametrize('count', [2, 3])
@pytest.mark.parametrize('via', ['pc', 'portal'])
def test_multiple_drives_wait_for_explicit_choice_and_bind_that_drive(lab, count, via):
    c, profile, ticket, request = setup(lab, count)
    request['targets'].reverse()  # Enumeration order must not trigger automatic first-disk selection.
    session, native = enroll(c, ticket, request)
    uid = session['clientId']
    assert not session['automation']['automaticDiskSelection']
    assert session['automation']['manualDiskSelection'] and session['automation']['targetIdentity'] == ''
    row = c.get('/api/clients/' + uid, headers=auth()).json()['client']
    assert row['stage'] == 'waiting-for-input' and row['disk_selection_ready'] and row['needs_attention']
    assert c.get('/api/routine-jobs?view=attention', headers=auth()).json()['total'] == 1
    with c.app.state.store.db() as db:
        assert db.execute('SELECT count(*) FROM jobs').fetchone()[0] == 0
        assert db.execute('SELECT target FROM routine_runs').fetchone()[0] is None
    assert c.post('/api/pxe/start', headers=native, json={}).status_code == 409
    assert c.post('/api/pxe/countdown', headers=native, json={'action': 'start'}).status_code == 409
    choice = {'profile_id': profile['id'], 'target_identity': TARGET}
    if via == 'portal':
        assert c.post('/api/clients/' + uid + '/disk', headers=auth(), json={**choice, 'target_identity': 'serial:MISSING'}).status_code == 409
        selected = c.post('/api/clients/' + uid + '/disk', headers=auth(), json=choice)
        assert selected.status_code == 200, selected.text
        pulse = c.post('/api/pxe/heartbeat', headers=native, json={'stage': 'waiting-for-input'}).json()
        assert pulse['automation']['automaticDiskSelection'] and pulse['automation']['targetIdentity'] == TARGET
    chosen = c.post('/api/pxe/select', headers=native, json=choice)
    assert chosen.status_code == 200 and chosen.json()['target_identity'] == TARGET
    assert c.post('/api/pxe/select', headers=native, json={**choice, 'target_identity': 'serial:DISK-1'}).status_code == 409
    if via == 'portal':
        assert c.post('/api/pxe/start', headers=native, json={}).status_code == 409
        assert c.post('/api/pxe/countdown', headers=native, json={'action': 'start'}).status_code == 200
        expire_countdown(c, uid)
    started = c.post('/api/pxe/start', headers=native, json={})
    assert started.status_code == 201, started.text
    run = c.get('/api/routines', headers=auth()).json()['runs'][0]
    assert run['target'] == TARGET and run['job_id'] == started.json()['id']


def test_no_drive_and_older_media_do_not_fall_back_to_automatic_selection(lab):
    c, profile, ticket, request = setup(lab)
    assert c.post('/api/pxe/enroll', headers={'Authorization': 'Bearer ' + ticket}, json={**request, 'targets': []}).status_code == 422
    old = c.post('/api/pxe/enroll', headers={'Authorization': 'Bearer ' + ticket}, json={**request, 'client_version': '0.12.10'})
    assert old.status_code == 409 and '0.12.11' in old.json()['detail']
    # Rejected enrollments must not consume the boot ticket.
    session, native = enroll(c, ticket, request)
    assert session['automation']['targetIdentity'] == TARGET


def test_auto_unless_multiple_is_not_an_erasure_policy(lab):
    c, root, profile = lab
    install_media(root)
    spec = {'name': 'Invalid erase policy', 'steps': [{'id': 'boot', 'kind': 'boot'},
            {'id': 'erase', 'kind': 'erase', 'target_policy': 'auto-unless-multiple'},
            {'id': 'finish', 'kind': 'finish'}]}
    saved = c.post('/api/routines', headers=auth(), json=spec)
    assert saved.status_code == 201, saved.text
    published = c.post('/api/routines/' + saved.json()['id'] + '/publish', headers=auth(), json={'expected_generation': 1})
    assert published.status_code == 409 and 'Erasure requires' in published.json()['detail']
