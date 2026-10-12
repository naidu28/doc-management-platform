// Server anchors are Unicode code points, not JavaScript UTF-16 offsets.
export const chars = (text) => Array.from(text);
export function applyState(previous, message) {
  if (message.type !== "document_state")
    throw new Error(message.reason || "Invalid subscription message");
  let document = message.document || previous.document;
  let selection = previous.selection || null;
  let selectionNotice = previous.selectionNotice || "";
  if (
    selection &&
    message.document &&
    selection.target.version !== message.document.version
  ) {
    selection = null;
    selectionNotice = "Document refreshed. Select the passage again to edit.";
  }
  for (const update of message.updates || []) {
    if (!document || update.previous_version !== document.version)
      throw new Error("Document revision gap");
    if (selection) {
      const next = rebaseSelection(selection, update);
      if (!next)
        selectionNotice =
          "Your highlighted passage was replaced by another edit. The document has refreshed.";
      selection = next;
    }
    const source = chars(document.text);
    const patches = update.patches || [update.patch];
    const pieces = [];
    let position = 0;
    for (const patch of patches) {
      if (patch.start < position || patch.end < patch.start || patch.end > source.length)
        throw new Error("Invalid document patch ranges");
      pieces.push(source.slice(position, patch.start).join(""), patch.replacement);
      position = patch.end;
    }
    pieces.push(source.slice(position).join(""));
    document = { ...document, version: update.version, text: pieces.join("") };
  }
  if (!document || document.version !== message.reservations.document_version)
    throw new Error("Reservation revision gap");
  if (selection) {
    const confirmed = message.reservations.reservations.find(
      (r) => r.reservation_id === selection.reservation_id,
    );
    if (confirmed) selection = confirmed.expires_at ? confirmed : null;
  }
  return {
    document,
    reservations: message.reservations.reservations,
    drafts: message.drafts || [],
    operations: message.operations || [],
    selection,
    selectionNotice,
  };
}
export const editKind = (target, replacement) =>
  target.start === target.end
    ? "insert"
    : replacement === ""
      ? "delete"
      : "modify";
export function sourceOffset(node, offset) {
  const element = node.nodeType === 1 ? node : node.parentElement;
  const span = element?.closest("[data-start]");
  if (!span) return null;
  const range = document.createRange();
  range.setStart(span, 0);
  range.setEnd(node, offset);
  return Number(span.dataset.start) + chars(range.toString()).length;
}

export function rebaseSelection(selection, update) {
  const a = selection.target;
  let delta = 0;
  for (const p of update.patches || [update.patch]) {
    const overlaps =
      a.start === a.end
        ? p.start <= a.start && a.start <= p.end
        : p.start === p.end
          ? a.start <= p.start && p.start <= a.end
          : a.start < p.end && p.start < a.end;
    if (overlaps) return null;
    if (p.end <= a.start) delta += chars(p.replacement).length - (p.end - p.start);
  }
  return {
    ...selection,
    target: { version: update.version, start: a.start + delta, end: a.end + delta },
  };
}
