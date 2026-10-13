# Document redlining and retrieval

Python/FastAPI + PostgreSQL, with Redis Streams for projections and Redis Pub/Sub
for live document notifications. Reservations are acquired synchronously; only validated, protected replacements
enter the durable queue. No first-arrival fairness or FIFO is required. Replacements commit directly; reviewer
accept/reject workflows are outside this version.

Part II: [Production strategy](PRODUCTION_STRATEGY.md).

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
separate schema provisioning from application startup before using a restricted
runtime role; the supplied API and worker entrypoints currently run initialization.
Multiple API and replacement-worker processes can share PostgreSQL. Use a dedicated
Redis instance: stream/group and Pub/Sub channel names are project-global.
Separate Redis database numbers do not isolate Pub/Sub channels. Redis durability
also depends on persistence configuration. PostgreSQL retains the outbox, but
already-published events are not automatically replayed after Redis data loss;
`python -m redlining.cli republish` schedules retained events for republication.

Prepare the fixtures using [the corpus instructions](data/README.md), or substitute
your own text file below. Import exact UTF-8 text without whitespace normalization:

```sh
.venv/bin/python -m redlining.cli load data/contracts/cuad-0444.txt --id contract-1
.venv/bin/python -m redlining.cli reindex
```

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
   independently deliver outcomes and patches to clients. The UI does not poll;
   API-only callers can use the operation status URL.

Reserve, renew, release, admission and execution all take the same
per-document database lock. This closes the race between overlap validation and
queue insertion. Pending replacements cannot expire or be released/edited.
Infrastructure failures leave their durable work and protection intact; invariant
violations fail safely without applying text. A crash before commit rolls back;
committed outcomes prevent duplicate application. Different documents can proceed
in parallel. Reservations are synchronous; only replacements enter the durable
queue. There is no counter assigning FIFO positions to writes.

Unsubmitted leases last 30 seconds; only explicit renewal extends them. An expired
reservation may renew or submit if current and free. Duplicate exact selection by
the same client reuses its protected reservation without renewal. Replacement text
is immutable per reservation: identical submissions reuse the original operation
and return its pending, committed, or failed outcome;
different text returns REPLACEMENT_MISMATCH, including after completion or failure.

Admission also stores an immutable content identity: document ID, source version,
start/end code-point offsets and SHA-256 of the exact UTF-8 replacement. A duplicate
under a different reservation returns 409 `DUPLICATE_REPLACEMENT` before another
operation enters the queue; one duplicate rejects the entire batch. Original
reservation/request-key retries still retrieve their existing operation. These
identities remain after terminal failure; use the original operation to inspect
the outcome. The same edit on a newer source revision is a distinct operation.

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
| GET | `/documents/{id}?version=1` | Current or immutable revision |
| POST | `/search` | Literal, phrase or terms search, optional document IDs |
| GET | `/documents` | Document discovery |
| WS | `/documents/{id}/updates` | Patches, protected reservations and outcomes |

V0 has no authentication or RBAC. `X-Client-ID` labels reservation ownership; it
is not an authenticated identity. Reserved writes, renewal and release rely on
knowing the reservation ID. Production authorization is proposed in Part II.

Typing is local: select → reserve → type → submit → commit. There is no draft-save
request or typing recovery. While the document is focused, the browser renews its
selected and locally edited reservations every 10 seconds. Renewal revalidates
range protection; failures are shown and submission still validates the reservation.
Hidden/unfocused pages stop renewing; submitted replacements remain protected until
completion. Browser presence heartbeats are separate from lease renewal.

Reservation state comes from the initial WebSocket snapshot and subsequent complete
protected sets. For document-scoped literal search use
`POST /search` with `{"text":"contract","mode":"literal","document_ids":["contract-1"]}`.

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
reservations retain their previous deadlines. Duplicate targets within one request
are rejected with 422; repeating a separate request for the same protected target
with the same client reuses its reservation. Replacement arrays also require
distinct reservation IDs.

Reserved replacements derive client/document/anchor identity from the reservations;
no client header or new idempotency key is needed. The document path is validated.
Replaying the same complete set and texts, in any order, returns the original result.
Changed payloads, partial replays and combining already-bound operations return 409.

### Reserve and replace in one call

Use the same replacement endpoint with targets instead of reservation IDs.
This is an alternative to the preceding example, starting from the original v1:

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
patches are sorted and refer to the same previous version. Stored revisions must
use this current format. The UI assembles text once, shifts selections
by cumulative deltas and waits for subscription outcomes after 202; it never polls
or overwrites a newer subscribed version with an older HTTP response.

`DocumentAnchor` uses half-open Unicode code-point offsets, not UTF-16 or byte
offsets. Insertion points conflict at either boundary of a protected range. Text
assembly costs O(n + total replacement length), plus O(k log k) sorting for k edits.
Overlap checks cost O(k*a + k²) for a other protected reservations; cumulative
anchor shifts use prefix sums and binary search. Document length is read once per
request. Cross-document atomic operations and partial success are unsupported.

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
normal lease behavior. Unsubmitted replacement text stays only in browser memory;
leaving or reloading the page discards it.

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
Indexing commits body, version and ranking vectors together.

