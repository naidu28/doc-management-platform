"""Real PostgreSQL policy tests; each test owns an isolated schema."""
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
import pytest
from fastapi.testclient import TestClient

from redlining.postgres import PostgresStore
from redlining.models import CreateEditReservationRequest, DocumentAnchor, SearchRequest
from redlining.services import ReplaceCommand, ServiceError


@pytest.fixture
def store():
    dsn = os.environ.get('REDLINING_TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('Set REDLINING_TEST_DATABASE_URL for PostgreSQL integration tests')
    name = 'policy_' + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as db:
        db.execute('CREATE EXTENSION IF NOT EXISTS pg_trgm')
        db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(name)))
    isolated = make_conninfo(dsn, options=f'-c search_path={name},public')
    s = PostgresStore(isolated,initialize=True)
    s.create_document('X','30 days; other clause. 😀 contract contract')
    try:
        yield s
    finally:
        with psycopg.connect(dsn, autocommit=True) as db:
            db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(name)))


def acquire(s,client='a',start=0,end=2,version=1):
    return s.reserve(client,'X',CreateEditReservationRequest(target=DocumentAnchor(version=version,start=start,end=end)))


def granted(s,**kwargs):
    return acquire(s,**kwargs).reservation_id


def finish(s,oid):
    while s.operation(oid)['status']=='queued':
        assert s.process_next()
    return s.operation(oid)


def test_reserve_is_immediate_and_overlap_never_queues(store):
    rid = granted(store)
    with pytest.raises(ServiceError,match='reserved'):
        acquire(store,client='b')
    with store.connection() as db:
        assert db.execute('SELECT count(*) AS n FROM replacements').fetchone()['n']==0
        assert db.execute("SELECT to_regclass(current_schema() || '.document_sequences') AS t").fetchone()['t'] is None
    oid = store.admit(ReplaceCommand(rid,'60'))
    held = store.reservation(rid)
    assert held.status=='submitted' and held.expires_at is None
    assert held.operation_id==oid
    assert finish(store,oid)['result_version']==2


def test_duplicate_selection_and_immutable_replacement(store):
    rid = granted(store)
    before = store.reservation(rid)
    assert acquire(store)==before
    oid = store.admit(ReplaceCommand(rid,'60'))
    assert store.admit(ReplaceCommand(rid,'60'))==oid
    finish(store,oid)
    assert store.admit(ReplaceCommand(rid,'60'))==oid
    with pytest.raises(ServiceError,match='different replacement'):
        store.admit(ReplaceCommand(rid,'90'))
    assert store.get_document('X').version==2


def test_pending_replacement_cannot_expire_release_renew_or_change_draft(store):
    rid = granted(store)
    store.save_draft(rid,'a','60')
    oid = store.admit(ReplaceCommand(rid,'60'))
    # A deadline cannot make submitted protection lapse, even if present erroneously.
    with store.connection(True) as db:
        db.execute('UPDATE reservations SET expires=0 WHERE id=%s',(rid,))
    for action in [lambda: store.release(rid), lambda: store.renew(rid),
                   lambda: store.save_draft(rid,'a','90'),lambda: acquire(store,client='b')]:
        with pytest.raises(ServiceError): action()
    assert store.reservation_snapshot('X').reservations[0].status=='submitted'
    assert finish(store,oid)['status']=='completed'


def test_release_before_admission_rejects_without_queueing(store):
    rid = granted(store)
    store.release(rid)
    with pytest.raises(ServiceError): store.admit(ReplaceCommand(rid,'60'))
    with store.connection() as db:
        assert db.execute('SELECT count(*) AS n FROM replacements').fetchone()['n']==0


@pytest.mark.parametrize('reverse',[False,True])
def test_disjoint_replacements_work_in_either_order(store,reverse):
    first = granted(store)
    second = granted(store,client='b',start=9,end=14)
    ids = [store.admit(ReplaceCommand(first,'sixty')),store.admit(ReplaceCommand(second,'different'))]
    # Exercise the same transactional executor in both legal scheduling orders.
    for oid in reversed(ids) if reverse else ids:
        with store.connection(True) as db:
            store.lock_document(db,'X')
            op = db.execute('SELECT * FROM replacements WHERE id=%s',(oid,)).fetchone()
            version = store.execute(db,op,store.now(db))
            db.execute("UPDATE replacements SET status='completed',result_version=%s WHERE id=%s",(version,oid))
    assert store.get_document('X').text.startswith('sixty days; different clause.')
    assert store.get_document('X').version==3
    assert not store.process_next()


