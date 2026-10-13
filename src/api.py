from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from .models import (
    Document, Identifier, TargetedReplacementsRequest,
    ReplacementAccepted, ReplacementOperation,
    CreateEditReservationsRequest, EditReservationsResult, ReservedReplacementsRequest, ReplacementResult, Error,
    Reservation, SearchRequest, SearchResponse,
)
from .services import RedliningService, RetrievalService, ServiceError


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


Client = Annotated[Identifier, Header(alias="X-Client-ID", min_length=1, max_length=200)]
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
async def replacement_operation(operation_id: Identifier, response: Response, service: Writer):
    result = await service.replacement_operation(operation_id)
    response.headers["Cache-Control"] = "no-store"
    if isinstance(result, ReplacementAccepted):
        response.headers["Retry-After"] = "2"
    return result


@writes.post("/documents/{document_id}/edit-reservations", response_model=EditReservationsResult)
async def reserve(document_id: Identifier, body: CreateEditReservationsRequest, client_id: Client, service: Writer):
    return await service.reserve_batch(client_id, document_id, body)


@writes.post("/documents/{document_id}/replacements", response_model=ReplacementResult, responses=PENDING_RESPONSE)
async def replace(document_id: Identifier, body: ReservedReplacementsRequest | TargetedReplacementsRequest,
                  service: Writer,
                  client_id: Annotated[Identifier | None, Header(alias="X-Client-ID", min_length=1, max_length=200)] = None,
                  request_key: Annotated[Identifier | None, Header(alias="Idempotency-Key", min_length=1, max_length=200)] = None):
    """Apply 1–100 edits atomically. Use reservation IDs OR versioned targets, never both.

    Target-based requests require X-Client-ID and Idempotency-Key. Reservation-based
    requests derive identity from the reservations and deduplicate by their set.
    """
    if isinstance(body, TargetedReplacementsRequest):
        if client_id is None or request_key is None:
            raise ServiceError(422,"MISSING_HEADERS","Target-based replacements require X-Client-ID and Idempotency-Key")
        result = await service.reserve_and_replace(client_id, document_id, request_key, body)
    else:
        result = await service.change_batch(document_id, body)
    return replacement_response(result)


@writes.post("/edit-reservations/{reservation_id}/renew", response_model=Reservation)
async def renew(reservation_id: Identifier, service: Writer):
    return await service.renew(reservation_id)


@writes.delete("/edit-reservations/{reservation_id}", status_code=204)
async def release(reservation_id: Identifier, service: Writer):
    await service.release(reservation_id)
    return Response(status_code=204)


@reads.get("/documents/{document_id}", response_model=Document)
async def document(document_id: Identifier, service: Reader,
                   version: Annotated[int | None, Query(ge=1)] = None):
    return await service.get_document(document_id, version)


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
