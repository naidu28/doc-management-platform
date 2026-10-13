import pytest
from redlining.models import CreateEditReservationRequest, DocumentAnchor
from redlining.services import ServiceError, ReplaceCommand


def reserve(store, client, start, end, version=1):
    return store.reserve(client, 'doc', CreateEditReservationRequest(target=DocumentAnchor(version=version,start=start,end=end)))


def test_reconnect_retains_long_history_for_visual_selection(pg_store):
    store=pg_store;store.create_document('doc','a tail')
    for version in range(1,103):
        r=reserve(store,'writer',0,1,version)
        store.admit(ReplaceCommand(r.reservation_id,'b' if version%2 else 'a'))
        store.process_next()
    assert 'document' in store.live_state('doc',1)
    state=store.live_state('doc',1,retain_history=True)
    assert len(state['updates'])==102
    assert state['updates'][0]['previous_version']==1


def test_workbench_has_no_server_drafts(pg_store):
    from fastapi.testclient import TestClient
    from redlining.api import create_app
    from redlining.ui import attach_ui
    store=pg_store
    store.create_document('doc','hello')
    app=create_app();attach_ui(app,store)
    client=TestClient(app)
    assert client.get('/documents').json()[0]['document_id']=='doc'
    assert client.put('/edit-reservations/unused/draft',json={'replacement':'local'}).status_code==404
    assert 'drafts' not in store.live_state('doc')
