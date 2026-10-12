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

| Method | Path | PostgreSQL behavior |
|---|---|---|
| POST | `/documents/{id}/edit-reservations` | 200 reservation; target + `X-Client-ID` |
| POST | `/edit-reservations/{id}/renew` | 200 renewed reservation |
| DELETE | `/edit-reservations/{id}` | 204 after release; 409 while replacement pending |
| PUT | `/edit-reservations/{id}/draft` | 200 saved draft; owning `X-Client-ID` required |
| POST | `/documents/{id}/edit-reservation-batches` | Atomically acquire 1–100 ranges; same version and `X-Client-ID` |
| POST | `/documents/{id}/replacement-batches` | Atomically commit 1–100 reserved replacements as one revision |
| POST | `/documents/{id}/replacements` | Reserve + submit 1–100 edits atomically; `X-Client-ID` and `Idempotency-Key` required |
| POST | `/replacements` | 200 committed text/version or 202 pending; reservation_id + replacement |
| GET | `/replacement-operations/{id}` | Pending, completed with exact result, or failed with error |
| GET | `/documents/{id}/edit-reservations` | Complete current protected set |
| GET | `/edit-reservations/{id}` | Reservation state |
| GET | `/documents/{id}?version=1` | Current or immutable requested revision |
| GET | `/documents/{id}/search?q=contract` | Literal search within indexed document |
| POST | `/search` | Literal, phrase or terms search |
| GET | `/documents` | Document discovery for workbench |
| WS | `/documents/{id}/updates` | Text, protected reservations, drafts, replacement outcomes |

Reservation and draft actions return after their transaction commits; state conflicts
are synchronous HTTP errors. POST /replacements returns 200 ChangeResult containing
{document_id, reservation_id, version, text}. The text is the immutable revision
created by this replacement, even if later edits have already committed.

The HTTP handler registers for document notifications before durable admission and
waits asynchronously, without holding a database transaction. Notifications wake a
read of the stored outcome; two-second reconciliation covers missed notifications.
When the completion wait expires, the response is **202 Accepted**, not a failed
write. It includes `Location: /replacement-operations/{id}`, `Retry-After: 2`, and:

```json
{"operation_id":"<id>","status":"pending","status_url":"/replacement-operations/<id>"}
```

`GET` that URL returns HTTP 200 for an existing operation, with one of:

- `{"operation_id":"...","status":"pending","status_url":"..."}`
- `{"operation_id":"...","status":"completed","result":{...}}`
- `{"operation_id":"...","status":"failed","error":{...}}`

`result` is the original committed document response, including text/version and
reservation ID(s), never a newer head. `error` is the stored structured failure.
An unknown operation returns 404. Status responses disable caching; pending reads
also include `Retry-After: 2`. API clients can inspect this URL or repeat their
identical submission. The UI uses WebSocket outcomes without polling or automatic
resubmission. A 202 confirms acceptance, not successful completion.

The default wait is 30 seconds **after admission**. Set `REPLACEMENT_WAIT_SECONDS`
to 0–30 (e.g. 10) to fit below deployment proxy timeouts; zero returns 202 immediately
unless the operation has already completed. Infrastructure timeouts can still
occur, so idempotency remains necessary. Disconnects/202 never cancel accepted work.
Stored failures observed during the POST wait remain structured HTTP errors.

WebSocket operations still report queued/completed/failed, result_version and failure.
The UI keeps its document on that stream rather than overwriting a newer version
with the HTTP response. Client IDs identify sessions, not authenticated principals.

Example after importing a document containing “30 days” at offsets 0–7:

```sh
curl -X POST localhost:8000/documents/contract-1/edit-reservations \
  -H 'Content-Type: application/json' -H 'X-Client-ID: example-client' \
  -d '{"target":{"version":1,"start":0,"end":2}}'
# Copy reservation_id from that response:
curl -X POST localhost:8000/replacements \
  -H 'Content-Type: application/json' \
  -d '{"reservation_id":"RETURNED_ID","replacement":"60"}'
# 200: {"document_id":"contract-1","reservation_id":"RETURNED_ID","version":2,"text":"60 days..."}
```

`DocumentAnchor` uses half-open Unicode code-point offsets, not JavaScript UTF-16
indices. Insertion points conflict at either boundary of another protected range.
Reservation acquisition requires the current version. Concurrent document updates
shift existing protected anchors eagerly. Client awareness reduces stale requests,
but racing and disconnected clients still need conflict handling.

## One-shot API: reserve and replace

