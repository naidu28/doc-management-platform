from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from redlining.api import create_app
from redlining.models import (
    DocumentAnchor, ReplacementResult, Document, EditReservationsResult,
    Reservation, SearchHit, SearchResponse,
)
from redlining.services import RedliningService, RetrievalService, ServiceError

CLIENT_ID = "client-1"
TARGET = {"version": 1, "start": 0, "end": 2}
CHANGE = {"reservation_id": "r-1", "replacement": "60"}
HEADERS = {}
DOC = Document(document_id="X", version=2, text="60 days")
RESULT = ReplacementResult(**DOC.model_dump(), reservation_ids=["r-1"])


@pytest.fixture
def api():
    writer = AsyncMock(spec=RedliningService)
    reader = AsyncMock(spec=RetrievalService)
    app = create_app(writer, reader)
    with TestClient(app, raise_server_exceptions=False, headers={"X-Client-ID": CLIENT_ID}) as client:
        yield client, writer, reader


def test_write_requires_collaboration_client_id():
    writer = AsyncMock(spec=RedliningService)
    with TestClient(create_app(writer)) as client:
        response = client.post("/documents/X/edit-reservations", json={'targets': [TARGET]})
    assert response.status_code == 422
    writer.reserve_batch.assert_not_awaited()


def test_missing_adapter_is_explicit():
    app = create_app()
    with TestClient(app) as client:
        assert client.get("/documents/X").status_code == 503


def test_change_returns_committed_document_without_polling(api):
    client, writer, _ = api
    writer.change_batch.return_value = RESULT
    response = client.post('/documents/X/replacements', json={'replacements': [CHANGE]})
    assert response.status_code == 200
    assert response.json() == RESULT.model_dump()
    assert "location" not in response.headers
    assert client.get("/operations/op-1").status_code == 404


@pytest.mark.parametrize("status,reason", [(409, "EDIT_CONFLICT"), (404, "DOCUMENT_NOT_FOUND")])
def test_service_errors(api, status, reason):
    client, writer, _ = api
    writer.change_batch.side_effect = ServiceError(status, reason, "Cannot apply edit", current_version=3)
    response = client.post('/documents/X/replacements', json={'replacements': [CHANGE]}, headers=HEADERS)
    assert response.status_code == status
    assert response.json() == {"error": "Cannot apply edit", "code": status, "reason": reason, "current_version": 3}


@pytest.mark.parametrize("target", [
    {**TARGET, "start": -1}, {**TARGET, "end": -1},
    {**TARGET, "version": 0}, {**TARGET, "start": True},
])
def test_invalid_anchors_never_reach_writer(api, target):
    client, writer, _ = api
    response = client.post("/documents/X/edit-reservations", json={'targets': [target]})
    assert response.status_code == 422
    writer.change_batch.assert_not_awaited()


def test_unknown_fields_are_rejected(api):
    client, writer, _ = api
    assert client.post('/documents/X/replacements', json={'replacements': [{**CHANGE, "actor_id": "admin"}]}).status_code == 422
    assert client.post('/documents/X/replacements', json={'replacements': [{**CHANGE, "target": {**TARGET, "expected_text": "30"}}]}).status_code == 422
    writer.change_batch.assert_not_awaited()


def test_empty_replacement(api):
    client, writer, _ = api
    writer.change_batch.return_value = RESULT
    assert client.post('/documents/X/replacements', json={'replacements': [{"reservation_id": "r-1", "replacement": ""}]}).status_code == 200


def test_zero_width_anchor():
    assert DocumentAnchor(version=1, start=0, end=0).end == 0


@pytest.mark.parametrize("state,code", [("active", 200)])
def test_reservation_lifecycle_routes(api, state, code):
    client, writer, _ = api
    reservation = Reservation(reservation_id="r-1", document_id="X", client_id=CLIENT_ID, target=DocumentAnchor(**TARGET),
                              created_at="2026-10-12T18:00:00Z", status=state,
                              expires_at="2026-10-12T18:00:30Z" if state == "active" else None)
    writer.reserve_batch.return_value = EditReservationsResult(reservations=[reservation])
    writer.reservation.return_value = reservation
    writer.renew.return_value = reservation
    writer.release.return_value = None
    response = client.post("/documents/X/edit-reservations", json={'targets': [TARGET]}, headers=HEADERS)
    assert response.status_code == code
    assert client.post("/edit-reservations/r-1/renew", headers=HEADERS).status_code == 200
    assert client.delete("/edit-reservations/r-1").status_code == 204
    writer.release.assert_awaited_once_with("r-1")


