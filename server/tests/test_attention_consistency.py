"""Counts, pages and detail classifications must agree, including history."""
import itertools
import sqlite3
from repxe.activity import routine_stage, routine_stage_sql
from test_api import auth
from test_pxe import lab
from test_routine_activity import wipe_only


def test_sql_and_detail_status_agree_with_terminal_history_priority():
    db=sqlite3.connect(':memory:')
    db.executescript('CREATE TABLE r(stage TEXT);CREATE TABLE c(stage TEXT);CREATE TABLE j(stage TEXT,report TEXT);CREATE TABLE w(stage TEXT);')
    for stage,client,job,report,erase in itertools.product(
        ['discovering','erase-running','windows-boot','windows-running','checkpoint','custom-boot','complete','cancelled','failed'],
        [None,'waiting-for-input','needs-operator','failed','complete'],
        [None,'awaiting-windows','complete','failed','cancelled'], [None,'{}'],[None,'operation-pending','failed']):
        for table in ('r','c','j','w'):db.execute('DELETE FROM '+table)
        db.execute('INSERT INTO r VALUES(?)',(stage,));db.execute('INSERT INTO c VALUES(?)',(client,))
        db.execute('INSERT INTO j VALUES(?,?)',(job,report));db.execute('INSERT INTO w VALUES(?)',(erase,))
        actual=db.execute('SELECT '+routine_stage_sql()+' FROM r,c,j,w').fetchone()[0]
        assert actual==routine_stage(stage,client,job,report is not None,erase)
        if stage in ('complete','cancelled','failed'):assert actual==stage
    db.close()


def test_attention_metric_filter_details_and_pages_retain_failures(lab):
    c,root,_=lab;run=wipe_only(c,root)
    with c.app.state.store.db() as db:
        original=dict(db.execute('SELECT * FROM routine_runs WHERE id=?',(run['id'],)).fetchone())
        for n in range(105):
            item={**original,'id':f'attention-fixture-{n:03}','mac':f'52:54:00:00:01:{n:02x}','stage':['failed','checkpoint','custom-boot','complete','cancelled'][n%5]}
            db.execute('INSERT INTO routine_runs('+','.join(item)+') VALUES('+','.join('?' for _ in item)+')',list(item.values()))
    dashboard=c.get('/api/dashboard',headers=auth()).json()
    first=c.get('/api/routine-jobs?view=attention',headers=auth()).json()
    second=c.get('/api/routine-jobs?view=attention&page=2',headers=auth()).json()
    assert first['total']==dashboard['routines']['counts']['attention']==dashboard['attention_count']==63
    assert first['pages']==2 and len(first['runs'])+len(second['runs'])==63
    for row in first['runs']+second['runs']:
        assert row['needs_attention'] and row['effective_stage'] in ('failed','needs-operator')
    detail=c.get('/api/routine-runs/attention-fixture-000',headers=auth()).json()
    assert detail['needs_attention'] and detail['stage']=='failed'
    assert c.get('/api/routine-jobs?view=attention&search=absent',headers=auth()).json()['total']==0
    # A search controls the list only; the global metric remains explicit.
    assert c.get('/api/dashboard?view=attention&search=absent',headers=auth()).json()['attention_count']==63


def test_failed_routine_can_be_cancelled_without_losing_its_report(lab):
    c,root,_=lab;run=wipe_only(c,root)
    with c.app.state.store.db() as db:
        db.execute("UPDATE routine_runs SET stage='failed',error='Unsupported operation' WHERE id=?",(run['id'],))
    endpoint='/api/routine-runs/'+run['id']
    before=c.get('/api/job-reports/routine:'+run['id'],headers=auth()).json()
    row=c.get(endpoint,headers=auth()).json();assert row['can_cancel'] and row['needs_attention']
    assert c.post(endpoint+'/action',headers=auth(),json={'action':'cancel','expected_step':row['step']+1}).status_code==409
    assert c.post(endpoint+'/action',headers=auth(),json={'action':'cancel','expected_step':row['step']}).status_code==200
    row=c.get(endpoint,headers=auth()).json();assert row['stage']=='cancelled' and not row['needs_attention'] and not row['can_cancel']
    assert c.get('/api/routine-jobs?view=attention',headers=auth()).json()['total']==0
    after=c.get('/api/job-reports/routine:'+run['id'],headers=auth()).json()
    assert before['routine']['id']==after['routine']['id'] and row['error']=='Unsupported operation'
    assert c.get('/api/job-reports/routine:'+run['id']+'/pdf',headers=auth()).status_code==200
