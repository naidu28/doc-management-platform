# Sandstone review UI

Minimal React/Vite interface for the Python service: search this document, search all documents, document selector, viewer/redliner, and Submit edits. Server state is delivered through one WebSocket-backed external store (`useSyncExternalStore`). Local state covers navigation/search controls, transient selection anchors, and unsubmitted replacement text. No client document persistence or operation polling.

## Local demonstration

First install the Python dependencies and create the database as described in the
root README. Then, from the repository root:

```sh
npm --prefix frontend install
.venv/bin/python scripts/download_corpus.py
.venv/bin/python scripts/prepare_corpus.py
DATABASE_URL=postgresql://localhost/sandstone .venv/bin/python -m scripts.review_demo
```

In a second terminal:

```sh
API_URL=http://127.0.0.1:8010 npm --prefix frontend run dev
```

Open the printed Vite URL. The demo uses the PostgreSQL database named by `DATABASE_URL` and all 510 CUAD contracts from the checksum-verified local corpus (`data/manifest.json`). Existing documents and edits are preserved. It runs real PostgreSQL writes and search indexing in a local background loop; WebSockets reconcile every two seconds, so Redis is optional for this demonstration. Production uses `src.main:build_app --factory`, `src.runtime`, and Redis as described in the root README. For that service, omit `API_URL` (defaults to port 8000).

Open a separate browser tab for another reviewer. The session ID persists through reloads via sessionStorage. To guarantee distinct sessions, open the URL manually in a new tab rather than duplicating an existing tab.

## Interactions

- Highlight source text (or click an insertion point) to acquire a lease through the list-based reservation API. The UI renews selected and edited reservations every 10 seconds while the document is focused and visible. A highlight can remain after expiry, but it no longer guarantees ownership.
- Type directly in the document to prepare a local replacement. Blue means modified, light green inserted, red deleted. Backspace/Delete in an empty suggestion deletes the selection; Escape discards it. Typing does not save a server draft or make a reservation permanent.
- Submit edits in the upper right. All local edits are sent together to `POST /documents/{id}/replacements` as one atomic batch. HTTP 200 means committed text and version; HTTP 202 means accepted and pending. WebSocket outcomes report completion or failure without polling or automatic resubmission.
- The right rail navigates current local edits with before/after hover text. Submitted edit history and a comparison baseline are not implemented.
- Both search inputs use server search. A stale search result prompts a new search rather than reserving an old anchor.

A reconnect requests missed patches from the last version. While a transient highlight exists, it requests retained patch history so the selection can be rebased for non-overlapping changes or cleared for overlapping changes. Subscription snapshots contain reservations and operation outcomes, not draft text. Unsubmitted replacement text stays in memory and is lost on reload or switching documents. The UI requires a current protected reservation before submission. At the API level, an expired reservation can still renew or submit if its anchor is current and the range is free.

## Checks

```sh
npm --prefix frontend test
npm --prefix frontend run build
.venv/bin/python -m pytest -q
```

The development proxy serves the Python API under `/api`, including WebSockets. A production host should provide the same routing and serve `frontend/dist`.

Replacement HTTP 202 is shown as accepted/pending; WebSocket outcomes report completion or failure. The UI does not poll the operation resource or automatically resubmit accepted edits. HTTP 200 continues to mean committed.
