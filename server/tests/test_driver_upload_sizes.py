from collections import namedtuple
import zipfile
import pytest
from fastapi import HTTPException
from repxe import app as module
from repxe.app import Upload, unpack_drivers
from test_api import client, auth
from test_driver_library import job, connect

Usage = namedtuple('Usage', 'total used free')


@pytest.mark.parametrize('size', [1024**3+1, 101*1024**3, 2*1024**4])
def test_driver_upload_declarations_have_no_fixed_byte_cap(client,monkeypatch,size):
    monkeypatch.setattr(module.shutil,'disk_usage',lambda _:Usage(4*1024**4,0,4*1024**4))
    spec={'kind':'drivers','name':'Large driver archive','filename':'drivers.zip','size':size,'sha256':'a'*64,'make':'Fixture maker','model':'Model A','windows_version':'26300'}
    result=client.post('/api/uploads',headers=auth(),json=spec)
    assert result.status_code==201,result.text
    assert result.json()['spec']['size']==size and result.json()['received']==0
    uid,credential=job(client);connect(client,uid,credential)
    result=client.post(f'/api/client-jobs/{uid}/driver-uploads',headers=credential,json=spec)
    assert result.status_code==201,result.text
    assert result.json()['spec']['size']==size


def test_driver_upload_still_checks_available_storage(client,monkeypatch):
    monkeypatch.setattr(module.shutil,'disk_usage',lambda _:Usage(1024**3,0,1024**3))
    spec={'kind':'drivers','name':'Too large for storage','filename':'drivers.zip','size':2*1024**3,'sha256':'a'*64,'make':'Fixture','model':'PC','windows_version':'26300'}
    assert client.post('/api/uploads',headers=auth(),json=spec).status_code==507


def test_images_keep_their_existing_size_limit():
    spec={'kind':'image','name':'Windows','filename':'windows.wim','size':100*1024**3,'sha256':'a'*64}
    assert Upload(**spec).size==spec['size']
    with pytest.raises(ValueError):Upload(**{**spec,'size':spec['size']+1})


def test_large_payload_reaches_streaming_extraction_without_allocating_gigabytes(tmp_path,monkeypatch):
    # Exercise archive headers larger than both old payload/expansion caps.
    # Stop when streaming starts; no synthetic gigabytes are written.
    entry=zipfile.ZipInfo('gpu/driver.bin');entry.file_size=5*1024**3
    class StreamingStarted(Exception):pass
    class Archive:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def infolist(self):return [entry]
        def open(self,selected):
            assert selected is entry
            raise StreamingStarted
    monkeypatch.setattr(module.zipfile,'ZipFile',lambda _:Archive())
    monkeypatch.setattr(module.shutil,'disk_usage',lambda _:Usage(8*1024**3,0,8*1024**3))
    with pytest.raises(StreamingStarted):unpack_drivers(tmp_path/'source.zip',tmp_path/'expanded')
    monkeypatch.setattr(module.shutil,'disk_usage',lambda _:Usage(1024**3,0,1024**3))
    with pytest.raises(HTTPException) as error:unpack_drivers(tmp_path/'source.zip',tmp_path/'not-enough-space')
    assert error.value.status_code==507
