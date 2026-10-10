from repxe import smart_health
from repxe.device_inventory import DriveHealth


def test_ata_health_failure_is_evidence_not_command_failure():
    h=smart_health.parse({'smart_status':{'passed':False},'power_on_time':{'hours':4200},'temperature':{'current':44},'ata_smart_attributes':{'table':[{'id':5,'name':'Reallocated_Sector_Ct','value':99,'worst':98,'thresh':10,'raw':{'value':7,'string':'7'},'when_failed':'now'},{'id':197,'raw':{'value':2}},{'id':198,'raw':{'value':1}}]}},8)
    DriveHealth.model_validate(h)
    assert h['status']=='available' and h['passed'] is False
    assert (h['reallocated_sectors'],h['pending_sectors'],h['uncorrectable_sectors'])==(7,2,1)
    assert h['attributes'][0]['when_failed']=='now'


def test_nvme_endurance_and_error_fields_preserve_zero_and_exhausted():
    h=smart_health.parse({'smart_status':{'passed':True},'nvme_smart_health_information_log':{'percentage_used':103,'critical_warning':0,'media_errors':0,'unsafe_shutdowns':7,'num_err_log_entries':0,'data_units_read':5,'data_units_written':8}},0)
    DriveHealth.model_validate(h)
    assert h['percentage_used']==103 and h['bytes_read']==2560000 and h['media_errors']==0


def test_unavailable_does_not_invent_healthy_or_zero_counters():
    h=smart_health.parse({'smartctl':{'messages':[{'string':'Unsupported'}]}},2)
    DriveHealth.model_validate(h)
    assert h['status']=='unavailable' and h['passed'] is None and h['percentage_used'] is None


def test_collector_uses_only_read_only_smart_query(monkeypatch):
    from types import SimpleNamespace
    calls=[]
    monkeypatch.setattr(smart_health.subprocess,'run',lambda args,**kw:calls.append(args) or SimpleNamespace(stdout='{"smart_status":{"passed":true}}',returncode=0))
    health,raw=smart_health.collect('/dev/nvme0n1')
    assert calls==[['smartctl','-a','-j','/dev/nvme0n1']] and health['passed']
