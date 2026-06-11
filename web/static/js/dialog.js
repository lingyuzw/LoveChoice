/* ============================================================
   dialog.js — WebSocket connection, dialog events, text send
   BranchWhisper · Precision Console
   ============================================================ */

import { state, PIPELINE_STEPS, ACTIVE_CONVERSATION_KEY } from "./state.js";
import { $, setText, showToast } from "./utils.js";
import { loadConversation, loadConversations } from "./api.js";
import { stopAssistantAudio, schedulePcm16, releaseAfterPlayback } from "./audio.js";

let onTranscriptUpdated = null;
let onPipelineUpdate = null;
let appearanceEventsBound = false;
let configEventsBound = false;
let lastRenderedTranscriptKey = "";
let transcriptBatchRendering = false;

export function setTranscriptCallback(fn) { onTranscriptUpdated = fn; }
export function setPipelineUpdater(fn) { onPipelineUpdate = fn; }

export function bindAppearanceRefresh() {
  if (appearanceEventsBound) return;
  appearanceEventsBound = true;
  window.addEventListener("branchwhisper:appearance-updated", () => {
    lastRenderedTranscriptKey = "";
    if (state.activeConversation) renderTranscript(state.activeConversation.messages || []);
  });
  bindRuntimeConfigRefresh();
}

export function bindRuntimeConfigRefresh() {
  if (configEventsBound) return;
  configEventsBound = true;
  window.addEventListener("branchwhisper:config-updated", (event) => {
    const detail = event.detail || {};
    state.currentConfig = { ...state.currentConfig, ...(detail.config || {}) };
    state.ttsEnabled = state.currentConfig.tts_enabled ?? state.ttsEnabled;
    if (detail.reconnectDialog) {
      reconnectDialog();
      return;
    }
    sendRuntimeSettings();
  });
}

/* ---- WebSocket ---- */

export function connectSocket() {
  if (state.ws && state.ws.readyState <= WebSocket.OPEN) return;
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const query = state.activeConversationId ? `?conversation_id=${encodeURIComponent(state.activeConversationId)}` : "";
  const ws = new WebSocket(`${scheme}://${location.host}/ws/dialog${query}`);
  state.ws = ws; ws.binaryType = "arraybuffer";

  ws.addEventListener("open", () => { state.connected = true; setText("topStatus", state.micActive ? "监听中" : "待机"); sendRuntimeSettings(); });
  ws.addEventListener("message", handleSocketMessage);
  ws.addEventListener("close", () => {
    state.connected = false; stopAssistantAudio();
    state.busy = false; state.assistantActive = false; state.interrupting = false;
    setText("topStatus", "断开");
    if (!state.previewMode && !state.manualSocketClose) { window.clearTimeout(state.reconnectTimer); state.reconnectTimer = window.setTimeout(connectSocket, 1200); }
  });
  ws.addEventListener("error", () => setText("topStatus", state.previewMode ? "预览" : "连接异常"));
}

export function reconnectDialog() {
  state.manualSocketClose = true; window.clearTimeout(state.reconnectTimer);
  if (state.ws && state.ws.readyState <= WebSocket.OPEN) state.ws.close();
  state.ws = null; state.connected = false; state.currentAssistant = null;
  window.setTimeout(() => { state.manualSocketClose = false; connectSocket(); }, 120);
}

function sendRuntimeSettings() {
  if (!state.ws || state.ws.readyState !== WebSocket.OPEN) return;
  const settings = { ...state.currentConfig };
  delete settings.llm_api_key;
  delete settings.api_llm_api_key;
  settings.tts_enabled = state.ttsEnabled;
  state.ws.send(JSON.stringify({ type: "settings", settings }));
}

/* ---- incoming messages ---- */

async function handleSocketMessage(event) {
  if (event.data instanceof ArrayBuffer) { schedulePcm16(event.data); return; }
  if (event.data instanceof Blob) { schedulePcm16(await event.data.arrayBuffer()); return; }
  let data; try { data = JSON.parse(event.data); } catch { return; }
  handleDialogEvent(data);
}

