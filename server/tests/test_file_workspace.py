import json
import os
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from test_api import client, auth, TOKENS
from test_pxe import lab, boot, enroll
from repxe.app import create_app


def read(c,root='custom-boot',path='scripts/menu.ipxe'):
    return c.get('/api/files/text',params={'root':root,'path':path},headers=auth())


def save(c,root,path,content,revision=None,headers=None):
    return c.put('/api/files/text',json={'root':root,'path':path,'content':content,'expected_revision':revision},headers=headers or auth())


def test_roots_require_admin_and_all_published_areas_are_editable(client):
    roots=client.get('/api/files/roots',headers=auth()).json();assert len(roots['roots'])==8
    assert roots['boot_menu']['enabled'] is False and roots['binary_uploads_available'] is True
    assert all(r['writable'] for r in roots['roots'])
    for role in ('agent','capture','deploy'):
        assert client.get('/api/files/roots',headers=auth(role)).status_code==403
        assert read(client).status_code==200
    for alias,relative in [('reports','reports'),('drivers','drivers/packages'),('windows-images','images/published'),('boot-releases','boot/pxe'),('client-releases','boot/client')]:
        (client.app.state.store.root/relative).mkdir(parents=True,exist_ok=True)
        assert save(client,alias,'test.txt','modified').status_code==200
    (client.app.state.store.root/'reports/fixture.json').write_text('{"evidence":"original"}')
    current=read(client,'reports','fixture.json').json();assert current['editable'] is True
    assert save(client,'reports','fixture.json','{}',current['revision']).status_code==200


def test_edits_are_atomic_versioned_conflict_checked_and_survive_restart(client):
    old=read(client).json();content=old['content'].replace('re-pxe boot menu','Fixture boot menu')
    response=save(client,old['root'],old['path'],content,old['revision']);assert response.status_code==200,response.text
    current=response.json();assert current['content']==content and current['revision']!=old['revision']
    assert save(client,old['root'],old['path'],old['content'],old['revision']).status_code==409
    history=client.get('/api/files/history',params={'root':old['root'],'path':old['path']},headers=auth()).json()
    assert len(history)==1
    previous=client.get('/api/files/history/'+history[0]['id'],headers=auth()).json();assert previous['content']==old['content']
    assert save(client,old['root'],old['path'],previous['content'],current['revision']).status_code==200
    with TestClient(create_app(client.app.state.store.root,TOKENS)) as restarted:
        assert read(restarted).json()['content']==old['content']
        assert len(restarted.get('/api/files/history',params={'root':old['root'],'path':old['path']},headers=auth()).json())==2


def test_paths_links_devices_and_reserved_assets_are_rejected(client,tmp_path):
    for path in ('../database/catalog.sqlite','/etc/passwd','scripts/../../secret.txt','scripts\\file.txt','scripts//menu.ipxe'):
        assert client.get('/api/files/text',params={'root':'custom-boot','path':path},headers=auth()).status_code==422
    root=client.app.state.store.root
    # Link fixtures point only at private regular test files, never host disks.
    (root/'config/private-test.txt').write_text('private fixture')
    (root/'boot/custom/scripts/link.txt').symlink_to(root/'config/private-test.txt')
    (root/'boot/custom/linked-folder').symlink_to(root/'config',target_is_directory=True)
    os.link(root/'config/private-test.txt',root/'boot/custom/scripts/shared.txt')
    os.mkfifo(root/'boot/custom/scripts/pipe.txt')
    for path in ('scripts/link.txt','linked-folder/private-test.txt','scripts/pipe.txt'):
        assert read(client,path=path).status_code==403
        assert save(client,'custom-boot',path,'overwrite').status_code==403
    listed=client.get('/api/files/list',params={'root':'custom-boot','path':'scripts'},headers=auth()).json()['entries']
    assert not {'link.txt','pipe.txt'}&{r['name'] for r in listed}
    shared=read(client,path='scripts/shared.txt').json();assert shared['editable'] is True
    assert save(client,'custom-boot','scripts/shared.txt','overwrite',shared['revision']).status_code==200
    assert save(client,'custom-boot','assets/example.txt','file').status_code==200
    assert (root/'config/private-test.txt').read_text()=='private fixture'


def test_create_text_folders_json_validation_and_size_limits(client):
    assert client.post('/api/files/folders',json={'root':'custom-boot','path':'scripts/tools'},headers=auth()).status_code==201
    assert client.post('/api/files/folders',json={'root':'custom-boot','path':'scripts/tools'},headers=auth()).status_code==409
    assert save(client,'custom-boot','scripts/tools/test.ipxe','invalid script').status_code==422
    assert save(client,'custom-boot','scripts/tools/test.ipxe','#!ipxe\necho Fixture\n').status_code==200
    assert save(client,'custom-boot','scripts/tools/test.ipxe','#!ipxe\nreplacement\n').status_code==409
    assert save(client,'configuration','test.json','broken').status_code==422
    assert save(client,'configuration','test.json','{"okay":true}\n').status_code==200
    assert save(client,'configuration','large.txt','é'*150000).status_code==422
    assert save(client,'configuration','nul.txt','\x00').status_code==422
    assert save(client,'configuration','file.iso','binary').status_code==200
    assert client.post('/api/files/folders',json={'root':'custom-boot','path':'assets/new'},headers=auth()).status_code==201


