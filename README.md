# Document redlining and retrieval

Search contract text, reserve passages while editing, and apply one or more
replacements as a single document revision. The service uses Python/FastAPI,
PostgreSQL, and Redis. Submitted edits commit directly; there is no reviewer
accept/reject step.

## Start the service

Prerequisites: Python 3.11+, PostgreSQL 16+, and Redis. From the repository root:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
createdb sandstone
export DATABASE_URL=postgresql://localhost/sandstone
export REDIS_URL=redis://localhost:6379/0
```

Run each command in a separate terminal with those environment variables:

```sh
redis-server --appendonly yes
.venv/bin/python -m redlining.runtime
.venv/bin/python -m uvicorn redlining.main:build_app --factory --reload
```

Keep the worker running: it completes replacements and updates search results.
Use a dedicated Redis instance. The database role must be able to initialize the
schema and `pg_trgm` extension. Incompatible older schemas require a fresh database;
automatic migrations are not provided.

Open [interactive API documentation](http://localhost:8000/docs) for request and
response schemas. For the browser workbench, follow [frontend setup](frontend/README.md).
V0 has no authentication or RBAC; `X-Client-ID` labels a client session, not an
authenticated user.

## Load and read a document

Import a UTF-8 text file through the CLI. This example supplies the document used below:

```sh
printf '30 days; other clause.' > /tmp/sandstone-example.txt
.venv/bin/python -m redlining.cli load /tmp/sandstone-example.txt --id contract-1
.venv/bin/python -m redlining.cli reindex

curl -s localhost:8000/documents
curl -s localhost:8000/documents/contract-1
```

The document response is:

```json
{"document_id":"contract-1","version":1,"text":"30 days; other clause."}
```

Use a new ID for each imported document. Text is preserved without whitespace
normalization. `GET /documents` lists IDs, titles, and current versions;
`GET /documents/contract-1?version=1` retrieves an immutable earlier revision.
For larger examples, use [the public contract corpus](data/README.md).

## Search

Search one document by supplying `document_ids`; omit it to search the whole corpus:

```sh
curl -s -X POST localhost:8000/search \
  -H 'Content-Type: application/json' \
  -d '{"text":"30 days","mode":"literal","document_ids":["contract-1"],"limit":10}'
```

```json
{
  "hits": [{
    "document_id": "contract-1",
    "target": {"version": 1, "start": 0, "end": 7},
    "context": "30 days; other clause.",
    "score": null
  }],
  "consistency": "eventual"
}
```

| Mode | Behavior |
|---|---|
| `literal` | Case-sensitive substring search; matches within each document appear in source order, with `score: null`. |
| `phrase` | Case-insensitive consecutive words, allowing punctuation/whitespace separators; one hit per matching document. |
| `terms` (default) | Case-insensitive words that must all occur in the document, in any order; one hit per matching document. |

Terms and phrase results rank by relevance, highest first. Scores are not confidence
values, and valid matches can score zero. A terms hit anchors the earliest matching
query word, not every occurrence. `limit` accepts 1–100 and defaults to 20.
An empty `document_ids` array returns no hits. There is no semantic search or LLM.

Search updates asynchronously after edits. A hit's `target.version` can therefore
be older than the current document. Fetch that version to view the exact match;
refresh/search again before reserving it if the document has advanced.

## Edit: reserve, then replace

Use this flow when someone selects a passage before typing. For the example's v1,
reserve the characters `30`:

```sh
curl -s -X POST localhost:8000/documents/contract-1/edit-reservations \
  -H 'Content-Type: application/json' -H 'X-Client-ID: example-client' \
  -d '{"targets":[{"version":1,"start":0,"end":2}]}'
```

The response contains `reservations: [...]`. Copy its first `reservation_id` into
the next request, replacing `<id>`:

```sh
curl -s -X POST localhost:8000/documents/contract-1/replacements \
  -H 'Content-Type: application/json' \
  -d '{"replacements":[{"reservation_id":"<id>","replacement":"60"}]}'
```

On completion, HTTP **200** returns the exact revision produced by your edits:

```json
{"document_id":"contract-1","reservation_ids":["<id>"],"version":2,"text":"60 days; other clause."}
```

- **Offsets.** Ranges are half-open Unicode code-point offsets: `start` is included,
  `end` is excluded. They are not UTF-8 bytes or JavaScript UTF-16 indices. Use
  `start == end` to insert, or an empty replacement to delete.
- **Protection.** A reservation lasts 30 seconds. Overlapping selections return 409;
  insertion points conflict at either boundary of a protected range. Disjoint edits
  can proceed concurrently, and protected anchors move as other edits commit.
- **Renew or release.** `POST /edit-reservations/<id>/renew` extends an eligible lease;
  `DELETE /edit-reservations/<id>` releases it and returns 204. Repeating the same
  selection with the same client reuses its protected reservation without renewing it.
  An expired reservation can renew or submit only if its anchor is current and free.
- **Local typing.** Typing does not save a server draft. Once submitted, replacement
  text cannot be changed, and its reservation cannot expire, renew, or release until
  processing finishes. Use a new reservation for a subsequent edit.

Reserved replacement, renewal, and release requests use the reservation ID;
they do not require a new client header or idempotency key.

## Edit in one request

For API integrations that already know the edit, send targets directly to the same
replacement endpoint. This is an alternative to the preceding example, starting at v1:

```sh
curl -s -X POST localhost:8000/documents/contract-1/replacements \
  -H 'Content-Type: application/json' \
  -H 'X-Client-ID: integration-client' -H 'Idempotency-Key: edit-001' \
  -d '{"replacements":[{"target":{"version":1,"start":0,"end":2},"replacement":"60"}]}'
