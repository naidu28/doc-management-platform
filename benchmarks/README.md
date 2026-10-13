# Reproducible contract search and redlining protocol

No LLM, API key, or paid dataset is involved. Run from the repository root:

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[test,benchmark]'
npm --prefix frontend ci
.venv/bin/python -m benchmarks.run --output reports/latest
```

Prerequisites: Python 3.11+, Node/npm, PostgreSQL 16+ binaries, Redis (`redis-server` on PATH), and Chrome. Selenium Manager obtains the compatible driver on first use, which requires internet access. On macOS the default PostgreSQL directory is `/opt/homebrew/opt/postgresql@16/bin`; elsewhere set `PGBIN` to its binary directory. PostgreSQL `initdb` must run as a non-root user.

The command downloads checksum-verified CUAD automatically if absent, starts a temporary loopback PostgreSQL cluster, temporary Redis with AOF, API, one durable worker, and Vite. It creates and drops only a uniquely named benchmark schema; application data is untouched. Browser profiles are temporary. Do not run this capacity experiment on a production host.

Alternatively use a dedicated development database whose user can create schemas and `pg_trgm`:

```bash
.venv/bin/python -m benchmarks.run \
  --database-url postgresql://localhost/document_redlining_benchmark \
  --repetitions 5 --output reports/my-run
```

The extension may remain in the external database; the benchmark schema is removed. Service logs, `results.json`, `report.md`, and final UI screenshot are saved to the output directory. A failed run raises an error and writes partial measurements to `failure.json`, plus browser screenshots where possible; it never generates a passing report. Use a fresh output directory for each run. Termination with SIGKILL cannot run cleanup; use a disposable database for interrupted experiments.

Recorded current run: [end-to-end report](../reports/current-2026-10-12/report.md).

## Dataset and retrieval evaluation

[CUAD](https://www.atticusprojectai.org/cuad/) contains 510 real commercial contracts with annotations for 41 clause categories. The download is pinned to a Hugging Face dataset revision and SHA-256 in `benchmarks/run.py`. Attribution: The Atticus Project; Hendrycks, Burns, Chen, Ball (2021), [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Only the pinned JSON is required, not the unrelated US Code corpus.

We derive diagnostic document-retrieval queries from category names and binary relevance judgments from nonempty annotated answers. This is **not an official CUAD ranking benchmark**: CUAD primarily evaluates clause extraction, and category words are imperfect search queries. No model generates queries or judgments. The full corpus is indexed before measurements; query results include only these contracts during the search phase.

The API and an in-memory SQLite FTS5/BM25 baseline receive identical AND-token queries. Terms-mode API results contain one anchor per document, ranked by native PostgreSQL cover density (best bounded section); the benchmark takes the first 10 document IDs. FTS5 returns 10 BM25-ranked document IDs. Literal searches remain unscored. Record that product-level distinction when interpreting nDCG@10, Recall@10, and MRR@10. We use familiar [BEIR metrics](https://github.com/beir-cellar/beir), not BEIR datasets or leaderboard comparisons. HTTP vs in-process latency is not an apples-to-apples engine benchmark. One warmup is discarded per query; default three timed repetitions across all 41 queries. Also check 30 literal source slices and an absent query. The current harness inherits query-cache settings: with defaults, the warmup records first use, the first timed request admits the result, and later requests may hit the cache. Set `QUERY_CACHE_MAX_QUERIES=0` for an uncached engine comparison; use the separate query-cache suite below to isolate cold/admission/warm timings.

## Reservation → replacement over HTTP

1. `GET /documents/{id}` gives text and version. Offsets are half-open Unicode code points, **not UTF-8 bytes or JavaScript UTF-16 units**.
2. `POST /documents/{id}/edit-reservations`, with `X-Client-ID`, reserves the range for 30 seconds. The server rejects a stale version or a protected overlap with 409 (including a different overlapping selection by the same client). Duplicate targets within one array return 422. An identical selection by the same client returns its existing reservation without extending the lease.
3. `POST /documents/{id}/replacements` supplies a replacements array containing reservation IDs and replacement texts. Admission binds the operation to the reservation and protects the range until execution finishes. The durable worker commits the new revision and eagerly moves other protected anchors.
4. HTTP 200 returns the **exact revision produced by this operation**, including full updated text. No WebSocket is required for an API caller. A changed payload for an already-bound reservation is rejected. Replaying the same payload returns the same revision, even if the document has advanced.
5. The completion wait is 30 seconds after admission (`DocumentService` in `src/runtime.py`). A 202 supplies a status URL and means work remains durably accepted. GET that URL for pending/completed/failed, or repeat the same submission to retrieve its result; do not create a new operation blindly. The wait can be shortened with REPLACEMENT_WAIT_SECONDS. The browser independently subscribes to `/documents/{id}/updates`.

Example, after fetching the current version and locating a real range:

```bash
curl -s http://localhost:8000/documents/example
curl -s -X POST http://localhost:8000/documents/example/edit-reservations \
  -H 'Content-Type: application/json' -H 'X-Client-ID: example-client' \
  -d '{"targets":[{"version":1,"start":0,"end":5}]}'
