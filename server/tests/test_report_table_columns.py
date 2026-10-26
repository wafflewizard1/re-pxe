"""Report-table observations and chronology across pagination; synthetic evidence only."""
import json
from copy import deepcopy
from test_client_management import lab, start, auth
from test_job_reports import SPECS
from test_routines import make, select
from test_workflows import install_media


def record(c,p,index,specs=None,report=None):
    _,client,job,_=start(c,p,mac=f'52:54:00:23:10:{index:02x}')
    at=f'2026-10-{index+1:02d}T12:00:00+00:00'
    with c.app.state.store.db() as db:
        db.execute("UPDATE jobs SET stage='complete',created=?,updated=?,report=? WHERE id=?",(at,at,json.dumps(report) if report else None,job))
        if specs is not None:db.execute('INSERT INTO client_inventory VALUES(?,?,?)',(client,json.dumps(specs),at))
    return client,job


def test_recent_jobs_select_newest_eight_before_serial_sorting(lab):
    c,root,p=lab
    ids=[]
    for i in range(10):
        specs=deepcopy(SPECS);specs['system']['serial']=f'A-{i:02d}'
        _,job=record(c,p,i,specs);ids.append(job)
    dashboard=c.get('/api/dashboard',headers=auth()).json()
    for rows in (dashboard['recent_jobs'],dashboard['routines']['recent']):
        assert [r['id'] for r in rows]==list(reversed(ids))[:8]
    # Calendar strings with different offsets must compare as instants.
    with c.app.state.store.db() as db:
        db.execute('UPDATE jobs SET updated=? WHERE id=?',('2026-10-11T00:30:00+13:00',ids[0]))
    assert c.get('/api/dashboard',headers=auth()).json()['recent_jobs'][0]['id']==ids[-1]


def test_table_observations_use_completion_specs_and_do_not_expose_raw_state(lab):
    c,root,p=lab
    enrollment=deepcopy(SPECS);enrollment['memory']['total_bytes']=2*1024**3
    final=deepcopy(SPECS);final['batteries']=[
        {'name':'Primary','design_capacity_mwh':50000,'full_charge_capacity_mwh':40000,'cycle_count':0,'health_percent':1},
        {'name':'Second','design_capacity_mwh':10000,'full_charge_capacity_mwh':12000,'cycle_count':250}]
    final['storage'][0]['health']={'passed':True,'power_on_hours':42,'percentage_used':0}
    final['graphics']=[{'name':'Graphics <A>'}]
    report={'native':{'hardware':{'specs':final},'private_secret':'DO-NOT-PROJECT'},'delivery_token':'PRIVATE-TOKEN'}
    _,job=record(c,p,1,enrollment,report)
    response=c.get('/api/job-reports',headers=auth());assert response.status_code==200,response.text
    item=next(r for r in response.json()['reports'] if r['id']==job);data=item['column_data']
    assert data['memory_bytes']==16*1024**3
    assert data['battery_health']==80 and data['battery_cycles']==250
    assert data['storage_bytes']==128*1024**3 and data['drive_count']==1
    assert data['smart']=='Passed' and data['ssd_wear']==0 and data['power_on_hours']==42
    assert data['secure_boot']==0 and data['asset_tag']=='ASSET-007' and data['graphics']=='Graphics <A>'
    assert all(value not in response.text for value in ('DO-NOT-PROJECT','PRIVATE-TOKEN','search_inventory','search_pre_inventory'))


def test_numeric_columns_sort_globally_with_missing_values_last(lab):
    c,root,p=lab
    for i,(health,cycles,memory,disk) in enumerate([(9,100,2,128),(80,0,32,1024),(105,10,8,256),(None,None,0,0)]):
        s=deepcopy(SPECS);s['system']['serial']=str(i)
        s['memory']['total_bytes']=memory*1024**3;s['storage'][0]['size_bytes']=disk*1024**3
        s['batteries']=[{'design_capacity_mwh':10000,'full_charge_capacity_mwh':health*100,'cycle_count':cycles}] if health is not None else []
        record(c,p,i,s)
    for key,ascending in [('battery_health',['0','1','2','3']),('battery_cycles',['1','2','0','3']),
                          ('memory_bytes',['0','2','1','3']),('storage_bytes',['0','2','1','3'])]:
        for direction,expected in [('asc',ascending),('desc',list(reversed(ascending[:-1]))+['3'])]:
            rows=[]
            for page in range(1,5):
                r=c.get('/api/job-reports',headers=auth(),params={'sort':key,'order':direction,'page_size':1,'page':page})
                assert r.status_code==200,r.text
                rows.extend(r.json()['reports'])
            assert [r['system_serial'] for r in rows]==expected,(key,direction)
    filters={'sort':'battery_health','order':'desc'}
    assert c.post('/api/job-reports/export',headers=auth(),json={'filters':filters,'include_certificates':False}).status_code==200


def test_routine_pre_windows_specs_and_retained_smart_are_available(lab):
    c,root,p=lab;install_media(root);make(c,p);select(c)
    with c.app.state.store.db() as db:
        run=db.execute('SELECT * FROM routine_runs').fetchone()
        pre={'system_serial':'PRE-001','cpu':'Earlier CPU','memory_bytes':8*1024**3,
             'disks':[{'identity':'serial:LAB-001','model':'SSD','size_bytes':64*1024**3,
                       'health':{'passed':False,'power_on_hours':0,'percentage_used':3}}]}
        db.execute("UPDATE routine_runs SET inventory=?,stage='complete' WHERE id=?",(json.dumps(pre),run['id']))
    response=c.get('/api/job-reports',headers=auth());assert response.status_code==200,response.text
    columns=response.json()['reports'][0]['column_data']
    assert columns['storage_bytes']==64*1024**3 and columns['memory_bytes']==8*1024**3
    assert columns['cpu']=='Earlier CPU' and columns['battery_health'] is None
    assert columns['smart']=='Failed' and columns['power_on_hours']==0
    # Later Windows inventory can omit SMART while the routine still retains it.
    c.put('/api/routine-settings',headers=auth(),json={'enabled':False})
    final=deepcopy(SPECS);final['system']['serial']='PRE-001'
    _,job=record(c,p,2,final)
    with c.app.state.store.db() as db:
        client=db.execute('SELECT id FROM clients WHERE job_id=?',(job,)).fetchone()[0]
        db.execute('UPDATE routine_runs SET job_id=?,client_id=? WHERE id=?',(job,client,run['id']))
    data=c.get('/api/job-reports',headers=auth()).json()['reports'][0]['column_data']
    assert data['smart']=='Failed' and data['power_on_hours']==0 and data['ssd_wear']==3
