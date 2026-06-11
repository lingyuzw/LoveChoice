/* ============================================================
   pages/dashboard.js - Chat dashboard
   BranchWhisper
   ============================================================ */

import { state, ACTIVE_CONVERSATION_KEY } from "../stores/state.js";
import { $, setText, renderIcons, formatConversationMeta, showToast, showSkeleton, showConfirm, createIcon } from "../utils/dom.js";
import {
  conversationExportUrl,
  loadConfig,
  loadServices,
  loadConversations,
  deleteConversation,
  updateConversation,
  uploadChatImage,
} from "../api/index.js";
import {
  bindAppearanceRefresh,
  bindRuntimeConfigRefresh,
  connectSocket,
  reconnectDialog,
  clearTranscript,
  selectConversation,
  resizeComposerInput,
  interruptAssistant,
  setTranscriptCallback,
  setPipelineUpdater,
  scrollTranscriptToBottom,
} from "../dialog.js";
import { startMic, stopMic, sendMicSamples, shouldTriggerBargeIn } from "../audio.js";
import {
  markConversationSnapshot,
  pauseConversationRefresh,
  refreshConversationsNow,
  setupConversationRefresh,
  startConversationRefresh,
  stopConversationRefresh,
} from "./dashboard/conversation-refresh.js";

let hasMessages = false;
let eventsBound = false;
let conversationSearchTimer = 0;

export async function initDashboard() {
  showSkeleton("conversationList", 5);
  await loadConfig();
  await loadServices();
  await loadConversations();

  normalizeActiveConversationForScope({ selectFallback: true });

  renderConversationList();
  resetPipelineCompact();
  setupConversationRefresh({ renderConversationList, syncChatView, isWeixinConversation });
  setPipelineUpdater((stage, label) => updatePipelineCompact(stage, label));
  setTranscriptCallback(() => {
    refreshConversationsNow({ reason: "transcript", skipActive: true });
  });
  bindAppearanceRefresh();
  bindRuntimeConfigRefresh();
  connectSocket();
  startConversationRefresh();
  drawScope();
  setupDashboardEvents();

  setText("topStatus", "待机");
  await refreshConversationsNow({ reason: "init", force: true });
  syncChatView();
}

export async function enterDashboard() {
  await Promise.allSettled([loadConfig(), loadConversations()]);
  const previousActiveId = state.activeConversationId;
  normalizeActiveConversationForScope();
  const activeChanged = previousActiveId !== state.activeConversationId;
  setupConversationRefresh({ renderConversationList, syncChatView, isWeixinConversation });
  renderConversationList();
  if (activeChanged) {
    clearTranscript();
    resetPipelineCompact();
    reconnectDialog();
  }
  syncChatView();
  startConversationRefresh();
  await refreshConversationsNow({ reason: "enter", force: true });
}

export function leaveDashboard() {
  stopConversationRefresh();
}

function setupDashboardEvents() {
  if (eventsBound) {
    updateTtsToggleIcon();
    return;
  }
  eventsBound = true;
  bindComposer("micBtn", "sendBtn", "interruptBtn", "resetBtn", "textInput");
  bindComposer("micBtnWelcome", "sendBtnWelcome", "interruptBtnWelcome", "resetBtnWelcome", "textInputWelcome");
  $("#attachImageBtn")?.addEventListener("click", () => $("#chatImageInput")?.click());
  $("#attachImageBtnWelcome")?.addEventListener("click", () => $("#chatImageInput")?.click());
  $("#chatImageInput")?.addEventListener("change", handleChatImageSelected);
  $("#newConversationBtn")?.addEventListener("click", newConversation);
  $("#conversationSearchInput")?.addEventListener("input", handleConversationSearch);
  $("#archiveModeBtn")?.addEventListener("click", toggleArchiveMode);
  document.querySelectorAll("[data-conversation-scope]").forEach((button) => {
    button.addEventListener("click", () => switchConversationScope(button.dataset.conversationScope || "recent"));
  });
  setupTtsToggle();
}