function handleDialogEvent(data) {
  switch (data.type) {
    case "ready": setText("topStatus", "待机"); pipeline("idle"); break;
    case "conversation": applyConversation(data.conversation, true); break;
    case "conversation_saved": applyConversation(data.conversation, false); break;
    case "settings": state.currentConfig = { ...state.currentConfig, ...(data.settings || {}) }; state.ttsEnabled = state.currentConfig.tts_enabled ?? true; break;
    case "trace": handleTrace(data); break;
    case "status": handleStatus(data); break;
    case "vad_start": state.busy = false; setText("vadLabel", "speech"); setText("topStatus", "收音"); pipeline("vad", "正在听"); break;
    case "vad_end": setText("vadLabel", `${data.duration_ms || 0}ms`); setText("topStatus", "识别"); pipeline("asr", "识别中"); state.busy = true; break;
    case "vad_short": setText("vadLabel", "short"); break;
    case "user": addMsg("user", data.text || "", { attachments: data.attachments || [] }); state.currentAssistant = null; pipeline("llm", "思考中"); break;
    case "assistant_start":
      window.clearTimeout(state.releaseTimer); state.assistantActive = true;
      state.interrupting = false; state.dropAudioUntilNextAssistant = false;
      state.currentAssistant = addMsg("assistant", ""); setText("topStatus", "生成"); pipeline("llm", "生成中"); break;
    case "llm_delta": pipeline("llm", "输出中"); appendAssistant(data.text || ""); break;
    case "assistant_attachment": appendAssistantAttachments(data.attachments || []); break;
    case "audio_format": state.ttsSampleRate = Number(data.sample_rate || 24000); setText("topStatus", "播放"); pipeline("tts", "播放中"); break;
    case "metric": setMetric(data.name, data.value); break;
    case "error": showToast(data.message || "出错", "error"); pipeline("error"); break;
    case "busy": setText("topStatus", "忙碌"); break;
    case "interrupted":
      stopAssistantAudio(); state.busy = false; state.assistantActive = false;
      state.interrupting = false; state.dropAudioUntilNextAssistant = false;
      setText("topStatus", state.micActive ? "监听中" : "待机"); pipeline(state.micActive ? "vad" : "idle", "已打断"); break;
    case "reset":
      stopAssistantAudio(); state.busy = false; state.assistantActive = false;
      state.interrupting = false; state.dropAudioUntilNextAssistant = false;
      applyConversation(data.conversation, true); setText("topStatus", "待机"); pipeline("idle"); break;
    case "turn_done":
      pipeline("done", "完成");
      releaseAfterPlayback({ onReleased: () => { setText("topStatus", state.micActive ? "监听中" : "待机"); } }); break;
    default: break;
  }
}

function handleTrace(data) {
  state.currentTraceId = data.trace_id || "";
  setText("traceMetric", state.currentTraceId ? state.currentTraceId.slice(-10) : "--");
}

function handleStatus(data) {
  if (data.trace_id && data.trace_id !== state.currentTraceId) handleTrace(data);
  const stage = data.stage || "idle";
  const label = statusLabel(data.label || data.status || "");
  if (stage === "vad" && data.device) setText("vadLabel", String(data.device));
  setText("topStatus", label || stage.toUpperCase());
  pipeline(stage, label);
}

function statusLabel(label) {
  const text = String(label || "");
  return {
    loading: "加载中",
    ready: "就绪",
    running: "运行中",
    warming: "预热中",
  }[text] || text;
}

function pipeline(stage = "idle", label = "") {
  if (onPipelineUpdate) onPipelineUpdate(stage, label);
}

function setMetric(name, value) {
  if (state.currentTraceId) setText("traceMetric", state.currentTraceId.slice(-10));
  const text = Number.isFinite(Number(value)) ? `${value}ms` : "--";
  if (name === "asr_ms") setText("asrMetric", text);
  if (name === "llm_first_token_ms") setText("llmMetric", text);
  if (name === "tts_first_audio_ms") setText("ttsMetric", text);
}

/* ---- transcript ---- */

