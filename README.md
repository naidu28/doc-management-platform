# Document redlining and retrieval

Python/FastAPI + PostgreSQL, with Redis Streams for projections and Redis Pub/Sub
for live document notifications. Reservations are acquired synchronously; only validated, protected replacements
enter the durable queue. No first-arrival fairness or FIFO is required. Replacements commit directly; reviewer
accept/reject workflows are outside this version.

## Run locally

Install PostgreSQL 16+ and Redis, then create a dedicated database:

```sh
createdb sandstone
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
export DATABASE_URL=postgresql://localhost/sandstone
export REDIS_URL=redis://localhost:6379/0
```

Run these in separate terminals with those environment variables:

```sh
redis-server --appendonly yes
.venv/bin/python -m redlining.runtime
.venv/bin/python -m uvicorn redlining.main:build_app --factory --reload
```

The database role needs permission to create the schema and trusted `pg_trgm`
extension. Initialization is serialized across starting processes. For production,
provision the schema separately and apply normal database permissions and backups.
Multiple API and replacement-worker processes can share PostgreSQL. Use a dedicated
Redis instance/database: stream/group names are project-global. Redis durability
also depends on its persistence configuration; PostgreSQL retains the outbox.

Import exact UTF-8 text; this does not normalize whitespace:

```sh
.venv/bin/python -m redlining.cli load data/contracts/cuad-0444.txt --id contract-1
.venv/bin/python -m redlining.cli reindex
```

Existing SQLite files are left untouched. They are not automatically migrated.
`redlining/storage.py`, its tests, and `REDLINING_DB` remain an explicit legacy
fixture mode; do not set `REDLINING_DB` when using the PostgreSQL application.
Historical data and benchmarks are described in [data/README.md](data/README.md).

## Request lifecycle

1. Subscribe to /documents/{id}/updates for text and current protected ranges.
2. Acquire a reservation: a short document-locked transaction returns the reservation
   immediately (200), or rejects a stale/overlapping selection (409).
3. Submit replacement text with the reservation ID. Admission locks the document,
   re-reads the current anchor, validates status/version/range, then atomically
   enqueues the replacement and makes its reservation nonexpiring. The HTTP request
   awaits completion outside that transaction.
4. A worker serializes execution per document, applies any queued replacement,
   eagerly advances other protected anchors, consumes the reservation and commits
   the result plus outbox. No FIFO guarantee is needed for these disjoint edits.
5. HTTP returns 200 with the exact committed revision and full text, or 202 with
   a status URL if still pending at the wait deadline. Subscriptions
   independently deliver outcomes and patches to clients. No client polling.

Reserve, renew, release, draft-save, admission and execution all take the same
per-document database lock. This closes the race between overlap validation and
queue insertion. Pending replacements cannot expire or be released/edited.
Infrastructure failures leave their durable work and protection intact; invariant
violations fail safely without applying text. A crash before commit rolls back;
committed outcomes prevent duplicate application. Different documents can proceed
in parallel, and there is no shared command queue or sequence-counter table.

Unsubmitted leases last 30 seconds; only explicit renewal extends them. An expired
reservation may renew or submit if current and free. Duplicate exact selection by
the same client reuses its protected reservation without renewal. Replacement text
is immutable per reservation: identical submissions return the original committed document result;
different text returns REPLACEMENT_MISMATCH, including after completion or failure.

When upgrading from the shared-FIFO version: drain that queue with the previous
worker, then stop old processes before starting this version. Initialization refuses
pending old commands. Drained commands/counters are archived as legacy_commands and
legacy_document_sequences; replacement outcomes are copied to preserve deduplication.
These archives do not participate in runtime ordering. Existing SQLite data is not migrated.

## API

One item and many items use the same contracts. Collections contain 1–100 items;
all edits in a request belong to one document and commit atomically.

| Method | Path | Contract |
|---|---|---|
| POST | `/documents/{id}/edit-reservations` | `{targets: [...]}` → `{reservations: [...]}`; requires `X-Client-ID` |
| POST | `/documents/{id}/replacements` | `{replacements: [...]}` using reservation IDs **or** targets; 200 committed result or 202 pending |
| GET | `/replacement-operations/{id}` | Pending, completed with exact result, or failed with error |
| POST | `/edit-reservations/{id}/renew` | Explicitly renew one reservation |
| DELETE | `/edit-reservations/{id}` | Release; 409 while submitted |
| PUT | `/edit-reservations/{id}/draft` | Save draft; owning `X-Client-ID` required |
| GET | `/documents/{id}/edit-reservations` | Complete protected reservation snapshot |
| GET | `/edit-reservations/{id}` | Reservation state |
| GET | `/documents/{id}?version=1` | Current or immutable revision |
| GET | `/documents/{id}/search?q=contract` | Literal search within indexed document |
| POST | `/search` | Literal, phrase or terms search, optional document IDs |
| GET | `/documents` | Document discovery |
| WS | `/documents/{id}/updates` | Patches, protected reservations, drafts and outcomes |

