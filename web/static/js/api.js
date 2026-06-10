/* ============================================================
   api.js — All backend API calls
   BranchWhisper
   ============================================================ */

import { state, DEFAULT_CONFIG } from "./state.js";

/* ---- generic fetch wrapper ---- */

export async function fetchJson(url, options = {}) {
  const resp = await fetch(url, options);
  const text = await resp.text();
  let data = {};
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = { detail: text };
    }
  }
  if (!resp.ok) {
    const detail = data.detail || data.error || `HTTP ${resp.status}`;
    const error = new Error(detail);
    error.status = resp.status;
    error.payload = data;
    throw error;
  }
  return data;
}

/* ---- config ---- */

export async function loadConfig() {
  try {
    const config = await fetchJson("/api/config");
    state.previewMode = false;
    state.currentConfig = { ...DEFAULT_CONFIG, ...config };
    state.ttsSampleRate = Number(config.tts_sample_rate || 24000);
    applyUiFontScale(state.currentConfig.ui_font_scale);
    return { ok: true, config: state.currentConfig };
  } catch (error) {
    state.previewMode = true;
    state.currentConfig = { ...DEFAULT_CONFIG };
    state.ttsSampleRate = DEFAULT_CONFIG.tts_sample_rate;
    applyUiFontScale(DEFAULT_CONFIG.ui_font_scale);
    return { ok: false, error, config: state.currentConfig };
  }
}

export function applyUiFontScale(value) {
  const scale = Number(value);
  const safeScale = Number.isFinite(scale) ? Math.max(0.85, Math.min(1.35, scale)) : 1;
  document.documentElement.style.setProperty("--ui-font-scale", String(safeScale));
}

export async function saveConfig(configData) {
  return fetchJson("/api/config", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(configData),
  });
}

export async function loadToolConfig() {
  const data = await fetchJson("/api/config/tools");
  state.toolConfig = data.tools || {};
  return state.toolConfig;
}

export async function saveToolConfig(tools) {
  const data = await fetchJson("/api/config/tools", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(tools),
  });
  state.toolConfig = data.tools || {};
  return state.toolConfig;
}

export async function listModelFiles(root = "", query = "") {
  const params = new URLSearchParams();
  if (root) params.set("root", root);
  if (query) params.set("query", query);
  return fetchJson(`/api/files/models?${params.toString()}`);
}

export async function loadBotProfiles() {
  const data = await fetchJson("/api/bot-profiles");
  state.botProfiles = data.profiles || [];
  return state.botProfiles;
}

export async function createBotProfile(profile) {
  const data = await fetchJson("/api/bot-profiles", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(profile),
  });
  state.botProfiles = data.profiles || state.botProfiles;
  return data.profile;
}

export async function updateBotProfile(id, profile) {
  const data = await fetchJson(`/api/bot-profiles/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(profile),
  });
  state.botProfiles = data.profiles || state.botProfiles;
  return data.profile;
}

export async function deleteBotProfile(id) {
  const data = await fetchJson(`/api/bot-profiles/${encodeURIComponent(id)}`, { method: "DELETE" });
  state.botProfiles = data.profiles || state.botProfiles;
  return data.ok;
}

export async function uploadAvatar(dataUrl) {
  return fetchJson("/api/assets/avatar", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ data_url: dataUrl }),
  });
}

export async function uploadChatImage(dataUrl) {
  return fetchJson("/api/assets/chat-image", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ data_url: dataUrl }),
  });
}

export async function loadStickers() {
  const data = await fetchJson("/api/stickers");
  state.stickers = data.stickers || [];
  return state.stickers;
}

export async function uploadSticker(dataUrl, tag = "默认", name = "") {
  const data = await fetchJson("/api/stickers", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ data_url: dataUrl, tag, name }),
  });
  state.stickers = data.stickers || state.stickers || [];
  return data.sticker;
}

export async function deleteSticker(stickerId) {
  const data = await fetchJson(`/api/stickers/${encodeURIComponent(stickerId)}`, { method: "DELETE" });
  state.stickers = data.stickers || state.stickers || [];
  return data.ok;
}

export async function resolveTool(text) {
  return fetchJson("/api/tools/resolve", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
}

export async function testTool(tool, argumentsData = {}) {
  return fetchJson("/api/tools/test", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ tool, arguments: argumentsData }),
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

export async function loadSystemResources() {
  try {
    const data = await fetchJson("/api/system/resources");
    state.systemResources = data;
    return { ok: true, resources: data };
  } catch {
    state.systemResources = null;
    return { ok: false, resources: null };
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

/* ---- integrations ---- */

export async function loadIntegrations() {
  try {
    const data = await fetchJson("/api/integrations");
    state.integrations = data.integrations || [];
    state.integrationEnv = data.environment || null;
    if (!state.integrations.some((item) => item.id === state.selectedIntegrationId)) {
      state.selectedIntegrationId = state.integrations[0]?.id || "weixin_personal";
    }
    return { ok: true, integrations: state.integrations, environment: state.integrationEnv };
  } catch (error) {
    state.integrationEnv = null;
    return { ok: false, error, integrations: state.integrations, environment: null };
  }
}

export async function createIntegration(data) {
  return syncIntegrations(await fetchJson("/api/integrations", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  }));
}

export async function updateIntegration(id, data) {
  return syncIntegrations(await fetchJson(`/api/integrations/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  }));
}