API consumers can submit ranges directly. The service creates/reuses reservations
and durably admits the replacements in **one transaction**, then returns the
committed document. It enforces the same reservation protections as the two-step
workflow; it never bypasses another client's selection. No WebSocket is required.

For document `contract-1` at v1 containing `30 days; other clause.`:

```sh
curl -X POST localhost:8000/documents/contract-1/replacements \
  -H 'Content-Type: application/json' \
  -H 'X-Client-ID: integration-client' \
  -H 'Idempotency-Key: contract-1-edit-001' \
  -d '{"replacements":[{"target":{"version":1,"start":0,"end":2},"replacement":"60"}]}'
# 200: {"document_id":"contract-1","reservation_ids":["<server-generated>"],
#       "version":2,"text":"60 days; other clause."}
```

Add more `{target, replacement}` items to the same array for an atomic batch
(maximum 100). All targets use the same current version, are distinct and must
not overlap. A failure rolls back all newly acquired reservations and admission.
Responses include the server-created/reused reservation IDs for inspection.

`Idempotency-Key` is required because a caller may lose the response before it
learns those IDs. The key is scoped to client ID + document ID. Repeat the same
key and payload after a timeout/disconnect to retrieve the original outcome,
even after later revisions. Item order is immaterial. A changed payload with the
same key returns 409 `IDEMPOTENCY_MISMATCH`; a new intended edit uses a new key.
Records are retained indefinitely, like other replacement history. Rejected
admissions do not consume a key. A 202 supplies the status URL for accepted work.

The existing reservation APIs remain available for interactive selection and
protected typing; `/replacements` and `/documents/{id}/replacement-batches` still
accept previously acquired reservation IDs.

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

## Per-document batches

Batch reservation and replacement are all-or-nothing. All ranges/reservations must
belong to the named document. The original single-item endpoints remain supported.

```sh
curl -X POST localhost:8000/documents/contract-1/edit-reservation-batches \
  -H 'Content-Type: application/json' -H 'X-Client-ID: example-client' \
  -d '{"targets":[{"version":1,"start":0,"end":2},{"version":1,"start":9,"end":14}]}'
# Response: {"reservations":[...]} in request order. Use their actual IDs:
curl -X POST localhost:8000/documents/contract-1/replacement-batches \
  -H 'Content-Type: application/json' \
  -d '{"replacements":[{"reservation_id":"FIRST_ID","replacement":"60"},{"reservation_id":"SECOND_ID","replacement":"updated"}]}'
# 200: {"document_id":"contract-1","reservation_ids":[...],"version":2,"text":"..."}
```

The example offsets assume a v1 document containing `30 days; other clause.`.
A reservation batch validates every range against one current version, existing
protection and its own newly reserved ranges, under one document lock. New leases
share a 30-second deadline. Reused reservations keep their original deadlines;
repeated exact targets return the same ID at each corresponding response position.
If any item fails, no reservations or events from the batch commit. Item-specific
errors include zero-based `item_index`; malformed bodies return 422.

Replacement batches require distinct reservation IDs. Admission validates the entire
set and protects it atomically. One durable operation owns multiple replacement
items. Execution sorts ranges, assembles text in one pass and commits exactly one
revision, consumes all reservations and updates other protected anchors by the
combined displacement. Patches share the preceding revision's coordinate system.
WebSocket updates for batches carry `patches: [...]`; single edits retain `patch`.
The UI handles both and submits multiple saved drafts as one atomic batch.

Replaying the same complete batch in any order returns its original result.
Changed payloads, partial replays, or combining previously bound operations produce
409. A batch cannot be replayed through the single-replacement endpoint, or vice
versa. The configured completion wait applies to the whole operation;
202 leaves it running. Inspect its status URL or repeat the exact complete batch.
Infrastructure failure rolls back execution and leaves all items durable; an
invariant failure commits a failed outcome and releases all batch protection.

For k edits, text assembly costs O(n + total replacement length), plus O(k log k)
sorting. Anchor displacement uses prefix sums and binary search; overlap validation
also checks protected ranges (O(k*a + k²), for a other reservations). k is bounded
at 100. Document length is read once per admission/reservation batch, not per item.
Cross-document batches and partial success are deliberately unsupported.

Upgrade with old API/worker processes stopped, and deploy the updated UI together
with the backend: old workers/clients do not understand multi-patch revisions.
Initialization backfills item membership for existing durable single replacements;
queued operations and historical replay remain supported. Legacy SQLite fixture
mode does not implement the new batch endpoints.

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
