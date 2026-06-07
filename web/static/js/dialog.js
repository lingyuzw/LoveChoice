/* ============================================================
   dialog.js — WebSocket connection, dialog events, text send
   LoveChoice Voice Console
   ============================================================ */

import { state, PIPELINE_STEPS, ACTIVE_CONVERSATION_KEY } from "./state.js";
import { $, setText, formatConversationMeta, showToast } from "./utils.js";
import { loadConversations } from "./api.js";
import { stopAssistantAudio, schedulePcm16, releaseAfterPlayback } from "./audio.js";

let onTranscriptUpdated = null;

export function setTranscriptCallback(fn) {
  onTranscriptUpdated = fn;
}

/* ---- WebSocket lifecycle ---- */

export function connectSocket() {
  if (state.ws && state.ws.readyState <= WebSocket.OPEN) return;
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const query = state.activeConversationId ? `?conversation_id=${encodeURIComponent(state.activeConversationId)}` : "";
  const ws = new WebSocket(`${scheme}://${location.host}/ws/dialog${query}`);
  state.ws = ws;
  ws.binaryType = "arraybuffer";

  ws.addEventListener("open", () => {
    state.connected = true;
    setDialogState(state.micActive ? "监听中" : "待机");
    sendRuntimeSettings();
  });

  ws.addEventListener("message", handleSocketMessage);
  ws.addEventListener("close", () => {
    state.connected = false;
    stopAssistantAudio();
    state.busy = false;
    state.assistantActive = false;
    state.interrupting = false;
    setDialogState("断开");
    if (!state.previewMode && !state.manualSocketClose) {
      window.clearTimeout(state.reconnectTimer);
      state.reconnectTimer = window.setTimeout(connectSocket, 1200);
    }
  });
  ws.addEventListener("error", () => setDialogState(state.previewMode ? "预览" : "连接异常"));
}

export function reconnectDialog() {
  state.manualSocketClose = true;
  window.clearTimeout(state.reconnectTimer);
  if (state.ws && state.ws.readyState <= WebSocket.OPEN) state.ws.close();
  state.ws = null;
  state.connected = false;
  state.currentAssistant = null;
  window.setTimeout(() => {
    state.manualSocketClose = false;
    connectSocket();
  }, 120);
}

function sendRuntimeSettings() {
  if (!state.ws || state.ws.readyState !== WebSocket.OPEN) return;
  state.ws.send(JSON.stringify({ type: "settings", settings: state.currentConfig }));
}

/* ---- incoming message dispatch ---- */

async function handleSocketMessage(event) {
  if (event.data instanceof ArrayBuffer) {
    schedulePcm16(event.data);
    return;
  }
  if (event.data instanceof Blob) {
    schedulePcm16(await event.data.arrayBuffer());
    return;
  }

  let data;
  try { data = JSON.parse(event.data); } catch { return; }
  handleDialogEvent(data);
}

function handleDialogEvent(data) {
  switch (data.type) {
    case "ready":
      setDialogState("待机");
      updatePipeline("idle");
      break;
    case "conversation":
      applyConversation(data.conversation, { renderMessages: true });
      break;
    case "conversation_saved":
      applyConversation(data.conversation, { renderMessages: false });
      break;
    case "settings":
      state.currentConfig = { ...state.currentConfig, ...(data.settings || {}) };
      break;
    case "status":
      if (data.stage === "vad") setText("vadLabel", data.label || "vad");
      break;
    case "vad_start":
      state.busy = false;
      setText("vadLabel", "speech");
      setDialogState("收音");
      updatePipeline("vad", "正在听...");
      break;
    case "vad_end":
      setText("vadLabel", `${data.duration_ms || 0} ms`);
      setDialogState("识别");
      updatePipeline("asr", "正在识别...");
      state.busy = true;
      break;
    case "vad_short":
      setText("vadLabel", "short");
      break;
    case "user":
      addMessage("user", data.text || "");
      state.currentAssistant = null;
      updatePipeline("llm", "正在思考...");
      break;
    case "assistant_start":
      window.clearTimeout(state.releaseTimer);
      state.assistantActive = true;
      state.interrupting = false;
      state.dropAudioUntilNextAssistant = false;
      state.currentAssistant = addMessage("assistant", "");
      setDialogState("生成");
      updatePipeline("llm", "正在生成...");
      break;
    case "tool":
      setDialogState(`工具 ${data.id || ""}`);
      break;
    case "llm_delta":
      updatePipeline("llm", "正在输出...");
      appendAssistant(data.text || "");
      break;
    case "audio_format":
      state.ttsSampleRate = Number(data.sample_rate || 24000);
      setDialogState("播放");
      updatePipeline("tts", "正在合成/播放...");
      setText("audioStateText", "播放中");
      setText("playbackState", "正在播放");
      break;
    case "metric":
      setMetric(data.name, data.value);
      break;
    case "error":
      showToast(data.message || "出错了", "error");
      setText("lastErrorText", data.message || "出错了");
      updatePipeline("error", "出错");
      break;
    case "busy":
      setDialogState("忙碌");
      break;
    case "interrupted":
      stopAssistantAudio();
      state.busy = false;
      state.assistantActive = false;
      state.interrupting = false;
      state.dropAudioUntilNextAssistant = false;
      setDialogState(state.micActive ? "监听中" : "待机");
      setText("interruptStateText", "已打断");
      updatePipeline(state.micActive ? "vad" : "idle", state.micActive ? "继续监听" : "已停止");
      break;
    case "reset":
      stopAssistantAudio();
      state.busy = false;
      state.assistantActive = false;
      state.interrupting = false;
      state.dropAudioUntilNextAssistant = false;
      applyConversation(data.conversation, { renderMessages: true });
      setDialogState("待机");
      updatePipeline("idle");
      break;
    case "turn_done":
      updatePipeline("done", "完成");
      releaseAfterPlayback({
        onReleased: () => {
          setDialogState(state.micActive ? "监听中" : "待机");
          setText("audioStateText", "空闲");
          setText("playbackState", "播放完成");
          setText("interruptStateText", "就绪");
        }
      });
      break;
    default:
      break;
  }
}