### Reserve, then replace

For a v1 document containing `30 days; other clause.`:

```sh
curl -X POST localhost:8000/documents/contract-1/edit-reservations \
  -H 'Content-Type: application/json' -H 'X-Client-ID: example-client' \
  -d '{"targets":[{"version":1,"start":0,"end":2}]}'
# 200: {"reservations":[{"reservation_id":"<id>", ...}]}

curl -X POST localhost:8000/documents/contract-1/replacements \
  -H 'Content-Type: application/json' \
  -d '{"replacements":[{"reservation_id":"<id>","replacement":"60"}]}'
# 200: {"document_id":"contract-1","reservation_ids":["<id>"],"version":2,"text":"60 days; other clause."}
```

Add items to either array for multiple edits; there is no separate batch route.
Acquisition checks all ranges against the current version, existing protection and
each other in one transaction. If any fail, none are acquired. Item-specific errors
include zero-based `item_index`. All newly created leases share a deadline; reused
reservations retain their previous deadlines. Duplicate exact targets return the
same reservation ID in the corresponding response positions. Replacements must use
distinct reservation IDs, so deduplicate such IDs before submitting.

Reserved replacements derive client/document/anchor identity from the reservations;
no client header or new idempotency key is needed. The document path is validated.
Replaying the same complete set and texts, in any order, returns the original result.
Changed payloads, partial replays and combining already-bound operations return 409.

### Reserve and replace in one call

Use the same replacement endpoint with targets instead of reservation IDs:

```sh
curl -X POST localhost:8000/documents/contract-1/replacements \
  -H 'Content-Type: application/json' \
  -H 'X-Client-ID: integration-client' -H 'Idempotency-Key: edit-001' \
  -d '{"replacements":[{"target":{"version":1,"start":0,"end":2},"replacement":"60"}]}'
```

All items must use one mode; mixing targets and reservation IDs is rejected. Every
target must reference the same current version. Target-based requests require both
headers; acquisition and durable admission happen in the same transaction. They
respect existing reservation protection, including for single-item requests.

The key is scoped to document + client. Identical replay (item order immaterial)
returns the original outcome even after later revisions. A changed payload with
the same key returns 409 `IDEMPOTENCY_MISMATCH`. Rejected admission does not consume
the key. Keys and outcomes are retained indefinitely. A new intended edit needs a
new key. No WebSocket is required for API-only callers.

### Completion and subscriptions

Every successful replacement returns the same shape, with `reservation_ids` as an
array and one new version regardless of item count. Admission protects all items;
a worker sorts ranges, assembles the document once and commits them together.
An invariant failure applies none and releases all operation protection; a crash
rolls back execution and leaves durable work available for recovery.

The HTTP request waits outside a transaction for completion. **200** includes the
exact committed text/version, never a newer head. When the wait ends, **202** means
accepted and still pending, with `Location`, `Retry-After: 2` and this body:

```json
{"operation_id":"<id>","status":"pending","status_url":"/replacement-operations/<id>"}
```

GET the status URL returns 200 with `status: pending`, `status: completed` plus
`result` (the same committed response shape), or `status: failed` plus structured
`error`. Unknown operations return 404. Status reads disable caching; pending reads
include `Retry-After`. Clients can also repeat the identical submission safely.
A 202 or disconnected request never cancels admitted work. Stored failures observed
during the POST wait remain structured HTTP errors.

The default completion wait is 30 seconds after admission; set
`REPLACEMENT_WAIT_SECONDS=0..30` below deployment proxy timeouts. Zero returns 202
unless already completed. Idempotency is still needed for infrastructure timeouts.

Every new WebSocket revision uses `patches: [...]`, including a single edit. All
patches are sorted and refer to the same previous version. The gateway normalizes
historical single patches on read. The UI assembles text once, shifts selections
by cumulative deltas and waits for subscription outcomes after 202; it never polls
or overwrites a newer subscribed version with an older HTTP response.

`DocumentAnchor` uses half-open Unicode code-point offsets, not UTF-16 or byte
offsets. Insertion points conflict at either boundary of a protected range. Text
assembly costs O(n + total replacement length), plus O(k log k) sorting for k edits.
Overlap checks cost O(k*a + k²) for a other protected reservations; cumulative
anchor shifts use prefix sums and binary search. Document length is read once per
request. Cross-document atomic operations and partial success are unsupported.

### Contract upgrade

The old root replacement route and separate batch routes are removed, not aliases.
Update API callers and deploy backend workers/UI together. Existing reservation IDs,
operation IDs and one-shot keys remain valid. Old singleton operations replay through
the new array contract with the same outcome; legacy revision patches are normalized.
PostgreSQL uses one operation/items execution path regardless of item count. Retained
leading-item database columns support historical migration, not a unary execution mode.
The explicit SQLite fixture adapter supports only single-item arrays; PostgreSQL is
the application backend for full operation sets.

