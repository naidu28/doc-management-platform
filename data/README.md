# Public test corpus

These fixtures are local benchmark data, not application state. Generated text,
archives and databases are ignored by Git. Manifests retain source URLs, SHA-256
checksums, byte/character lengths, and extraction details.

Sources and attribution:

- CUAD v1, The Atticus Project; Dan Hendrycks, Collin Burns, Anya Chen and Spencer
  Ball (2021), *CUAD: An Expert-Annotated NLP Dataset for Legal Contract Review*.
  510 commercial contracts and labeled clause spans. License: CC BY 4.0 according
  to the official Hugging Face dataset metadata. Contract text is extracted from
  the release JSON unchanged; no Unicode/newline normalization. Keep attribution
  when reusing it. https://www.atticusprojectai.org/cuad/
  https://huggingface.co/datasets/theatticusproject/cuad
- U.S. House Office of the Law Revision Counsel, U.S. Code Title 42, release
  119-120, official XML. A large legal compilation, not a commercial contract.
  Text fixture joins stripped nonempty XML text nodes with newlines; this is a
  benchmark extraction, not a faithful rendered legal edition. It contains
  headings/notes as well as operative text. Freeze it before assigning offsets.
  https://uscode.house.gov/download.html
- `stress/cuad-composite.txt` concatenates the contracts with labeled separators.
  This is a synthetic composite, never represented as one real contract. Component
  boundaries are recorded separately. It retains the original source attribution.

Reproduce (download step requires network):

```sh
.venv/bin/python scripts/download_corpus.py
.venv/bin/python scripts/prepare_corpus.py
.venv/bin/python scripts/prepare_large_legal_text.py
```

Inspect `manifest.json`, `experiment-results.json`, and `legal/manifest.json` for
actual sizes and local smoke timings. Downloads use the release URLs; the CUAD
main URL can change, so compare recorded checksums when reproducing. The scripts
rebuild ONLY their named experiment databases. Production service adapters are
not connected to these databases.

CUAD annotations supply 13,823 verified exact answer spans for constructing
retrieval checks. Their natural-language questions are not automatically a gold
benchmark for lexical search: select queries and expected results deliberately.

## Application storage (implemented in redlining/storage.py)

1. Canonical UTF-8 fixtures on disk for loading/reproducibility. Preserve exact
   text after ingestion so anchors always use stable Unicode code-point offsets.
2. SQLite `documents(document_id, current_version, title, ...)` and immutable
   `document_revisions(document_id, version, text TEXT, ...)`. Use full snapshots
   initially: simple historical retrieval; cost is O(document bytes) per revision.
   Patch history/outbox/reservations remain ordinary SQLite tables. Compression or
   snapshot+patch reconstruction can follow measured storage pressure.
3. SQLite `search_documents(id INTEGER PRIMARY KEY, document_id UNIQUE,
   indexed_version, title, body TEXT)` plus an external-content FTS5 virtual table
   over title/body. This is a separate versioned projection, not the live mutable
   document table: async indexing must not pair old postings with newer content.
4. Index worker updates projection content AND postings in one SQLite transaction
   using FTS5 delete-old/insert-new (or synchronization triggers). Atomically guard
   indexed_version so delayed work cannot roll it backward. Readers see all-old
   or all-new content/metadata/postings. No-op/duplicate versions can be skipped.

Start with one indexed record per document. SQLite FTS5 supplies token/phrase
matching, BM25 and snippets; use unicode61 without aggressive stemming initially.
The included experiment verifies those primitives on the downloaded corpus.
A whole changed document is re-tokenized on update; the entire corpus is not.
This matches the chosen linear replacement design and avoids premature chunk IDs.

For literal mode use exact matching on canonical source text (bounded document
scope, linear scans initially). Token FTS must not be used as a universal substring
prefilter: it can miss within-token or punctuation-sensitive matches. A separate
FTS5 trigram index is a possible later accelerator, with explicit short-query and
case behavior. Never pass raw user input as FTS query syntax: quote/compile the
structured query. SQL binding alone does not escape the FTS query language.

FTS5 snippet markup is display output, not an exact source offset. The implemented
resolver instead uses highlight() over the complete unmodified indexed source,
checks that removing its unique markers reconstructs that source exactly, and
computes the first marked span. It never searches for a repeated snippet. Tests
cover repeated phrases, Unicode, and diacritic-insensitive token matches.

Passage indexing is the next candidate optimization if benchmarks show expensive
whole-document indexing or poor long-document ranking. Store exact start/end and
revision per passage, use boundary-aware segmentation, and define phrase queries
across boundaries. Do not promise incremental passage updates before solving
changed boundaries and downstream offset shifts. Compare this with the document
baseline using the same corpus before choosing it.

Prior art:
- SQLite FTS5 external-content tables and synchronization:
  https://www.sqlite.org/fts5.html#external_content_and_contentless_tables
- Built-in BM25 and snippet functions:
  https://www.sqlite.org/fts5.html#auxiliary_functions
- Trigram tokenizer and substring limitations:
  https://www.sqlite.org/fts5.html#the_trigram_tokenizer

These experiments measure one local run. They do not establish p95/p99 latency,
queue behavior, durable revision performance, index freshness, or concurrent writes.

The actual service benchmark is scripts/benchmark_service.py, with results in
service-benchmark.json. The earlier experiment databases remain isolated fixtures.
