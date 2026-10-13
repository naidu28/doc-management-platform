# Scoped-search in-memory index benchmark

> Historical experiment: the document postings cache, its configuration, and its benchmark runner have since been removed. These measurements document why; the active system retains only corpus-wide query-result caching.

Run: 2026-10-13T00:32:16.944966+00:00. Python 3.13.6; macOS-26.6.2-arm64-arm-64bit-Mach-O.

## Method

Same PostgreSQL projection, requests, ranking, snippets, and response serialization for all arms. FastAPI TestClient executes the real ASGI routes and threaded service against a temporary PostgreSQL database; these are in-process API timings, excluding a real client network connection. PostgreSQL pages are warmed. Cold means only the application postings cache is empty before each request; warm means that exact query/document has already run. Disabled uses a zero-byte budget. Execution order rotates between arms. No concurrent load.

Twelve contracts are selected deterministically at evenly spaced length quantiles from all 510 CUAD contracts, plus a synthetic concatenation of the full corpus. Queries cover a common term, four ending tokens as terms/phrase, a literal control, and an absent-term control. This is a repeated-open-document workload, not a production traffic distribution. Each query is timed 7 times per arm. Every measured response must equal the disabled response, including scores, versions and offsets.

Budget: 32 MiB per store. Oversized entries use fallback markers, so a warm cache hit need not mean a usable inverted index. Retained bytes are the cache accounting estimate, not process RSS; input text and transient allocations are additional.

## Results

| Workload | Query | Disabled p50 / p95 ms | Cold p50 / p95 ms | Warm p50 / p95 ms | Warm p50 change |
|---|---|---:|---:|---:|---:|
| contracts | terms_common | 4.41 / 6.12 | 11.22 / 61.67 | 4.39 / 6.74 | -0.4% |
| contracts | terms_tail | 5.08 / 8.94 | 11.71 / 64.80 | 4.77 / 8.73 | -6.1% |
| contracts | phrase_tail | 4.81 / 11.65 | 11.46 / 64.46 | 4.70 / 8.91 | -2.2% |
| contracts | literal_control | 4.01 / 6.35 | 3.96 / 6.47 | 3.97 / 6.00 | -1.1% |
| contracts | absent_control | 4.15 / 4.92 | 4.07 / 4.90 | 4.13 / 4.92 | -0.4% |
| large_composite | terms_common | 185.91 / 200.67 | 499.33 / 516.40 | 187.60 / 197.15 | +0.9% |
| large_composite | terms_tail | 405.89 / 482.08 | 690.98 / 830.89 | 402.39 / 409.68 | -0.9% |
| large_composite | phrase_tail | 845.92 / 881.04 | 1160.81 / 1185.81 | 857.03 / 870.17 | +1.3% |
| large_composite | literal_control | 137.67 / 142.58 | 135.06 / 141.00 | 134.17 / 141.97 | -2.5% |
| large_composite | absent_control | 86.23 / 88.25 | 87.73 / 93.77 | 85.41 / 96.73 | -1.0% |

Negative change means faster; positive means slower. Percentages compare pooled medians, not paired speedup averages. Contract tails mix different source sizes; large-document tails have only 7 samples per query/arm.

All 1365 measured responses matched exactly. Maximum estimated retained cache bytes: 7,079,505.

## Interpretation

The cache only avoids repeated Python token/regex resolution; PostgreSQL filtering/ranking and full candidate-body transfer still run. Cold tokenization can outweigh resolution savings. Common matches near the beginning need little regex work; late matches offer more opportunity. Literal and absent-query controls bypass index construction. Oversized documents still use the original resolver after a bounded failed build.

Do not infer capacity, corpus-ranking improvement, or consistent latency gains from this micro-workload. Raw per-request timings, cache hit/build counts, index admission, source sizes, and queries are in results.json.

## Decision from this run

Warm contract terms-tail p50 improved from 5.08 to 4.77 ms (6.1%), and phrase-tail
from 4.81 to 4.70 ms (2.2%). Common-term p50 was essentially unchanged. The bypass
controls also varied by roughly 0.4–1.1%, so small differences should not be treated
as robust gains without repeated trials. Phrase-tail p95 improved from 11.65 to
8.91 ms in this sample, but this is a mixed-document distribution.

Cold builds raised contract term/phrase p50 to 11.22–11.71 ms from 4.41–5.08 ms.
The 26.8 MB composite never admitted a complete index within 32 MiB; first searches
paid roughly 285–315 ms extra for the failed build, and warmed fallback markers
provided no meaningful resolver acceleration. These markers count as cache hits
in the raw counters, but `complete_indexes` is zero for the composite.

Recommendation: keep this accelerator optional, and prefer `SEARCH_CACHE_BYTES=0`
for general traffic until a repeated-search workload demonstrates amortized value.
The implementation default was not changed by this experiment. A future experiment
could test compact postings and document admission thresholds before building.