def test_queued_anchor_moves_eagerly(store):
    first = granted(store)
    other = granted(store,client='b',start=9,end=14)
    oid = store.admit(ReplaceCommand(first,'sixty'))
    second = store.admit(ReplaceCommand(other,'different'))
    # Commit the first operation explicitly to inspect the still-queued anchor.
    with store.connection(True) as db:
        store.lock_document(db,'X')
        version = store.execute(db,store.operation(oid),store.now(db))
        db.execute("UPDATE replacements SET status='completed',result_version=%s WHERE id=%s",(version,oid))
    assert store.reservation(other).target==DocumentAnchor(version=2,start=12,end=17)
    assert finish(store,second)['status']=='completed'


def test_expired_competing_reservations_admit_at_most_one(store):
    first = granted(store)
    with store.connection(True) as db: db.execute('UPDATE reservations SET expires=0 WHERE id=%s',(first,))
    second = granted(store,client='b')
    with store.connection(True) as db: db.execute('UPDATE reservations SET expires=0 WHERE id=%s',(second,))
    def submit(rid):
        try: return store.admit(ReplaceCommand(rid,'60'))
        except ServiceError as e: return e.reason
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(submit,[first,second]))
    assert results.count('RANGE_RESERVED')==1
    with store.connection() as db:
        assert db.execute('SELECT count(*) AS n FROM replacements').fetchone()['n']==1
    assert store.process_next()
    assert store.get_document('X').version==2


def test_process_crash_rolls_back_and_recovers(store):
    rid = granted(store)
    oid = store.admit(ReplaceCommand(rid,'60'))
    code = """import os,sys
from redlining.postgres import PostgresStore
class Crash(PostgresStore):
    def execute(self,*args):
        super().execute(*args)
        os._exit(23)
Crash(sys.argv[1]).process_next()
"""
    assert subprocess.run([sys.executable,'-c',code,store.dsn],timeout=10).returncode==23
    assert store.operation(oid)['status']=='queued'
    assert store.reservation(rid).status=='submitted'
    assert store.get_document('X').version==1
    restarted = PostgresStore(store.dsn)
    assert finish(restarted,oid)['status']=='completed'
    assert restarted.admit(ReplaceCommand(rid,'60'))==oid
    assert not restarted.process_next()


def test_multiple_processes_race_for_one_reservation(store):
    code = """import sys
from redlining.postgres import PostgresStore
from redlining.models import CreateEditReservationRequest,DocumentAnchor
from redlining.services import ServiceError
s=PostgresStore(sys.argv[1])
try:
    s.reserve(sys.argv[2],'X',CreateEditReservationRequest(target=DocumentAnchor(version=1,start=0,end=2)))
    print('granted')
except ServiceError as e: print(e.reason)
"""
    processes = [subprocess.Popen([sys.executable,'-c',code,store.dsn,str(i)],stdout=subprocess.PIPE,text=True) for i in range(6)]
    results = [p.communicate(timeout=15)[0].strip() for p in processes]
    assert results.count('granted')==1
    assert results.count('RANGE_RESERVED')==5


def test_workers_execute_each_replacement_once(store):
    ids=[]
    for i in range(5):
        rid=granted(store,client=str(i),start=i*3,end=i*3+1)
        ids.append(store.admit(ReplaceCommand(rid,str(i))))
    code='import sys; from redlining.postgres import PostgresStore; s=PostgresStore(sys.argv[1]);\nwhile s.process_next(): pass'
    workers=[subprocess.Popen([sys.executable,'-c',code,store.dsn]) for _ in range(3)]
    assert all(p.wait(timeout=15)==0 for p in workers)
    assert store.get_document('X').version==6
    assert all(store.operation(oid)['status']=='completed' for oid in ids)


def test_busy_document_does_not_block_other_documents(store):
    store.create_document('Y','hello')
    x=granted(store)
    y=store.reserve('a','Y',CreateEditReservationRequest(target=DocumentAnchor(version=1,start=0,end=2))).reservation_id
    store.admit(ReplaceCommand(x,'60'));oid=store.admit(ReplaceCommand(y,'hi'))
    with store.connection(True) as db:
        store.lock_document(db,'X')
        with ThreadPoolExecutor() as pool: assert pool.submit(store.process_next).result(timeout=5)
        assert store.operation(oid)['status']=='completed'
    assert store.process_next()