def test_versioned_retrieval(api):
    client, _, reader = api
    reader.get_document.return_value = DOC
    assert client.get("/documents/X?version=2").json() == DOC.model_dump()
    reader.get_document.assert_awaited_with("X", 2)
    assert client.get("/documents/X").status_code == 200
    reader.get_document.assert_awaited_with("X", None)
    assert client.get("/documents/X?version=0").status_code == 422


def test_search_anchor_and_scope(api):
    client, _, reader = api
    reader.search.return_value = SearchResponse(hits=[SearchHit(document_id="X", target=DocumentAnchor(**TARGET), context="30 days")])
    response = client.post("/search", json={"text": "30", "mode": "literal", "document_ids": ["X"]})
    assert response.status_code == 200
    assert response.json()["hits"][0]["target"] == TARGET
    assert response.json()["consistency"] == "eventual"
    assert reader.search.call_args.args[0].document_ids == ["X"]
    client.post("/search", json={"text": "30", "document_ids": []})
    assert reader.search.call_args.args[0].document_ids == []


@pytest.mark.parametrize("query", [{"text": " "}, {"text": "x", "limit": 101}, {"text": "x", "version": 1}])
def test_invalid_search(api, query):
    client, _, reader = api
    assert client.post("/search", json=query).status_code == 422
    reader.search.assert_not_awaited()


def test_unexpected_error_does_not_expose_document_text(api):
    client, _, reader = api
    reader.get_document.side_effect = RuntimeError("secret contract text")
    response = client.get("/documents/X")
    assert response.status_code == 500
    assert "secret" not in response.text


def test_openapi_documents_committed_response(api):
    client, _, _ = api
    schema = client.get("/openapi.json").json()
    responses = schema["paths"]["/documents/{document_id}/replacements"]["post"]["responses"]
    assert {"200", "202", "409", "422"} <= responses.keys()
    assert "504" not in responses
    assert responses["202"]["content"]["application/json"]["schema"]["$ref"].endswith("/ReplacementAccepted")


def test_reads_require_no_client_header(api):
    client, _, reader = api
    client.headers.pop("X-Client-ID")
    reader.get_document.return_value = DOC
    reader.search.return_value = SearchResponse(hits=[])
    assert client.get("/documents/X").status_code == 200
    assert client.post("/search", json={"text": "notice"}).status_code == 200
    schema = client.get("/openapi.json").json()
    assert "Principal" not in schema["components"]["schemas"]
    assert "securitySchemes" not in schema["components"]


@pytest.mark.parametrize("reason", ["EDIT_CONFLICT", "REFRESH_REQUIRED", "REPLACEMENT_MISMATCH", "RESERVATION_RELEASED"])
def test_reserved_change_conflict_mapping(api, reason):
    client, writer, _ = api
    writer.change_batch.side_effect = ServiceError(409, reason, "Cannot apply edit", 3)
    response = client.post('/documents/X/replacements', json={'replacements': [{"reservation_id": "r-1", "replacement": "60"}]})
    assert response.status_code == 409
    assert response.json()["reason"] == reason
    assert response.json()["current_version"] == 3


def test_reserved_change_looks_up_identity_from_reservation(api):
    from redlining.services import ReplaceCommand
    client, writer, _ = api
    client.headers.pop("X-Client-ID")
    writer.change_batch.return_value = RESULT
    response = client.post('/documents/X/replacements', json={'replacements': [{"reservation_id": "r-1", "replacement": "60"}]})
    assert response.status_code == 200
    assert writer.change_batch.call_args.args[0] == "X"
    assert writer.change_batch.call_args.args[1].replacements[0].reservation_id == "r-1"
    assert response.json() == RESULT.model_dump()


@pytest.mark.parametrize("body", [{"replacement": "60"}, {"reservation_id": "r-1", "replacement": "60", "target": TARGET}])
def test_reserved_change_rejects_missing_reservation_or_redundant_anchor(api, body):
    client, writer, _ = api
    assert client.post('/documents/X/replacements', json={'replacements': [body]}).status_code == 422
    writer.change_batch.assert_not_awaited()


def test_reacquisition_blocked_by_active_reservation(api):
    client, writer, _ = api
    writer.renew.side_effect = ServiceError(409, "RANGE_RESERVED", "Another client holds this selection")
    response = client.post("/edit-reservations/r-1/renew")
    assert response.status_code == 409
    assert response.json()["reason"] == "RANGE_RESERVED"


def test_socket_payload_versions_and_range():
    from pydantic import ValidationError
    from redlining.models import DocumentUpdated
    body = dict(event_id="e-1", document_id="X", previous_version=1, version=2,
                patches=[{"start": 0, "end": 2, "replacement": "60"}])
    assert DocumentUpdated(**body).version == 2
    with pytest.raises(ValidationError):
        DocumentUpdated(**{**body, "version": 4})