/* ---- pipeline state ---- */

function setDialogState(text) {
  setText("dialogState", text);
  setText("pipelineStateText", text);
}

function updatePipeline(stage = "idle", label = "") {
  const activeIndex = PIPELINE_STEPS.indexOf(stage);
  for (const [index, key] of PIPELINE_STEPS.entries()) {
    const el = document.getElementById(`pipeline${key[0].toUpperCase()}${key.slice(1)}`);
    if (!el) continue;
    el.classList.remove("active", "done", "error");
    if (stage === "error") el.classList.add("error");
    else if (stage === "done") el.classList.add("done");
    else if (index < activeIndex) el.classList.add("done");
    else if (index === activeIndex) el.classList.add("active");
  }
  document.querySelectorAll(".status-dot[data-stage]").forEach((dot) => {
    dot.classList.toggle("active", dot.dataset.stage === stage || stage === "done");
  });
  if (label) setText("pipelineStateText", label);
  if (stage === "idle") {
    setText("pipelineStateText", "等待用户输入");
    setText("audioStateText", "空闲");
    setText("playbackState", "等待播放");
    setText("interruptStateText", "就绪");
  }
}

export { updatePipeline };

function setMetric(name, value) {
  const text = Number.isFinite(Number(value)) ? `${value}ms` : "--";
  if (name === "asr_ms") setText("asrMetric", text);
  if (name === "llm_first_token_ms") setText("llmMetric", text);
  if (name === "tts_first_audio_ms") setText("ttsMetric", text);
}

/* ---- transcript rendering ---- */

function addMessage(role, text) {
  const transcript = $("#transcript");
  if (!transcript) return null;
  const node = document.createElement("div");
  node.className = `message ${role}`;
  node.textContent = text;
  transcript.appendChild(node);
  scrollTranscript();
  return node;
}

function appendAssistant(text) {
  if (!state.currentAssistant) state.currentAssistant = addMessage("assistant", "");
  if (state.currentAssistant) state.currentAssistant.textContent += text;
  scrollTranscript();
}

export function clearTranscript() {
  const transcript = $("#transcript");
  if (transcript) transcript.innerHTML = "";
  state.currentAssistant = null;
}

function scrollTranscript() {
  const transcript = $("#transcript");
  if (transcript) transcript.scrollTop = transcript.scrollHeight;
}

function renderTranscript(messages) {
  clearTranscript();
  for (const message of messages) {
    if (message.role === "user" || message.role === "assistant" || message.role === "system") {
      addMessage(message.role, message.content || "");
    }
  }
}

/* ---- conversation management ---- */

function applyConversation(conversation, options = {}) {
  if (!conversation) return;
  state.activeConversation = conversation;
  state.activeConversationId = conversation.id;
  localStorage.setItem(ACTIVE_CONVERSATION_KEY, conversation.id);
  setText("conversationTitle", conversation.title || "新的对话");
  setText("conversationMeta", formatConversationMeta(conversation));
  if (options.renderMessages) renderTranscript(conversation.messages || []);
  loadConversations().then(() => {
    if (onTranscriptUpdated) onTranscriptUpdated();
  });
}

export async function selectConversation(conversationId) {
  if (!conversationId || conversationId === state.activeConversationId) return;
  state.activeConversationId = conversationId;
  localStorage.setItem(ACTIVE_CONVERSATION_KEY, conversationId);
  reconnectDialog();
}

/* ---- text input ---- */

export async function sendText() {
  const input = $("#textInput");
  const text = input?.value.trim();
  if (!text) return;
  if (!state.ws || state.ws.readyState !== WebSocket.OPEN) {
    showToast("对话后端未连接，请先启动 Web 控制台。", "error");
    return;
  }
  if (state.busy && state.assistantActive) interruptAssistant("text");
  state.busy = true;
  setDialogState("发送");
  updatePipeline("llm", "文本已发送");
  state.ws.send(JSON.stringify({ type: "text", text }));
  input.value = "";
  resizeComposerInput(input);
}

export function resizeComposerInput(input) {
  if (!input) return;
  input.style.height = "auto";
  const maxHeight = 132;
  const nextHeight = Math.min(maxHeight, Math.max(44, input.scrollHeight));
  input.style.height = `${nextHeight}px`;
  input.style.overflowY = input.scrollHeight > maxHeight ? "auto" : "hidden";
}

/* ---- interrupt ---- */

export function interruptAssistant(reason = "voice") {
  if (!state.ws || state.ws.readyState !== WebSocket.OPEN) return;
  state.interrupting = true;
  state.lastInterruptAt = performance.now();
  state.bargeInFrames = 0;
  state.dropAudioUntilNextAssistant = true;
  stopAssistantAudio();
  state.busy = false;
  state.assistantActive = false;
  setDialogState("打断");
  setText("interruptStateText", "打断中");
  setText("audioStateText", "已停止");
  state.ws.send(JSON.stringify({ type: "interrupt", reason }));
}
