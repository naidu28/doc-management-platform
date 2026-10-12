# React document review UI

1. Build a React/Vite workbench: two plain search fields (current document and all documents), a document selector, source reader with inline replacement editor, top-right Submit edits, and a narrow green/blue/red change rail. No branding panels, breadcrumbs, decorative typography, or dashboard chrome.
2. Keep documents, protected ranges, draft replacements, and durable command acceptance on the server. One external React store consumes the existing WebSocket snapshot/patch stream; component state is limited to navigation, query controls, focus, and typing awaiting acknowledgment.
3. Selecting text acquires a 30-second lease. First input atomically persists a replacement and promotes the active reservation to non-expiring protection. Save subsequent typing serially; submit only after saves finish. Discard explicitly releases protection. Persist the session ID across reloads. Retain expired visual selections until a conflicting committed patch invalidates them; rebase non-overlapping changes.
4. Add document listing and draft-save endpoints, and include durable drafts in live snapshots. Preserve the existing FIFO replacement pipeline. Submit multiple edits individually. Durable acceptance completes the client action; WebSocket updates deliver the resulting document.
5. Validate draft expiry/overlap/rebasing and recovery, Unicode offsets, subscription patch ordering, frontend build, and browser interactions. Seed an isolated demonstration database and run the UI against real services.

Initial scope: plain-text source, lexical search, draft change rail. Committed markers require a review-baseline/history projection and are a follow-up; do not mislabel them as available.
