/* ============================================================
   ui-dashboard.js — Chat dashboard UI (index.html)
   LoveChoice Voice Console
   ============================================================ */

import { state, ACTIVE_CONVERSATION_KEY } from "./state.js";
import { $, setText, renderIcons, formatConversationMeta, showToast, showSkeleton } from "./utils.js";
import { loadConfig, loadServices, loadConversations, createConversation, deleteConversation } from "./api.js";
import { connectSocket, reconnectDialog, clearTranscript, selectConversation, sendText, resizeComposerInput, interruptAssistant, setTranscriptCallback, updatePipeline } from "./dialog.js";
import { ensureAudioContext, startMic, stopMic, sendMicSamples, shouldTriggerBargeIn } from "./audio.js";

/* ---- init ---- */

export async function initDashboard() {
  setupDashboardEvents();
  showSkeleton("conversationList", 6);

  const configResult = await loadConfig();
  renderCapabilityStatus(configResult.config);

  await loadServices();
  renderServiceOverview();
  setSystemState(serviceSummaryText());

  await loadConversations();
  if (!state.activeConversationId && state.conversations.length) {
    state.activeConversationId = state.conversations[0].id;
    localStorage.setItem(ACTIVE_CONVERSATION_KEY, state.activeConversationId);
  }
  renderConversationList();
  updatePipeline("idle");
  connectSocket();
  drawScope();

  // re-render conversation list when transcript updates
  setTranscriptCallback(() => renderConversationList());
}

function setupDashboardEvents() {
  $("#micBtn")?.addEventListener("click", toggleMic);
  $("#sendBtn")?.addEventListener("click", sendText);
  $("#resetBtn")?.addEventListener("click", newConversation);
  $("#newConversationBtn")?.addEventListener("click", newConversation);
  $("#interruptBtn")?.addEventListener("click", () => interruptAssistant("manual"));
  const textInput = $("#textInput");
  textInput?.addEventListener("input", () => resizeComposerInput(textInput));
  textInput?.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      sendText();
    }
  });
  resizeComposerInput(textInput);
}

/* ---- service overview (topbar status) ---- */

function setSystemState(text) {
  const el = $("#systemState");
  if (el) el.textContent = text;
}

function serviceSummaryText() {
  const running = state.services.filter((s) => s.running).length;
  return `后端在线 · ${running}/${state.services.length} 运行`;
}

function renderServiceOverview() {
  const host = $("#serviceOverview");
  if (!host) return;
  const ids = ["asr", "llm", "tts"];
  host.innerHTML = "";
  for (const id of ids) {
    const service = state.services.find((item) => item.id === id);
    const status = service?.running ? "running" : service?.health?.ok === false ? "failed" : "";
    const node = document.createElement("span");
    node.className = status;
    node.innerHTML = `<i></i>${id.toUpperCase()}`;
    host.appendChild(node);
  }
}

function renderCapabilityStatus(config = state.currentConfig) {
  const memoryOn = config.memory_enabled !== false && config.memory_extract_enabled !== false;
  const toolsOn = config.tools_enabled !== false && config.tools_auto_call !== false;
  setText("memoryStatus", memoryOn ? "默认开启" : "等待保存开启");
  setText("memoryDetail", `SQLite 自动记忆 · 每轮最多注入 ${config.memory_max_context_items || 12} 条`);
  setText("toolsStatus", toolsOn ? "默认自动调用" : "等待保存开启");
  setText("toolsDetail", "热点新闻 / 搜索 / 网页读取 / 天气 / 财经价格");
  setText("apiKeyState", config.llm_api_key_set ? `已保存 ${config.llm_api_key_masked}` : "未保存，可填 sk-... 格式");
}

/* ---- conversation list ---- */

function renderConversationList() {
  const host = $("#conversationList");
  if (!host) return;
  host.innerHTML = "";
  const conversations = state.conversations.slice(0, 24);
  if (!conversations.length) {
    const empty = document.createElement("p");
    empty.className = "conversation-empty";
    empty.textContent = "还没有保存的对话";
    host.appendChild(empty);
    return;
  }
  for (const conversation of conversations) {
    const item = document.createElement("div");
    item.className = `conversation-item ${conversation.id === state.activeConversationId ? "active" : ""}`;

    const openButton = document.createElement("button");
    openButton.type = "button";
    openButton.className = "conversation-open";
    openButton.innerHTML = "<strong></strong><span></span><small></small>";
    openButton.querySelector("strong").textContent = conversation.title || "新的对话";
    openButton.querySelector("span").textContent = conversation.last_message || "空会话";
    openButton.querySelector("small").textContent = formatConversationMeta(conversation);
    openButton.addEventListener("click", () => selectConversation(conversation.id));

    const deleteButton = document.createElement("button");
    deleteButton.type = "button";
    deleteButton.className = "conversation-delete";
    deleteButton.title = "删除对话";
    deleteButton.innerHTML = '<i data-lucide="trash-2"></i>';
    deleteButton.addEventListener("click", () => handleDeleteConversation(conversation));

    item.append(openButton, deleteButton);
    host.appendChild(item);
  }
  renderIcons();
}