def test_draft_lease_expiry_and_renewal(store):
    rid=granted(store);expiry=store.reservation(rid).expires_at
    store.save_draft(rid,'a','60')
    assert store.reservation(rid).expires_at==expiry
    assert store.renew(rid).expires_at>=expiry
    with store.connection(True) as db: db.execute('UPDATE reservations SET expires=0 WHERE id=%s',(rid,))
    with pytest.raises(ServiceError,match='Renew'):store.save_draft(rid,'a','90')
    assert finish(store,store.admit(ReplaceCommand(rid,'60')))['status']=='completed'


def test_worker_invariant_failure_releases_protection_without_editing(store):
    rid=granted(store);oid=store.admit(ReplaceCommand(rid,'60'))
    with store.connection(True) as db: db.execute('UPDATE reservations SET version=99 WHERE id=%s',(rid,))
    result=finish(store,oid)
    assert result['failure']['reason']=='QUEUE_INVARIANT'
    assert store.get_document('X').version==1
    assert store.reservation_snapshot('X').reservations==[]
    assert store.live_state('X',1)['operations'][0]['status']=='failed'


def test_search_revision_offsets_and_large_document(store):
    text='ordinary clause. '*660000+'😀 UNIQUEEND contract'
    store.create_document('large',text);store.index_revision('large',1)
    for mode,needle in [('literal','UNIQUEEND'),('phrase','UNIQUEEND contract'),('terms','contract UNIQUEEND')]:
        hit=store.search(SearchRequest(text=needle,mode=mode,document_ids=['large'])).hits[0]
        assert text[hit.target.start:hit.target.end].startswith('UNIQUEEND')
        if mode != 'literal':
            assert hit.score > 0
    rid=granted(store);finish(store,store.admit(ReplaceCommand(rid,'60')))
    store.index_revision('X',2)
    assert not store.index_revision('X',1)


def test_socket_and_http_reservation_admission(store):
    from redlining.main import build_app
    with TestClient(build_app(database_url=store.dsn,redis_url='redis://127.0.0.1:1')) as client:
        with client.websocket_connect('/documents/X/updates') as ws:
            assert ws.receive_json()['document']['version']==1
            granted_response=client.post('/documents/X/edit-reservations',headers={'X-Client-ID':'a'},json={'target':{'version':1,'start':0,'end':2}})
            assert granted_response.status_code==200
            rid=granted_response.json()['reservation_id']
            assert client.post('/documents/X/edit-reservations',headers={'X-Client-ID':'b'},json={'target':{'version':1,'start':0,'end':2}}).status_code==409
            import time
            with ThreadPoolExecutor() as pool:
                pending=pool.submit(client.post,'/replacements',json={'reservation_id':rid,'replacement':'60'})
                for _ in range(100):
                    if store.reservation(rid).status=='submitted':break
                    time.sleep(.01)
                assert client.delete('/edit-reservations/'+rid).status_code==409
                assert store.process_next()
                response=pending.result(timeout=5)
            assert response.status_code==200
            assert response.json()['text'].startswith('60 days')
            assert response.json()['version']==2
            for _ in range(5):
                state=ws.receive_json()
                if state['reservations']['document_version']==2:break
            assert state['updates'][0]['patch']['replacement']=='60'
            assert state['operations'][0]['status']=='completed'
        with client.websocket_connect('/documents/X/updates') as ws:
            state=ws.receive_json()
            assert state['operations'][0]['reservation_id']==response.json()['reservation_id']
            assert 'commands' not in state


def test_postgres_outbox_stream_and_index_delivery(store,redis_url):
    import asyncio
    from redis.asyncio import Redis
    from redlining.runtime import Workers
    async def scenario():
        redis=Redis.from_url(redis_url,decode_responses=True);worker=Workers(store,redis)
        try:
            await worker.groups()
            rid=granted(store);finish(store,store.admit(ReplaceCommand(rid,'60')))
            async with redis.pubsub() as pubsub:
                await pubsub.subscribe('document:X');await pubsub.get_message(timeout=1)
                await worker.relay_once();assert not store.pending_events()
                await worker.consume_once('search-index-v1');await worker.consume_once('live-delivery-v1')
                assert (await pubsub.get_message(ignore_subscribe_messages=True,timeout=1))['type']=='message'
                assert store.search(SearchRequest(text='60',mode='literal')).hits[0].target.version==2
        finally: await redis.aclose()
    asyncio.run(scenario())