Ranking is local to each section: terms dispersed across sections can still match
exactly but receive zero if no section contains all terms. Words of 2 KiB or more
cannot be ranked by PostgreSQL but remain exactly searchable. The section size
and overlap are initial defaults, not tuned against CUAD. Short/common queries
still scan and rank many candidates; candidate bodies stream to Python one at a
time. See [PostgreSQL ranking](https://www.postgresql.org/docs/16/textsearch-controls.html#TEXTSEARCH-RANKING)
and [limits](https://www.postgresql.org/docs/16/textsearch-limitations.html).

Corpus-wide terms/phrase queries have a separate bounded result cache. Defaults:

| Setting | Default | Purpose |
|---|---:|---|
| `QUERY_CACHE_MAX_QUERIES` | 64 | Maximum resident query results; also caps first-use admission records |
| `QUERY_CACHE_BYTES` | 8388608 (8 MiB) | Shared estimated retained-memory budget for results and admission records |
| `QUERY_CACHE_IDLE_SECONDS` | 300 | Expire entries after five minutes without use |

Any zero setting disables result caching. Queries enter after their second use
within the idle window; least-recently-used results are evicted at capacity. Exact
query text, mode, and result limit identify a query. Cached payloads include scores,
contexts, and anchors, not document bodies. Literal and document-scoped searches
bypass caching and use PostgreSQL plus exact source resolution directly.
The per-document postings cache and `SEARCH_CACHE_BYTES` setting have been removed.

A PostgreSQL trigger records inserts, updates (including forced same-version
reindexing), and deletes of search projections in the same transaction. A singleton
generation row orders committed updates; the change log retains only the latest
1,000 changes. This introduces a short shared lock during projection writes, not
source-document editing. Schema initialization installs the trigger; incompatible
older schemas require a fresh schema or a separately implemented migration.

On the next use of a cached query, one repeatable-read snapshot checks the generation
and reconciles up to 100 distinct changed documents. Newly matching documents can
enter the results; changed snippets/anchors refresh even when scores do not change.
A disappearing or demoted top result triggers a full query to refill from uncached
candidates. More than 100 changed documents, or falling behind retained history,
also triggers a full query. Unchanged queries skip ranking and full-body transfer.
Updates are applied lazily on reads, so idle cached queries create no per-write
matching work. Cache and probation records expire lazily on cache access.

Each API store/process owns its cache; all observe the shared committed generation.
A missed Redis notification does not compromise result validity. Local cache work
is serialized, and cold/refill queries can delay concurrent cache access. Byte
budgets are estimates, excluding transient query results and allocator overhead.
These defaults are initial bounds, not capacity claims. The recorded postings-cache
benchmark predates this result cache and does not measure its performance.

Committed outbox events feed Redis Streams. Independent consumer groups index
revisions and publish document invalidations. Gateways fetch current database state
on notification; snapshots never install old reservation lists. One Redis listener
serves each open document per API process. Every socket sends serially and reconciles
every two seconds to cover missed notifications. Initial snapshots contain full
text. Reconnects with `after_version` receive missed patches; gaps over 100 revisions
fall back to full text unless `retain_history=1`. A version ahead of the server also
gets full text. Every state bundle includes the complete protected reservation list.

Replacement history and full revisions are retained indefinitely. V0 sends compact
replacement outcomes on each refresh, so bounded replay/retention is outstanding.
There is no Stream trimming or poison-event quarantine. Flat text assembly costs
O(n+r). For k edits and a other protected anchors, sorting costs O(k log k), anchor
shift lookup costs O(a log(k+1)), and persistence performs a anchor updates.
Execution also checks O(k*a) range pairs; near-linear text assembly is not a
guarantee of near-linear total request latency.
Historical SQLite experiments do not characterize the PostgreSQL application.

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

Historical run before API consolidation: [contract search and redlining report](reports/run-2026-10-12/report.md), including request/commit order, before/after text and browser evidence.

Historical run with native relevance scoring: [updated measurements and before/after comparison](reports/ranked-2026-10-12/report.md).

Measured cache comparison: [disabled vs cold vs warm scoped search](reports/cache-2026-10-12/report.md).
Warm contract searches showed modest gains (terms-tail p50 6.1%, phrase-tail 2.2%);
cold construction more than doubled median latency. The 26.8 MB composite exceeded
the 32 MiB postings budget and fell back. This experimental cache has been removed;
the historical report is retained as evidence for that decision.

Corpus-wide query-result cache: [disabled vs first-use vs warm benchmark](reports/query-cache-2026-10-12/report.md).
Across 41 category queries on all 510 CUAD contracts, warm p50 was 3.84 ms versus
182.36 ms uncached (47.5×); p95 was 4.42 versus 345.75 ms. First-use and admission
p50 remained about 183–184 ms. Incremental maintenance after unrelated additions,
new matches, or same-score anchor changes took about 5–6 ms; removing a winning
match required a full query (about 107 ms in that maintenance workload).
All 516 measured responses matched exactly. These are sequential in-process ASGI
measurements with real PostgreSQL, excluding network latency and index-write time;
maintenance scenarios have only three samples each. They measure the query-result
cache separately from the earlier per-document postings-cache experiment.

Schema initialization creates the current schema only. Existing schemas are not
migrated or backfilled; initialize a fresh database/schema for incompatible changes.
