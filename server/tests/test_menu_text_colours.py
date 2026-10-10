"""Managed routine appearance edits preserve routing and supported colour indices."""
import pytest
from repxe.boot_appearance import DEFAULT_APPEARANCE,appearance_lines,commands
from repxe.routines import Menu
from test_api import client,auth

def test_colour_defaults_remove_blue_rectangle_with_valid_ipxe_indices():
    lines=commands(Menu(),'127.0.0.1')+appearance_lines(DEFAULT_APPEARANCE)
    assert 'cpair --foreground 7 --background 9 1 || echo' in lines
    assert 'cpair --foreground 6 --background 9 2 || echo' in lines
    assert not any('--background 8' in line or line.startswith('colour') and line.split()[-3:] == ['8','||','echo'] for line in lines)

@pytest.mark.parametrize('line',['colour --basic 15 8','colour --rgb 0x11223344 7','cpair --background 8 1','cpair --background 9 8','colour --basic 1 --basic 2 6','chain http://example/erase.ipxe','cpair --background 9 1 || reboot',':assigned','exit'])
def test_colour_file_rejects_invalid_palette_or_routing_commands(line):
    with pytest.raises(ValueError):appearance_lines('#!ipxe\n'+line+'\n')

def test_colour_editor_applies_immediately_with_revision_guards(client):
    url='/api/files/text';params={'root':'configuration','path':'routines-appearance.ipxe'}
    opened=client.get(url,headers=auth(),params=params).json();assert opened['editable']
    content='#!ipxe\ncolour --basic 7 --rgb 0xffaa33 7\ncpair --foreground 7 --background 9 1\ncpair --foreground 0 --background 6 2\n'
    saved=client.put(url,headers=auth(),json={**params,'content':content,'expected_revision':opened['revision']});assert saved.status_code==200,saved.text
    preview=client.get('/api/routine-menu',headers=auth()).json()['preview'];assert 'colour --basic 7 --rgb 0xffaa33 7 || echo' in preview;assert 'cpair --foreground 0 --background 6 2 || echo' in preview;assert ':next\nexit' in preview
    rejected=client.put(url,headers=auth(),json={**params,'content':'#!ipxe\ncolour 8\n','expected_revision':saved.json()['revision']});assert rejected.status_code==422
    assert client.get(url,headers=auth(),params=params).json()['content']==content
    stale=client.put(url,headers=auth(),json={**params,'content':DEFAULT_APPEARANCE,'expected_revision':opened['revision']});assert stale.status_code==409

def test_editor_refuses_invalid_managed_menu_json_before_replacement(client):
    params={'root':'configuration','path':'routines-menu.json'};opened=client.get('/api/files/text',headers=auth(),params=params).json()
    rejected=client.put('/api/files/text',headers=auth(),json={**params,'content':'{"theme":"unsupported"}','expected_revision':opened['revision']});assert rejected.status_code==422
    assert client.get('/api/files/text',headers=auth(),params=params).json()['revision']==opened['revision']
