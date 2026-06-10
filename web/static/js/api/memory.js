import { state } from "../stores/state.js";
import { fetchJson } from "./client.js";

export async function loadMemory(limit = 12, query = "", layer = "", mode = "") {
  const params = new URLSearchParams({ limit: String(limit) });
  if (query) params.set("query", query);
  if (layer) params.set("layer", layer);
  if (mode) params.set("mode", mode);
  const data = await fetchJson(`/api/memory?${params.toString()}`);
  state.memories = data.items || [];
  state.memoryMode = data.mode || mode || state.currentConfig.dialog_mode || "local";
  return state.memories;
}

export async function addMemory(value, mode = "") {
  return fetchJson("/api/memory", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ value, layer: "mid", mode }),
  });
}

export async function deleteMemory(id) {
  return fetchJson(`/api/memory/${encodeURIComponent(id)}`, { method: "DELETE" });
}
