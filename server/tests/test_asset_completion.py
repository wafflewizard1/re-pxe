"""Fresh completion facts and serial-bound evidence for shared technician NICs."""
from copy import deepcopy
from io import BytesIO
import json
import zipfile

import pytest
from pypdf import PdfReader
from test_api import client, native_job, technician_snapshot
from test_client_management import lab, start, auth, control
from test_job_reports import SPECS
from test_routine_activity import wipe_only, finish_erase
from test_workflows import PDF


def record_specs(c, uid, serial):
    specs = deepcopy(SPECS)
    specs['system']['serial'] = serial
    with c.app.state.store.db() as db:
        db.execute('INSERT OR REPLACE INTO client_inventory VALUES(?,?,?)', (uid, json.dumps(specs), '2026-10-08T01:00:00+00:00'))
    return specs


def test_shared_adapter_keeps_serial_assets_controls_and_paging_separate(lab):
    c, _, profile = lab
    _, first, first_job, _ = start(c, profile)
    record_specs(c, first, 'Z-002')
    _, second, second_job, _ = start(c, profile)
    record_specs(c, second, 'A-001')
    listing = c.get('/api/clients?view=all&page_size=1', headers=auth()).json()
    assert listing['client_total'] == 2 and listing['clients'][0]['system_serial'] == 'A-001'
    details = c.get('/api/clients/' + first, headers=auth()).json()
    assert [s['id'] for s in details['sessions']] == [first]
    assert details['client']['can_reset']
    assert [r['report_id'] for r in details['attachments']] == [first_job]
    recent = c.get('/api/dashboard', headers=auth()).json()['recent_jobs']
    assert {r['id']: r['system_serial'] for r in recent} == {first_job: 'Z-002', second_job: 'A-001'}
    assert c.post('/api/clients/' + first + '/reset', headers=auth(), json=control(job=first_job)).json()['cancelled_jobs'] == [first_job]
    assert c.get('/api/jobs/' + second_job, headers=auth()).json()['stage'] == 'created'
    assert not c.get('/api/clients/' + second, headers=auth()).json()['controls']


def test_same_serial_on_different_adapters_retains_asset_history(lab):
    c, _, profile = lab
    _, first, first_job, _ = start(c, profile)
    record_specs(c, first, 'Asset-001')
    _, second, second_job, _ = start(c, profile, '52:54:00:12:34:57')
    record_specs(c, second, 'asset-001')
    details = c.get('/api/clients/' + second, headers=auth()).json()
    assert {s['id'] for s in details['sessions']} == {first, second}
    assert {r['report_id'] for r in details['attachments']} == {first_job, second_job}
    assert not c.get('/api/clients/' + first, headers=auth()).json()['client']['can_reset']