def make_legacy_queue(store, rid, status):
    with store.connection(True) as db:
        db.execute('CREATE TABLE document_sequences(document_id text PRIMARY KEY, next_sequence bigint)')
        db.execute('CREATE TABLE commands(id text PRIMARY KEY, document_id text, reservation_id text, kind text, payload jsonb, status text, result jsonb, failure jsonb)')
        db.execute("INSERT INTO commands VALUES('old-operation','X',%s,'replace',%s::jsonb,%s,%s::jsonb,NULL)",
                   (rid,json.dumps({'replacement':'60'}),status,json.dumps({'version':2})))


def test_upgrade_refuses_pending_work_without_changing_old_schema(store):
    rid=granted(store)
    make_legacy_queue(store,rid,'queued')
    with pytest.raises(RuntimeError,match='Drain'):
        PostgresStore(store.dsn,initialize=True)
    with store.connection() as db:
        assert db.execute('SELECT status FROM commands').fetchone()['status']=='queued'
        assert db.execute("SELECT to_regclass(current_schema() || '.legacy_commands') AS t").fetchone()['t'] is None


def test_upgrade_archives_history_and_preserves_replacement_deduplication(store):
    rid=granted(store)
    make_legacy_queue(store,rid,'completed')
    upgraded=PostgresStore(store.dsn,initialize=True)
    assert upgraded.admit(ReplaceCommand(rid,'60'))=='old-operation'
    assert upgraded.operation('old-operation')['result_version']==2
    with upgraded.connection() as db:
        assert db.execute("SELECT to_regclass(current_schema() || '.commands') AS t").fetchone()['t'] is None
        assert db.execute("SELECT to_regclass(current_schema() || '.document_sequences') AS t").fetchone()['t'] is None
        assert db.execute('SELECT count(*) AS n FROM legacy_commands').fetchone()['n']==1
    # Repeated initialization is safe and preserves the same identity.
    assert PostgresStore(store.dsn,initialize=True).admit(ReplaceCommand(rid,'60'))=='old-operation'


def test_admission_failure_rolls_back_queue_and_protection(store):
    rid=granted(store)
    with store.connection(True) as db:
        db.execute("""CREATE FUNCTION reject_outbox() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'test outbox unavailable'; END $$""")
        db.execute('CREATE TRIGGER reject_event BEFORE INSERT ON outbox FOR EACH ROW EXECUTE FUNCTION reject_outbox()')
    with pytest.raises(psycopg.Error,match='test outbox unavailable'):
        store.admit(ReplaceCommand(rid,'60'))
    assert store.reservation(rid).status=='active'
    assert store.reservation(rid).expires_at is not None
    with store.connection() as db:
        assert db.execute('SELECT count(*) AS n FROM replacements').fetchone()['n']==0


def test_http_result_uses_recorded_revision_not_latest_head(store):
    rid=granted(store);oid=store.admit(ReplaceCommand(rid,'60'));finish(store,oid)
    other=granted(store,version=2);finish(store,store.admit(ReplaceCommand(other,'90')))
    result=store.replacement_result(oid)
    assert result.version==2 and result.text.startswith('60 days')
    assert store.get_document('X').version==3


def test_http_wait_uses_real_redis_completion_notification(store,redis_url):
    import asyncio
    from redis.asyncio import Redis
    from redlining.main import LiveHub
    from redlining.runtime import DocumentService, Workers
    rid=granted(store)
    async def scenario():
        redis=Redis.from_url(redis_url,decode_responses=True)
        hub=LiveHub(redis);worker=Workers(store,redis)
        service=DocumentService(store,hub,wait_seconds=5,reconcile_seconds=60)
        loop=asyncio.get_running_loop();admitted=asyncio.Event();original=store.admit
        def admit(command):
            oid=original(command);loop.call_soon_threadsafe(admitted.set);return oid
        store.admit=admit
        try:
            await worker.groups()
            task=asyncio.create_task(service.change(ReplaceCommand(rid,'60')))
            await admitted.wait()
            await worker.write_once()
            await worker.relay_once()
            await worker.consume_once('live-delivery-v1')
            result=await asyncio.wait_for(task,3)
            assert result.text.startswith('60 days') and result.version==2
            assert not hub.documents
        finally:
            await redis.aclose()
    asyncio.run(scenario())