function bindComposer(micId, sendId, intrId, resetId, inputId) {
  $(`#${micId}`)?.addEventListener("click", toggleMic);
  $(`#${sendId}`)?.addEventListener("click", () => sendText(inputId));
  if (intrId) $(`#${intrId}`)?.addEventListener("click", () => interruptAssistant("manual"));
  if (resetId) $(`#${resetId}`)?.addEventListener("click", newConversation);

  const input = $(`#${inputId}`);
  input?.addEventListener("input", () => resizeComposerInput(input));
  input?.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      sendText(inputId);
    }
  });
  resizeComposerInput(input);
}

function setupTtsToggle() {
  const buttons = document.querySelectorAll("#ttsToggleBtn, #ttsToggleBtnWelcome");
  if (!buttons.length) return;
  updateTtsToggleIcon();
  buttons.forEach((btn) => {
    btn.addEventListener("click", () => {
      state.ttsEnabled = !state.ttsEnabled;
      updateTtsToggleIcon();
      state.currentConfig.tts_enabled = state.ttsEnabled;
      if (state.ws && state.ws.readyState === WebSocket.OPEN) {
        state.ws.send(JSON.stringify({ type: "settings", settings: { tts_enabled: state.ttsEnabled } }));
      }
    });
  });
}

function updateTtsToggleIcon() {
  const iconName = state.ttsEnabled ? "volume-2" : "volume-x";
  const label = state.ttsEnabled ? "语音开启" : "语音关闭";
  document.querySelectorAll("#ttsToggleBtn, #ttsToggleBtnWelcome").forEach((btn) => {
    btn.replaceChildren(createIcon(iconName));
    btn.title = label;
    btn.classList.toggle("off", !state.ttsEnabled);
  });
  renderIcons();
}

function syncChatView() {
  const msgs = document.querySelectorAll("#transcript .message");
  const hadMessages = hasMessages;
  hasMessages = msgs.length > 0;

  const welcome = $("#chatWelcome");
  const messages = $("#chatMessages");
  const composer = $("#chatComposer");

  if (hasMessages) {
    if (welcome) welcome.style.display = "none";
    if (messages) messages.style.display = "flex";
    if (composer) composer.style.display = "block";
  } else {
    if (welcome) welcome.style.display = "flex";
    if (messages) messages.style.display = "none";
    if (composer) composer.style.display = "none";
  }
  if (hasMessages && !hadMessages) scrollTranscriptToBottom({ smooth: false });
}

function sendText(inputId) {
  const input = $(`#${inputId}`);
  const text = input?.value.trim();
  const attachments = state.pendingAttachments || [];
  if (!text && !attachments.length) return;
  if (!state.ws || state.ws.readyState !== WebSocket.OPEN) {
    showToast("对话后端未连接", "error");
    return;
  }
  if (state.busy && state.assistantActive) interruptAssistant("text");
  state.busy = true;

  const other = $(`#${inputId === "textInput" ? "textInputWelcome" : "textInput"}`);
  if (other) other.value = "";

  setText("topStatus", "发送");
  updatePipelineCompact("llm", "已发送");
  if (attachments.length) {
    state.ws.send(JSON.stringify({ type: "message", text, attachments }));
    clearPendingAttachments();
  } else {
    state.ws.send(JSON.stringify({ type: "text", text }));
  }
  input.value = "";
  resizeComposerInput(input);
  if (!hasMessages) syncChatView();
}

async function handleChatImageSelected(event) {
  const file = event.target.files?.[0];
  if (!file) return;
  try {
    if (!file.type.startsWith("image/")) throw new Error("请选择图片文件");
    const dataUrl = await fileToDataUrl(file);
    const result = await uploadChatImage(dataUrl);
    const asset = result.asset;
    state.pendingAttachments = [
      ...(state.pendingAttachments || []),
      { type: "image", asset_id: asset.id, url: asset.url, mime: asset.mime, name: file.name },
    ].slice(-4);
    renderPendingAttachments();
    showToast("图片已添加，发送后枝语会尝试看懂它", "success");
  } catch (error) {
    showToast(`图片添加失败：${error.message}`, "error");
  } finally {
    event.target.value = "";
  }
}

