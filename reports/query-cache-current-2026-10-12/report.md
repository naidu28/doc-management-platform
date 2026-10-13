# Corpus query-result cache benchmark

Run: 2026-10-13T02:11:28.265275+00:00; Python 3.13.6; macOS-26.6.2-arm64-arm-64bit-Mach-O.

## Method

All 510 pinned CUAD contracts, 26820468 UTF-8 bytes, in one isolated PostgreSQL database. Forty-one category-label queries use terms mode and limit 10 across the complete corpus. Every measured cached response equals its uncached counterpart, including ranking, snippets, versions and offsets. Document-scoped search is uncached; this experiment isolates corpus result caching.

Timings use FastAPI TestClient through real ASGI routing and threaded service calls to PostgreSQL. They include database connections and response serialization but exclude real HTTP network latency. PostgreSQL is warmed before comparison. Arms execute serially in rotating order; no concurrent load.

Disabled: query cache off. Cold: first request after an empty application cache. Admission: second identical request, which populates the cache (first request excluded from this timing). Warm: third or later request after both admission searches. Warm retains queries across the run. Each query has 3 measured repetitions per arm. Bounds: 64 results, 64 admission records, 8 MiB combined estimated memory, five-minute idle expiry. Retained bytes are not process RSS.

After steady measurements, 3 rounds modify the projection through a separate store: add an unrelated document, add a new top result, change its version/anchor while preserving score, then remove its match through replacement. Update/execution/indexing time is excluded; each timed request reconciles one committed update. Maintenance queries use “agreement”; they are not directly comparable to the pooled 41-query distribution.

## Results

| Workload | Arm | n | p50 ms | p95 ms | Cache hits | Incremental refreshes | Full refills |
|---|---|---:|---:|---:|---:|---:|---:|
| steady | disabled | 123 | 195.95 | 350.25 | 0 | 0 | 0 |
| steady | cold | 123 | 197.12 | 368.92 | 0 | 0 | 0 |
| steady | admission | 123 | 206.75 | 373.99 | 0 | 0 | 0 |
| steady | warm | 123 | 3.74 | 6.64 | 123 | 0 | 0 |
| unrelated_update | disabled | 3 | 113.26 | 121.04 | 0 | 0 | 0 |
| unrelated_update | warm | 3 | 5.95 | 6.24 | 0 | 3 | 0 |
| new_match | disabled | 3 | 111.20 | 140.47 | 0 | 0 | 0 |
| new_match | warm | 3 | 6.01 | 7.28 | 0 | 3 | 0 |
| anchor_update | disabled | 3 | 111.72 | 115.75 | 0 | 0 | 0 |
| anchor_update | warm | 3 | 5.66 | 6.28 | 0 | 3 | 0 |
| winner_removed | disabled | 3 | 109.91 | 115.72 | 0 | 0 | 0 |
| winner_removed | warm | 3 | 112.34 | 113.63 | 0 | 3 | 3 |

Warm pooled p50 speedup: **52.4×**; median latency reduction **98.1%**. All **516** timed responses matched exactly. Peak retained estimate: **138,885 bytes**.

## Interpretation

Other disposable verification processes shared this host during portions of the run; timings may include resource contention. Treat this as correctness and repeated-query smoke evidence, not an isolated latency baseline.

Warm results avoid corpus filtering, ranking and body transfer; PostgreSQL is still queried for the committed generation. The first two uses still execute the complete search. New and unchanged-score matches can be maintained from changed documents; removing a winner requires a full refill. This workload deliberately repeats queries, so it is not a production hit-rate or capacity claim. Maintenance tail estimates have only 3 samples per case/arm. Idle expiry, eviction churn, multi-process duplication, parallel contention and heavy write rates are not benchmarked here. Full individual timings and source hashes are in results.json.
