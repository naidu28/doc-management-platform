"""Adversarial requests against a disposable PostgreSQL schema.

Run: REDLINING_TEST_DATABASE_URL=postgresql://localhost/sandstone_test \
     .venv/bin/python -m scripts.probe_api_assumptions
No application documents are touched. Expiry is accelerated only in probe rows.
"""
import json
import os
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from fastapi.testclient import TestClient
from redlining.postgres import PostgresStore
from redlining.api import create_app
from redlining.runtime import DocumentService
from redlining.ui import attach_ui
from redlining.services import ReplaceCommand


def main():
    dsn = os.environ['REDLINING_TEST_DATABASE_URL']
    schema = 'adversarial_' + uuid4().hex
    with psycopg.connect(dsn,autocommit=True) as db:
        db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    results=[]
    def record(name, ok, **evidence):
        results.append(dict(name=name,passed=bool(ok),**evidence))
    try:
        store=PostgresStore(make_conninfo(dsn,options=f'-c search_path={schema},public'),initialize=True)
        service=DocumentService(store,wait_seconds=.03,reconcile_seconds=.01)
        app=create_app(service,service);attach_ui(app,store)
        with TestClient(app,raise_server_exceptions=False) as client:
            def doc(name,text='A😀BC other clause'):
                store.create_document(name,text);store.index_revision(name,1)
            def reserve(name,start=0,end=1,version=1,who='a'):
                return client.post(f'/documents/{name}/edit-reservations',headers={'X-Client-ID':who},json={'targets':[dict(version=version,start=start,end=end)]})
            doc('validation')
            for name,target in [('negative',dict(version=1,start=-1,end=1)),('reversed',dict(version=1,start=3,end=1)),('fractional',dict(version=1,start=.5,end=1)),('boolean',dict(version=1,start=True,end=2)),('out_of_bounds',dict(version=1,start=0,end=999999))]:
                r=client.post('/documents/validation/edit-reservations',headers={'X-Client-ID':'a'},json={'targets':[target]})
                record(name,r.status_code==422,status=r.status_code)
            r=reserve('validation',version=99);record('stale_revision',r.status_code==409,status=r.status_code,reason=r.json().get('reason'))
            doc('racing')
            with ThreadPoolExecutor(2) as pool:
                responses=list(pool.map(lambda who:reserve('racing',who=who),['a','b']))
            record('simultaneous_overlap',sorted(r.status_code for r in responses)==[200,409],statuses=[r.status_code for r in responses])
            doc('boundary');reserve('boundary',0,1)
            r=reserve('boundary',1,1,who='b');record('insertion_at_reserved_boundary',r.status_code==409,status=r.status_code)
            doc('unicode');rid=reserve('unicode',1,2).json()['reservations'][0]['reservation_id']
            oid=store.admit(ReplaceCommand(rid,'X'));store.process_next()
            record('emoji_anchor_is_one_codepoint',store.get_document('unicode').text=='AXBC other clause',text=store.get_document('unicode').text)
            r=client.post('/documents/unicode/replacements',json={'replacements':[{'reservation_id':rid,'replacement':'X'}]})
            record('duplicate_submit_exactly_once',r.status_code==200 and store.get_document('unicode').version==2,status=r.status_code,version=store.get_document('unicode').version)
            r=client.post('/documents/unicode/replacements',json={'replacements':[{'reservation_id':rid,'replacement':'Y'}]})
            record('changed_retry_rejected',r.status_code==409,status=r.status_code)
            doc('atomic');r=client.post('/documents/atomic/edit-reservations',headers={'X-Client-ID':'a'},json={'targets':[dict(version=1,start=0,end=1),dict(version=1,start=0,end=2)]})
            record('overlapping_batch_rolls_back',r.status_code==409 and not store.reservation_snapshot('atomic').reservations,status=r.status_code,reservations=len(store.reservation_snapshot('atomic').reservations))
            doc('duplicates')
            duplicate=client.post('/documents/duplicates/edit-reservations',headers={'X-Client-ID':'a'},json={'targets':[dict(version=1,start=0,end=1)]*2})
            rows=duplicate.json().get('reservations',[])
            submitted=client.post('/documents/duplicates/replacements',json={'replacements':[{'reservation_id':r['reservation_id'],'replacement':'X'} for r in rows]}) if rows else None
            record('reservation_batch_outputs_are_submittable',duplicate.status_code==422 or len({r['reservation_id'] for r in rows})==len(rows),reserve_status=duplicate.status_code,returned_rows=len(rows),distinct_ids=len({r['reservation_id'] for r in rows}),submit_status=submitted.status_code if submitted is not None else None)
            doc('expiry');rid=reserve('expiry').json()['reservations'][0]['reservation_id']
            with store.connection(True) as db: db.execute('UPDATE reservations SET expires=0 WHERE id=%s',(rid,))
            rival=reserve('expiry',who='b')
            renewed=client.post(f'/edit-reservations/{rid}/renew')
            record('expired_lease_cannot_overlap',rival.status_code==200 and renewed.status_code==409,
                   rival_status=rival.status_code,renewal_status=renewed.status_code)
            doc('nul');rid=reserve('nul').json()['reservations'][0]['reservation_id']
            for name,path,method,body in [
                ('nul_search','/search','post',{'text':'\x00','mode':'literal'}),
                ('nul_replacement','/documents/nul/replacements','post',{'replacements':[{'reservation_id':rid,'replacement':'a\x00b'}]}),
                ('nul_document_id','/search','post',{'text':'A','document_ids':['bad\x00id']}),
                ('lone_surrogate_search','/search','post',{'text':'\ud800'}),
            ]:
                response=client.request(method,path,headers={'X-Client-ID':'a','Content-Type':'application/json'},content=json.dumps(body))
                record(name,response.status_code in (200,422),status=response.status_code,reason=response.json().get('reason'))
            record('invalid_text_did_not_poison_queue',not store.process_next(),reservation_status=store.reservation(rid).status)
            r=client.post('/search',json={'text':'A','document_ids':[]});record('empty_search_scope_returns_none',r.status_code==200 and r.json()['hits']==[],status=r.status_code)
            doc('timeout');rid=reserve('timeout').json()['reservations'][0]['reservation_id']
            r=client.post('/documents/timeout/replacements',json={'replacements':[{'reservation_id':rid,'replacement':'Z'}]});store.process_next()
            replay=client.post('/documents/timeout/replacements',json={'replacements':[{'reservation_id':rid,'replacement':'Z'}]})
            record('timeout_retry_reuses_durable_work',r.status_code==202 and replay.status_code==200 and store.get_document('timeout').version==2,first_status=r.status_code,retry_status=replay.status_code,version=store.get_document('timeout').version)
            doc('batch-contract')
            rid=reserve('batch-contract').json()['reservations'][0]['reservation_id']
            for name,items in [
                ('empty_replacement_batch', []),
                ('duplicate_replacement_ids', [{'reservation_id':rid,'replacement':'Z'}]*2),
                ('mixed_replacement_forms', [{'reservation_id':rid,'replacement':'Z'}, {'target':dict(version=1,start=4,end=5),'replacement':'Y'}]),
                ('nul_reservation_id', [{'reservation_id':'bad\x00id','replacement':'Z'}]),
                ('surrogate_replacement', [{'reservation_id':rid,'replacement':'\ud800'}]),
                ('oversized_replacement_batch', [{'reservation_id':str(i),'replacement':'Z'} for i in range(101)]),
            ]:
                r=client.post('/documents/batch-contract/replacements',content=json.dumps({'replacements':items}),headers={'Content-Type':'application/json'})
                record(name,r.status_code==422,status=r.status_code)
            doc('wrong-document')
            r=client.post('/documents/wrong-document/replacements',json={'replacements':[{'reservation_id':rid,'replacement':'Z'}]})
            record('wrong_document_does_not_bind',r.status_code==409 and store.reservation(rid).status=='active',status=r.status_code)
            r=client.post('/documents/batch-contract/replacements',json={'replacements':[{'reservation_id':rid,'replacement':'Z'},{'reservation_id':'missing','replacement':'Y'}]})
            record('bad_second_item_rolls_back',r.status_code==404 and store.reservation(rid).status=='active',status=r.status_code)
            client.delete(f'/edit-reservations/{rid}')
            r=client.post('/documents/batch-contract/replacements',json={'replacements':[{'reservation_id':rid,'replacement':'Z'}]})
            record('released_reservation_rejected',r.status_code==409,status=r.status_code)
            doc('expired-free');rid=reserve('expired-free').json()['reservations'][0]['reservation_id']
            with store.connection(True) as db: db.execute('UPDATE reservations SET expires=0 WHERE id=%s',(rid,))
            r=client.post('/documents/expired-free/replacements',json={'replacements':[{'reservation_id':rid,'replacement':'Z'}]})
            held=store.reservation(rid)
            record('expired_free_reservation_revalidated',r.status_code==202 and held.status=='submitted' and held.expires_at is None,status=r.status_code)
            release=client.delete(f'/edit-reservations/{rid}')
            renew=client.post(f'/edit-reservations/{rid}/renew')
            record('accepted_work_cannot_release_or_renew',release.status_code==renew.status_code==409,release_status=release.status_code,renew_status=renew.status_code)
            store.process_next()
        from redlining.main import build_app
        with TestClient(build_app(database_url=store.dsn,redis_url='redis://127.0.0.1:1'),raise_server_exceptions=False) as socket_client:
            try:
                with socket_client.websocket_connect('/documents/validation/updates?after_version=%C2%B2') as ws:
                    state=ws.receive_json()
                    record('malformed_socket_version_is_handled',state.get('type') in ('error','document_state'),reply=state.get('type'))
            except Exception as exc:
                record('malformed_socket_version_is_handled',False,exception=type(exc).__name__,detail=str(exc)[:140])
        print(json.dumps(dict(passed=sum(r['passed'] for r in results),failed=sum(not r['passed'] for r in results),results=results),indent=2))
        if any(not result['passed'] for result in results):
            raise SystemExit(1)
    finally:
        with psycopg.connect(dsn,autocommit=True) as db:
            db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))

if __name__=='__main__':main()
