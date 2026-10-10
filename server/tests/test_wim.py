"""Integration tests executed inside the built Linux image with real wimtools."""
from pathlib import Path
import shutil
import subprocess

import pytest
from repxe.app import verify_wim

pytestmark = pytest.mark.skipif(not shutil.which('wimlib-imagex'), reason='wimtools is installed in the Docker image')


@pytest.fixture
def wim(tmp_path):
    source = tmp_path/'source'
    source.mkdir()
    (source/'sentinel.txt').write_text('Disposable filesystem image. This is not a bootable Windows OS.')
    image = tmp_path/'fixture.wim'
    subprocess.run(['wimlib-imagex','capture',str(source),str(image),'fixture','--compress=none'],
                   check=True,capture_output=True)
    subprocess.run(['wimlib-imagex','info',str(image),'1','--image-property=WINDOWS/ARCH=9',
                    '--image-property=WINDOWS/EDITIONID=Fixture','--image-property=WINDOWS/VERSION/BUILD=0'],
                   check=True,capture_output=True)
    return image


def test_real_wim_metadata_and_integrity(wim):
    result = verify_wim(wim, {'image_index':1})
    assert result['selected_image']['architecture'] == '9'
    assert result['selected_image']['edition'] == 'Fixture'


def test_bad_index(wim):
    with pytest.raises(ValueError,match='index'):
        verify_wim(wim,{'image_index':2})


def test_corrupt_wim(wim):
    with wim.open('r+b') as stream:
        stream.write(b'BROKEN HEADER')
    with pytest.raises(ValueError,match='integrity'):
        verify_wim(wim,{'image_index':1})


def test_non_amd64_wim(wim):
    subprocess.run(['wimlib-imagex','info',str(wim),'1','--image-property=WINDOWS/ARCH=0'],check=True,capture_output=True)
    with pytest.raises(ValueError,match='AMD64'):
        verify_wim(wim,{'image_index':1})
