import React, { useLayoutEffect, useRef } from "react";

// React owns the value; the browser owns the caret and composition while typing.
// Avoid rewriting an unchanged DOM value on subscription updates.
export function InlineSuggestion({
  value,
  editable,
  active,
  kind,
  hasDraft,
  onChange,
  onDiscard,
  onActivate,
  register,
  id,
}) {
  const ref = useRef(null);
  const composing = useRef(false);
  useLayoutEffect(() => {
    const element = ref.current;
    if (element && !composing.current && element.textContent !== value) {
      element.textContent = value;
      if (document.activeElement === element) placeCaret(element);
    }
  }, [value]);
  useLayoutEffect(() => {
    if (active && editable) {
      ref.current.focus({ preventScroll: true });
      placeCaret(ref.current);
    }
  }, [active, editable]);
  function insert(text) {
    const element = ref.current;
    const selection = window.getSelection();
    if (!selection?.rangeCount || !element.contains(selection.anchorNode))
      return;
    const range = selection.getRangeAt(0);
    range.deleteContents();
    const node = document.createTextNode(text);
    range.insertNode(node);
    range.setStartAfter(node);
    range.collapse(true);
    selection.removeAllRanges();
    selection.addRange(range);
    onChange(element.textContent);
  }
  return (
    <span
      ref={(element) => {
        ref.current = element;
        register(element);
      }}
      id={id}
      data-suggestion="true"
      className={`suggestion ${hasDraft ? kind : "lease"} ${!value ? "empty-suggestion" : ""}`}
      contentEditable={editable ? "plaintext-only" : false}
      suppressContentEditableWarning
      role={editable ? "textbox" : undefined}
      aria-label={editable ? "Suggested replacement" : undefined}
      aria-multiline={editable ? true : undefined}
      tabIndex={editable ? 0 : undefined}
      title={
        editable
          ? "Type to suggest a change. Escape discards this suggestion."
          : "Another reviewer’s suggestion"
      }
      onFocus={onActivate}
      onCompositionStart={() => {
        composing.current = true;
      }}
      onCompositionEnd={(event) => {
        composing.current = false;
        onChange(event.currentTarget.textContent);
      }}
      onInput={(event) => {
        const text = event.currentTarget.textContent;
        if (event.nativeEvent.inputType === "historyUndo" && !text) onDiscard();
        else onChange(text);
      }}
      onPaste={(event) => {
        event.preventDefault();
        insert(event.clipboardData.getData("text/plain"));
      }}
      onKeyDown={(event) => {
        event.stopPropagation();
        if (
          (event.metaKey || event.ctrlKey) &&
          event.key.toLowerCase() === "z" &&
          !event.shiftKey &&
          !value &&
          hasDraft
        ) {
          event.preventDefault();
          onDiscard();
        } else if (event.key === "Escape") {
          event.preventDefault();
          onDiscard();
        } else if (event.key === "Enter" && !event.nativeEvent.isComposing) {
          event.preventDefault();
          insert("\n");
        } else if (!value && ["Backspace", "Delete"].includes(event.key)) {
          event.preventDefault();
          onChange("");
        }
      }}
    />
  );
}
function placeCaret(element) {
  const range = document.createRange();
  range.selectNodeContents(element);
  range.collapse(false);
  const selection = window.getSelection();
  selection.removeAllRanges();
  selection.addRange(range);
}