function renderPendingAttachments() {
  for (const id of ["attachmentPreviewStrip", "attachmentPreviewStripWelcome"]) {
    const host = $(`#${id}`);
    if (!host) continue;
    host.replaceChildren();
    const items = state.pendingAttachments || [];
    host.hidden = !items.length;
    for (const item of items) {
      const chip = document.createElement("div");
      chip.className = "attachment-preview-chip";
      const img = document.createElement("img");
      img.src = item.url;
      img.alt = item.name || "待发送图片";
      const label = document.createElement("span");
      label.textContent = item.name || "图片";
      const remove = document.createElement("button");
      remove.type = "button";
      remove.title = "移除图片";
      remove.appendChild(createIcon("x"));
      remove.addEventListener("click", () => {
        state.pendingAttachments = (state.pendingAttachments || []).filter((candidate) => candidate.asset_id !== item.asset_id);
        renderPendingAttachments();
      });
      chip.append(img, label, remove);
      host.appendChild(chip);
    }
  }
  renderIcons();
}

function clearPendingAttachments() {
  state.pendingAttachments = [];
  renderPendingAttachments();
}

function fileToDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(reader.error || new Error("文件读取失败"));
    reader.readAsDataURL(file);
  });
}

function resetPipelineCompact() {
  document.querySelectorAll("#pipelineCompact .pipeline-row").forEach((row) => {
    row.classList.remove("active", "done");
    row.querySelector("small").textContent = "--";
  });
}

function updatePipelineCompact(stage, label) {
  const steps = ["vad", "asr", "llm", "tts"];
  const index = steps.indexOf(stage);
  const rows = document.querySelectorAll("#pipelineCompact .pipeline-row");
  rows.forEach((row) => row.classList.remove("active", "done"));
  if (stage === "idle") {
    rows.forEach((row) => { row.querySelector("small").textContent = "--"; });
    return;
  }
  rows.forEach((row, i) => {
    if (i < index) row.classList.add("done");
    else if (i === index) row.classList.add("active");
  });
  if (label && index >= 0) {
    const row = rows[index];
    if (row) row.querySelector("small").textContent = label;
  }
}

function renderConversationList() {
  markConversationSnapshot();
  return renderScopedConversationList();
}

function renderScopedConversationList() {
  const host = $("#conversationList");
  if (!host) return;
  host.innerHTML = "";
  const conversations = visibleConversations().slice(0, 40);
  updateConversationScopeUi(conversations.length);
  if (!conversations.length) {
    const empty = document.createElement("p");
    empty.className = "conversation-empty";
    empty.textContent = emptyConversationText();
    host.appendChild(empty);
    return;
  }

  for (const conversation of conversations) {
    const item = document.createElement("div");
    item.className = `conversation-item ${conversation.id === state.activeConversationId ? "active" : ""}`;

    const openBtn = document.createElement("button");
    openBtn.type = "button";
    openBtn.className = "conversation-open";
    const title = document.createElement("strong");
    title.textContent = conversation.title || "新的对话";
    const preview = document.createElement("span");
    preview.textContent = conversation.summary || conversation.last_message || "空会话";
    const meta = document.createElement("small");
    meta.textContent = `${conversation.favorite ? "★ " : ""}${conversationMetaLabel(conversation)}`;
    openBtn.append(title, preview, meta);
    openBtn.addEventListener("click", () => selectConversation(conversation.id));

    const actions = document.createElement("div");
    actions.className = "conversation-actions";
    actions.append(
      conversationIcon("star", conversation.favorite ? "取消收藏" : "收藏", () => handleFavoriteConversation(conversation)),
      conversationIcon("pencil", "重命名", () => handleRenameConversation(conversation)),
      conversationIcon(conversation.archived ? "archive-restore" : "archive", conversation.archived ? "取消归档" : "归档", () => handleArchiveConversation(conversation)),
      conversationIcon("download", "导出", () => handleExportConversation(conversation)),
      conversationIcon("trash-2", "删除", () => handleDeleteConversation(conversation), "danger"),
    );
    item.append(openBtn, actions);
    host.appendChild(item);
  }
  renderIcons();
}

function visibleConversations() {
  if (state.conversationArchivedMode === "archived") return state.conversations || [];
  const scope = state.conversationScope || "recent";
  return (state.conversations || []).filter((conversation) => {
    const external = isWeixinConversation(conversation);
    return scope === "weixin" ? external : !external;
  });
}