```

Both headers are required. The request acquires protection and submits the edit
together, respecting existing reservations. Reuse the same key and payload when
retrying; use a new key for a new intended operation. Keys are scoped to document
and client and retained indefinitely. No WebSocket is required.

## Multiple edits

Add 1–100 items to `targets` or `replacements`; single and multiple edits use the
same endpoints. Each request belongs to one document. All target anchors must
reference the same current version and must not overlap.

Reservations are acquired together or not at all. Replacements commit together
as **one new revision**, with no partial success. Use either reservation IDs or
targets throughout a replacement request, never a mixture. Duplicate targets or
reservation IDs within an array return 422.

## Pending work, retries, and errors

Replacement requests wait up to 30 seconds after acceptance. HTTP **202** means
work remains accepted and pending, not that the write failed:

```json
{"operation_id":"<id>","status":"pending","status_url":"/replacement-operations/<id>"}
```

Follow `status_url` (also returned in `Location`), respecting `Retry-After: 2`.
The status endpoint returns HTTP 200 with one of:

- `status: "pending"` and the status URL.
- `status: "completed"` and `result`, containing the original committed text/version.
- `status: "failed"` and a structured `error`.

A disconnected request or 202 does not cancel accepted work. Retry the identical
submission to recover its outcome; do not create a new operation blindly. Reserved
retries must contain the complete original set of IDs and texts; item order does
not matter. A replay returns the original revision even if later edits have committed.
Do not overwrite newer subscribed document state with that older result.

| Response | What to do |
|---|---|
| 404 | Check the document, reservation, revision, or operation ID. |
| 409 `STALE_VERSION` | Fetch current text and choose the range again. |
| 409 `RANGE_RESERVED` | Wait for protection to end or choose another range. |
| 409 `REPLACEMENT_MISMATCH` / `IDEMPOTENCY_MISMATCH` | Restore the original retry payload; changed edits require a new operation. |
| 409 `DUPLICATE_REPLACEMENT` | The same source-version range and replacement were already accepted under another reservation; retrieve the original operation. |
| 422 | Correct invalid ranges, duplicate items, missing headers, or malformed input. |

Errors contain `error`, HTTP `code`, and a machine-readable `reason`; some include
`current_version` or a zero-based `item_index`. Stored processing failures encountered
while waiting are returned as HTTP errors. Set `REPLACEMENT_WAIT_SECONDS` between
0 and 30 to keep the completion wait below your proxy timeout.

## Subscribe to live updates

Connect to `ws://localhost:8000/documents/contract-1/updates`. The first
`document_state` message includes the full `document`, a `reservations` snapshot,
and compact `operations` outcomes. Later messages contain `updates` with versioned
`patches`, plus the complete current protected set and operation outcomes.

Apply each update only to its `previous_version`. All patches in that update use
the same original coordinates; apply them together or from right to left. Replace
the protected reservation list with each received snapshot. Disjoint writes have
no promised completion order.

Reconnect with `?after_version=2` to request missed patches. Gaps over 100 revisions
normally return full text; add `retain_history=1` when retaining an outstanding
selection requires the patch history. Live updates reduce stale-version conflicts,
but clients must still handle 409s from races or lost connections.

For browser presence, also supply `client_id`, send `{"type":"heartbeat"}` every
five seconds, and close with reason `page-left` on departure. When the last connection
for that client/document leaves, unsubmitted reservations are released. Unexpected
disconnects have a five-second reconnect grace; missing heartbeats expire after
20 seconds. Presence does not renew edit leases. API-only callers can omit presence.

The supplied UI handles these subscriptions and renews local edit reservations every
10 seconds while focused and visible. Leaving or reloading discards unsubmitted
text; submitted work continues. The UI receives completion through subscriptions
rather than polling the operation endpoint.

## Tests and further reading

```sh
createdb sandstone_test
REDLINING_TEST_DATABASE_URL=postgresql://localhost/sandstone_test .venv/bin/python -m pytest -q
npm --prefix frontend ci
npm --prefix frontend test
npm --prefix frontend run build
```

Database tests use isolated schemas and skip without the test DSN. Browser tests
are opt-in; see the testing protocol.

- [API lifecycle and design](design.py)
- [Part II: production strategy](PRODUCTION_STRATEGY.md)
- [Corpus setup](data/README.md) and [benchmark/testing protocol](benchmarks/README.md)
- [Current end-to-end benchmark report](reports/current-2026-10-12/report.md)

- [Current query-cache benchmark](reports/query-cache-current-2026-10-12/report.md)
- [PostgreSQL/Redis verification](reports/verification-current-2026-10-12/pytest.txt): 105 passed, one opt-in browser test skipped.

The current end-to-end run disables query-result caching and verifies ten browser
clients converge on v11. Other verification processes shared the host during the
run, so latency measurements are smoke evidence rather than isolated baselines.
