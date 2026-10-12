from concurrent.futures import ThreadPoolExecutor

import pytest

from redlining.models import CreateEditReservationRequest, DocumentAnchor, SearchRequest
from redlining.services import ReplaceCommand, ServiceError
from redlining.storage import Store


@pytest.fixture
def storage(tmp_path):
    now = [1000.0]
    store = Store(tmp_path/'test.sqlite', clock=lambda:now[0])
    store.create_document('X', '30 days; other clause. 😀 contract contract')
    return store, now


def reserve(s, start, end, client='a', version=1):
    return s.reserve(client, 'X', CreateEditReservationRequest(target=DocumentAnchor(version=version,start=start,end=end)))


def test_duplicate_acquisition_and_expiry(storage):
    s, now = storage
    first = reserve(s,0,2)
    now[0] += 10
    assert reserve(s,0,2) == first
    assert s.renew(first.reservation_id).expires_at.timestamp() == 1040
    now[0] = 1041
    second = reserve(s,0,2,client='b')
    assert second.reservation_id != first.reservation_id
    with pytest.raises(ServiceError, match='reserved'):
        s.admit(ReplaceCommand(first.reservation_id,'60'))


def test_submitted_protection_never_expires(storage):
    s, now = storage
    r = reserve(s,0,2)
    oid = s.admit(ReplaceCommand(r.reservation_id,'60'))
    now[0] += 1000
    s.expire()
    assert s.reservation(r.reservation_id).status == 'submitted'
    with pytest.raises(ServiceError):
        reserve(s,0,2,client='b')
    with pytest.raises(ServiceError):
        s.release(r.reservation_id)
    assert s.process_next()
    assert s.operation(oid).document.text.startswith('60 days')


def test_duplicate_submissions_return_original_revision(storage):
    s, _ = storage
    r = reserve(s,0,2)
    command = ReplaceCommand(r.reservation_id,'600')
    oid = s.admit(command)
    assert s.admit(command) == oid
    s.process_next()
    second = reserve(s,0,3,version=2)
    s.admit(ReplaceCommand(second.reservation_id,'900'))
    s.process_next()
    assert s.admit(command) == oid
    assert s.operation(oid).document.version == 2
    assert s.operation(oid).document.text.startswith('600')
    with pytest.raises(ServiceError) as error:
        s.admit(ReplaceCommand(r.reservation_id,'different'))
    assert error.value.reason == 'REPLACEMENT_MISMATCH'


def test_active_and_queued_anchors_move_together(storage):
    s, _ = storage
    first = reserve(s,0,2)
    queued = reserve(s,9,14,client='b')
    active = reserve(s,22,23,client='c')
    s.admit(ReplaceCommand(first.reservation_id,'6000'))
    s.admit(ReplaceCommand(queued.reservation_id,'OTHER'))
    s.process_next()
    assert s.reservation(queued.reservation_id).target == DocumentAnchor(version=2,start=11,end=16)
    assert s.reservation(active.reservation_id).target == DocumentAnchor(version=2,start=24,end=25)
    s.process_next()
    assert s.get_document('X').text.startswith('6000 days; OTHER clause.')
    assert s.reservation(active.reservation_id).target.version == 3


def test_expired_stale_anchor_requires_refresh(storage):
    s, now = storage
    old = reserve(s,9,14)
    now[0] += 31
    other = reserve(s,0,2)
    s.admit(ReplaceCommand(other.reservation_id,'600'))
    s.process_next()
    for action in [lambda: s.renew(old.reservation_id), lambda: s.admit(ReplaceCommand(old.reservation_id,'x')),
                   lambda: reserve(s,0,2,version=1)]:
        with pytest.raises(ServiceError) as error:
            action()
        assert error.value.reason == 'STALE_VERSION'


def test_insertion_boundaries_are_protected(storage):
    s, _ = storage
    reserve(s,0,2)
    for offset in (0,1,2):
        with pytest.raises(ServiceError):
            reserve(s,offset,offset,client='b')
    assert reserve(s,2,7,client='b')  # Adjacent non-empty ranges are disjoint.


def test_admission_does_not_edit_document(storage):
    s, _ = storage
    r=reserve(s,0,2)
    oid=s.admit(ReplaceCommand(r.reservation_id,'60'))
    assert s.get_document('X').version == 1
    assert s.operation(oid).status == 'queued'
    # A new process/connection recovers the queued command.
    restarted=Store(s.path, clock=s.clock)
    restarted.process_next()
    assert restarted.operation(oid).status == 'completed'


def test_transaction_rolls_back_all_execution_writes(storage, monkeypatch):
    s, _ = storage
    r=reserve(s,0,2)
    oid=s.admit(ReplaceCommand(r.reservation_id,'60'))
    original=s.emit
    monkeypatch.setattr(s,'emit',lambda *a,**kw: (_ for _ in ()).throw(RuntimeError('crash')))
    with pytest.raises(RuntimeError):
        s.process_next()
    assert s.get_document('X').version == 1
    assert s.operation(oid).status == 'queued'
    monkeypatch.setattr(s,'emit',original)
    s.process_next()
    assert s.get_document('X').version == 2


