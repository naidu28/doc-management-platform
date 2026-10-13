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
not connected to these experiment databases. The live `scripts.review_demo`
imports the 510 checksum-verified contract text files into PostgreSQL and indexes
them for search. It preserves existing documents and edits on restart.

CUAD annotations supply 13,823 verified exact answer spans for constructing
retrieval checks. Their natural-language questions are not automatically a gold
benchmark for lexical search: select queries and expected results deliberately.

## Application storage

The current application uses PostgreSQL exclusively. See the root README for
its schema, revision lifecycle, and search projection. SQLite data here belongs
to standalone retrieval experiments and benchmark comparisons.

These experiments measure one local run. They do not establish p95/p99 latency,
queue behavior, durable revision performance, index freshness, or concurrent writes.

Historical local service measurements remain in service-benchmark.json.
Current end-to-end benchmarks are documented in benchmarks/README.md.
