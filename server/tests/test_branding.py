import base64
import hashlib
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from pypdf import PdfReader
from test_client_management import lab, auth
from test_job_reports import start_with_specs
from repxe import branding, shredos_agent


def raster():
    output=BytesIO();Image.new('RGBA',(420,140),(17,130,102,200)).save(output,format='PNG')
    return output.getvalue()


def test_upload_validation_auth_persistence_and_derivatives(lab):
    c,root,p=lab
    assert c.put('/api/branding/logo',content=raster()).status_code==401
    assert c.get('/api/branding').json()['logo_url'] is None
    assert c.put('/api/branding/logo',headers=auth(),content=b'<svg onload="bad()"/>').status_code==422
    response=c.put('/api/branding/logo',headers=auth(),content=raster()+b'<script>bad()</script>')
    assert response.status_code==200,response.text
    row=branding.read(c.app.state.store)
    assert b'<script>' not in row['png']
    assert Image.open(BytesIO(row['jpeg'])).size==(256,256)
    assert Image.open(BytesIO(row['icon'])).size==(64,64)
    assert c.get(response.json()['logo_url']).content==row['png']
    assert c.get('/branding/../database/catalog.sqlite').status_code==404
    assert c.get('/api/branding').json()==response.json()
    assert c.delete('/api/branding/logo',headers=auth()).status_code==200
    assert branding.read(c.app.state.store) is None
    assert c.get('/branding/favicon.png').status_code==200


def test_csrf_is_required_for_website_mutation(lab):
    c,root,p=lab
    session=c.post('/api/login',json={'username':'admin','password':'admin'})
    assert session.status_code==200
    assert c.put('/api/branding/logo',content=raster()).status_code==403
    assert c.put('/api/branding/logo',headers={'X-CSRF-Token':session.json()['csrf_token']},content=raster()).status_code==200


def test_repxe_pdf_embeds_logo(lab):
    c,root,p=lab;uid,job=start_with_specs(c,p)
    c.put('/api/branding/logo',headers=auth(),content=raster())
    pdf=PdfReader(BytesIO(c.get('/api/job-reports/'+job+'/pdf',headers=auth()).content))
    assert len(pdf.pages[0].images)==1
    assert 'SYS-001' in pdf.pages[0].extract_text()


def test_claim_logo_in_ram_and_clear_between_drives(lab,tmp_path):
    c,root,p=lab;c.put('/api/branding/logo',headers=auth(),content=raster())
    value=branding.claim_logo(c.app.state.store)
    payload=shredos_agent.prepare_logo({'branding_logo':value},tmp_path)
    assert (tmp_path/'logo.jpg').read_bytes()==payload
    assert hashlib.sha256(payload).hexdigest()==value['sha256']
    with pytest.raises(RuntimeError): shredos_agent.prepare_logo({'branding_logo':{**value,'sha256':'0'*64}},tmp_path)
    assert shredos_agent.prepare_logo({},tmp_path) is None
    assert not (tmp_path/'logo.jpg').exists()


def test_original_per_drive_pdf_selected_byte_for_byte(tmp_path):
    per=b'%PDF-1.4\nOriginal multi-page drive SMART report\n%%EOF\n'
    (tmp_path/'nwipe_report_test_Model_Test_Serial_001_device_nvme0n1.pdf').write_bytes(per)
    (tmp_path/'nwipe_system_report.pdf').write_bytes(b'%PDF-1.4\nSystem summary\n%%EOF\n')
    assert shredos_agent.original_drive_pdf(tmp_path,'/dev/nvme0n1')==per
    assert shredos_agent.original_drive_pdf(tmp_path,'/dev/sda') is None
    (tmp_path/'nwipe_report_other_device_nvme0n1.pdf').write_bytes(per)
    assert shredos_agent.original_drive_pdf(tmp_path,'/dev/nvme0n1') is None
