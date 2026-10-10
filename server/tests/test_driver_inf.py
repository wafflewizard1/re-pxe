import pytest
from repxe.driver_inf import amd64_catalog
from repxe.driver_library import inf_details
from test_api import client, driver_zip, start, send, auth


@pytest.mark.parametrize('directives,expected', [
    ('CatalogFile=common.cat', 'common.cat'),
    ('CatalogFile.nt=nt.cat\nCatalogFile=generic.cat', 'nt.cat'),
    ('CatalogFile.ntx86=x86.cat\nCatalogFile.NTAMD64=x64.cat\nCatalogFile.ntarm64=arm.cat', 'x64.cat'),
    ('CatalogFile=generic.cat\nCatalogFile.nt=nt.cat\nCatalogFile.ntamd64=x64.cat', 'x64.cat'),
    ('CatalogFile.NTamd64="x64;driver.cat" ; inline comment', 'x64;driver.cat'),
])
def test_select_effective_amd64_catalog(directives,expected):
    assert amd64_catalog('[Version]\n'+directives+'\n[Other]\nCatalogFile=unrelated.cat') == expected


@pytest.mark.parametrize('text', [
    '[Other]\nCatalogFile=x64.cat',
    '[Version]\nCatalogFile.ntx86=x86.cat',
    '[Version]\nCatalogFile=generic.cat\nCatalogFile.ntamd64=',
    '[Version]\nCatalogFile=x64.cat\nCatalogFile=other.cat',
])
def test_missing_or_ambiguous_catalog_is_rejected(text):
    with pytest.raises(ValueError):
        amd64_catalog(text)


@pytest.mark.parametrize('encoding', ['utf-8-sig','utf-16'])
def test_export_with_other_platform_catalogs_validates(client,encoding):
    inf='[Version]\nCatalogFile=x86.cat\nCatalogFile.NTx86=x86.cat\nCatalogFile.NTamd64=x64.cat\nCatalogFile.NTarm64=arm.cat\n'
    payload=driver_zip({'hp/driver.inf':inf.encode(encoding),'hp/X64.CAT':'fixture','hp/driver.sys':'fixture'})
    uid=start(client,payload);assert send(client,uid,payload).status_code==200
    result=client.post(f'/api/uploads/{uid}/finish',headers=auth())
    assert result.status_code==200,result.text
    assert result.json()['status']=='published'


def test_generic_catalog_cannot_mask_missing_amd64_catalog(client):
    payload=driver_zip({'hp/driver.inf':'[Version]\nCatalogFile=generic.cat\nCatalogFile.NTamd64=x64.cat\n','hp/generic.cat':'fixture'})
    uid=start(client,payload);assert send(client,uid,payload).status_code==200
    result=client.post(f'/api/uploads/{uid}/finish',headers=auth())
    assert result.status_code==422
    assert 'hp/driver.inf' in result.json()['detail'] and 'x64.cat' in result.json()['detail']


def test_large_intel_style_inf_validates_and_keeps_provider_strings(client):
    inf='[Version]\nCatalogFile=audio.cat\nProvider=%Maker%\n'+('; exported device mappings\n'*220000)+'[Strings]\nMaker="Intel Corporation"\n'
    assert 4*1024**2 < len(inf) < 16*1024**2
    payload=driver_zip({'audio/driver.inf':inf,'audio/audio.cat':'fixture'})
    uid=start(client,payload);assert send(client,uid,payload).status_code==200
    result=client.post(f'/api/uploads/{uid}/finish',headers=auth())
    assert result.status_code==200,result.text
    path=client.app.state.store.root/'drivers/packages'/uid/'files/audio/driver.inf'
    assert inf_details(path)['provider']=='Intel Corporation'


def test_inf_size_remains_bounded(client):
    payload=driver_zip({'audio/driver.inf':'[Version]\nCatalogFile=audio.cat\n'+' '*(16*1024**2),'audio/audio.cat':'fixture'})
    uid=start(client,payload)
    # Upload chunks are independently bounded to 16 MiB.
    assert send(client,uid,payload[:8*1024**2]).status_code==200
    assert send(client,uid,payload[8*1024**2:],8*1024**2).status_code==200
    result=client.post(f'/api/uploads/{uid}/finish',headers=auth())
    assert result.status_code==422 and result.json()['detail']=='INF exceeds 16 MiB limit'