export async function deleteIntegration(id) {
  return syncIntegrations(await fetchJson(`/api/integrations/${encodeURIComponent(id)}`, { method: "DELETE" }));
}

export async function startIntegration(id) {
  return syncIntegrations(await fetchJson(`/api/integrations/${encodeURIComponent(id)}/start`, { method: "POST" }));
}

export async function stopIntegration(id) {
  return syncIntegrations(await fetchJson(`/api/integrations/${encodeURIComponent(id)}/stop`, { method: "POST" }));
}

export async function restartIntegration(id) {
  return syncIntegrations(await fetchJson(`/api/integrations/${encodeURIComponent(id)}/restart`, { method: "POST" }));
}

export async function loginIntegration(id) {
  return syncIntegrations(await fetchJson(`/api/integrations/${encodeURIComponent(id)}/login`, { method: "POST" }));
}

export async function startIntegrationQrLogin(id, force = false) {
  return syncIntegrations(await fetchJson(`/api/integrations/${encodeURIComponent(id)}/login/qr`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ force }),
  }));
}

export async function pollIntegrationQrLogin(id, verifyCode = "") {
  return syncIntegrations(await fetchJson(`/api/integrations/${encodeURIComponent(id)}/login/poll`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ verify_code: verifyCode }),
  }));
}

export async function installIntegration(id) {
  return syncIntegrations(await fetchJson(`/api/integrations/${encodeURIComponent(id)}/install`, { method: "POST" }));
}

export async function startIntegrationBridge(id, branchwhisperUrl = "") {
  const body = branchwhisperUrl ? { branchwhisper_url: branchwhisperUrl } : {};
  return syncIntegrations(await fetchJson(`/api/integrations/${encodeURIComponent(id)}/bridge/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }));
}

export async function fetchIntegrationLogs(id, scope = "all") {
  const data = await fetchJson(`/api/integrations/${encodeURIComponent(id)}/logs?max_bytes=64000&scope=${encodeURIComponent(scope)}`);
  return data.logs || "";
}

export async function clearIntegrationLogs(id) {
  return fetchJson(`/api/integrations/${encodeURIComponent(id)}/logs`, { method: "DELETE" });
}

export async function testIntegrationDialog(id, text) {
  return fetchJson("/api/integrations/dialog", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      platform_id: id,
      session_id: "web_probe",
      sender_id: "integration_console",
      text,
    }),
  });
}

function syncIntegrations(data) {
  state.integrations = data.integrations || state.integrations;
  state.integrationEnv = data.environment || state.integrationEnv;
  return data;
}

/* ---- conversations ---- */

export async function loadConversations() {
  const params = new URLSearchParams();
  if (state.conversationFilter) params.set("query", state.conversationFilter);
  if (state.conversationArchivedMode) params.set("archived", state.conversationArchivedMode);
  const data = await fetchJson(`/api/conversations${params.toString() ? `?${params}` : ""}`);
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

export async function updateConversation(conversationId, data) {
  const result = await fetchJson(`/api/conversations/${encodeURIComponent(conversationId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  state.conversations = result.conversations || state.conversations;
  return result.conversation;
}

export function conversationExportUrl(conversationId) {
  return `/api/conversations/${encodeURIComponent(conversationId)}/export.md`;
}

/* ---- memory ---- */

export async function loadMemory(limit = 12, query = "", layer = "") {
  const params = new URLSearchParams({ limit: String(limit) });
  if (query) params.set("query", query);
  if (layer) params.set("layer", layer);
  const data = await fetchJson(`/api/memory?${params.toString()}`);
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

/* ---- reminders ---- */

export async function loadReminders() {
  const data = await fetchJson("/api/reminders");
  state.reminders = data.reminders || [];
  return state.reminders;
}

export async function createReminder(data) {
  const result = await fetchJson("/api/reminders", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  state.reminders = result.reminders || state.reminders;
  return result.reminder;
}

export async function deleteReminder(id) {
  const result = await fetchJson(`/api/reminders/${encodeURIComponent(id)}`, { method: "DELETE" });
  state.reminders = result.reminders || state.reminders;
  return result.ok;
}

/* ---- proactive ---- */

export async function loadProactiveConfig() {
  const data = await fetchJson("/api/proactive/config");
  state.proactiveConfig = data.config || {};
  return state.proactiveConfig;
}

export async function saveProactiveConfig(config) {
  const data = await fetchJson("/api/proactive/config", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(config),
  });
  state.proactiveConfig = data.config || {};
  return state.proactiveConfig;
}

export async function loadProactiveEvents() {
  const data = await fetchJson("/api/proactive/events");
  state.proactiveEvents = data.events || [];
  return state.proactiveEvents;
}

export async function testProactiveMessage(content = "") {
  const data = await fetchJson("/api/proactive/test", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content }),
  });
  state.proactiveEvents = data.events || state.proactiveEvents;
  return data.event;
}

export async function dismissProactiveEvent(id) {
  const data = await fetchJson(`/api/proactive/events/${encodeURIComponent(id)}/dismiss`, { method: "POST" });
  state.proactiveEvents = data.events || state.proactiveEvents;
  return data.ok;
}