function isWeixinConversation(conversation) {
  const text = `${conversation.platform_id || ""} ${conversation.source || ""}`.toLowerCase();
  return text.includes("weixin");
}

function activeConversationInCurrentScope() {
  if (!state.activeConversationId) return true;
  return visibleConversations().some((conversation) => conversation.id === state.activeConversationId);
}

function normalizeActiveConversationForScope(options = {}) {
  const visible = visibleConversations();
  if (!state.activeConversationId) {
    const fallback = options.selectFallback ? visible[0] : null;
    state.activeConversationId = fallback?.id || "";
    state.activeConversation = fallback || null;
    if (fallback?.id) {
      localStorage.setItem(ACTIVE_CONVERSATION_KEY, fallback.id);
    } else {
      localStorage.removeItem(ACTIVE_CONVERSATION_KEY);
    }
    return;
  }

  const active = visible.find((conversation) => conversation.id === state.activeConversationId);
  if (active) {
    state.activeConversation = active;
    localStorage.setItem(ACTIVE_CONVERSATION_KEY, active.id);
    return;
  }

  state.activeConversationId = "";
  state.activeConversation = null;
  localStorage.removeItem(ACTIVE_CONVERSATION_KEY);
}

function clearActiveConversationForScope() {
  state.activeConversationId = "";
  state.activeConversation = null;
  localStorage.removeItem(ACTIVE_CONVERSATION_KEY);
  pauseConversationRefresh();
  clearTranscript();
  resetPipelineCompact();
  reconnectDialog();
  syncChatView();
  setText("topStatus", state.conversationScope === "weixin" ? "微信聊天" : "新对话");
}

function conversationMetaLabel(conversation) {
  const prefix = isWeixinConversation(conversation) ? "微信 · " : "";
  return `${prefix}${formatConversationMeta(conversation)}`;
}

function emptyConversationText() {
  if (state.conversationArchivedMode === "archived") return "暂无归档对话";
  if (state.conversationScope === "weixin") return "还没有微信聊天。先在接入页给微信发一条消息。";
  return "还没有保存的对话。发送第一条消息后会出现在这里。";
}

function switchConversationScope(scope) {
  state.conversationScope = scope === "weixin" ? "weixin" : "recent";
  renderConversationList();
  if (!activeConversationInCurrentScope()) {
    clearActiveConversationForScope();
  }
  refreshConversationsNow({ reason: "scope", force: true });
}

function updateConversationScopeUi(count) {
  document.querySelectorAll("[data-conversation-scope]").forEach((button) => {
    button.classList.toggle("active", button.dataset.conversationScope === state.conversationScope);
  });
  const label = state.conversationArchivedMode === "archived"
    ? "归档"
    : state.conversationScope === "weixin" ? "微信聊天" : "最近";
  setText("conversationRailLabel", label);
  setText("conversationRailCount", String(count));
}

function conversationIcon(icon, title, handler, tone = "") {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = `conversation-icon ${tone}`;
  btn.title = title;
  btn.append(createIcon(icon));
  btn.addEventListener("click", (event) => {
    event.stopPropagation();
    handler();
  });
  return btn;
}

function handleConversationSearch(event) {
  window.clearTimeout(conversationSearchTimer);
  state.conversationFilter = event.target.value.trim();
  conversationSearchTimer = window.setTimeout(async () => {
    await loadConversations();
    renderConversationList();
    await refreshConversationsNow({ reason: "search", force: true });
  }, 180);
}

async function toggleArchiveMode() {
  state.conversationArchivedMode = state.conversationArchivedMode === "archived" ? "active" : "archived";
  $("#archiveModeBtn")?.classList.toggle("active", state.conversationArchivedMode === "archived");
  await loadConversations();
  renderConversationList();
  await refreshConversationsNow({ reason: "archive-mode", force: true });
}

async function newConversation() {
  state.activeConversationId = "";
  state.activeConversation = null;
  localStorage.removeItem(ACTIVE_CONVERSATION_KEY);
  pauseConversationRefresh();
  clearTranscript();
  resetPipelineCompact();
  renderConversationList();
  reconnectDialog();
  syncChatView();
  setText("topStatus", "新对话");
}