async function newConversation() {
  if (state.previewMode) {
    clearTranscript();
    showToast("预览模式：已清空对话区", "info");
    return;
  }
  try {
    const conversation = await createConversation();
    state.activeConversationId = conversation.id;
    localStorage.setItem(ACTIVE_CONVERSATION_KEY, conversation.id);
    await loadConversations();
    renderConversationList();
    reconnectDialog();
  } catch (error) {
    showToast(`新建会话失败：${error.message}`, "error");
  }
}

async function handleDeleteConversation(conversation) {
  if (!conversation?.id || state.previewMode) return;
  const title = conversation.title || "这次对话";
  if (!window.confirm(`删除「${title}」？这条记录会从本地历史里移除。`)) return;

  const wasActive = conversation.id === state.activeConversationId;
  try {
    await deleteConversation(conversation.id);
    if (wasActive) {
      state.activeConversationId = "";
      state.activeConversation = null;
      localStorage.removeItem(ACTIVE_CONVERSATION_KEY);
      clearTranscript();
    }
    await loadConversations();
    renderConversationList();
    if (wasActive) reconnectDialog();
  } catch (error) {
    showToast(`删除会话失败：${error.message}`, "error");
  }
}

/* ---- microphone toggle ---- */

async function toggleMic() {
  if (state.micActive) {
    stopMic();
    setText("dialogState", "待机");
    updatePipeline("idle");
  } else {
    if (!state.connected) {
      showToast("对话通道未连接，不能打开麦克风。", "error");
      return;
    }
    await startMic({
      onSendSamples: (samples) => {
        if (state.busy && state.assistantActive && shouldTriggerBargeIn()) {
          interruptAssistant("voice");
        }
        if (state.busy) return;
        sendMicSamples(samples);
      }
    });
    setText("dialogState", "监听中");
    updatePipeline("vad", "正在听...");
  }
}

/* ---- waveform visualization ---- */

function drawScope() {
  const canvas = $("#scopeCanvas");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const width = canvas.clientWidth || 900;
  const height = canvas.clientHeight || 220;
  if (canvas.width !== Math.floor(width * dpr) || canvas.height !== Math.floor(height * dpr)) {
    canvas.width = Math.floor(width * dpr);
    canvas.height = Math.floor(height * dpr);
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = "#f8faf9";
  ctx.fillRect(0, 0, width, height);

  // subtle grid
  ctx.strokeStyle = "rgba(23, 32, 38, 0.06)";
  ctx.lineWidth = 1;
  for (let x = 0; x < width; x += 24) {
    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, height); ctx.stroke();
  }
  for (let y = 0; y < height; y += 24) {
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(width, y); ctx.stroke();
  }

  // waveform line
  state.levels.push(state.latestLevel);
  state.levels.shift();
  ctx.beginPath();
  state.levels.forEach((level, index) => {
    const x = (index / (state.levels.length - 1)) * width;
    const wobble = Math.sin(index * 0.45 + performance.now() / 260) * 0.06;
    const y = height / 2 - (level + wobble) * height * 0.36;
    if (index === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  const lineColor = state.busy ? "#c77900" : state.micActive ? "#007c74" : "#3158d4";
  ctx.strokeStyle = lineColor;
  ctx.lineWidth = 3;
  ctx.shadowColor = lineColor;
  ctx.shadowBlur = 8;
  ctx.stroke();
  ctx.shadowBlur = 0;

  // center line
  ctx.beginPath();
  ctx.moveTo(0, height / 2);
  ctx.lineTo(width, height / 2);
  ctx.strokeStyle = "rgba(23, 32, 38, 0.14)";
  ctx.lineWidth = 1;
  ctx.stroke();

  requestAnimationFrame(drawScope);
}