function addMsg(role, text, meta = {}) {
  const t = $("#transcript"); if (!t) return null;
  const shouldFollow = !transcriptBatchRendering && isTranscriptNearBottom(t);
  const row = document.createElement("div");
  row.className = `message-row ${role}`;
  const avatar = document.createElement("div");
  avatar.className = "message-avatar";
  const identity = chatIdentity(role, meta);
  const name = identity.name;
  if (identity.avatarUrl) {
    const image = document.createElement("img");
    image.src = identity.avatarUrl;
    image.alt = name;
    avatar.appendChild(image);
  } else {
    avatar.textContent = identity.initial;
  }
  const body = document.createElement("div");
  body.className = "message-body";
  const label = document.createElement("small");
  label.className = "message-name";
  label.textContent = name;
  const node = document.createElement("div");
  node.className = `message ${role}`;
  node.textContent = text;
  if (text || role === "assistant") body.append(label, node);
  else body.append(label);
  const attachmentsNode = renderMessageAttachments(meta.attachments || []);
  if (attachmentsNode) body.appendChild(attachmentsNode);
  row.append(avatar, body);
  t.appendChild(row);
  if (shouldFollow) scrollTranscriptToBottom({ smooth: false });
  return node;
}

function chatIdentity(role, meta = {}) {
  const isExternal = Boolean(meta.platform_id || meta.sender_id);
  if (isExternal && (meta.display_name || meta.avatar_url)) {
    const externalName = meta.display_name || (role === "user" ? "我" : "枝语");
    return {
      name: externalName,
      avatarUrl: meta.avatar_url || "",
      initial: firstIdentityChar(externalName, role === "user" ? "我" : "枝"),
    };
  }
  const name = role === "user"
    ? (state.currentConfig.web_user_name || "我")
    : (state.currentConfig.web_assistant_name || "枝语");
  const avatarUrl = role === "user"
    ? (state.currentConfig.web_user_avatar_url || "")
    : (state.currentConfig.web_assistant_avatar_url || "");
  return {
    name,
    avatarUrl,
    initial: firstIdentityChar(name, role === "user" ? "我" : "枝"),
  };
}

function firstIdentityChar(name, fallback) {
  const chars = Array.from(String(name || "").trim());
  return chars[0] || fallback;
}
function appendAssistant(text) {
  const t = $("#transcript");
  const shouldFollow = isTranscriptNearBottom(t);
  if (!state.currentAssistant) state.currentAssistant = addMsg("assistant", "");
  if (state.currentAssistant) state.currentAssistant.textContent += text;
  if (shouldFollow) scrollTranscriptToBottom({ smooth: false });
}
function appendAssistantAttachments(attachments) {
  if (!attachments?.length) return;
  const t = $("#transcript");
  const shouldFollow = isTranscriptNearBottom(t);
  if (!state.currentAssistant) state.currentAssistant = addMsg("assistant", "");
  const body = state.currentAssistant?.closest(".message-body");
  if (!body) return;
  const node = renderMessageAttachments(attachments);
  if (node) body.appendChild(node);
  if (shouldFollow) scrollTranscriptToBottom({ smooth: false });
}
export function clearTranscript() {
  const t = $("#transcript");
  if (t) t.replaceChildren();
  state.currentAssistant = null;
  lastRenderedTranscriptKey = "";
}
function isTranscriptNearBottom(t = $("#transcript"), threshold = 96) {
  if (!t) return true;
  return t.scrollHeight - t.scrollTop - t.clientHeight <= threshold;
}
function scrollTranscript() { const t = $("#transcript"); if (t) t.scrollTop = t.scrollHeight; }
export function scrollTranscriptToBottom(options = {}) {
  const smooth = Boolean(options.smooth);
  const run = () => {
    const t = $("#transcript");
    if (!t) return;
    if (smooth && typeof t.scrollTo === "function") t.scrollTo({ top: t.scrollHeight, behavior: "smooth" });
    else t.scrollTop = t.scrollHeight;
  };
  run();
  if (options.once) return;
  requestAnimationFrame(run);
}
function renderTranscript(msgs) {
  const key = transcriptRenderKey(msgs);
  if (key && key === lastRenderedTranscriptKey) return;
  const t = $("#transcript");
  const shouldFollow = isTranscriptNearBottom(t);
  transcriptBatchRendering = true;
  clearTranscript();
  for (const m of msgs) {
    if (["user","assistant","system"].includes(m.role)) addMsg(m.role, m.content || "", m);
  }
  transcriptBatchRendering = false;
  lastRenderedTranscriptKey = key;
  if (shouldFollow || !msgs.length) scrollTranscriptToBottom({ smooth: false, once: true });
}