# Use reservations[0].reservation_id from that response:
curl -s -X POST http://localhost:8000/documents/example/replacements \
  -H 'Content-Type: application/json' \
  -d '{"replacements":[{"reservation_id":"<returned-id>","replacement":"Updated text"}]}'
```

The executable suite creates its own small document (no manual ID needed), reserves ten disjoint ranges, submits ten concurrent replacements, verifies immutable responses and v1→v11, then tests replay, changed payload, overlap and stale-version rejection. It also checks three sequential replacements and eventual indexing on a >10 MB synthetic concatenation of the contracts. This large text is **not represented as a real individual contract**.

## Selenium protocol

Ten independent headless Chrome sessions select the same small document, each select a different token with a DOM Range and dispatch the mouseup handled by the UI. They type through Selenium into the actual editable suggestion, keeping replacement text in browser memory until submission. A barrier releases ten real Submit clicks. No browser edit is injected through a test-only HTTP API. Each client must observe v11 and exactly the expected text through normal UI subscriptions. The report records client click-start order and database commit version order, not inferred FIFO.

This smoke run uses normal UI lease behavior, including renewal while a page is focused and visible. The harness does not bypass protection. A slow or unfocused browser can lose its lease and fail the run; this is not a controlled lease-expiry test. Ten concurrent disjoint edits test anchor movement and convergence. They do not measure sustained throughput, ten conflicting edits, or crash recovery. Existing integration suites cover additional database/worker failure paths.

Use `--compare <previous-results.json>` to compare compatible runs with the same corpus and query definitions.

## Atomic batch verification

`tests/test_postgres.py` additionally covers batch rollback, duplicate selection,
combined anchor displacement, complete replay, competing batches, crash recovery,
HTTP/WebSocket batch completion and a >10 MB insertion/deletion batch with reindexing.
The end-to-end benchmark measures ten independent writes; the integration suite
separately verifies multiple edits inside one transaction.

An opt-in two-browser test selects two passages, types locally, verifies lease renewal beyond 30 seconds, submits one batch,
and verifies that both browsers observe exactly one new revision:

```bash
REDLINING_BROWSER_TESTS=1 .venv/bin/pytest -q tests/test_browser_batches.py
```

It provisions the same disposable stack as the benchmark harness and requires
PostgreSQL binaries, Redis, Chrome and the benchmark dependencies.

## Corpus query-result cache comparison

```sh
.venv/bin/python -m benchmarks.query_cache --output reports/query-cache-new-run --repetitions 3
```

Indexes all 510 CUAD contracts and compares the same 41 corpus-wide category
queries with result caching disabled, on first use, on admission (second use),
and warm. Document-scoped searches are uncached. It then measures
reconciliation after unrelated additions, new matching documents, same-score anchor
changes, and removal of a cached winner. Every measured response is checked against
the uncached result. This uses in-process ASGI requests with real PostgreSQL;
network latency, concurrent traffic, and mutation/indexing time are excluded.

Recorded run: [corpus query-cache comparison](../reports/query-cache-current-2026-10-12/report.md).
