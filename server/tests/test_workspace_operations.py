import hashlib
import os
from test_api import client, auth


def op(c, action, paths, **extra):
    return c.post('/api/files/operations',headers=auth(),json={'action':action,'paths':paths,**extra})


def test_every_data_folder_hidden_unicode_and_arbitrary_text_is_available(client):
    root=client.app.state.store.root
    for area in ['database','reports','uploads/staging','boot/custom/assets','boot/pxe','drivers/packages']:
        (root/area).mkdir(parents=True,exist_ok=True)
        tree=client.get('/api/files/tree',params={'path':area},headers=auth())
        assert tree.status_code==200 and tree.json()['writable']
        p=area+'/.测试file'
        response=client.put('/api/files/text',headers=auth(),json={'root':'workspace','path':p,'content':'raw text'})
        assert response.status_code==200,response.text
        entries=client.get('/api/files/tree',params={'path':area},headers=auth()).json()['entries']
        assert any(e['name']=='.测试file' and e['editable'] for e in entries)
        assert op(client,'rename',[p],name='Renamed file.anything').status_code==200
        assert op(client,'delete',[area+'/Renamed file.anything']).status_code==200


def test_recursive_copy_move_rename_delete_and_copy_collision(client):
    root=client.app.state.store.root
    (root/'reports/Test folder/sub').mkdir(parents=True)
    (root/'reports/Test folder/sub/data.bin').write_bytes(b'\0\xfffixture')
    (root/'reports/Test folder/.hidden').write_text('fixture')
    assert op(client,'copy',['reports/Test folder'],destination='reports').json()['targets']==['reports/Test folder - Copy']
    assert op(client,'copy',['reports/Test folder'],destination='reports').json()['targets']==['reports/Test folder - Copy (2)']
    assert op(client,'move',['reports/Test folder - Copy'],destination='database').status_code==200
    assert op(client,'rename',['database/Test folder - Copy'],name='Other').status_code==200
    assert (root/'database/Other/sub/data.bin').read_bytes()==b'\0\xfffixture'
    assert (root/'database/Other/.hidden').read_text()=='fixture'
    assert op(client,'delete',['database/Other','reports/Test folder - Copy (2)']).status_code==200
    assert not (root/'database/Other').exists()
    assert (root/'reports/Test folder/sub/data.bin').read_bytes()==b'\0\xfffixture'


def test_conflicts_validate_batch_before_mutation_and_reject_stale_items(client):
    root=client.app.state.store.root
    for name in ['one','two']:(root/'reports'/name).write_text(name)
    (root/'config/two').write_text('existing')
    assert op(client,'move',['reports/one','reports/two'],destination='config').status_code==409
    assert (root/'reports/one').exists() and not (root/'config/one').exists()
    entries=client.get('/api/files/tree?path=reports',headers=auth()).json()['entries']
    token=next(e['token'] for e in entries if e['name']=='one')
    (root/'reports/one').write_text('changed')
    assert op(client,'delete',['reports/one'],tokens={'reports/one':token}).status_code==409
    assert (root/'reports/one').read_text()=='changed'
    (root/'reports/folder/sub').mkdir(parents=True)
    assert op(client,'move',['reports/folder'],destination='reports/folder/sub').status_code==422
    assert op(client,'copy',['reports/folder'],destination='reports/folder').status_code==422
    assert op(client,'delete',['reports/folder','reports/folder/sub']).status_code==422


def test_links_devices_traversal_and_workspace_root_cannot_escape(client,tmp_path):
    root=client.app.state.store.root
    outside=tmp_path/'outside';outside.mkdir();(outside/'private.txt').write_text('untouched')
    (root/'reports/link').symlink_to(outside,target_is_directory=True)
    os.mkfifo(root/'reports/pipe')
    for action in ['copy','move','delete']:
        assert op(client,action,['reports/link/private.txt'],destination='config').status_code==403
        assert op(client,action,['../outside/private.txt'],destination='config').status_code==422
        assert op(client,action,[''],destination='config').status_code==422
    assert op(client,'rename',['reports/link'],name='../escaped').status_code==422
    assert op(client,'copy',['reports/link'],destination='config').status_code==403
    assert op(client,'copy',['reports/pipe'],destination='config').status_code==403
    assert not any(p.name.startswith('.copy-') for p in (root/'config').iterdir())
    assert op(client,'delete',['reports/link','reports/pipe']).status_code==200
    assert (outside/'private.txt').read_text()=='untouched'