def test_download_and_editor_binary_limits(client):
    root=client.app.state.store.root
    (root/'reports/binary.bin').write_bytes(b'\x00\xfffixture')
    response=client.get('/api/files/download',params={'root':'reports','path':'binary.bin'},headers=auth());assert response.content==b'\x00\xfffixture'
    assert response.headers['x-content-type-options']=='nosniff'
    assert read(client,'reports','binary.bin').status_code==415
    (root/'reports/large.txt').write_bytes(b'x'*(256*1024+1))
    assert read(client,'reports','large.txt').status_code==413
    (root/'config/bad.txt').write_bytes(b'\xff')
    assert save(client,'configuration','bad.txt','replacement',__import__('hashlib').sha256(b'\xff').hexdigest()).status_code==415


def test_large_existing_workspace_file_does_not_block_startup(client):
    root=client.app.state.store.root
    data=b'x'*(256*1024+1)
    (root/'config/workshop-notes.md').write_bytes(data)
    with TestClient(create_app(root,TOKENS)) as restarted:
        assert read(restarted,'configuration','workshop-notes.md').status_code==413
        assert restarted.get('/api/boot-menu',headers=auth()).json()['enabled'] is False
    assert (root/'config/workshop-notes.md').read_bytes()==data


def test_published_folders_show_catalog_names_without_changing_paths(client):
    from repxe.app import now
    root=client.app.state.store.root
    uid='12345678-1234-1234-1234-123456789abc'
    (root/'images/published'/uid).mkdir()
    with client.app.state.store.db() as db:
        db.execute('INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?)',
                   (uid,'image','Workshop Windows 11','a'*64,10,'{}',now(),'Fixture'))
    entry=client.get('/api/files/list',params={'root':'windows-images'},headers=auth()).json()['entries'][0]
    assert entry['display_name']=='Workshop Windows 11' and entry['name']==uid and entry['path']==uid


def test_cookie_editor_requires_csrf_and_records_operator(client):
    with TestClient(client.app,base_url='https://testserver') as browser:
        session=browser.post('/api/login',json={'username':'admin','password':'admin'}).json()
        current=read(browser,'configuration','workshop-notes.md').json()
        payload={'root':current['root'],'path':current['path'],'content':'# Fixture edited\n','expected_revision':current['revision']}
        assert browser.put('/api/files/text',json=payload).status_code==403
        headers={'X-CSRF-Token':session['csrf_token'],'Origin':'https://testserver'}
        response=browser.put('/api/files/text',json=payload,headers=headers);assert response.status_code==200,response.text
        history=browser.get('/api/files/history',params={'root':current['root'],'path':current['path']}).json();assert history[0]['actor']=='admin'


def test_boot_menu_default_opt_in_managed_fallback_and_completion_guard(lab):
    c,root,profile=lab
    old_script=c.get('/pxe/boot.ipxe?mac=52:54:00:12:34:56').text;assert 'wimboot quiet' in old_script
    settings=c.get('/api/boot-menu',headers=auth()).json()
    bad=save(c,'configuration','boot-menu.json','{"schemaVersion":1,"enabled":"yes"}',settings['revision']);assert bad.status_code==422
    response=c.put('/api/boot-menu',json={'enabled':True,'expected_revision':settings['revision']},headers=auth());assert response.status_code==200,response.text
    with c.app.state.store.db() as db:before=db.execute('SELECT count(*) FROM clients').fetchone()[0]
    script=c.get('/pxe/boot.ipxe?mac=52:54:00:12:34:57');assert script.status_code==200
    assert '/custom/scripts/menu.ipxe' in script.text and '/pxe/managed.ipxe?mac=' in script.text
    with c.app.state.store.db() as db:assert db.execute('SELECT count(*) FROM clients').fetchone()[0]==before
    script=c.get('/pxe/managed.ipxe?mac=52:54:00:12:34:57');assert 'wimboot quiet' in script.text
    import re
    ticket=re.search('/pxe/config/([A-Za-z0-9_-]+)',script.text)[1]
    headers,uid=enroll(c,ticket)
    assert c.post('/api/pxe/select',json={'profile_id':profile['id'],'target_identity':'serial:LAB-001'},headers=headers).status_code==200
    started=c.post('/api/pxe/start',json={},headers=headers);assert started.status_code==201
    with c.app.state.store.db() as db:db.execute("UPDATE jobs SET stage='awaiting-windows' WHERE id=?",(started.json()['id'],))
    for route in ('/pxe/boot.ipxe','/pxe/managed.ipxe'):
        script=c.get(route+'?mac=52:54:00:12:34:57');assert 'completion is pending' in script.text and 'menu.ipxe' not in script.text
    # Corrupt external configuration fails back to managed imaging, rather than
    # preventing a new client from booting the established workflow.
    (root/'config/boot-menu.json').write_text('{broken')
    assert 'wimboot quiet' in c.get('/pxe/boot.ipxe?mac=52:54:00:12:34:58').text
