from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

Identifier = Annotated[str, Field(min_length=1, max_length=200)]
Version = Annotated[int, Field(strict=True, ge=1)]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DocumentAnchor(Model):
    """Half-open Unicode code-point offsets in an immutable revision."""

    version: Version
    start: int = Field(strict=True, ge=0)
    end: int = Field(strict=True, ge=0)

    @model_validator(mode="after")
    def valid_range(self):
        if self.end < self.start:
            raise ValueError("end must be greater than or equal to start")
        return self


class CreateEditReservationRequest(Model):
    target: DocumentAnchor


class ApplyDocumentChangeRequest(Model):
    reservation_id: Identifier
    replacement: str


class Reservation(Model):
    reservation_id: Identifier
    document_id: Identifier
    client_id: Identifier
    target: DocumentAnchor
    status: Literal["active", "submitted", "expired", "released", "consumed", "conflicted", "failed"]
    created_at: AwareDatetime
    expires_at: AwareDatetime | None = None
    blocker_client_ids: list[Identifier] = Field(default_factory=list)
    operation_id: Identifier | None = None


class ReservationSnapshot(Model):
    """Complete protected set at one database read snapshot; no event sequence."""
    document_id: Identifier
    document_version: Version
    reservations: list[Reservation]

    @model_validator(mode="after")
    def aligned(self):
        for reservation in self.reservations:
            if (reservation.document_id != self.document_id
                    or reservation.target.version != self.document_version
                    or reservation.status not in ("active", "submitted")):
                raise ValueError("snapshot must contain aligned protected reservations")
        return self


class Document(Model):
    document_id: Identifier
    version: Version
    text: str


class Error(Model):
    error: str
    code: int  # HTTP status, compatible with the brief.
    reason: str
    current_version: Version | None = None
    item_index: int | None = None


class ChangeResult(Document):
    """The immutable document revision produced by this replacement."""
    reservation_id: Identifier


class OperationStatus(Model):
    """Compact live outcome; document content travels once in the state bundle."""
    operation_id: Identifier
    reservation_id: Identifier
    status: Literal["queued", "running", "completed", "failed"]
    result_version: Version | None = None
    failure: Error | None = None


class PendingOperation(Model):
    operation_id: Identifier
    reservation_id: Identifier
    status: Literal["queued", "running"]


class CompletedOperation(Model):
    operation_id: Identifier
    reservation_id: Identifier
    status: Literal["completed"] = "completed"
    document: Document


class FailedOperation(Model):
    operation_id: Identifier
    reservation_id: Identifier
    status: Literal["failed"] = "failed"
    failure: Error


Operation = Annotated[
    PendingOperation | CompletedOperation | FailedOperation,
    Field(discriminator="status"),
]


class SearchRequest(Model):
    text: str = Field(min_length=1, max_length=4096)
    mode: Literal["literal", "phrase", "terms"] = "terms"
    document_ids: list[Identifier] | None = Field(default=None, max_length=100)
    limit: int = Field(default=20, strict=True, ge=1, le=100)

    @model_validator(mode="after")
    def nonblank_query(self):
        if not self.text.strip():
            raise ValueError("query must contain non-whitespace text")
        return self


class SearchHit(Model):
    document_id: Identifier
    target: DocumentAnchor  # Version is the indexed version, not necessarily latest.
    context: str
    score: float | None = Field(default=None, description=(
        "PostgreSQL terms/phrase relevance: higher is better, normalized to [0,1). "
        "Document-level best-section score, not confidence. Literal searches return null."
    ))


class SearchResponse(Model):
    hits: list[SearchHit]
    consistency: Literal["eventual"] = "eventual"


class DocumentPatch(Model):
    """Offsets refer to the event's previous_version."""
    start: int = Field(strict=True, ge=0)
    end: int = Field(strict=True, ge=0)
    replacement: str

    @model_validator(mode="after")
    def valid_range(self):
        if self.end < self.start:
            raise ValueError("end must be greater than or equal to start")
        return self


class DocumentUpdated(Model):
    """Committed patch payload; live state bundles contain patches and reservations."""
    event_id: Identifier
    document_id: Identifier
    previous_version: Version
    version: Version
    patch: DocumentPatch

    @model_validator(mode="after")
    def next_version(self):
        if self.version != self.previous_version + 1:
            raise ValueError("update must advance exactly one revision")
        return self


class CreateEditReservationBatchRequest(Model):
    targets: list[DocumentAnchor] = Field(min_length=1, max_length=100)

    @model_validator(mode='after')
    def same_version(self):
        if len({target.version for target in self.targets}) != 1:
            raise ValueError('All targets must reference the same document version')
        return self


class EditReservationBatchResult(Model):
    reservations: list[Reservation]


class ApplyReplacementBatchRequest(Model):
    replacements: list[ApplyDocumentChangeRequest] = Field(min_length=1, max_length=100)

    @model_validator(mode='after')
    def unique_reservations(self):
        ids = [item.reservation_id for item in self.replacements]
        if len(set(ids)) != len(ids):
            raise ValueError('Each reservation may appear only once')
        return self


class ReplacementBatchResult(Document):
    reservation_ids: list[Identifier]


class DocumentBatchUpdated(Model):
    """Sorted nonoverlapping patches, all relative to previous_version."""
    event_id: Identifier
    document_id: Identifier
    previous_version: Version
    version: Version
    patches: list[DocumentPatch] = Field(min_length=1, max_length=100)


class ReservedReplacement(Model):
    target: DocumentAnchor
    replacement: str


class ReserveAndReplaceRequest(Model):
    replacements: list[ReservedReplacement] = Field(min_length=1, max_length=100)

    @model_validator(mode='after')
    def coherent_targets(self):
        targets = [(item.target.version,item.target.start,item.target.end) for item in self.replacements]
        if len({target[0] for target in targets}) != 1:
            raise ValueError('All targets must reference the same document version')
        if len(set(targets)) != len(targets):
            raise ValueError('Each target must appear only once')
        return self


class ReplacementAccepted(Model):
    operation_id: Identifier
    status: Literal['pending'] = 'pending'
    status_url: str


class ReplacementCompleted(Model):
    operation_id: Identifier
    status: Literal['completed'] = 'completed'
    result: ChangeResult | ReplacementBatchResult


class ReplacementFailed(Model):
    operation_id: Identifier
    status: Literal['failed'] = 'failed'
    error: Error


ReplacementOperation = Annotated[
    ReplacementAccepted | ReplacementCompleted | ReplacementFailed,
    Field(discriminator='status'),
]