async function handleRenameConversation(conversation) {
  const title = prompt("新的会话名称", conversation.title || "");
  if (title === null) return;
  try {
    await updateConversation(conversation.id, { title: title.trim() || conversation.title || "新的对话" });
    await loadConversations();
    renderConversationList();
  } catch (error) {
    showToast(`重命名失败：${error.message}`, "error");
  }
}

async function handleFavoriteConversation(conversation) {
  try {
    await updateConversation(conversation.id, { favorite: !conversation.favorite });
    await loadConversations();
    renderConversationList();
  } catch (error) {
    showToast(`收藏失败：${error.message}`, "error");
  }
}

async function handleArchiveConversation(conversation) {
  try {
    await updateConversation(conversation.id, { archived: !conversation.archived });
    if (conversation.id === state.activeConversationId && !conversation.archived) {
      state.activeConversationId = "";
      state.activeConversation = null;
      localStorage.removeItem(ACTIVE_CONVERSATION_KEY);
      pauseConversationRefresh();
      clearTranscript();
      reconnectDialog();
    }
    await loadConversations();
    renderConversationList();
    syncChatView();
  } catch (error) {
    showToast(`归档失败：${error.message}`, "error");
  }
}

function handleExportConversation(conversation) {
  if (!conversation?.id) return;
  window.open(conversationExportUrl(conversation.id), "_blank");
}

async function handleDeleteConversation(conversation) {
  if (!conversation?.id || state.previewMode) return;
  if (!(await showConfirm(`删除「${conversation.title || "这次对话"}」？`))) return;
  const wasActive = conversation.id === state.activeConversationId;
  try {
    await deleteConversation(conversation.id);
    if (wasActive) {
      state.activeConversationId = "";
      state.activeConversation = null;
      localStorage.removeItem(ACTIVE_CONVERSATION_KEY);
      pauseConversationRefresh();
      clearTranscript();
      syncChatView();
    }
    await loadConversations();
    renderConversationList();
    if (wasActive) reconnectDialog();
  } catch (error) {
    showToast(`删除失败：${error.message}`, "error");
  }
}

async function toggleMic() {
  if (state.micActive) {
    stopMic();
    setText("topStatus", "待机");
    updatePipelineCompact("idle");
    return;
  }
  if (!state.connected) {
    showToast("对话通道未连接", "error");
    return;
  }
  await startMic({
    onSendSamples: (samples) => {
      if (state.busy && state.assistantActive && shouldTriggerBargeIn()) interruptAssistant("voice");
      if (state.busy) return;
      sendMicSamples(samples);
    },
  });
  setText("topStatus", "监听中");
  updatePipelineCompact("vad", "listening");
}

function drawScope() {
  const canvas = $("#scopeCanvas");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const width = canvas.clientWidth || 280;
  const height = canvas.clientHeight || 70;
  if (canvas.width !== Math.floor(width * dpr) || canvas.height !== Math.floor(height * dpr)) {
    canvas.width = Math.floor(width * dpr);
    canvas.height = Math.floor(height * dpr);
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue("--bg").trim() || "#0d1117";
  ctx.fillRect(0, 0, width, height);

  ctx.strokeStyle = "rgba(255,255,255,0.03)";
  ctx.lineWidth = 0.5;
  for (let x = 0; x < width; x += 14) {
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x, height);
    ctx.stroke();
  }

  state.levels.push(state.latestLevel);
  state.levels.shift();
  ctx.beginPath();
  state.levels.forEach((level, i) => {
    const x = (i / (state.levels.length - 1)) * width;
    const y = height / 2 - level * height * 0.42;
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  const lineColor = state.busy ? "#d29922" : state.micActive ? "#d4a853" : "rgba(88,166,255,0.4)";
  ctx.strokeStyle = lineColor;
  ctx.lineWidth = 1.5;
  ctx.shadowColor = lineColor;
  ctx.shadowBlur = 6;
  ctx.stroke();
  ctx.shadowBlur = 0;

  requestAnimationFrame(drawScope);
}

function escapeHtml(value) {
  return String(value || "").replace(/[&<>"']/g, (char) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]
  ));
}