def test_streamed_binary_upload_replace_conflict_and_authorization(client):
    path='reports/原始证书.bin';body=b'\x00\xff'+b'x'*1048577
    response=client.put('/api/files/upload',params={'path':path},headers=auth(),content=body)
    assert response.status_code==200,response.text
    response=client.get('/api/files/download',params={'root':'workspace','path':path},headers=auth())
    assert response.content==body and 'filename*=UTF-8' in response.headers['content-disposition']
    assert client.put('/api/files/upload',params={'path':path},headers=auth(),content=b'new').status_code==409
    entry=client.get('/api/files/tree?path=reports',headers=auth()).json()['entries'][0]
    assert client.put('/api/files/upload',params={'path':path,'expected_token':entry['token']},headers=auth(),content=b'new').status_code==200
    assert client.put('/api/files/upload',params={'path':path,'expected_token':entry['token']},headers=auth(),content=b'overwrite').status_code==409
    root=client.app.state.store.root
    assert (root/path).read_bytes()==b'new'
    assert not any(p.name.startswith('.upload-') for p in (root/'reports').iterdir())
    for role in ['capture','agent','deploy']:
        assert client.put('/api/files/upload',params={'path':'reports/forbidden'},headers=auth(role),content=b'x').status_code==403
        assert client.post('/api/files/operations',headers=auth(role),json={'action':'delete','paths':[path]}).status_code==403


def test_operations_and_upload_require_csrf_for_cookie_sessions(client):
    from fastapi.testclient import TestClient
    with TestClient(client.app,base_url='https://testserver') as browser:
        session=browser.post('/api/login',json={'username':'admin','password':'admin'}).json()
        root=client.app.state.store.root;(root/'reports/fixture').write_text('fixture')
        assert browser.post('/api/files/operations',json={'action':'delete','paths':['reports/fixture']}).status_code==403
        assert browser.put('/api/files/upload?path=reports/new',content=b'fixture').status_code==403
        headers={'X-CSRF-Token':session['csrf_token'],'Origin':'https://testserver'}
        assert browser.put('/api/files/upload?path=reports/new',headers=headers,content=b'fixture').status_code==200
        assert browser.post('/api/files/operations',headers=headers,json={'action':'delete','paths':['reports/fixture']}).status_code==200


def test_large_upload_chunks_preserve_target_until_complete_and_validate_offset(client):
    root=client.app.state.store.root
    path='reports/large.bin';(root/path).write_bytes(b'original')
    token=client.get('/api/files/tree?path=reports',headers=auth()).json()['entries'][0]['token']
    first=client.put('/api/files/upload',params={'path':path,'finish':False},headers=auth(),content=b'a'*1048576)
    assert first.status_code==200 and not first.json()['complete']
    uid=first.json()['upload_id'];assert (root/path).read_bytes()==b'original'
    wrong=client.put('/api/files/upload',params={'path':path,'upload_id':uid,'offset':1},headers=auth(),content=b'wrong')
    assert wrong.status_code==409 and (root/path).read_bytes()==b'original'
    final=client.put('/api/files/upload',params={'path':path,'upload_id':uid,'offset':1048576,'expected_token':token},headers=auth(),content=b'b'*1048576)
    assert final.status_code==200 and final.json()['complete']
    assert (root/path).read_bytes()==b'a'*1048576+b'b'*1048576
    assert not (root/'reports'/('.upload-'+uid)).exists()
    assert client.put('/api/files/upload',params={'path':path,'upload_id':'../../outside'},headers=auth(),content=b'bad').status_code==422