function transcriptRenderKey(msgs = []) {
  const parts = (msgs || []).map((m, index) => {
    const attachments = Array.isArray(m.attachments) ? m.attachments.length : 0;
    const content = String(m.content || "");
    const sample = content.length > 96 ? `${content.slice(0, 48)}…${content.slice(-48)}` : content;
    return `${m.id || index}:${m.role || ""}:${m.updated_at || m.created_at || ""}:${content.length}:${sample}:${attachments}`;
  });
  return `${state.activeConversationId || "new"}::${parts.join("|")}`;
}

function renderMessageAttachments(attachments) {
  const items = (attachments || []).filter((item) => item && (item.url || item.summary));
  if (!items.length) return null;
  const wrap = document.createElement("div");
  wrap.className = "message-attachments";
  for (const item of items) {
    if (item.type === "image") {
      const figure = document.createElement("figure");
      figure.className = "message-image";
      const img = document.createElement("img");
      img.src = item.url;
      img.alt = item.summary || item.name || "图片";
      figure.appendChild(img);
      if (item.summary) {
        const cap = document.createElement("figcaption");
        cap.textContent = item.summary;
        figure.appendChild(cap);
      }
      wrap.appendChild(figure);
    } else if (item.type === "sticker") {
      const sticker = document.createElement("div");
      sticker.className = "message-sticker";
      const img = document.createElement("img");
      img.src = item.url;
      img.alt = item.tag || item.name || "表情包";
      sticker.title = item.tag || item.name || "表情包";
      sticker.appendChild(img);
      wrap.appendChild(sticker);
    }
  }
  return wrap;
}

/* ---- conversation ---- */

function applyConversation(conversation, renderMessages) {
  if (!conversation) return;
  state.activeConversation = conversation; state.activeConversationId = conversation.id;
  localStorage.setItem(ACTIVE_CONVERSATION_KEY, conversation.id);
  setText("topStatus", conversation.title || "新的对话");
  if (renderMessages) renderTranscript(conversation.messages || []);
  loadConversations().then(() => { if (onTranscriptUpdated) onTranscriptUpdated(); });
}

export function renderExternalConversation(conversation) {
  if (!conversation || conversation.id !== state.activeConversationId) return;
  state.activeConversation = conversation;
  setText("topStatus", conversation.title || "新的对话");
  renderTranscript(conversation.messages || []);
}

export async function selectConversation(conversationId, options = {}) {
  if (!conversationId) return;
  if (conversationId === state.activeConversationId) return;
  const summary = (state.conversations || []).find((item) => item.id === conversationId);
  let conversation = null;
  try {
    conversation = await loadConversation(conversationId);
  } catch {
    conversation = null;
  }
  state.activeConversationId = conversationId;
  localStorage.setItem(ACTIVE_CONVERSATION_KEY, conversationId);
  if (conversation || summary) {
    conversation = conversation || summary;
    applyConversation(conversation, true);
    scrollTranscriptToBottom({ smooth: false, once: true });
  }
  if (!options.skipReconnect) reconnectDialog();
}

/* ---- text ---- */

export async function sendText() {
  const input = $("#textInput"); const text = input?.value.trim(); if (!text) return;
  if (!state.ws || state.ws.readyState !== WebSocket.OPEN) { showToast("对话后端未连接", "error"); return; }
  if (state.busy && state.assistantActive) interruptAssistant("text");
  state.busy = true; setText("topStatus", "发送"); pipeline("llm", "已发送");
  state.ws.send(JSON.stringify({ type: "text", text })); input.value = ""; resizeComposerInput(input);
}

export function resizeComposerInput(input) {
  if (!input) return; input.style.height = "auto";
  const h = Math.min(124, Math.max(38, input.scrollHeight));
  input.style.height = `${h}px`; input.style.overflowY = input.scrollHeight > 124 ? "auto" : "hidden";
}

export function interruptAssistant(reason = "voice") {
  if (!state.ws || state.ws.readyState !== WebSocket.OPEN) return;
  state.interrupting = true; state.lastInterruptAt = performance.now(); state.bargeInFrames = 0;
  state.dropAudioUntilNextAssistant = true; stopAssistantAudio(); state.busy = false; state.assistantActive = false;
  setText("topStatus", "打断"); state.ws.send(JSON.stringify({ type: "interrupt", reason }));
}

/* ---- pipeline compact (for dashboard sidebar) ---- */
export function updatePipelineCompact(stage, label) {
  // This is called by dialog.js when the pipeline callback is set by dashboard
  if (onPipelineUpdate) onPipelineUpdate(stage, label);
}
