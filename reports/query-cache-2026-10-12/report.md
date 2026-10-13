# Corpus query-result cache benchmark

Run: 2026-10-13T00:53:41.862110+00:00; Python 3.13.6; macOS-26.6.2-arm64-arm-64bit-Mach-O.

## Method

All 510 pinned CUAD contracts, 26820468 UTF-8 bytes, in one isolated PostgreSQL database. Forty-one category-label queries use terms mode and limit 10 across the complete corpus. Every measured cached response equals its uncached counterpart, including ranking, snippets, versions and offsets. The per-document postings cache is disabled in every arm.

Timings use FastAPI TestClient through real ASGI routing and threaded service calls to PostgreSQL. They include database connections and response serialization but exclude real HTTP network latency. PostgreSQL is warmed before comparison. Arms execute serially in rotating order; no concurrent load.

Disabled: query cache off. Cold: first request after an empty application cache. Admission: second identical request, which populates the cache (first request excluded from this timing). Warm: third or later request after both admission searches. Warm retains queries across the run. Each query has 3 measured repetitions per arm. Bounds: 64 results, 64 admission records, 8 MiB combined estimated memory, five-minute idle expiry. Retained bytes are not process RSS.

After steady measurements, 3 rounds modify the projection through a separate store: add an unrelated document, add a new top result, change its version/anchor while preserving score, then remove its match through replacement. Update/execution/indexing time is excluded; each timed request reconciles one committed update. Maintenance queries use “agreement”; they are not directly comparable to the pooled 41-query distribution.

## Results

| Workload | Arm | n | p50 ms | p95 ms | Cache hits | Incremental refreshes | Full refills |
|---|---|---:|---:|---:|---:|---:|---:|
| steady | disabled | 123 | 182.36 | 345.75 | 0 | 0 | 0 |
| steady | cold | 123 | 183.49 | 313.11 | 0 | 0 | 0 |
| steady | admission | 123 | 183.88 | 322.18 | 0 | 0 | 0 |
| steady | warm | 123 | 3.84 | 4.42 | 123 | 0 | 0 |
| unrelated_update | disabled | 3 | 105.69 | 106.16 | 0 | 0 | 0 |
| unrelated_update | warm | 3 | 5.03 | 5.07 | 0 | 3 | 0 |
| new_match | disabled | 3 | 108.70 | 108.79 | 0 | 0 | 0 |
| new_match | warm | 3 | 5.69 | 5.88 | 0 | 3 | 0 |
| anchor_update | disabled | 3 | 107.29 | 108.07 | 0 | 0 | 0 |
| anchor_update | warm | 3 | 5.20 | 5.65 | 0 | 3 | 0 |
| winner_removed | disabled | 3 | 109.46 | 112.13 | 0 | 0 | 0 |
| winner_removed | warm | 3 | 106.91 | 108.69 | 0 | 3 | 3 |

Warm pooled p50 speedup: **47.5×**; median latency reduction **97.9%**. All **516** timed responses matched exactly. Peak retained estimate: **138,885 bytes**.

## Interpretation

Warm results avoid corpus filtering, ranking and body transfer; PostgreSQL is still queried for the committed generation. The first two uses still execute the complete search. New and unchanged-score matches can be maintained from changed documents; removing a winner requires a full refill. This workload deliberately repeats queries, so it is not a production hit-rate or capacity claim. Maintenance tail estimates have only 3 samples per case/arm. Idle expiry, eviction churn, multi-process duplication, parallel contention and heavy write rates are not benchmarked here. Full individual timings and source hashes are in results.json.
