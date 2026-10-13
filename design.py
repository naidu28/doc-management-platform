"""Redlining policy: synchronous reservations, durable conflict-free replacements.

SCOPE
PostgreSQL is authoritative. Replacement submission commits directly when processed;
no reviewer accept/reject workflow, RBAC, first-arrival fairness or batch transaction.
UI subscriptions expose protected ranges/owners. Races are settled by atomic checks.
The old SQLite adapter is retained only for explicit legacy fixtures/tests.

RESERVATIONS: SHORT SYNCHRONOUS TRANSACTIONS
Acquire, renew, release and draft-save lock the document row (FOR NO KEY UPDATE),
read current state AFTER acquiring that lock, validate, mutate and insert outbox.
HTTP returns the result after commit (200, or 204 for release); conflicts return 409.
There is no shared command queue, command-position counter, or FIFO guarantee.

Acquisition requires the current version and valid half-open Unicode code-point
bounds. Same client/exact protected range returns the existing reservation without
renewing it; other overlap rejects. New selections get 30 seconds from grant time
using the database clock. Only explicit renewal extends an unsubmitted lease.
Draft saving requires ownership and an unexpired eligible reservation; it never
extends protection. Expiry is a time predicate, reflected in subscription snapshots.

REPLACEMENT ADMISSION: TRANSACTION 1
Lock the document and re-read the reservation (its anchor may have moved while
waiting). Check for an existing replacement first: same text returns the stored
operation ID regardless of outcome, different text fails REPLACEMENT_MISMATCH.
Otherwise require eligible status, current version, valid bounds, and no overlap
with other protected reservations. An expired reservation may submit if still
current and free. Released/consumed/failed reservations require new acquisition.
Atomically insert into replacements, set reservation submitted with expires=NULL,
bind operation ID and write outbox. Commit admission, then await execution outside the transaction.

Submitted reservations protect their ranges until terminal outcome. They cannot
expire, be released/renewed, or have draft text changed. No conflicting replacements
enter the queue through the supported API. Failed admission inserts no queue row.

EXECUTION: TRANSACTION 2
Workers choose a document with queued replacements, lock that document and pick
any queued replacement. No promised order among replacements. Other documents can
execute concurrently. Recheck protection invariants defensively; a violation is
an internal QUEUE_INVARIANT failure, not an ordinary late business conflict.

Flat-string replacement writes an immutable revision and patch, increments version,
eagerly moves ALL other protected anchors (including submitted reservations), and
consumes the applied reservation. Record completed outcome and outbox atomically.
Disjoint replacements can execute in either order because their current anchors
move after each commit. Insertion points conflict at both range boundaries.

An invariant failure rolls back partial changes via savepoint, records a terminal
failure and releases reservation protection in the same transaction. An unexpected
infrastructure failure/process crash rolls back execution and leaves queued work
and its protection intact. Recovery executes uncommitted work, never a second copy
of a committed replacement. Same-text resubmission returns its original identity.

SUBSCRIPTIONS / PROJECTIONS
POST /documents/{id}/replacements awaits execution, returning 200 ReplacementResult
with document_id, reservation_id, version and full text. Read the immutable revision
recorded in the operation outcome, never the latest head. Same-text resubmission
returns that original result even after subsequent edits. Stored failure becomes
an HTTP error. The handler attaches to the document's LiveHub before admission,
then reads the durable outcome after notifications; it holds no database transaction
while waiting. The event loop remains free to handle other requests.

Two-second server reconciliation recovers lost notifications or Redis outages.
After the completion wait (default 30s, REPLACEMENT_WAIT_SECONDS=0..30), return
202 Accepted with operation_id/status_url and Location/Retry-After headers.
GET /replacement-operations/{id} returns pending, completed with the original
result, or failed with a stored error. This is not cancellation; callers can also
repeat the identical submission to retrieve its outcome. Request cancellation
also leaves accepted work intact. WebSockets independently keep all clients current;
the UI does not overwrite newer subscribed state with an older HTTP result.

Outbox -> Redis Streams -> search-index and live-delivery consumer groups.
Pub/Sub is an invalidation; gateways read fresh PostgreSQL snapshots and send text
or patches plus the full protected reservation list, drafts and compact operations.
Two-second server reconciliation covers missed notifications; no client polling.
V0 retains full revisions/outcomes forever and sends all compact replacement
outcomes on refresh. Bounded replay/retention remains future work.

RETRIEVAL
One PostgreSQL indexed projection per document. pg_trgm filters LIKE/ILIKE
candidates; Python resolves literal/Unicode lexical phrase/AND-term source offsets.
Indexed-version anchors can lag the current document. Version-guarded upserts
prevent index rollback. Terms/phrase results use native ts_rank_cd, descending,
with document-ID ties. Each projection contains bounded simple-configuration
vectors (256 words, 32 overlap); the document score is the maximum section rank
(normalization 32). This avoids vector limits on huge documents without changing
anchors or creating passage APIs. No BM25 or stemming. Dispersed matches may
score zero; literal matches keep source order and null scores. Existing rows
backfill on startup. Short/common queries can still scan many large candidates.
Flat replacement is O(n+r), protected-anchor updates O(a).

UPGRADING THE PREVIOUS POSTGRES SCHEMA
Drain the previous shared queue using the previous application before upgrading;
stop old processes before starting this version. Initialization refuses pending
old commands rather than dropping accepted work. A drained queue and its counter
are archived as legacy_commands/legacy_document_sequences; historical replacement
outcomes are copied into replacements to preserve deduplication. The archived tables
are not used for admission or scheduling. Fresh databases create neither old table.
"""
from redlining.models import ReplacementResult, DocumentAnchor, DocumentUpdated, ReservationSnapshot, SearchRequest
from redlining.services import ReplaceCommand


# Per-document batching extension
# Reservation batches acquire all targets in one transaction, or none. New leases
# share a deadline; exact same-client targets reuse existing IDs without renewal.
# Replacement batches bind distinct reservations to one durable operation in
# replacement_items. A batch produces one immutable revision containing sorted
# patches in the preceding revision's coordinates. Text is assembled once;
# protected anchors advance by cumulative deltas. All item protection is consumed
# on success or released on terminal failure; crashes roll back for recovery.
# Complete identical batches replay regardless of item order. Partial/changed
# replay and cross-document membership fail. Single-item arrays use the same execution path.
# Old workers and clients must be stopped/upgraded before enabling batch writes.

# One-shot POST /documents/{id}/replacements accepts versioned ranges + text.
# Required Idempotency-Key is scoped to document/client and stored atomically with
# reservation acquisition and durable admission; complete replay returns the
# original revision even after newer edits. The same replacement endpoint accepts reserved arrays or target-based arrays.
# Browser presence uses shared PG connection rows: explicit leave releases only
# unsubmitted reservations after the last connection leaves; reconnect grace 5s,
# heartbeat TTL 20s. Worker cleanup survives API crashes. API-only leases unchanged.

# Unified contracts: POST document edit-reservations accepts targets[] and returns
# reservations[]. POST document replacements accepts homogeneous reserved or
# targeted replacements[]; every result has reservation_ids[] and every update
# has patches[]. Retired root/batch endpoints are removed; historic state survives.
