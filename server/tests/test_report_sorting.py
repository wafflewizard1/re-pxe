import json
from datetime import datetime
from test_client_management import lab, start, auth
from test_routines import make, select
from test_workflows import install_media
from repxe.job_reports import ReportFilters


def test_report_columns_sort_across_pages_and_normalize_timestamp_offsets(lab):
    c,root,p=lab
    records=[('Z-003','Zulu','failed','2026-10-10T01:00:00+13:00'),('A-001','Alpha','complete','2026-10-09T14:00:00+00:00'),('M-002','Middle','cancelled','2026-10-09T13:00:00+00:00')]
    for n,(serial,model,stage,updated) in enumerate(records):
        _,uid,job,_=start(c,p,mac=f'52:54:00:10:10:{n:02x}')
        with c.app.state.store.db() as db:
            db.execute('UPDATE jobs SET stage=?,created=?,updated=? WHERE id=?',(stage,updated,updated,job))
            db.execute('UPDATE clients SET model=? WHERE id=?',(model,uid))
            db.execute('INSERT INTO client_inventory VALUES(?,?,?)',(uid,json.dumps({'system':{'serial':serial}}),updated))
    default=c.get('/api/job-reports',headers=auth()).json()['reports']
    assert [r['system_serial'] for r in default]==['A-001','M-002','Z-003']
    for sort,expected in [('serial',['A-001','M-002','Z-003']),('model',['A-001','M-002','Z-003']),('status',['M-002','A-001','Z-003']),('processed',['Z-003','M-002','A-001']),('created',['Z-003','M-002','A-001'])]:
        for order,serials in [('asc',expected),('desc',list(reversed(expected)))]:
            result=[]
            for page in (1,2,3):
                response=c.get('/api/job-reports',headers=auth(),params={'sort':sort,'order':order,'page_size':1,'page':page})
                assert response.status_code==200,response.text
                result+=response.json()['reports']
            assert [r['system_serial'] for r in result]==serials,(sort,order)
    assert ReportFilters().sort=='processed' and ReportFilters().order=='desc'
    assert c.get('/api/job-reports?sort=invalid',headers=auth()).status_code==422


def test_routine_column_sort_is_case_insensitive_and_export_accepts_it(lab):
    c,root,p=lab;install_media(root)
    first=make(c,p);select(c)
    run=c.get('/api/routines',headers=auth()).json()['runs'][0]
    with c.app.state.store.db() as db:
        original=dict(db.execute('SELECT * FROM routine_runs WHERE id=?',(run['id'],)).fetchone())
        db.execute('DELETE FROM routine_runs WHERE id=?',(run['id'],))
        for n,name in enumerate(['Zulu','alpha','Middle']):
            row={**original,'id':f'00000000-0000-4000-8000-{n:012d}','mac':f'52:54:00:10:20:{n:02x}'}
            spec=json.loads(row['spec']);spec['name']=name;row['spec']=json.dumps(spec)
            db.execute('INSERT INTO routine_runs('+','.join(row)+') VALUES('+','.join('?' for _ in row)+')',list(row.values()))
    for order,expected in [('asc',['alpha','Middle','Zulu']),('desc',['Zulu','Middle','alpha'])]:
        response=c.get('/api/job-reports',headers=auth(),params={'sort':'routine','order':order})
        assert response.status_code==200,response.text
        assert [r['routine_name'] for r in response.json()['reports']]==expected
    response=c.post('/api/job-reports/export',headers=auth(),json={'filters':{'sort':'routine','order':'desc'},'include_certificates':False})
    assert response.status_code==200,response.text
