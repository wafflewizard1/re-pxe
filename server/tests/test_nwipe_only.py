import json,re,pytest
from repxe import erase_policy,capabilities,shredos_agent
from test_api import auth
from test_pxe import lab
from test_workflows import install_media,MAC,TARGET,SIZE
from test_routines import make,select,intake

REMOVED=['purge','ata_sanitize_block','nvme_sanitize_block','ata_secure_enhanced','ata_secure','nvme_format_user','ata_sanitize_crypto','nvme_sanitize_crypto','nvme_format_crypto','ata_sanitize_overwrite','nvme_sanitize_overwrite']

@pytest.mark.parametrize('method',REMOVED)
def test_removed_methods_rejected_by_policy_and_api(lab,method):
    c,root,p=lab;install_media(root)
    with pytest.raises(ValueError,match='Only nwipe'):erase_policy.resolve({'method':method,'fallback_method':'zero'}, {})
    assert c.post('/api/workflows',headers=auth(),json={'name':'No hardware','method':method}).status_code==422
    spec={'name':'No hardware','steps':[{'id':'boot','kind':'boot'},{'id':'erase','kind':'erase','method':method},{'id':'finish','kind':'finish'}]}
    assert c.post('/api/routines',headers=auth(),json=spec).status_code==422
    spec['steps'][1]['method']='zero';spec['steps'][1]['fallback_method']=method
    assert c.post('/api/routines',headers=auth(),json=spec).status_code==422
    assert set(x['id'] for x in c.get('/api/routines',headers=auth()).json()['erase_methods'])==set(erase_policy.METHODS)

def test_legacy_unstarted_operation_cannot_be_armed_and_history_remains(lab):
    c,root,p=lab;install_media(root);make(c,p,{'method':'zero'});select(c);intake(c)
    with c.app.state.store.db() as db:
        row=db.execute('SELECT * FROM workflow_runs').fetchone();spec=json.loads(row['spec']);spec['method']='nvme_sanitize_crypto';db.execute('UPDATE workflow_runs SET spec=? WHERE id=?',(json.dumps(spec),row['id']))
    script=c.get('/pxe/managed.ipxe',params={'mac':MAC}).text;ticket=re.search(r'repxe_ticket=([A-Za-z0-9_-]+)',script)[1]
    inv={'system_serial':'PC-001','disks':[{'identity':TARGET,'size_bytes':SIZE}]}
    response=c.post('/api/erase/claim',headers={'Authorization':'Bearer '+ticket},json={'mac':MAC,'agent_version':'re-pxe-shredos-8','inventory':inv})
    assert response.status_code==409 and 'Only nwipe' in response.text
    with c.app.state.store.db() as db:assert db.execute('SELECT stage FROM workflow_runs').fetchone()[0]=='queued'
    historical=c.get('/api/workflow-runs/'+row['id'],headers=auth());assert historical.status_code==200 and historical.json()['workflow']['method']=='nvme_sanitize_crypto'
    routine=c.get('/api/routines',headers=auth()).json()['runs'][0]
    assert c.get('/api/job-reports/'+routine['report_id']+'/pdf',headers=auth()).status_code==200

def test_companion_contains_no_separate_hardware_engine(tmp_path):
    payload=capabilities.companion('https://127.0.0.1:8443','fixture')
    import gzip
    archive=gzip.decompress(payload)
    assert b'hardware_erase.py' not in archive and b're-pxe-hardware' not in archive
    assert b'/usr/bin/nwipe' in archive and b'--PDFreportpath=' in archive
    assert all(m['engine']=='nwipe' for m in erase_policy.catalogue())
    class API:
        def request(self,*a,**k):return {}
    with pytest.raises(RuntimeError,match='Only nwipe'):
        shredos_agent.execute_run(API(),{'method':'nvme_sanitize_crypto','token':'test'},tmp_path/'unused')
    assert not (tmp_path/'unused').exists()
