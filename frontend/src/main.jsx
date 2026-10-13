import React, { useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { api, clientId, createDocumentStore, useDocument } from "./store";
import { chars, editKind, sourceOffset } from "./protocol";
import "./style.css";
import { InlineSuggestion } from "./InlineSuggestion";
const pathId = (id) => encodeURIComponent(id);

function SearchBox({ documentId, label, onPick }) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const request = useRef(0);
  useEffect(() => {
    request.current++;
    setResults(null);
    setBusy(false);
  }, [documentId]);
  async function search(event) {
    event.preventDefault();
    const current = ++request.current;
    setBusy(true);
    setError("");
    try {
      const data = await api("/search", {
        method: "POST",
        body: JSON.stringify({
          text: query,
          mode: documentId ? "literal" : "terms",
          document_ids: documentId ? [documentId] : null,
          limit: 40,
        }),
      });
      if (current === request.current) setResults(data.hits);
    } catch (error) {
      if (current === request.current) setError(error.message);
    } finally {
      if (current === request.current) setBusy(false);
    }
  }
  return (
    <div className="search-control">
      <form onSubmit={search}>
        <input
          aria-label={label}
          placeholder={label}
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
        <button disabled={busy || !query.trim()}>
          {busy ? "Searching…" : "Search"}
        </button>
      </form>
      {error && (
        <div role="alert" className="search-results">
          {error}
        </div>
      )}
      {results && (
        <div className="search-results">
          <div className="results-heading">
            <span>{results.length} results</span>
            <button
              aria-label={"Close " + label + " results"}
              onClick={() => setResults(null)}
            >
              ×
            </button>
          </div>
          {results.map((hit, index) => (
            <button
              className="search-result"
              key={index}
              onClick={() => {
                onPick(hit);
                setResults(null);
              }}
            >
              {!documentId && <strong>{hit.document_id}</strong>}
              <span>{hit.context}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
function App() {
  const [documents, setDocuments] = useState([]),
    [selected, setSelected] = useState(""),
    [hit, setHit] = useState(null),
    [error, setError] = useState("");
  useEffect(() => {
    api("/documents")
      .then((rows) => {
        setDocuments(rows);
        setSelected(rows[0]?.document_id || "");
      })
      .catch((error) => setError(error.message));
  }, []);
  function pick(result) {
    setSelected(result.document_id);
    setHit(result);
  }
  return (
    <main className="app">
      <div className="searches">
        <SearchBox label="Search all documents" onPick={pick} />
        {selected && (
          <SearchBox
            label="Search this document"
            documentId={selected}
            onPick={pick}
          />
        )}
      </div>
      {error && (
        <div role="alert" className="alert">
          {error}
        </div>
      )}
      {selected ? (
        <Workbench
          key={selected}
          id={selected}
          hit={hit}
          documentPicker={
            <select
              aria-label="Open document"
              value={selected}
              onChange={(event) => {
                setSelected(event.target.value);
                setHit(null);
              }}
            >
              {documents.map((doc) => (
                <option key={doc.document_id} value={doc.document_id}>
                  {doc.title || doc.document_id}
                </option>
              ))}
            </select>
          }
        />
      ) : (
        <p className="empty">
          {error
            ? "The document service is unavailable."
            : "No documents. Import a document to begin."}
        </p>
      )}
    </main>
  );
}
function Workbench({ id, documentPicker, hit }) {
  const store = useMemo(() => createDocumentStore(id), [id]);
  const state = useDocument(store);
  const { document: doc, connection } = state;
  const reservations =
    state.selection &&
    !state.reservations.some(
      (r) => r.reservation_id === state.selection.reservation_id,
    )
      ? [...state.reservations, state.selection]
      : state.reservations;
  const [active, setActive] = useState(null),
    [buffers, setBuffers] = useState({}),
    [notice, setNotice] = useState(""),
    [busy, setBusy] = useState(false);
  const bufferRef = useRef({}),
    editors = useRef(new Map()),
    selectionEpoch = useRef(0),
    acquisition = useRef(null),
    pendingTyping = useRef(null),
    scroll = useRef(null),
    alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    const stop = store.start();
    return () => {
      alive.current = false;
      selectionEpoch.current++;
      stop();
    };
  }, [store]);
  const edits = reservations.filter(
    (r) => r.status === "active" && r.reservation_id in buffers,
  );
  const mine = edits.filter(
    (r) => r.client_id === clientId && r.status === "active",
  );
  const source = useMemo(() => chars(doc?.text || ""), [doc?.text]);
  const activeReservation = reservations.find(
    (r) => r.reservation_id === active,
  );
  // Renew local work while this document is actively open and focused. The
  // server still revalidates every renewal; a lost lease never implies ownership.
  useEffect(() => {
    if (connection !== "live") return;
    let stopped = false;
    let running = false;
    const timer = setInterval(async () => {
      if (running || !document.hasFocus() || document.hidden) return;
      running = true;
      try {
        const current = store.getSnapshot();
        const ids = new Set([...Object.keys(bufferRef.current), active].filter(Boolean));
        for (const rid of ids) {
          const r = current.reservations.find((r) => r.reservation_id === rid)
            || (current.selection?.reservation_id === rid ? current.selection : null);
          if (!r || r.status !== "active" || r.client_id !== clientId) continue;
          try {
            const renewed = await store.perform(`/edit-reservations/${pathId(rid)}/renew`, { method: "POST" });
            if (!stopped) store.renewSelection(renewed);
          } catch (error) {
            if (!stopped) message(error);
          }
        }
      } finally { running = false; }
    }, 10000);
    return () => { stopped = true; clearInterval(timer); };
  }, [store, active, connection]);
  function message(e) {
    if (alive.current) setNotice(e.message || String(e));
  }
  function jump(rid) {
    setActive(rid);
    requestAnimationFrame(() =>
      document
        .getElementById("edit-" + rid)
        ?.scrollIntoView({ behavior: "smooth", block: "center" }),
    );
  }
  useEffect(() => {
    if (!hit || !doc) return;
    if (hit.target.version !== doc.version) {
      setNotice(
        "This search result refers to an earlier revision. Search again to locate current text.",
      );
      return;
    }
    requestAnimationFrame(() =>
      document
        .getElementById("search-match")
        ?.scrollIntoView({ behavior: "smooth", block: "center" }),
    );
  }, [hit, doc?.version]);
  // Keep local edits when switching selections; release empty selections.
  async function clearLease() {
    if (
      activeReservation?.expires_at &&
      activeReservation.status === "active" &&
      !(active in bufferRef.current)
    )
      await store.perform(`/edit-reservations/${pathId(active)}`, {
        method: "DELETE",
      });
  }
  async function selectText(event) {
    if (
      busy ||
      connection !== "live" ||
      event.target.closest("[data-suggestion],button")
    )
      return;
    const selection = window.getSelection();
    if (!selection?.rangeCount) return;
    const range = selection.getRangeAt(0);
    if (
      !event.currentTarget.contains(range.startContainer) ||
      !event.currentTarget.contains(range.endContainer)
    )
      return;
    const start = sourceOffset(range.startContainer, range.startOffset),
      end = sourceOffset(range.endContainer, range.endOffset);
    if (start === null || end === null) return;
    const epoch = ++selectionEpoch.current;
    pendingTyping.current = null;
    const acquiring = (async () => {
      await clearLease();
      return store.perform(`/documents/${pathId(id)}/edit-reservations`, {
        method: "POST",
        body: JSON.stringify({ targets: [{ version: doc.version, start, end }] }),
      });
    })();
    acquisition.current = acquiring;
    try {
      const r = (await acquiring).reservations[0];
      if (epoch !== selectionEpoch.current || !alive.current) {
        if (r.expires_at)
          await store.perform(
            `/edit-reservations/${pathId(r.reservation_id)}`,
            {
              method: "DELETE",
            },
          );
        return;
      }
      store.select(r);
      setActive(r.reservation_id);
      setNotice("");
      requestAnimationFrame(() => {
        const element = editors.current.get(r.reservation_id);
        if (element && document.activeElement !== element) {
          element.focus({ preventScroll: true });
          const caret = document.createRange();
          caret.selectNodeContents(element);
          caret.collapse(false);
          const selection = window.getSelection();
          selection.removeAllRanges();
          selection.addRange(caret);
        }
      });
      if (pendingTyping.current !== null) {
        update(r, pendingTyping.current);
        pendingTyping.current = null;
      }
    } catch (e) {
      message(e);
    } finally {
      if (acquisition.current === acquiring) acquisition.current = null;
    }
  }
  // Buffer keystrokes made while reservation acquisition is in flight.
  function sourceKeyDown(event) {
    if (
      event.target.closest("[data-suggestion]") ||
      event.metaKey ||
      event.ctrlKey ||
      event.altKey ||
      event.nativeEvent.isComposing
    )
      return;
    if (!acquisition.current) return;
    if (Array.from(event.key).length === 1 || event.key === "Enter") {
      event.preventDefault();
      pendingTyping.current =
        (pendingTyping.current ?? "") +
        (event.key === "Enter" ? "\n" : event.key);
    } else if (["Backspace", "Delete"].includes(event.key)) {
      event.preventDefault();
      pendingTyping.current = Array.from(pendingTyping.current ?? "")
        .slice(0, -1)
        .join("");
    }
  }
  useEffect(() => {
    if (activeReservation && activeReservation.status === "active")
      requestAnimationFrame(() => editors.current.get(active)?.focus());
  }, [active, !!activeReservation]);
  function update(r, text) {
    const rid = r.reservation_id;
    bufferRef.current = { ...bufferRef.current, [rid]: text };
    setBuffers(bufferRef.current);
  }
  async function discard(r) {
    setBusy(true);
    try {
      await store.perform(`/edit-reservations/${pathId(r.reservation_id)}`, {
        method: "DELETE",
      });
      delete bufferRef.current[r.reservation_id];
      setBuffers({ ...bufferRef.current });
      store.clearSelection();
      setActive(null);
      setNotice("Edit discarded.");
    } catch (e) {
      message(e);
    } finally {
      setBusy(false);
    }
  }
  async function submit() {
    setBusy(true);
    setNotice("");
    try {
      const replacements = mine.map((r) => ({
        reservation_id: r.reservation_id,
        replacement: bufferRef.current[r.reservation_id],
      }));
      if (replacements.length) {
        const result = await store.perform(
          `/documents/${pathId(id)}/replacements`,
          { method: "POST", body: JSON.stringify({ replacements }) },
        );
        for (const r of mine) delete bufferRef.current[r.reservation_id];
        setBuffers({ ...bufferRef.current });
        setActive(null);
        setNotice(result.status === "pending" ? "Edits accepted. Waiting for document updates." : "Edits applied.");
      }
    } catch (e) {
      message(e);
    } finally {
      setBusy(false);
    }
  }
  const boundaries = new Set([0, source.length]);
  for (const r of reservations) {
    boundaries.add(r.target.start);
    boundaries.add(r.target.end);
  }
  if (hit && doc && hit.target.version === doc.version) {
    boundaries.add(hit.target.start);
    boundaries.add(hit.target.end);
  }
  const points = [...boundaries].sort((a, b) => a - b);
  function editor(r) {
    const rid = r.reservation_id,
      value = buffers[rid] ?? "";
    const hasDraft = rid in buffers;
    return (
      <InlineSuggestion
        key={rid}
        id={"edit-" + rid}
        value={value}
        kind={editKind(r.target, value)}
        hasDraft={hasDraft}
        editable={
          r.client_id === clientId &&
          r.status === "active" &&
          !busy &&
          connection === "live"
        }
        active={active === rid}
        onActivate={() => setActive(rid)}
        register={(element) => {
          if (element) editors.current.set(rid, element);
          else editors.current.delete(rid);
        }}
        onChange={(text) => update(r, text)}
        onDiscard={() => discard(r)}
      />
    );
  }
  return (
    <>
      <header className="document-bar">
        {documentPicker}
        <span className="revision">{doc ? `v${doc.version}` : "Loading…"}</span>
        {connection !== "live" && (
          <span className="connection">Reconnecting…</span>
        )}
        <button
          className="submit"
          onClick={submit}
          disabled={!mine.length || busy || connection !== "live"}
        >
          {busy
            ? "Working…"
            : `Submit edits${mine.length ? ` (${mine.length})` : ""}`}
        </button>
      </header>
      {(notice ||
        state.error ||
        state.operationNotice ||
        state.selectionNotice) && (
        <div role="status" className="alert">
          {state.error ||
            state.operationNotice ||
            notice ||
            state.selectionNotice}
          <button
            aria-label="Dismiss notification"
            onClick={() => {
              setNotice("");
              store.dismiss();
            }}
          >
            ×
          </button>
        </div>
      )}
      <div className="reader-layout">
        <div className="reader-scroll" ref={scroll}>
          <article className="paper">
            {!doc ? (
              <p>Connecting to the document…</p>
            ) : (
              <div
                className="source"
                onMouseUp={selectText}
                onKeyDown={sourceKeyDown}
                onPaste={(event) => {
                  if (
                    !event.target.closest("[data-suggestion]") &&
                    acquisition.current
                  ) {
                    event.preventDefault();
                    pendingTyping.current =
                      (pendingTyping.current ?? "") +
                      event.clipboardData.getData("text/plain");
                  }
                }}
                onKeyUp={(e) => {
                  if (e.key === "Shift") selectText(e);
                }}
                tabIndex={0}
                aria-label="Document text. Select a range to edit."
              >
                {points.flatMap((start, i) => {
                  const end = points[i + 1];
                  const ending = reservations.filter(
                    (r) => r.target.end === start,
                  );
                  const covering = reservations.find(
                    (r) =>
                      r.target.start <= start &&
                      r.target.end >= end &&
                      r.target.start !== r.target.end,
                  );
                  const hitHere =
                    hit?.target.version === doc.version &&
                    start === hit.target.start;
                  return [
                    ...ending
                      .filter(
                        (r) =>
                          active === r.reservation_id ||
                          r.reservation_id in buffers,
                      )
                      .map(editor),
                    end !== undefined ? (
                      <span
                        key={"source-" + start}
                        data-start={start}
                        id={hitHere ? "search-match" : undefined}
                        className={
                          (covering
                            ? "protected " +
                              (covering.reservation_id in buffers
                                ? "suggested-source " +
                                  editKind(
                                    covering.target,
                                    buffers[covering.reservation_id],
                                  )
                                : covering.client_id === clientId
                                  ? "owned"
                                  : "other")
                            : "") + (hitHere ? " search-match" : "")
                        }
                        title={
                          covering
                            ? "Protected by " +
                              (covering.client_id === clientId
                                ? "you"
                                : covering.client_id.slice(0, 8))
                            : undefined
                        }
                      >
                        {source.slice(start, end).join("")}
                      </span>
                    ) : null,
                  ];
                })}
              </div>
            )}
          </article>
        </div>
        <aside className="change-rail" aria-label="Draft change overview">
          <div className="rail-track">
            {edits.map((r) => {
              const value =
                buffers[r.reservation_id];
              return (
                <button
                  key={r.reservation_id}
                  className={"rail-mark " + editKind(r.target, value)}
                  style={{
                    top: `${(r.target.start / Math.max(source.length, 1)) * 94}%`,
                  }}
                  onClick={() => jump(r.reservation_id)}
                  aria-label={`${editKind(r.target, value)} at character ${r.target.start}`}
                  title={`${source.slice(r.target.start, r.target.end).join("").slice(0, 80) || "(insertion)"} → ${value.slice(0, 80) || "(deleted)"}`}
                />
              );
            })}
          </div>
        </aside>
      </div>
    </>
  );
}
createRoot(document.getElementById("root")).render(<App />);
