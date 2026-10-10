"""Optional Purge follow-up is immutable policy, not inferred success evidence."""
import pytest
from repxe import erase_policy
from test_pxe import lab
from test_api import auth
from test_nist_presets import media,graph,queue_inventory,claim,completed,evidence
from test_workflows import PDF


def test_followup_default_and_legacy_hash_compatibility():
    default=erase_policy.settings({'method':'nist_purge'})
    assert default['prng_followup'] is True
    assert erase_policy.fingerprint(default)==erase_policy.fingerprint({**default,'prng_followup':True})
    assert erase_policy.fingerprint(default)!=erase_policy.fingerprint({**default,'prng_followup':False})
    assert 'prng_followup' in erase_policy.authorized_keys(default)
    for method in ('zero','prng','nist_clear','sanitize_block'):
        assert 'prng_followup' not in erase_policy.authorized_keys({'method':method})
        assert erase_policy.fingerprint({'method':method,'prng_followup':False})==erase_policy.fingerprint({'method':method,'prng_followup':True})
    assert erase_policy.fingerprint({'method':'zero'})=='a0180d9311b7907aad9ca58d5a403f180ffa7adbf9d44cb037744030287cd1d6'


@pytest.mark.parametrize('value',[None,0,1,'false','true'])
def test_followup_strict_bool_rejected_by_policy_and_api(lab,value):
    c,_,_=lab
    with pytest.raises(ValueError,match='boolean'):erase_policy.settings({'method':'nist_purge','prng_followup':value})
    response=c.post('/api/workflows',headers=auth(),json={'name':'Bad follow-up','method':'nist_purge','prng_followup':value})
    assert response.status_code==422


def test_native_only_purge_needs_original_pdf_without_followup(lab):
    c,root,p=lab;media(root);graph(c,p,prng_followup=False);queue_inventory(c)
    run,h,_=claim(c,begin=True)
    assert run['prng_followup'] is False
    response=c.put('/api/erase/certificate/followup',headers={**h,'Content-Type':'application/pdf'},content=PDF)
    assert response.status_code==409
    payload=evidence(run);assert not any(phase['phase']=='followup' for phase in payload['phases'])
    wrong={**payload,'prng_followup':True};assert completed(c,run,h,wrong).status_code==409
    wrong={**payload,'controller_state':'unknown'};assert completed(c,run,h,wrong).status_code==409
    wrong=evidence(run,purge=True);assert completed(c,run,h,wrong).status_code==409
    response=completed(c,run,h,payload);assert response.status_code==200,response.text
    routine=c.get('/api/routines',headers=auth()).json()['runs'][0]
    assert routine['stage']=='windows-boot'
    report=c.get('/api/job-reports/'+routine['report_id'],headers=auth()).json()
    assert report['erasure']['evidence']['prng_followup'] is False
    with c.app.state.store.db() as db:assert not db.execute('SELECT * FROM workflow_phase_certificates').fetchall()


def test_default_checked_purge_still_requires_followup(lab):
    c,root,p=lab;media(root);graph(c,p);queue_inventory(c);run,h,_=claim(c,begin=True)
    assert run['prng_followup'] is True
    response=completed(c,run,h,evidence(run));assert response.status_code==409 and 'follow-up PDF' in response.text
