import { useSyncExternalStore } from "react";
import { applyState } from "./protocol.js";
const key = "sandstone-client";
export const clientId = sessionStorage.getItem(key) || crypto.randomUUID();
sessionStorage.setItem(key, clientId);
export async function api(path, options = {}) {
  const response = await fetch("/api" + path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      "X-Client-ID": clientId,
      ...options.headers,
    },
  });
  if (response.status === 204) return null;
  const body = await response.json();
  if (!response.ok)
    throw new Error(body.error || `Request failed (${response.status})`);
  return body;
}
const initial = {
  document: null,
  reservations: [],
  operations: [],
  operationNotice: "",
  connection: "connecting",
  error: "",
  selection: null,
  selectionNotice: "",
};
export function createDocumentStore(id) {
  let state = initial,
    socket,
    retry,
    heartbeat,
    stopped = false;
  const listeners = new Set();
  const submitted = new Set();
  function operationNotice(operations) {
    const own = operations.filter((op) => submitted.has(op.reservation_id));
    const failed = own.find((op) => op.status === "failed");
    if (failed) return failed.failure?.error || "An edit failed. Select the passage again.";
    if (!submitted.size) return "";
    return own.length === submitted.size && own.every((op) => op.status === "completed")
      ? "Edits applied."
      : "Edits queued. Waiting for document updates.";
  }
  const set = (patch) => {
    state = { ...state, ...patch };
    listeners.forEach((fn) => fn());
  };
  function connect() {
    if (stopped) return;
    set({ connection: "connecting" });
    const params = new URLSearchParams({ client_id: clientId });
    if (state.document) params.set("after_version", state.document.version);
    if (state.selection) params.set("retain_history", "1");
    socket = new WebSocket(
      `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/api/documents/${encodeURIComponent(id)}/updates?${params}`,
    );
    const current = socket;
    socket.onopen = () => {
      if (current !== socket || stopped) return;
      clearInterval(heartbeat);
      heartbeat = setInterval(() => {
        if (current.readyState === WebSocket.OPEN)
          current.send(JSON.stringify({ type: "heartbeat" }));
      }, 5000);
    };
    socket.onmessage = (event) => {
      if (current !== socket || stopped) return;
      try {
        const next = applyState(state, JSON.parse(event.data));
        set({
          ...next,
          operationNotice: operationNotice(next.operations),
          connection: "live",
          error: "",
        });
      } catch (error) {
        set({ error: error.message, connection: "reconnecting" });
        socket.close();
      }
    };
    socket.onclose = () => {
      if (current !== socket) return;
      clearInterval(heartbeat);
      if (!stopped) {
        set({ connection: "reconnecting" });
        retry = setTimeout(connect, 1500);
      }
    };
    socket.onerror = () => current.close();
  }
  return {
    async perform(path, options) {
      if (state.connection !== "live") throw new Error("Wait for the document subscription to reconnect.");
      // Successful replacement responses contain their exact committed revision.
      // Keep live document state on the subscription: newer edits may already exist.
      const result = await api(path, options);
      if (result?.status === "pending") {
        const payload = JSON.parse(options.body);
        const items = payload.replacements || [payload];
        for (const item of items) if (item.reservation_id) submitted.add(item.reservation_id);
        set({
          operationNotice: operationNotice(state.operations),
          selection: null,
          reservations: state.reservations.map((r) => submitted.has(r.reservation_id)
            ? { ...r, status: "submitted", expires_at: null } : r),
        });
      }
      return result;
    },
    subscribe(fn) {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
    getSnapshot: () => state,
    start() {
      const leave = () => {
        stopped = true;
        clearTimeout(retry);
        clearInterval(heartbeat);
        socket?.close(1000, "page-left");
      };
      const resume = (event) => {
        if (event.persisted && stopped) {
          stopped = false;
          connect();
        }
      };
      stopped = false;
      globalThis.addEventListener?.("pagehide", leave);
      globalThis.addEventListener?.("pageshow", resume);
      connect();
      return () => {
        globalThis.removeEventListener?.("pagehide", leave);
        globalThis.removeEventListener?.("pageshow", resume);
        leave();
      };
    },
    select(reservation) {
      set({ selection: reservation, selectionNotice: "" });
    },
    renewSelection(reservation) {
      // HTTP can arrive after a new selection or a subscribed document patch.
      // A renewal must never resurrect or move an obsolete highlight.
      if (state.selection?.reservation_id === reservation.reservation_id &&
          state.document?.version === reservation.target.version) {
        set({ selection: reservation });
      }
    },
    clearSelection() {
      set({ selection: null });
    },
    dismiss() {
      set({ selectionNotice: "" });
    },
    accept(rid) {
      submitted.add(rid);
      set({
        operationNotice: operationNotice(state.operations),
        selection:
          state.selection?.reservation_id === rid ? null : state.selection,
        reservations: state.reservations.map((r) =>
          r.reservation_id === rid ? { ...r, status: "submitted" } : r,
        ),
      });
    },
    refresh() {
      socket?.close();
    },
  };
}
export const useDocument = (store) =>
  useSyncExternalStore(store.subscribe, store.getSnapshot);
