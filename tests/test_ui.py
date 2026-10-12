import pytest
from redlining.storage import Store
from redlining.models import CreateEditReservationRequest, DocumentAnchor
from redlining.services import ServiceError, ReplaceCommand
from redlining.ui import save_draft


def reserve(store, client, start, end, version=1):
    return store.reserve(client, 'doc', CreateEditReservationRequest(target=DocumentAnchor(version=version,start=start,end=end)))


def test_draft_survives_expiry_rebases_and_submits(tmp_path):
    now=[1000]
    store=Store(tmp_path/'test.db',clock=lambda:now[0])
    store.create_document('doc','hello world')
    r=reserve(store,'a',6,11)
    save_draft(store,r.reservation_id,'a','earth')
    now[0]+=1000
    store.expire()
    assert store.reservation(r.reservation_id).expires_at is None
    assert store.renew(r.reservation_id).expires_at is None
    with pytest.raises(ServiceError): reserve(store,'b',6,11)
    before=reserve(store,'b',0,5)
    store.admit(ReplaceCommand(before.reservation_id,'hi'))
    store.process_next()
    state=store.live_state('doc')
    assert state['drafts'][0]['replacement']=='earth'
    assert store.reservation(r.reservation_id).target.start==3
    store.admit(ReplaceCommand(r.reservation_id,'earth'))
    store.process_next()
    assert store.get_document('doc').text=='hi earth'
    assert store.live_state('doc')['drafts']==[]


def test_expired_lease_cannot_promote_over_other_reservation(tmp_path):
    now=[1000];store=Store(tmp_path/'test.db',clock=lambda:now[0]);store.create_document('doc','hello')
    r=reserve(store,'a',0,5);now[0]+=31;reserve(store,'b',0,5)
    with pytest.raises(ServiceError): save_draft(store,r.reservation_id,'a','bye')


def test_discard_releases_durable_protection(tmp_path):
    store=Store(tmp_path/'test.db');store.create_document('doc','hello');r=reserve(store,'a',0,5)
    save_draft(store,r.reservation_id,'a','');store.release(r.reservation_id)
    reserve(store,'b',0,5)
    assert store.live_state('doc')['drafts']==[]


def test_expired_visual_selection_rebases_before_typing(tmp_path):
    now=[1000];store=Store(tmp_path/'test.db',clock=lambda:now[0]);store.create_document('doc','hello world')
    r=reserve(store,'a',6,11);now[0]+=31;store.expire()
    before=reserve(store,'b',0,5);store.admit(ReplaceCommand(before.reservation_id,'hi'));store.process_next()
    save_draft(store,r.reservation_id,'a','earth')
    assert store.reservation(r.reservation_id).target.start==3
    assert store.reservation(r.reservation_id).target.version==2


def test_expired_visual_selection_invalidated_by_commit(tmp_path):
    now=[1000];store=Store(tmp_path/'test.db',clock=lambda:now[0]);store.create_document('doc','hello')
    r=reserve(store,'a',0,5);now[0]+=31;store.expire()
    other=reserve(store,'b',0,5);store.admit(ReplaceCommand(other.reservation_id,'bye'));store.process_next()
    with pytest.raises(ServiceError,match='replaced'):save_draft(store,r.reservation_id,'a','new')


def test_reconnect_retains_long_history_for_visual_selection(tmp_path):
    store=Store(tmp_path/'test.db');store.create_document('doc','a tail')
    for version in range(1,103):
        r=reserve(store,'writer',0,1,version)
        store.admit(ReplaceCommand(r.reservation_id,'b' if version%2 else 'a'))
        store.process_next()
    assert 'document' in store.live_state('doc',1)
    state=store.live_state('doc',1,retain_history=True)
    assert len(state['updates'])==102
    assert state['updates'][0]['previous_version']==1
