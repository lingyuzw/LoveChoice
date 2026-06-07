/* ============================================================
   api.js — All backend API calls
   LoveChoice Voice Console
   ============================================================ */

import { state, DEFAULT_CONFIG } from "./state.js";

/* ---- generic fetch wrapper ---- */

export async function fetchJson(url, options = {}) {
  const resp = await fetch(url, options);
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}

/* ---- config ---- */

export async function loadConfig() {
  try {
    const config = await fetchJson("/api/config");
    state.previewMode = false;
    state.currentConfig = { ...DEFAULT_CONFIG, ...config };
    state.ttsSampleRate = Number(config.tts_sample_rate || 24000);
    return { ok: true, config: state.currentConfig };
  } catch (error) {
    state.previewMode = true;
    state.currentConfig = { ...DEFAULT_CONFIG };
    state.ttsSampleRate = DEFAULT_CONFIG.tts_sample_rate;
    return { ok: false, error, config: state.currentConfig };
  }
}

export async function saveConfig(configData) {
  return fetchJson("/api/config", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(configData),
  });
}

/* ---- services ---- */

export async function loadServices() {
  try {
    const data = await fetchJson("/api/services");
    state.previewMode = false;
    state.services = data.services || [];
    return { ok: true, services: state.services };
  } catch {
    state.previewMode = true;
    return { ok: false, services: state.services };
  }
}

export async function startService(serviceId) {
  return fetchJson(`/api/services/${serviceId}/start`, { method: "POST" });
}

export async function stopService(serviceId) {
  return fetchJson(`/api/services/${serviceId}/stop`, { method: "POST" });
}

export async function startAllServices() {
  return fetchJson("/api/services/start-all", { method: "POST" });
}

export async function stopAllServices() {
  return fetchJson("/api/services/stop-all", { method: "POST" });
}

export async function updateServiceConfig(serviceId, data) {
  return fetchJson(`/api/services/${serviceId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
}

export async function fetchServiceLogs(serviceId) {
  const data = await fetchJson(`/api/services/${serviceId}/logs?max_bytes=36000`);
  return data.logs || "";
}

export async function clearServiceLogs(serviceId) {
  return fetchJson(`/api/services/${encodeURIComponent(serviceId)}/logs`, { method: "DELETE" });
}

export async function clearAllServiceLogs() {
  return fetchJson("/api/services/logs", { method: "DELETE" });
}

/* ---- conversations ---- */

export async function loadConversations() {
  const data = await fetchJson("/api/conversations");
  state.conversations = data.conversations || [];
  return state.conversations;
}

export async function createConversation() {
  const data = await fetchJson("/api/conversations", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({}),
  });
  return data.conversation;
}

export async function deleteConversation(conversationId) {
  return fetchJson(`/api/conversations/${encodeURIComponent(conversationId)}`, { method: "DELETE" });
}

/* ---- memory ---- */

export async function loadMemory() {
  const data = await fetchJson("/api/memory?limit=12");
  state.memories = data.items || [];
  return state.memories;
}

export async function addMemory(value) {
  return fetchJson("/api/memory", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ value, layer: "mid" }),
  });
}

export async function deleteMemory(id) {
  return fetchJson(`/api/memory/${encodeURIComponent(id)}`, { method: "DELETE" });
}
