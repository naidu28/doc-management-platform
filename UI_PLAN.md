# React document review UI

1. React/Vite workbench with document-scoped and corpus search through `POST /search`, a document selector, source reader, inline replacement editor, Submit edits, and a change rail.
2. PostgreSQL owns documents, protected ranges, and accepted replacements. The React store consumes WebSocket snapshots and patches. Unsubmitted text stays in component memory and is discarded on navigation/reload.
3. Selecting text acquires a 30-second lease. While the document is focused, renew selected and locally edited reservations every 10 seconds. Each renewal revalidates protection; hidden/unfocused pages do not renew. Discard releases protection, and browser departure cleanup releases unsubmitted reservations.
4. Submit 1–100 local edits atomically through the common replacement contract. Accepted replacements have non-expiring protection until execution; there is no arrival-order fairness requirement. HTTP returns committed text or a pending operation; WebSocket updates deliver live outcomes.
5. Validate lease renewal, expiry/overlap, Unicode offsets, subscription ordering, local typing, atomic submission, and browser convergence. Other users see protected ranges, not unfinished replacement text.

Initial scope: plain-text source, lexical search, local change rail. Committed review markers require a separate history projection.