@pytest.mark.parametrize('client_serial,erase_serial,status', [
    ('ASSET-A', 'ASSET-B', 200),
    ('ASSET-A', 'asset-a', 409),
    ('ASSET-A', '', 409),
    ('', 'ASSET-B', 409),
])
def test_shared_adapter_reset_respects_active_erasure_pc_binding(lab, client_serial, erase_serial, status):
    from repxe.workflows import Template
    c, _, profile = lab
    _, client_id, job, _ = start(c, profile)
    record_specs(c, client_id, client_serial)
    # A technician has moved this USB NIC to another PC, whose independent
    # armed operation must survive controls on the old Windows asset.
    at = '2026-10-08T01:00:00+00:00'
    inventory = json.dumps({'system_serial': erase_serial, 'disks': [
        {'identity': 'serial:OTHER-DISK', 'size_bytes': 16 * 1024**2}]})
    with c.app.state.store.db() as db:
        db.execute('''INSERT INTO workflow_runs(id,template_id,spec,mac,target,size,asset_tag,
            stage,created,updated,inventory,token_hash,expires) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            ('active-erase', 'fixture', Template(name='Other asset erase').model_dump_json(),
             '52:54:00:12:34:56', 'serial:OTHER-DISK', 16 * 1024**2, '', 'erasing', at, at,
             inventory, 'retained-erasure-credential', '2099-01-01T00:00:00+00:00'))
        before = dict(db.execute("SELECT * FROM workflow_runs WHERE id='active-erase'").fetchone())
    response = c.post('/api/clients/' + client_id + '/reset', headers=auth(), json=control(job=job))
    assert response.status_code == status, response.text
    with c.app.state.store.db() as db:
        assert dict(db.execute("SELECT * FROM workflow_runs WHERE id='active-erase'").fetchone()) == before
        job_stage = db.execute('SELECT stage FROM jobs WHERE id=?', (job,)).fetchone()['stage']
        controls = db.execute('SELECT count(*) FROM client_control_events WHERE client_id=?', (client_id,)).fetchone()[0]
    if status == 200:
        assert response.json()['cancelled_jobs'] == [job] and job_stage == 'cancelled' and controls == 1
    else:
        assert 'active erasure workflow' in response.text and job_stage == 'created' and controls == 0


def enrolled_native(c, monkeypatch):
    job, cap, snapshot = native_job(c, monkeypatch)
    with c.app.state.store.db() as db:
        db.execute("INSERT INTO clients(id,mac,ticket_hash,ticket_expires,created,seen,job_id) VALUES('asset-client','52:54:00:12:34:56','fixture','2099-01-01T00:00:00+00:00','2026-10-08T01:00:00+00:00','2026-10-08T01:00:00+00:00',?)", (job,))
    specs = record_specs(c, 'asset-client', 'SYS-001')
    return job, cap, technician_snapshot(snapshot), specs


def test_finish_refreshes_gpus_and_battery_in_asset_report_and_pdf(client, monkeypatch):
    job, cap, snapshot, specs = enrolled_native(client, monkeypatch)
    specs['graphics'] = [{'name': 'Newly identified GPU', 'manufacturer': 'Example'}]
    specs['batteries'] = [{'name': 'Battery', 'serial': 'BAT-001', 'design_capacity_mwh': 50000,
                          'full_charge_capacity_mwh': 40000, 'health_percent': 1, 'cycle_count': 123,
                          'source': 'powercfg-batteryreport'}]
    snapshot['hardware'] = {'specs': specs}
    result = client.post(cap['path'], headers={'Authorization': 'Bearer ' + cap['token']}, json=snapshot)
    assert result.status_code == 200, result.text
    assert client.post(cap['path'], headers={'Authorization': 'Bearer ' + cap['token']}, json=snapshot).json()['duplicate']
    for endpoint in ['/api/clients/asset-client', '/api/job-reports/' + job]:
        detail = client.get(endpoint, headers=auth()).json()
        actual = detail.get('device', detail)['specs']
        assert actual['graphics'][0]['name'] == 'Newly identified GPU'
        assert actual['batteries'][0]['health_percent'] == 80
    text = '\n'.join(p.extract_text() for p in PdfReader(BytesIO(client.get('/api/job-reports/' + job + '/pdf', headers=auth()).content)).pages)
    assert 'Newly identified GPU' in text and '80.0%' in text and '40000 mWh' in text and '123' in text


def test_completion_refresh_rejects_changed_pc_and_malformed_inventory(client, monkeypatch):
    job, cap, snapshot, specs = enrolled_native(client, monkeypatch)
    headers = {'Authorization': 'Bearer ' + cap['token']}
    specs['system']['serial'] = 'OTHER-PC'
    snapshot['hardware'] = {'specs': specs}
    assert client.post(cap['path'], headers=headers, json=snapshot).status_code == 409
    specs['system']['serial'] = 'SYS-001'
    specs['graphics'] = [{'name': 'x' * 300}]
    assert client.post(cap['path'], headers=headers, json=snapshot).status_code == 422
    with client.app.state.store.db() as db:
        assert db.execute('SELECT report FROM jobs WHERE id=?', (job,)).fetchone()['report'] is None
        assert json.loads(db.execute("SELECT inventory FROM client_inventory WHERE client_id='asset-client'").fetchone()['inventory'])['system']['serial'] == 'SYS-001'


@pytest.mark.parametrize('design,full,expected', [(0,40000,None), (50000,0,None), (50000,55000,110), (50000,40000,80)])
def test_battery_health_uses_capacity_and_preserves_unknowns(design, full, expected):
    from repxe.device_inventory import Battery
    assert Battery(design_capacity_mwh=design, full_charge_capacity_mwh=full).health_percent == expected


def test_shredos_pdf_attached_by_serial_and_exported_with_portal_pdf(lab):
    c, root, profile = lab
    run = wipe_only(c, root)
    finish_erase(c)
    assert c.put('/api/routine-settings', headers=auth(), json={'enabled': False}).status_code == 200
    _, client_id, job, _ = start(c, profile, '52:54:00:12:34:77')
    record_specs(c, client_id, 'PC-001')
    detail = c.get('/api/clients/' + client_id, headers=auth()).json()
    erased = next(a for a in detail['attachments'] if a['report_id'] == run['report_id'])
    assert erased['certificate_url'] and erased['certificate_sha256']
    assert c.get(erased['certificate_url'], headers=auth()).content == PDF
    exported = c.post('/api/job-reports/export', headers=auth(), json={'ids': [run['report_id']]})
    assert exported.status_code == 200, exported.text
    with zipfile.ZipFile(BytesIO(exported.content)) as archive:
        assert len(archive.namelist()) == 2
        assert archive.read(next(n for n in archive.namelist() if n.startswith('shredos-'))) == PDF
