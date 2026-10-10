"""Nwipe policy and routine graph acceptance; synthetic HTTP and regular files only."""
import json,re,pytest
from repxe import erase_policy, routines
from test_api import auth
from test_pxe import lab
from test_workflows import install_media,MAC,TARGET,SIZE,PDF,result
from test_routines import select,intake


def graph(c,p,root,method='zero',failure=True):
    path=root/'boot/custom/scripts/failure.ipxe';path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text('#!ipxe\necho ${repxe_serial} ${repxe_error}\nchain ${repxe_continue}\n')
    steps=[{'id':'boot','kind':'boot'},{'id':'erase','kind':'erase','method':method},{'id':'windows','kind':'windows','profile_id':p['id']},
           *([{'id':'failure','kind':'ipxe','ipxe_path':'scripts/failure.ipxe'}] if failure else []),{'id':'finish','kind':'finish'}]
    edges=[{'source':'boot','target':'erase'},{'source':'erase','target':'windows'},{'source':'windows','target':'finish'}]
    if failure:edges += [{'source':'erase','target':'failure','outcome':'failure'},{'source':'failure','target':'finish'}]
    spec={'name':'Nwipe graph','graph':True,'steps':steps,'connections':edges}
    r=c.post('/api/routines',headers=auth(),json=spec);assert r.status_code==201,r.text
    uid=r.json()['id'];r=c.post('/api/routines/'+uid+'/publish',headers=auth(),json={'expected_generation':1});assert r.status_code==200,r.text
    c.put('/api/routine-settings',headers=auth(),json={'default_id':uid})
    return uid,spec



def test_graph_invalid_routes_draft_and_publication(lab):
    c,root,p=lab;install_media(root);uid,spec=graph(c,p,root)
    for edges,reason in [([{'source':'erase','target':'erase'}],'different'),
        ([{'source':'boot','target':'finish'}],'Every node'),
        ([{'source':'boot','target':'erase'},{'source':'erase','target':'windows'},{'source':'erase','target':'windows','outcome':'failure'},{'source':'windows','target':'finish'},{'source':'failure','target':'finish'}],'Failure branches')]:
        bad={**spec,'connections':edges}
        r=c.post('/api/routines',headers=auth(),json=bad)
        if reason=='different':assert r.status_code==422;continue
        assert r.status_code==201,r.text
        published=c.post('/api/routines/'+r.json()['id']+'/publish',headers=auth(),json={'expected_generation':1})
        assert published.status_code==409 and reason in published.text
    edges=[{'source':'boot','target':'erase'},{'source':'erase','target':'failure'},{'source':'failure','target':'erase'},{'source':'windows','target':'finish'}]
    r=c.post('/api/routines',headers=auth(),json={**spec,'connections':edges});assert r.status_code==201
    assert 'loops' in c.post('/api/routines/'+r.json()['id']+'/publish',headers=auth(),json={'expected_generation':1}).text



def erase_claim(c,mac=MAC,method='zero',capabilities=None,serial='PC-001'):
    script=c.get('/pxe/managed.ipxe',params={'mac':mac}).text
    ticket=re.search(r'repxe_ticket=([A-Za-z0-9_-]+)',script)[1]
    inv={'system_serial':serial,'disks':[{'identity':TARGET if mac==MAC else 'serial:OTHER','size_bytes':SIZE,'capabilities':capabilities or {}}]}
    r=c.post('/api/erase/claim',headers={'Authorization':'Bearer '+ticket},json={'mac':mac,'agent_version':'re-pxe-shredos-2','inventory':inv})
    assert r.status_code==201,r.text
    run=r.json();h={'Authorization':'Bearer '+run['token']};assert c.post('/api/erase/begin',headers=h,json={}).status_code==200
    return run,h



def test_failure_custom_screen_bound_snapshot_one_use_and_retained_report(lab):
    c,root,p=lab;install_media(root);uid,spec=graph(c,p,root);select(c);intake(c)
    run,h=erase_claim(c)
    bad=result(run,run_id=run['id'],system_serial='WRONG',settings_sha256=run['settings_sha256'],result='failed',exit_code=1,errors=1,certificate_sha256=None)
    assert c.post('/api/erase/result',headers=h,json=bad).status_code==409
    bad['system_serial']=run['system_serial'];bad['evidence']={'engine':'nwipe','error':'Drive failed; ${unsafe}\nboot','safe_to_reboot':True}
    r=c.post('/api/erase/result',headers=h,json=bad);assert r.status_code==200 and r.json()['reboot_for_routine']
    routine=c.get('/api/routines',headers=auth()).json()['runs'][0];assert routine['stage']=='custom-boot' and routine['error']
    # Editing the workspace does not change the already-published boot script.
    (root/'boot/custom/scripts/failure.ipxe').write_text('#!ipxe\necho CHANGED\n')
    script=c.get('/pxe/managed.ipxe',params={'mac':MAC}).text
    assert 'CHANGED' not in script and 'set repxe_serial PC-001' in script
    assert '${unsafe}' not in script and '\nboot\n' not in script
    assert 'wimboot' not in script and 'nwipe' not in script
    url=re.search(r'set repxe_continue (.+)',script)[1].replace('http://127.0.0.1:8089','')
    assert c.get(url).status_code==200
    assert c.get(url).status_code==401
    r=c.get('/api/routine-runs/'+routine['id'],headers=auth()).json();assert r['stage']=='failed' and r['error'].startswith('Drive failed')
    pdf=c.get('/api/job-reports/routine:'+r['id']+'/pdf',headers=auth());assert pdf.status_code==200 and pdf.content.startswith(b'%PDF')



def test_nwipe_command_controls_match_pinned_cli_and_count_device_errors(tmp_path):
    from repxe import shredos_agent
    policy=erase_policy.settings({'method':'prng','rounds':3,'verification':'last','prng':'chacha20','final_blank':False,
        'io_mode':'direct','direction':'reverse','sync_blocks':100,'retry_io_errors':False,'abort_on_block_errors':False})
    args=shredos_agent.nwipe_args(policy,'/dev/vda',tmp_path)
    assert args[-1]=='/dev/vda'
    assert {'--directio','--reverse','--sync=100','--no-retry-on-io-errors','--no-abort-on-block-errors','--prng=chacha20','--verify=last'} <= set(args)
    assert not any(a.startswith('--io-mode') for a in args)
    counts=shredos_agent.device_errors(' vdb | 50 | 50 | 50 |\n! vda | 2 | 3 | 4 |','/dev/vda')
    assert counts=={'pass_errors':2,'verification_errors':3,'sync_errors':4}
