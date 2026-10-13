"""Service ports; storage adapters implement the lifecycle in design.py.

No RBAC. X-Client-ID identifies selection creators; a reservation supplies the
client/document/anchor to the internal writer. Atomic exact-range ownership lookup reuses active reservations; stale selections
are rejected. Duplicate acquisition never renews a lease. A reservation binds to one replacement forever.
Business failures are terminal; duplicate commands return their stored outcome.
"""
from dataclasses import dataclass
from typing import Protocol

from .models import (
    Document, CreateEditReservationsRequest, EditReservationsResult,
    ReservedReplacementsRequest, ReplacementResult, TargetedReplacementsRequest, ReplacementAccepted, ReplacementOperation,
    Reservation, ReservationSnapshot, CreateEditReservationRequest, SearchRequest, SearchResponse,
)


class ServiceError(Exception):
    def __init__(self, status: int, reason: str, message: str, current_version: int | None = None, item_index: int | None = None):
        self.item_index = item_index
        self.status = status
        self.reason = reason
        self.message = message
        self.current_version = current_version
        super().__init__(message)


@dataclass(frozen=True)
class ReplaceCommand:
    reservation_id: str
    replacement: str


class RedliningService(Protocol):
    async def reserve_and_replace(self, client_id: str, document_id: str, request_key: str, request: TargetedReplacementsRequest) -> ReplacementResult | ReplacementAccepted: ...
    async def replacement_operation(self, operation_id: str) -> ReplacementOperation: ...
    async def reserve_batch(self, client_id: str, document_id: str, request: CreateEditReservationsRequest) -> EditReservationsResult: ...
    async def change_batch(self, document_id: str, request: ReservedReplacementsRequest) -> ReplacementResult | ReplacementAccepted: ...
    async def reserve(self, client_id: str, document_id: str, request: CreateEditReservationRequest) -> Reservation: ...
    async def reservation_snapshot(self, document_id: str) -> ReservationSnapshot:
        """Read current version and all protected ranges in one read transaction."""
        ...
    async def reservation(self, reservation_id: str) -> Reservation: ...
    async def renew(self, reservation_id: str) -> Reservation:
        """Renew explicitly; reject stale anchors and protected overlaps."""
        ...
    async def release(self, reservation_id: str) -> None:
        """Logical release only; never deletes dedupe history. Pending work conflicts."""
        ...
    async def change(self, command: ReplaceCommand) -> ReplacementResult | ReplacementAccepted:
        """Await the exact committed revision; return pending at the wait deadline."""
        ...


class RetrievalService(Protocol):
    async def get_document(self, document_id: str, version: int | None) -> Document: ...
    async def search(self, request: SearchRequest) -> SearchResponse:
        """None document_ids = entire corpus; [] = no documents. No history."""
        ...