def test_native_relevance_ranks_before_document_id_and_keeps_exact_anchors(store):
    bodies = {'a-weak':'contract unrelated words '*40 + 'liability',
              'z-strong':'contract liability. '*8,
              'b-false-positive':'contractual liability'}
    for did, body in bodies.items():
        store.create_document(did, body)
        store.index_revision(did,1)
    for mode in ('terms','phrase'):
        hits=store.search(SearchRequest(text='contract liability',mode=mode)).hits
        assert hits[0].document_id=='z-strong'
        assert hits[0].score > 0
        assert all(h.document_id!='b-false-positive' for h in hits)
        assert [h.score for h in hits]==sorted((h.score for h in hits),reverse=True)
        for hit in hits:
            assert bodies[hit.document_id][hit.target.start:hit.target.end] in hit.context
    literal=store.search(SearchRequest(text='contract',mode='literal')).hits
    assert all(h.score is None for h in literal)


def test_ranking_backfills_existing_rows_and_reindexes_new_revision(store):
    store.create_document('rank','alpha beta '*4);store.index_revision('rank',1)
    with store.connection(True) as db:
        db.execute("UPDATE search_documents SET ranking_sections=NULL WHERE document_id='rank'")
    upgraded=PostgresStore(store.dsn,initialize=True)
    assert upgraded.search(SearchRequest(text='alpha beta')).hits[0].score > 0
    with store.connection(True) as db:
        db.execute("INSERT INTO revisions VALUES ('rank',2,'gamma delta',NULL)")
        db.execute("UPDATE documents SET version=2 WHERE id='rank'")
    store.index_revision('rank',2)
    assert not store.search(SearchRequest(text='alpha beta')).hits
    hit=store.search(SearchRequest(text='gamma delta')).hits[0]
    assert hit.score > 0 and hit.target.version==2


def batch_request(*ranges):
    from redlining.models import CreateEditReservationBatchRequest
    return CreateEditReservationBatchRequest(targets=[DocumentAnchor(version=1,start=a,end=b) for a,b in ranges])


def test_reservation_batch_atomicity_duplicates_and_shared_deadline(store):
    with pytest.raises(ServiceError) as error:
        store.reserve_batch('a','X',batch_request((0,2),(1,4)))
    assert error.value.item_index==1
    assert store.reservation_snapshot('X').reservations==[]
    results=store.reserve_batch('a','X',batch_request((0,2),(9,14),(0,2))).reservations
    assert results[0].reservation_id==results[2].reservation_id
    assert results[0].expires_at==results[1].expires_at
    assert len(store.reservation_snapshot('X').reservations)==2
    original=results[0].expires_at
    replay=store.reserve_batch('a','X',batch_request((0,2))).reservations[0]
    assert replay.expires_at==original
    with pytest.raises(ServiceError):
        store.reserve_batch('b','X',batch_request((15,20),(0,2)))
    assert len(store.reservation_snapshot('X').reservations)==2


def test_batch_commits_once_and_moves_queued_anchor_with_combined_delta(store):
    rows=store.reserve_batch('a','X',batch_request((0,2),(9,14),(15,21))).reservations
    commands=[ReplaceCommand(rows[0].reservation_id,'sixty'),ReplaceCommand(rows[1].reservation_id,'')]
    oid=store.admit_batch('X',commands)
    queued=store.admit(ReplaceCommand(rows[2].reservation_id,'provision'))
    with store.connection(True) as db:
        store.lock_document(db,'X')
        version=store.execute(db,store.operation(oid),store.now(db))
        db.execute("UPDATE replacements SET status='completed',result_version=%s WHERE id=%s",(version,oid))
    assert version==2
    assert store.reservation(rows[2].reservation_id).target==DocumentAnchor(version=2,start=13,end=19)
    state=store.live_state('X',1)
    assert len(state['updates'])==1 and len(state['updates'][0]['patches'])==2
    first_result=store.replacement_result(oid)
    assert first_result.text=='sixty days;  clause. 😀 contract contract'
    finish(store,queued)
    assert store.get_document('X').version==3
    assert store.admit_batch('X',list(reversed(commands)))==oid
    assert store.replacement_result(oid)==first_result
    for changed in ([commands[0]], [commands[0],ReplaceCommand(rows[1].reservation_id,'new')]):
        with pytest.raises(ServiceError): store.admit_batch('X',changed)
    with pytest.raises(ServiceError): store.admit(commands[0])


