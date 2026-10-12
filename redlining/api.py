from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from .models import (
    ApplyDocumentChangeRequest, ChangeResult, Document, ReserveAndReplaceRequest,
    ReplacementAccepted, ReplacementOperation,
    CreateEditReservationBatchRequest, EditReservationBatchResult, ApplyReplacementBatchRequest, ReplacementBatchResult, Error,
    Reservation, ReservationSnapshot, CreateEditReservationRequest, SearchRequest, SearchResponse,
)
from .services import RedliningService, RetrievalService, ServiceError, ReplaceCommand


def redlining_service(request: Request) -> RedliningService:
    service = request.app.state.redlining
    if service is None:
        raise ServiceError(503, "SERVICE_UNAVAILABLE", "Redlining adapter is not configured")
    return service


def retrieval_service(request: Request) -> RetrievalService:
    service = request.app.state.retrieval
    if service is None:
        raise ServiceError(503, "SERVICE_UNAVAILABLE", "Retrieval adapter is not configured")
    return service


Client = Annotated[str, Header(alias="X-Client-ID", min_length=1, max_length=200)]
Writer = Annotated[RedliningService, Depends(redlining_service)]
Reader = Annotated[RetrievalService, Depends(retrieval_service)]
writes = APIRouter(tags=["redlining"])
reads = APIRouter(tags=["retrieval"])


PENDING_RESPONSE = {202: {"model": ReplacementAccepted, "description": "Durably accepted; still pending",
    "headers": {"Location": {"schema": {"type": "string"}}, "Retry-After": {"schema": {"type": "integer"}}}}}


def replacement_response(result):
    if isinstance(result, ReplacementAccepted):
        return JSONResponse(status_code=202, content=result.model_dump(),
            headers={"Location": result.status_url, "Retry-After": "2", "Cache-Control": "no-store"})
    return result


@reads.get("/replacement-operations/{operation_id}", response_model=ReplacementOperation)
async def replacement_operation(operation_id: str, response: Response, service: Writer):
    result = await service.replacement_operation(operation_id)
    response.headers["Cache-Control"] = "no-store"
    if isinstance(result, ReplacementAccepted):
        response.headers["Retry-After"] = "2"
    return result


@writes.post("/documents/{document_id}/edit-reservations", response_model=Reservation)
async def reserve(document_id: str, body: CreateEditReservationRequest, response: Response, client_id: Client, service: Writer):
    result = await service.reserve(client_id, document_id, body)
    return result


@writes.post("/documents/{document_id}/edit-reservation-batches", response_model=EditReservationBatchResult)
async def reserve_batch(document_id: str, body: CreateEditReservationBatchRequest, client_id: Client, service: Writer):
    return await service.reserve_batch(client_id, document_id, body)


@writes.post("/documents/{document_id}/replacements", response_model=ReplacementBatchResult, responses=PENDING_RESPONSE)
async def reserve_and_replace(document_id: str, body: ReserveAndReplaceRequest, client_id: Client,
                              request_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=200)],
                              service: Writer):
    """Reserve and admit 1–100 replacements atomically; return their committed revision."""
    return replacement_response(await service.reserve_and_replace(client_id, document_id, request_key, body))


@writes.post("/documents/{document_id}/replacement-batches", response_model=ReplacementBatchResult, responses=PENDING_RESPONSE)
async def replace_batch(document_id: str, body: ApplyReplacementBatchRequest, service: Writer):
    return replacement_response(await service.change_batch(document_id, body))


@writes.get("/documents/{document_id}/edit-reservations", response_model=ReservationSnapshot)
async def reservation_snapshot(document_id: str, service: Writer):
    return await service.reservation_snapshot(document_id)


@writes.get("/edit-reservations/{reservation_id}", response_model=Reservation)
async def reservation(reservation_id: str, service: Writer):
    return await service.reservation(reservation_id)


@writes.post("/edit-reservations/{reservation_id}/renew", response_model=Reservation)
async def renew(reservation_id: str, service: Writer):
    return await service.renew(reservation_id)


@writes.delete("/edit-reservations/{reservation_id}", status_code=204)
async def release(reservation_id: str, service: Writer):
    await service.release(reservation_id)
    return Response(status_code=204)


@writes.post("/replacements", response_model=ChangeResult, responses=PENDING_RESPONSE)
async def change(body: ApplyDocumentChangeRequest, service: Writer):
    return replacement_response(await service.change(ReplaceCommand(**body.model_dump())))


@reads.get("/documents/{document_id}", response_model=Document)
async def document(document_id: str, service: Reader,
                   version: Annotated[int | None, Query(ge=1)] = None):
    return await service.get_document(document_id, version)


@reads.get("/documents/{document_id}/search", response_model=SearchResponse)
async def search_document(document_id: str, service: Reader,
                          q: Annotated[str, Query(min_length=1, max_length=4096)],
                          limit: Annotated[int, Query(ge=1, le=100)] = 20):
    if not q.strip():
        raise ServiceError(422, "INVALID_REQUEST", "Query must contain text")
    return await service.search(SearchRequest(text=q, mode="literal", document_ids=[document_id], limit=limit))


@reads.post("/search", response_model=SearchResponse)
async def search(body: SearchRequest, service: Reader):
    return await service.search(body)


def create_app(redlining: RedliningService | None = None,
               retrieval: RetrievalService | None = None) -> FastAPI:
    app = FastAPI(title="Document Redlining & Retrieval", version="0.1.0")
    app.state.redlining = redlining
    app.state.retrieval = retrieval

    @app.exception_handler(ServiceError)
    async def service_error(request: Request, exc: ServiceError):
        body = Error(error=exc.message, code=exc.status, reason=exc.reason,
                     current_version=exc.current_version, item_index=exc.item_index)
        return JSONResponse(status_code=exc.status, content=body.model_dump(exclude_none=True))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        # Do not echo contract text or request bodies in error responses.
        return JSONResponse(status_code=422, content={"error": "Invalid request", "code": 422, "reason": "INVALID_REQUEST"})

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        return JSONResponse(status_code=exc.status_code, headers=exc.headers,
                            content={"error": str(exc.detail), "code": exc.status_code, "reason": "HTTP_ERROR"})

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        return JSONResponse(status_code=500, content={"error": "Internal server error", "code": 500, "reason": "INTERNAL_ERROR"})

    errors = {code: {"model": Error} for code in (404, 409, 422, 500, 503)}
    app.include_router(writes, responses=errors)
    app.include_router(reads, responses=errors)
    return app


app = create_app()