## Browser departure

Browser document subscriptions identify their client and send a heartbeat every
five seconds. Leaving a document or page closes the socket with `page-left`; when
the last connection for that client/document leaves, unsubmitted reservations are
released. Submitted single/batch replacements remain protected and finish normally.

Unexpected disconnects allow five seconds to reconnect. Missing heartbeats expire
a connection after 20 seconds; the durable worker cleans it up, including after
an API-process crash. PostgreSQL tracks connections across API processes and checks
remaining connections under the document lock before releasing ranges. Cleanup
emits normal reservation snapshots. It does not renew the 30-second edit lease.
Browser suspension can also lose presence; reconnect and reselect if protection
was released. Undelivered close messages fall back to heartbeat/lease expiry.
An in-flight acquisition arriving after departure cleanup still has the ordinary
30-second lease bound. API-only clients do not register browser presence and keep
normal lease behavior. Draft text/history is retained, but released drafts no
longer hold ranges or appear as active edits.

## Search and subscriptions

One PostgreSQL projection row stores each indexed document's exact body and version.
A `pg_trgm` GIN index filters LIKE/ILIKE candidates. Python resolves exact source
locations: literal substring matches, Unicode lexical phrases, or AND terms. Query
operators are not accepted as executable syntax. A hit always carries its indexed
version, which can lag the authoritative document. A version-guarded upsert prevents
old indexing work from replacing a newer projection. CLI reindex repairs projections.

Terms and phrase searches rank by PostgreSQL's built-in `ts_rank_cd` (cover
density), descending, with document ID as a deterministic tie-breaker. `score`
is a document's strongest section score, normalized with flag 32 to [0,1); it
is not a probability or BM25. Literal find-in-document stays in source order
with `score: null`. Exact Unicode source checks still determine which hits qualify.

Each projection stores an array of bounded `simple`-configuration vectors: 256
words with 32-word overlap. This covers the whole document, including the tail
of 10+ MB inputs, without exceeding PostgreSQL's vector/position limits. There
are no passage IDs or new services. No stemming or stopword removal is applied.
Existing rows are backfilled once during initialization under the schema lock;
future indexing commits body, version and ranking vectors together. On a large
existing corpus, allow time for that initial backfill before serving traffic.

Ranking is local to each section: terms dispersed across sections can still match
exactly but receive zero if no section contains all terms. Words of 2 KiB or more
cannot be ranked by PostgreSQL but remain exactly searchable. The section size
and overlap are initial defaults, not tuned against CUAD. Short/common queries
still scan and rank many candidates; candidate bodies stream to Python one at a
time. See [PostgreSQL ranking](https://www.postgresql.org/docs/16/textsearch-controls.html#TEXTSEARCH-RANKING)
and [limits](https://www.postgresql.org/docs/16/textsearch-limitations.html).

Committed outbox events feed Redis Streams. Independent consumer groups index
revisions and publish document invalidations. Gateways fetch current database state
on notification; snapshots never install old reservation lists. One Redis listener
serves each open document per API process. Every socket sends serially and reconciles
every two seconds to cover missed notifications. Initial/reconnect snapshots contain
full text; normal updates contain patches and complete protected reservation lists.

Replacement history and full revisions are retained indefinitely. V0 sends compact
replacement outcomes on each refresh, so bounded replay/retention is outstanding.
There is no Stream trimming, poison-event quarantine, or automated legacy database
migration. Flat replacement costs O(n+r); protected-anchor updates cost O(a).
Previous SQLite benchmark results do not characterize the PostgreSQL adapter.

## Tests and UI

```sh
export REDLINING_TEST_DATABASE_URL=postgresql://localhost/sandstone_test
.venv/bin/python -m pytest -q
npm --prefix frontend test
npm --prefix frontend run build
```

Create that dedicated test database first. PostgreSQL tests create and drop only
unique test schemas. They verify atomic admission, multi-process races/execution,
crash rollback/recovery, document independence, lease rules, subscriptions, and
source offsets on a document larger than 10 MB. Without the test DSN they skip.
Redis integration tests launch an isolated loopback Redis server.

See [frontend/README.md](frontend/README.md) for the workbench and demo.
The exact policy is maintained in [design.py](design.py).

## Reproducible end-to-end benchmarks

See [the testing protocol](benchmarks/README.md) for automatic CUAD setup, HTTP search and reservation/replacement suites, ten concurrent Selenium clients, and report generation. The harness compares current search against a local FTS5/BM25 baseline without LLMs.

Measured run: [contract search and redlining report](reports/run-2026-10-12/report.md), including request/commit order, before/after text and browser evidence.

With native relevance scoring: [updated measurements and before/after comparison](reports/ranked-2026-10-12/report.md).