def test_invalid_batch_admission_does_not_bind_any_reservation(store):
    a=granted(store)
    b=granted(store,start=9,end=14)
    store.release(b)
    with pytest.raises(ServiceError) as error:
        store.admit_batch('X',[ReplaceCommand(a,'60'),ReplaceCommand(b,'new')])
    assert error.value.item_index==1
    assert store.reservation(a).status=='active'
    with store.connection() as db:
        assert db.execute('SELECT count(*) AS n FROM replacements').fetchone()['n']==0
        assert db.execute('SELECT count(*) AS n FROM replacement_items').fetchone()['n']==0
    store.create_document('Y','another')
    with pytest.raises(ServiceError):store.admit_batch('Y',[ReplaceCommand(a,'60')])


def test_batch_failure_rolls_back_all_text_and_releases_all_protection(store):
    rows=store.reserve_batch('a','X',batch_request((0,2),(9,14))).reservations
    oid=store.admit_batch('X',[ReplaceCommand(r.reservation_id,'new') for r in rows])
    with store.connection(True) as db:
        db.execute('UPDATE reservations SET version=99 WHERE id=%s',(rows[1].reservation_id,))
    finish(store,oid)
    assert store.operation(oid)['status']=='failed'
    assert store.get_document('X').version==1
    assert all(store.reservation(r.reservation_id).status=='failed' for r in rows)
    assert store.reservation_snapshot('X').reservations==[]


def test_competing_batches_share_reservation_exactly_once(store):
    rows=store.reserve_batch('a','X',batch_request((0,2),(9,14),(15,21))).reservations
    def admit(pair):
        try:return store.admit_batch('X',[ReplaceCommand(rows[i].reservation_id,'new') for i in pair])
        except ServiceError as e:return e.reason
    with ThreadPoolExecutor(2) as pool:
        outcomes=list(pool.map(admit,[(0,1),(1,2)]))
    assert outcomes.count('REPLACEMENT_MISMATCH')==1
    oid=next(o for o in outcomes if o!='REPLACEMENT_MISMATCH')
    finish(store,oid)
    assert store.get_document('X').version==2


def test_batch_http_completion_and_socket_patch_list(store):
    from redlining.main import build_app
    with TestClient(build_app(database_url=store.dsn,redis_url='redis://127.0.0.1:1')) as client:
        with client.websocket_connect('/documents/X/updates') as ws:
            ws.receive_json()
            r=client.post('/documents/X/edit-reservation-batches',headers={'X-Client-ID':'a'},
                json={'targets':[{'version':1,'start':0,'end':2},{'version':1,'start':9,'end':14}]})
            assert r.status_code==200
            body={'replacements':[{'reservation_id':item['reservation_id'],'replacement':'new'} for item in r.json()['reservations']]}
            with ThreadPoolExecutor(1) as pool:
                pending=pool.submit(client.post,'/documents/X/replacement-batches',json=body)
                import time
                deadline=time.monotonic()+5
                while not store.process_next():
                    assert time.monotonic()<deadline
                    time.sleep(.01)
                response=pending.result(timeout=5)
            assert response.status_code==200
            assert response.json()['version']==2
            assert len(response.json()['reservation_ids'])==2
            assert response.json()['text']==store.get_document('X').text
            state=ws.receive_json()
            while not state.get('updates'):state=ws.receive_json()
            assert len(state['updates'])==1 and len(state['updates'][0]['patches'])==2
            assert client.post('/documents/X/replacement-batches',json=body).json()==response.json()
            bad=client.post('/documents/X/replacement-batches',json={'replacements':body['replacements']*2})
            assert bad.status_code==422


def test_batch_crash_before_commit_recovers_after_restart(store, monkeypatch):
    rows=store.reserve_batch('a','X',batch_request((0,2),(9,14))).reservations
    commands=[ReplaceCommand(r.reservation_id,'new') for r in rows]
    oid=store.admit_batch('X',commands)
    emit=store.emit
    def crash(db,document_id,version,kind='reservations_changed',**payload):
        if kind=='document_updated':raise RuntimeError('simulated worker crash')
        return emit(db,document_id,version,kind,**payload)
    monkeypatch.setattr(store,'emit',crash)
    with pytest.raises(RuntimeError):store.process_next()
    assert store.get_document('X').version==1
    assert store.operation(oid)['status']=='queued'
    assert all(store.reservation(r.reservation_id).status=='submitted' for r in rows)
    recovered=PostgresStore(store.dsn,initialize=True)
    assert recovered.admit_batch('X',commands)==oid
    finish(recovered,oid)
    assert recovered.get_document('X').version==2
    assert not recovered.process_next()


