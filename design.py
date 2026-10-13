"""Redlining policy: synchronous reservations, durable conflict-free replacements.

SCOPE
PostgreSQL is authoritative. Replacement submission commits directly when processed;
no reviewer accept/reject workflow, RBAC or first-arrival fairness.
One to 100 edits within one document commit atomically as one revision.
UI subscriptions expose protected ranges/owners. Races are settled by atomic checks.
The application uses PostgreSQL exclusively.

RESERVATIONS: SHORT SYNCHRONOUS TRANSACTIONS
Acquire, renew and release lock the document row (FOR NO KEY UPDATE),
read current state AFTER acquiring that lock, validate, mutate and insert outbox.
HTTP returns the result after commit (200, or 204 for release); conflicts return 409.
Reservations do not enter the replacement queue. No counter assigns FIFO
positions to writes, and there is no first-arrival ordering guarantee.

Acquisition requires the current version and valid half-open Unicode code-point
bounds. Same client/exact protected range returns the existing reservation without
renewing it; other overlap rejects. New selections get 30 seconds from grant time
using the database clock. Only explicit renewal extends an unsubmitted lease.
Typing stays in browser memory until submission; no server draft storage or recovery.
The focused workbench explicitly renews selected and locally edited reservations
every 10 seconds; hidden/unfocused pages do not renew. Renewal revalidates eligibility.
Expiry is a time predicate, reflected in subscription snapshots.

REPLACEMENT ADMISSION: TRANSACTION 1
Lock the document and re-read the reservation (its anchor may have moved while
waiting). Check for an existing replacement first: the identical complete set of
reservation IDs and texts returns the stored operation ID regardless of outcome.
Changed text fails REPLACEMENT_MISMATCH; partial or combined operation replays fail.
Otherwise require eligible status, current version, valid bounds, and no overlap
with other protected reservations. An expired reservation may submit if still
current and free. Released/consumed/failed reservations require new acquisition.
Atomically insert into replacements, set reservation submitted with expires=NULL,
bind operation ID and write outbox. Commit admission, then await execution outside the transaction.

Admission also records document ID, source version, range and replacement SHA-256.
A duplicate identity under a different reservation rejects the entire batch with
DUPLICATE_REPLACEMENT. Identities remain after terminal failure.

Submitted reservations protect their ranges until terminal outcome. They cannot
expire or be released/renewed. No conflicting replacements
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
with document_id, reservation_ids, version and full text. Read the immutable revision
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
or patches plus the full protected reservation list and compact operations.
Two-second server reconciliation covers missed notifications; the UI does not poll.
API-only consumers can read the operation status URL after a 202.
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
score zero; literal matches keep source order and null scores. Ranking vectors
are created during indexing. Short/common queries can still scan many large candidates.
Document-scoped and literal searches resolve directly without application caching.
Corpus terms/phrase results are cached after two uses, bounded by 64 queries,
8 MiB including admission records, and 300 seconds idle expiry (configurable).
A transactional projection trigger maintains a generation and the last 1000
projection-change records (document IDs can repeat). Reads reconcile up to 100 changed documents in the same snapshot;
demoted/deleted winners or excess lag trigger a full refill. Missed notifications
do not affect correctness; a shared generation-row lock serializes projection commits.
For k edits and a other protected anchors: text assembly O(n+r), sorting
O(k log k), anchor-shift lookup O(a log(k+1)) plus a database updates. Defensive
overlap checking costs O(k*a); these costs are separate from text assembly.

SCHEMA
Initialization creates the current schema only; no migrations or backfills.
"""
from src.models import ReplacementResult, DocumentAnchor, DocumentUpdated, ReservationSnapshot, SearchRequest
from src.services import ReplaceCommand


# Per-document batching extension
# Reservation batches acquire all targets in one transaction, or none. New leases
# share a deadline; exact same-client targets reuse existing IDs without renewal
# across requests. Duplicate targets within one array are rejected with 422.
# Replacement batches bind distinct reservations to one durable operation in
# replacement_items. A batch produces one immutable revision containing sorted
# patches in the preceding revision's coordinates. Text is assembled once;
# protected anchors advance by cumulative deltas. All item protection is consumed
# on success or released on terminal failure; crashes roll back for recovery.
# Complete identical batches replay regardless of item order. Partial/changed
# replay and cross-document membership fail. Single-item arrays use the same execution path.

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
# has patches[]. Retired root/batch endpoints are removed. No automatic migration
# of old schemas or scalar-patch revision payloads is provided.
