import test from "node:test";
import assert from "node:assert/strict";
import { applyState, editKind } from "./protocol.js";
test("patches use Unicode code points and install aligned reservations", () => {
  const next = applyState(
    { document: { document_id: "x", version: 1, text: "A😀BC" } },
    {
      type: "document_state",
      updates: [
        {
          previous_version: 1,
          version: 2,
          patch: { start: 2, end: 3, replacement: "new" },
        },
      ],
      reservations: { document_version: 2, reservations: [] },
      drafts: [],
    },
  );
  assert.equal(next.document.text, "A😀newC");
});
test("revision gaps require a fresh subscription", () =>
  assert.throws(
    () =>
      applyState(
        { document: { version: 1, text: "abc" } },
        {
          type: "document_state",
          updates: [
            {
              previous_version: 2,
              version: 3,
              patch: { start: 0, end: 1, replacement: "" },
            },
          ],
          reservations: { document_version: 3, reservations: [] },
        },
      ),
    /gap/,
  ));
test("insertion, deletion and modification categories", () => {
  assert.equal(editKind({ start: 1, end: 1 }, "a"), "insert");
  assert.equal(editKind({ start: 1, end: 2 }, ""), "delete");
  assert.equal(editKind({ start: 1, end: 2 }, "b"), "modify");
});
import { rebaseSelection } from "./protocol.js";
test("a retained selection shifts for a non-overlapping replacement", () => {
  const s = { target: { version: 1, start: 6, end: 11 } };
  assert.deepEqual(
    rebaseSelection(s, {
      version: 2,
      patch: { start: 0, end: 5, replacement: "hi" },
    }).target,
    { version: 2, start: 3, end: 8 },
  );
});
test("a conflicting committed replacement clears the highlight", () =>
  assert.equal(
    rebaseSelection(
      { target: { version: 1, start: 6, end: 11 } },
      { version: 2, patch: { start: 7, end: 9, replacement: "x" } },
    ),
    null,
  ));
test("lease expiry alone preserves the visual selection", () => {
  const selection = {
    reservation_id: "r",
    expires_at: "old",
    target: { version: 1, start: 0, end: 3 },
  };
  assert.deepEqual(
    applyState(
      { document: { version: 1, text: "abc" }, selection },
      {
        type: "document_state",
        updates: [],
        reservations: { document_version: 1, reservations: [] },
        drafts: [],
      },
    ).selection,
    selection,
  );
});

test("batch patches use one base revision and combine Unicode shifts", () => {
  const previous = {
    document: { document_id: "X", version: 1, text: "aa bb cc" },
    selection: { reservation_id: "r", target: { version: 1, start: 6, end: 8 } },
  };
  const update = { previous_version: 1, version: 2, patches: [
    { start: 0, end: 2, replacement: "😀" },
    { start: 3, end: 5, replacement: "LONG" },
  ] };
  const state = applyState(previous, { type: "document_state", updates: [update],
    reservations: { document_version: 2, reservations: [] } });
  assert.equal(state.document.text, "😀 LONG cc");
  assert.deepEqual(state.selection.target, { version: 2, start: 7, end: 9 });
  const conflicted = applyState({ ...previous, selection: {
    reservation_id: "r", target: { version: 1, start: 3, end: 5 },
  } }, { type: "document_state", updates: [update],
    reservations: { document_version: 2, reservations: [] } });
  assert.equal(conflicted.selection, null);
});
