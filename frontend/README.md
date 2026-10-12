# Sandstone review UI

Minimal React/Vite interface for the Python service: search this document, search all documents, document selector, viewer/redliner, and Submit edits. Server state is delivered through one WebSocket-backed external store (`useSyncExternalStore`). Local state covers navigation/search controls, transient selection anchors, and typing awaiting server acknowledgment. No client document persistence or operation polling.

## Local demonstration

From the repository root:

```sh
npm --prefix frontend install
DATABASE_URL=postgresql://localhost/sandstone .venv/bin/python -m scripts.review_demo
```

In a second terminal:

```sh
API_URL=http://127.0.0.1:8010 npm --prefix frontend run dev
```

Open the printed Vite URL. The demo uses the PostgreSQL database named by `DATABASE_URL` and three explicitly fictional sample contracts. It runs real PostgreSQL writes and search indexing in a local background loop; WebSockets reconcile every two seconds, so Redis is optional for this demonstration. Production uses `redlining.main:build_app --factory`, `redlining.runtime`, and Redis as described in the root README. For that service, omit `API_URL` (defaults to port 8000).

Open a separate browser tab for another reviewer. The session ID persists through reloads via sessionStorage. To guarantee distinct sessions, open the URL manually in a new tab rather than duplicating an existing tab.

## Interactions

- Highlight source text (or click an insertion point) to acquire a lease. It remains yellow even after lease expiry. Non-overlapping committed patches shift the highlight; overlapping committed patches clear it.
- Type a replacement to queue a draft save on the server. Saving does not extend the 30-second reservation; use Renew 30s explicitly. Blue means modified, light green inserted, red deleted. Backspace/Delete in an empty replacement field deletes the selection. Explicit Discard releases it.
- Submit edits in the upper right. Reservations and drafts return synchronously. Replacement requests remain open until they return the committed text and version. The subscription independently keeps all clients current; there is no HTTP polling. Multiple edits are accepted independently, not as an atomic batch.
- The right rail navigates current draft edits with before/after hover text. Submitted edit history and a comparison baseline are not implemented.
- Search results are server responses. A stale search result prompts a new search rather than reserving an old anchor.

A reconnect requests missed patches from the last version. While a transient highlight exists, it requests complete retained patch history so the selection can be rebased or invalidated only by an overlapping committed change. Drafts are recovered from server snapshots but do not grant protection after lease expiry. A stale or contested selection must be selected again.

## Checks

```sh
npm --prefix frontend test
npm --prefix frontend run build
.venv/bin/python -m pytest -q
```

The development proxy serves the Python API under `/api`, including WebSockets. A production host should provide the same routing and serve `frontend/dist`.

Replacement HTTP 202 is shown as accepted/pending; WebSocket outcomes report completion or failure. The UI does not poll the operation resource or automatically resubmit accepted edits. HTTP 200 continues to mean committed.