def test_large_batch_inserts_deletes_unicode_and_reindexes(store):
    text='a'*5_300_000+'😀 middle '+'z'*5_300_000
    store.create_document('large-batch',text)
    from redlining.models import CreateEditReservationBatchRequest
    targets=[DocumentAnchor(version=1,start=0,end=0),
             DocumentAnchor(version=1,start=5_300_000,end=5_300_002),
             DocumentAnchor(version=1,start=len(text),end=len(text))]
    rows=store.reserve_batch('a','large-batch',CreateEditReservationBatchRequest(targets=targets)).reservations
    commands=[ReplaceCommand(rows[0].reservation_id,'START '),ReplaceCommand(rows[1].reservation_id,''),
              ReplaceCommand(rows[2].reservation_id,' UNIQUEBATCHEND')]
    oid=store.admit_batch('large-batch',list(reversed(commands)))
    finish(store,oid)
    result=store.replacement_result(oid)
    assert result.text=='START '+text[:5_300_000]+text[5_300_002:]+' UNIQUEBATCHEND'
    assert result.version==2
    store.index_revision('large-batch',2)
    assert store.search(SearchRequest(text='UNIQUEBATCHEND',mode='literal')).hits[0].target.version==2


def one_shot_body():
    from redlining.models import ReserveAndReplaceRequest
    return ReserveAndReplaceRequest(replacements=[
        {'target':{'version':1,'start':0,'end':2},'replacement':'60'},
        {'target':{'version':1,'start':9,'end':14},'replacement':'new'}])


def test_one_shot_atomic_idempotent_and_survives_newer_revision(store):
    body=one_shot_body()
    with ThreadPoolExecutor(2) as pool:
        ids=list(pool.map(lambda _:store.admit_one_shot('a','X','request-1',body),range(2)))
    assert ids[0]==ids[1]
    assert all(r.status=='submitted' for r in store.reservation_snapshot('X').reservations)
    finish(store,ids[0]);first=store.replacement_result(ids[0])
    assert first.text=='60 days; new clause. 😀 contract contract'
    rid=granted(store,start=0,end=2,version=2)
    finish(store,store.admit(ReplaceCommand(rid,'90')))
    assert store.admit_one_shot('a','X','request-1',body)==ids[0]
    assert store.replacement_result(ids[0])==first
    body.replacements.reverse()
    assert store.admit_one_shot('a','X','request-1',body)==ids[0]
    body.replacements[0].replacement='different'
    with pytest.raises(ServiceError,match='Idempotency-Key'):
        store.admit_one_shot('a','X','request-1',body)


def test_one_shot_failure_rolls_back_reservations_and_request_key(store):
    blocker=granted(store,client='b',start=9,end=14)
    with pytest.raises(ServiceError) as error:store.admit_one_shot('a','X','k',one_shot_body())
    assert error.value.item_index==1
    assert len(store.reservation_snapshot('X').reservations)==1
    with store.connection() as db:
        assert db.execute('SELECT count(*) AS n FROM one_shot_requests').fetchone()['n']==0
        assert db.execute('SELECT count(*) AS n FROM replacements').fetchone()['n']==0
    store.release(blocker)
    finish(store,store.admit_one_shot('a','X','k',one_shot_body()))
    assert store.get_document('X').version==2


def test_one_shot_http_returns_document_and_validates_key(store):
    from redlining.main import build_app
    with TestClient(build_app(database_url=store.dsn,redis_url='redis://localhost:1')) as client:
        body=one_shot_body().model_dump()
        assert client.post('/documents/X/replacements',json=body,headers={'X-Client-ID':'a'}).status_code==422
        with ThreadPoolExecutor(1) as pool:
            pending=pool.submit(client.post,'/documents/X/replacements',json=body,
                                headers={'X-Client-ID':'a','Idempotency-Key':'k'})
            import time
            deadline=time.monotonic()+5
            while not store.process_next():
                assert time.monotonic()<deadline
                time.sleep(.01)
            response=pending.result(timeout=5)
        assert response.status_code==200 and response.json()['version']==2
        assert len(response.json()['reservation_ids'])==2
        assert client.post('/documents/X/replacements',json=body,
                           headers={'X-Client-ID':'a','Idempotency-Key':'k'}).json()==response.json()


