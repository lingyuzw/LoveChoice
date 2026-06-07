/* ============================================================
   ui-settings.js — Configuration page (settings.html)
   LoveChoice Voice Console · Precision Console
   ============================================================ */

import { state, DEFAULT_CONFIG } from "./state.js";
import { $, setValue, value, setText, setPlaceholder, renderIcons, showToast } from "./utils.js";
import { loadConfig, saveConfig, loadServices, updateServiceConfig } from "./api.js";

/* ---- init ---- */

export async function initSettings() {
  setupSettingsEvents();
  const configResult = await loadConfig();
  fillConfig(configResult.config);
  await loadServices();
  renderProfileList();
  setText("topStatus", configResult.ok ? "后端在线" : "静态预览");
}

function setupSettingsEvents() {
  $("#saveAllBtn")?.addEventListener("click", saveSettingsPage);
  window.addEventListener("scroll", highlightNavSection, { passive: true });
}

function highlightNavSection() {
  const sections = document.querySelectorAll(".settings-panel[id]");
  const navLinks = document.querySelectorAll(".settings-nav a[href^='#']");
  let currentId = "";
  for (const s of sections) { if (s.getBoundingClientRect().top < 160) currentId = s.id; }
  navLinks.forEach((l) => { l.classList.toggle("nav-active", l.getAttribute("href") === `#${currentId}`); });
}

/* ---- config form ---- */

const CONFIG_FIELD_MAP = [
  { key: "asr_mode", id: "asrMode" },
  { key: "asr_url", id: "asrUrl" },
  { key: "asr_model", id: "asrModel" },
  { key: "llm_url", id: "llmUrl" },
  { key: "llm_model", id: "llmModel" },
  { key: "temperature", id: "temperature" },
  { key: "max_tokens", id: "maxTokens" },
  { key: "history_turns", id: "historyTurns" },
  { key: "system", id: "systemPrompt" },
  { key: "tts_url", id: "ttsUrl" },
  { key: "tts_speed", id: "ttsSpeed" },
  { key: "tts_seed", id: "ttsSeed" },
  { key: "tts_volume", id: "ttsVolume" },
  { key: "tts_fade_ms", id: "ttsFadeMs" },
  { key: "vad_threshold", id: "vadThreshold" },
  { key: "vad_min_silence_ms", id: "vadMinSilence" },
  { key: "vad_speech_pad_ms", id: "vadSpeechPad" },
  { key: "pre_speech_ms", id: "preSpeech" },
  { key: "min_utterance_ms", id: "minUtterance" },
  { key: "max_utterance_sec", id: "maxUtterance" },
];

const NUM_FIELDS = new Set(["temperature", "max_tokens", "history_turns", "tts_speed", "tts_seed", "tts_volume", "tts_fade_ms", "vad_threshold", "vad_min_silence_ms", "vad_speech_pad_ms", "pre_speech_ms", "min_utterance_ms", "max_utterance_sec"]);

function fillConfig(config) {
  setPlaceholder("llmApiKey", config.llm_api_key_masked || "");
  for (const f of CONFIG_FIELD_MAP) {
    const val = config[f.key] !== undefined ? config[f.key] : DEFAULT_CONFIG[f.key];
    setValue(f.id, val);
  }
}

function collectConfig() {
  const result = {};
  for (const f of CONFIG_FIELD_MAP) {
    const raw = value(f.id, "");
    result[f.key] = NUM_FIELDS.has(f.key) ? (Number(raw) || DEFAULT_CONFIG[f.key]) : (raw || state.currentConfig[f.key] || DEFAULT_CONFIG[f.key]);
  }
  result.llm_api_key = value("llmApiKey", "").trim();
  return result;
}

/* ---- save ---- */

async function saveSettingsPage() {
  if (state.previewMode) { showToast("预览模式：无法保存", "info"); return; }
  try {
    await saveConfig(collectConfig());
    for (const s of state.services) { await updateServiceConfig(s.id, collectProfileConfig(s.id)); }
    await loadConfig(); fillConfig(state.currentConfig);
    await loadServices(); renderProfileList();
    showToast("配置已应用", "success");
  } catch (e) { showToast(`保存失败：${e.message}`, "error"); }
}

/* ---- profile cards ---- */

function renderProfileList() {
  const host = $("#profileList"); if (!host) return;
  host.innerHTML = "";
  for (const s of state.services) host.appendChild(createProfileCard(s));
  renderIcons();
  const hash = location.hash.replace("#", ""); if (hash) document.getElementById(`profile-${hash}`)?.scrollIntoView({ block: "center" });
}

function createProfileCard(service) {
  const card = document.createElement("section");
  const port = service.health_url ? new URL(service.health_url).port || "--" : "--";
  card.className = "profile-card"; card.id = `profile-${service.id}`; card.dataset.serviceId = service.id;
  card.innerHTML = `
    <div class="profile-head">
      <div><strong>${service.label || service.id}</strong><span>${service.description || ""}</span></div>
      <button class="small-button test-log" type="button"><i data-lucide="scroll-text"></i>日志</button>
    </div>
    <div class="profile-summary"><span>${service.running ? "运行中" : "待启动"}</span><span>端口 ${port}</span></div>
    <details class="advanced-profile" open>
      <summary><i data-lucide="sliders-horizontal"></i>高级启动参数</summary>
      <div class="inline-actions command-actions" style="margin-bottom:6px">
        <button class="small-button copy-command" type="button"><i data-lucide="copy"></i>复制命令</button>
      </div>
      <div class="form-grid">
        <label><span>Working Directory</span><input class="profile-cwd" type="text" /></label>
        <label><span>Health URL</span><input class="profile-health" type="text" /></label>
        <label><span>Startup Wait sec</span><input class="profile-wait" type="number" min="0" max="180" step="1" /></label>
        <label class="wide"><span>Start Command</span><textarea class="profile-command"></textarea></label>
      </div>
    </details>
  `;
  card.querySelector(".profile-cwd").value = service.cwd || "";
  card.querySelector(".profile-health").value = service.health_url || "";
  card.querySelector(".profile-wait").value = service.startup_wait_sec ?? 0;
  card.querySelector(".profile-command").value = service.command || "";
  card.querySelector(".test-log").addEventListener("click", () => { location.href = `/static/index.html#services`; });
  card.querySelector(".copy-command").addEventListener("click", async () => {
    const cmd = card.querySelector(".profile-command")?.value || ""; if (!cmd.trim()) return;
    try { await navigator.clipboard.writeText(cmd); showToast("已复制", "success"); } catch { showToast("复制失败", "error"); }
  });
  return card;
}

function collectProfileConfig(serviceId) {
  const card = document.querySelector(`[data-service-id="${serviceId}"]`);
  return {
    cwd: card?.querySelector(".profile-cwd")?.value.trim() || "",
    health_url: card?.querySelector(".profile-health")?.value.trim() || "",
    startup_wait_sec: Number(card?.querySelector(".profile-wait")?.value || 0),
    command: card?.querySelector(".profile-command")?.value.trim() || "",
  };
}