def test_design_uses_actual_contracts():
    import design
    from redlining.services import ReplaceCommand
    assert design.DocumentAnchor is DocumentAnchor
    assert design.ReplaceCommand is ReplaceCommand


def test_reservation_snapshot_route_and_alignment(api):
    from pydantic import ValidationError
    from redlining.models import ReservationSnapshot
    reservation = Reservation(reservation_id="r-1", document_id="X", client_id=CLIENT_ID,
        target=DocumentAnchor(**TARGET),
        status="active", created_at="2026-10-12T18:00:00Z")
    with pytest.raises(ValidationError):
        ReservationSnapshot(document_id="X", document_version=2, reservations=[reservation])


def test_one_shot_is_documented_with_required_idempotency_key(api):
    client, _, _ = api
    schema = client.get("/openapi.json").json()
    operation = schema["paths"]["/documents/{document_id}/replacements"]["post"]
    assert "TargetedReplacementsRequest" in schema["components"]["schemas"]
    assert any(p["name"] == "Idempotency-Key" for p in operation["parameters"])
    # Conditional requirement is enforced only for target-based bodies.
    assert len(operation["requestBody"]["content"]["application/json"]["schema"]["anyOf"]) == 2
    assert "200" in operation["responses"]


def test_retired_routes_are_absent(api):
    client,_,_=api
    paths=client.get('/openapi.json').json()['paths']
    for route in ['/replacements','/documents/{document_id}/replacement-batches','/documents/{document_id}/edit-reservation-batches']:
        assert route not in paths
    for route in ['/replacements','/documents/X/replacement-batches','/documents/X/edit-reservation-batches']:
        assert client.post(route,json={}).status_code==404


@pytest.mark.parametrize('items',[
    [],
    [{'reservation_id':'r','replacement':'x'}, {'target':TARGET,'replacement':'x'}],
    [{'reservation_id':'r','target':TARGET,'replacement':'x'}],
    [{'reservation_id':'r','replacement':'x'}]*2,
    [{'target':TARGET,'replacement':'x'}]*101,
])
def test_unified_replacement_rejects_mixed_empty_duplicate_and_oversized_requests(api,items):
    client,writer,_=api
    assert client.post('/documents/X/replacements',json={'replacements':items}).status_code==422
    writer.change_batch.assert_not_awaited()
    writer.reserve_and_replace.assert_not_awaited()


def test_target_headers_are_conditional_and_unary_result_is_array(api):
    client,writer,_=api
    writer.reserve_and_replace.return_value=RESULT
    body={'replacements':[{'target':TARGET,'replacement':'60'}]}
    assert client.post('/documents/X/replacements',json=body).status_code==422
    response=client.post('/documents/X/replacements',json=body,headers={'Idempotency-Key':'key'})
    assert response.status_code==200 and response.json()['reservation_ids']==['r-1']
    writer.reserve_and_replace.assert_awaited_once()


def test_retired_read_routes(api):
    client, _, _ = api
    assert client.get('/documents/X/edit-reservations').status_code == 405
    assert client.get('/edit-reservations/r-1').status_code == 405
    assert client.get('/documents/X/search?q=contract').status_code == 404
    assert client.put('/edit-reservations/r-1/draft', json={'replacement':'local'}).status_code == 404


@pytest.mark.parametrize('body', [
    {'replacements':[{'reservation_id':'r-1','replacement':'bad\x00text'}]},
    {'replacements':[{'reservation_id':'bad\x00id','replacement':'valid'}]},
    {'replacements':[{'reservation_id':'r-1','replacement':'\ud800'}]},
])
def test_invalid_unicode_rejected_before_writer(api, body):
    import json
    client, writer, _ = api
    response = client.post('/documents/X/replacements',content=json.dumps(body),headers={'Content-Type':'application/json'})
    assert response.status_code == 422
    writer.change_batch.assert_not_awaited()


def test_duplicate_reservation_targets_rejected_before_writer(api):
    client, writer, _ = api
    response = client.post('/documents/X/edit-reservations',json={'targets':[TARGET,TARGET]})
    assert response.status_code == 422
    writer.reserve_batch.assert_not_awaited()


@pytest.mark.parametrize('body', [{'text':'bad\x00text'}, {'text':'valid','document_ids':['bad\x00id']}])
def test_invalid_search_unicode_rejected_before_reader(api, body):
    client, _, reader = api
    response = client.post('/search',json=body)
    assert response.status_code == 422
    reader.search.assert_not_awaited()