def test_browser_departure_keeps_other_connections_and_submitted_work(store):
    first=store.open_browser_connection('X','a');second=store.open_browser_connection('X','a')
    active=granted(store);queued=granted(store,start=9,end=14)
    oid=store.admit(ReplaceCommand(queued,'new'))
    store.close_browser_connection('X',first,leaving=True)
    assert store.reservation(active).status=='active'
    store.close_browser_connection('X',second,leaving=True)
    assert store.reservation(active).status=='released'
    assert store.reservation(queued).status=='submitted'
    finish(store,oid)
    assert store.get_document('X').version==2


def test_browser_reconnect_and_crash_cleanup_across_process_instances(store):
    connection=store.open_browser_connection('X','a');rid=granted(store)
    store.close_browser_connection('X',connection)
    other=PostgresStore(store.dsn)
    restored=other.open_browser_connection('X','a')
    with store.connection(True) as db:
        db.execute('UPDATE browser_connections SET expires=0 WHERE id=%s',(connection,))
    store.expire()
    assert store.reservation(rid).status=='active'
    assert other.heartbeat_browser_connection('X',restored)
    with store.connection(True) as db:db.execute('UPDATE browser_connections SET expires=0 WHERE id=%s',(restored,))
    other.expire()
    assert store.reservation(rid).status=='released'
    assert not store.heartbeat_browser_connection('X',restored)
    # API-only ownership never registers a browser and retains normal lease rules.
    independent=granted(store,client='api')
    other.expire()
    assert store.reservation(independent).status=='active'


def test_browser_socket_registers_heartbeats_and_explicit_leave(store):
    from redlining.main import build_app
    import time
    with TestClient(build_app(database_url=store.dsn,redis_url='redis://localhost:1')) as client:
        with client.websocket_connect('/documents/X/updates?client_id=a') as ws:
            ws.receive_json()
            rid=granted(store)
            ws.send_json({'type':'heartbeat'})
            ws.receive_json()
            ws.close(code=1000,reason='page-left')
            deadline=time.monotonic()+3
            while store.reservation(rid).status!='released':
                assert time.monotonic()<deadline
                time.sleep(.01)


@pytest.mark.parametrize('kind',['single','batch','one-shot'])
def test_http_pending_status_resource_and_original_result(store,kind):
    from redlining.main import build_app
    app=build_app(database_url=store.dsn,redis_url='redis://localhost:1')
    app.state.redlining.wait_seconds=0
    if kind=='one-shot':
        path='/documents/X/replacements';body=one_shot_body().model_dump()
    elif kind=='batch':
        rows=store.reserve_batch('a','X',batch_request((0,2),(9,14))).reservations
        path='/documents/X/replacement-batches';body={'replacements':[{'reservation_id':r.reservation_id,'replacement':'new'} for r in rows]}
    else:
        path='/replacements';body={'reservation_id':granted(store),'replacement':'60'}
    with TestClient(app) as client:
        headers={'X-Client-ID':'a','Idempotency-Key':'pending-request'}
        response=client.post(path,json=body,headers=headers)
        assert response.status_code==202
        location=response.headers['Location']
        assert response.json()['status_url']==location
        assert response.headers['Retry-After']=='2'
        assert response.json()['status']=='pending'
        assert client.get(location).json()['status']=='pending'
        assert client.post(path,json=body,headers=headers).json()==response.json()
        finish(store,response.json()['operation_id'])
        completed=client.get(location)
        assert completed.status_code==200
        assert completed.headers['Cache-Control']=='no-store'
        assert completed.json()['status']=='completed'
        result=completed.json()['result']
        assert result['version']==2 and result['text']==store.get_document('X').text
        next_rid=granted(store,start=0,end=1,version=2)
        finish(store,store.admit(ReplaceCommand(next_rid,'Z')))
        assert client.get(location).json()['result']==result
        replay=client.post(path,json=body,headers=headers)
        assert replay.status_code==200 and replay.json()==result
        assert client.get('/replacement-operations/missing').status_code==404


def test_operation_resource_reports_failed_outcome_without_false_success(store):
    from redlining.main import build_app
    rid=granted(store);oid=store.admit(ReplaceCommand(rid,'60'))
    with store.connection(True) as db:db.execute('UPDATE reservations SET version=99 WHERE id=%s',(rid,))
    finish(store,oid)
    with TestClient(build_app(database_url=store.dsn,redis_url='redis://localhost:1')) as client:
        response=client.get('/replacement-operations/'+oid)
        assert response.status_code==200 and response.json()['status']=='failed'
        assert response.json()['error']['reason']=='QUEUE_INVARIANT'
        assert 'result' not in response.json()