def test_100_concurrent_edits_and_overlapping_attempts(tmp_path):
    s=Store(tmp_path/'load.sqlite')
    text=' '.join(f'word{i:03d}' for i in range(100))
    s.create_document('X',text)
    with ThreadPoolExecutor(max_workers=10) as pool:
        reservations=list(pool.map(lambda i: reserve(s,i*8,i*8+7,client=f'client-{i%10}'), range(100)))
        def blocked(i):
            try:
                reserve(s,i*8,i*8+7,client='intruder')
            except ServiceError as e:
                return e.reason
        assert list(pool.map(blocked,range(7))) == ['RANGE_RESERVED']*7
        ids=list(pool.map(lambda pair:s.admit(ReplaceCommand(pair[1].reservation_id,f'new-{pair[0]}')),
                          enumerate(reservations)))
        def drain(_):
            while s.process_next():
                pass
        list(pool.map(drain,range(4)))
    assert s.get_document('X').text == ' '.join(f'new-{i}' for i in range(100))
    assert s.get_document('X').version == 101
    assert all(s.operation(oid).status=='completed' for oid in ids)
    assert s.reservation_snapshot('X').reservations == []
    with s.connection() as db:
        assert db.execute('SELECT count(*) FROM revisions').fetchone()[0] == 101
        assert db.execute("SELECT count(*) FROM outbox WHERE kind='document_updated'").fetchone()[0] == 100


def test_concurrent_same_selection_creates_one_row(storage):
    s,_=storage
    with ThreadPoolExecutor(max_workers=10) as pool:
        rows=list(pool.map(lambda _:reserve(s,0,2),range(30)))
    assert len({r.reservation_id for r in rows}) == 1


def test_exact_search_offsets_and_out_of_order_index(storage):
    s,_=storage
    s.index_revision('X',1)
    for mode, query in [('literal','contract'),('terms','contract'),('phrase','other clause')]:
        hits=s.search(SearchRequest(text=query,mode=mode)).hits
        assert hits
        for hit in hits:
            source=s.get_document('X',hit.target.version).text
            assert source[hit.target.start:hit.target.end] == query
    assert len(s.search(SearchRequest(text='contract',mode='literal')).hits)==2
    assert not s.search(SearchRequest(text='contract',document_ids=[])).hits
    r=reserve(s,0,2)
    s.admit(ReplaceCommand(r.reservation_id,'sixty'))
    s.process_next()
    assert s.search(SearchRequest(text='30',mode='literal')).hits[0].target.version==1
    assert s.index_revision('X',2)
    assert not s.index_revision('X',1)
    assert not s.search(SearchRequest(text='30',mode='literal')).hits
    assert s.search(SearchRequest(text='sixty')).hits[0].target.version==2


def test_query_syntax_is_literal(storage):
    s,_=storage
    s.index_revision('X',1)
    assert not s.search(SearchRequest(text='contract OR banana',mode='terms')).hits


def test_snapshot_and_patch_agree(storage):
    s,_=storage
    r=reserve(s,0,2)
    s.admit(ReplaceCommand(r.reservation_id,'600'))
    before=s.live_state('X')
    s.process_next()
    after=s.live_state('X',1)
    patch=after['updates'][0]['patch']
    text=before['document']['text']
    assert text[:patch['start']]+patch['replacement']+text[patch['end']:] == s.get_document('X').text
    assert after['reservations']['document_version']==2


def test_unicode_fts_offsets_and_repeated_phrase(tmp_path):
    s=Store(tmp_path/'unicode.sqlite')
    text='😀 prefix; Café agreement; Café agreement.'
    s.create_document('X',text)
    s.index_revision('X',1)
    hit=s.search(SearchRequest(text='cafe agreement',mode='phrase')).hits[0]
    assert text[hit.target.start:hit.target.end]=='Café agreement'
    assert hit.target.start==10
    assert len(s.search(SearchRequest(text='Café agreement',mode='literal')).hits)==2


def test_rebuild_repairs_current_projection(storage):
    s,_=storage
    s.index_revision('X',1)
    with s.connection(True) as db:
        db.execute("UPDATE search_documents SET body='wrong' WHERE document_id='X'")
    s.index_revision('X',1,force=True)
    assert s.search(SearchRequest(text='contract')).hits


def test_subscription_retains_completion_and_failure_without_version_change(storage):
    s, _ = storage
    r = reserve(s, 0, 2)
    oid = s.admit(ReplaceCommand(r.reservation_id, '60'))
    assert s.live_state('X')['operations'][0]['status'] == 'queued'
    s.process_next()
    completed = s.live_state('X')['operations'][0]
    assert completed['operation_id'] == oid
    assert completed['status'] == 'completed'
    assert completed['result_version'] == 2
    r2 = reserve(s, 0, 2, version=2)
    oid2 = s.admit(ReplaceCommand(r2.reservation_id, '90'))
    # A violated writer invariant is terminal and emits no document patch.
    with s.connection(True) as db:
        db.execute('UPDATE reservations SET version=1 WHERE id=?', (r2.reservation_id,))
    s.process_next()
    state = s.live_state('X', 2)
    assert state['updates'] == []
    failed = state['operations'][1]
    assert failed['operation_id'] == oid2
    assert failed['status'] == 'failed'
    assert failed['failure']['reason'] == 'QUEUE_INVARIANT'
    assert s.live_state('X')['operations'] == state['operations']
