/* ============================================================
   ui-settings.js — Configuration page (settings.html)
   LoveChoice Voice Console
   ============================================================ */

import { state, DEFAULT_CONFIG } from "./state.js";
import { $, setValue, value, setText, setPlaceholder, renderIcons, showToast } from "./utils.js";
import { loadConfig, saveConfig, loadServices, updateServiceConfig, loadMemory, addMemory, deleteMemory } from "./api.js";

/* ---- init ---- */

export async function initSettings() {
  setupSettingsEvents();

  const configResult = await loadConfig();
  fillConfig(configResult.config);
  renderCapabilityStatus(configResult.config);

  await loadServices();
  renderProfileList();
  await loadMemory();
  renderMemoryList();
  setText("systemState", configResult.ok ? "后端在线" : "静态预览");
}

function setupSettingsEvents() {
  $("#saveAllBtn")?.addEventListener("click", saveSettingsPage);
  $("#refreshMemoryBtn")?.addEventListener("click", () => loadMemory().then(renderMemoryList));
  $("#addMemoryBtn")?.addEventListener("click", handleAddMemory);

  // scrolling nav highlight
  window.addEventListener("scroll", highlightNavSection, { passive: true });
}

/* ---- nav highlight ---- */

function highlightNavSection() {
  const sections = document.querySelectorAll(".settings-panel[id]");
  const navLinks = document.querySelectorAll(".settings-nav a[href^='#']");
  let currentId = "";
  for (const section of sections) {
    const top = section.getBoundingClientRect().top;
    if (top < 180) currentId = section.id;
  }
  navLinks.forEach((link) => {
    link.classList.toggle("nav-active", link.getAttribute("href") === `#${currentId}`);
  });
}

/* ---- config form management ---- */

// Maps config keys to DOM element IDs for automated fill/collect
const CONFIG_FIELD_MAP = [
  { key: "asr_mode", id: "asrMode", type: "value" },
  { key: "asr_url", id: "asrUrl", type: "value" },
  { key: "asr_model", id: "asrModel", type: "value" },
  { key: "llm_url", id: "llmUrl", type: "value" },
  { key: "llm_model", id: "llmModel", type: "value" },
  { key: "temperature", id: "temperature", type: "value" },
  { key: "max_tokens", id: "maxTokens", type: "value" },
  { key: "history_turns", id: "historyTurns", type: "value" },
  { key: "system", id: "systemPrompt", type: "value" },
  { key: "memory_short_to_mid_days", id: "memoryShortToMidDays", type: "value" },
  { key: "memory_short_to_mid_count", id: "memoryShortToMidCount", type: "value" },
  { key: "memory_mid_to_long_days", id: "memoryMidToLongDays", type: "value" },
  { key: "memory_mid_to_long_count", id: "memoryMidToLongCount", type: "value" },
  { key: "memory_short_delete_days", id: "memoryShortDeleteDays", type: "value" },
  { key: "memory_mid_downgrade_days", id: "memoryMidDowngradeDays", type: "value" },
  { key: "memory_long_downgrade_days", id: "memoryLongDowngradeDays", type: "value" },
  { key: "memory_max_context_items", id: "memoryMaxContextItems", type: "value" },
  { key: "tts_url", id: "ttsUrl", type: "value" },
  { key: "tts_speed", id: "ttsSpeed", type: "value" },
  { key: "tts_seed", id: "ttsSeed", type: "value" },
  { key: "tts_volume", id: "ttsVolume", type: "value" },
  { key: "tts_fade_ms", id: "ttsFadeMs", type: "value" },
  { key: "vad_threshold", id: "vadThreshold", type: "value" },
  { key: "vad_min_silence_ms", id: "vadMinSilence", type: "value" },
  { key: "vad_speech_pad_ms", id: "vadSpeechPad", type: "value" },
  { key: "pre_speech_ms", id: "preSpeech", type: "value" },
  { key: "min_utterance_ms", id: "minUtterance", type: "value" },
  { key: "max_utterance_sec", id: "maxUtterance", type: "value" },
];

function fillConfig(config) {
  setPlaceholder("llmApiKey", config.llm_api_key_masked || "");
  for (const field of CONFIG_FIELD_MAP) {
    const val = config[field.key] !== undefined ? config[field.key] : DEFAULT_CONFIG[field.key];
    if (field.type === "value") setValue(field.id, val);
  }
}

function collectConfig() {
  const result = {};
  for (const field of CONFIG_FIELD_MAP) {
    const raw = value(field.id, "");
    if (field.id === "systemPrompt") {
      result[field.key] = raw.trim() || state.currentConfig.system || "";
      continue;
    }
    // numeric fields
    const numFields = ["temperature", "max_tokens", "history_turns",
      "memory_short_to_mid_days", "memory_short_to_mid_count", "memory_mid_to_long_days",
      "memory_mid_to_long_count", "memory_short_delete_days", "memory_mid_downgrade_days",
      "memory_long_downgrade_days", "memory_max_context_items",
      "tts_speed", "tts_seed", "tts_volume", "tts_fade_ms",
      "vad_threshold", "vad_min_silence_ms", "vad_speech_pad_ms",
      "pre_speech_ms", "min_utterance_ms", "max_utterance_sec"];
    if (numFields.includes(field.key)) {
      result[field.key] = Number(raw) || DEFAULT_CONFIG[field.key];
    } else {
      result[field.key] = raw || state.currentConfig[field.key] || DEFAULT_CONFIG[field.key];
    }
  }
  // special fields
  result.llm_api_key = value("llmApiKey", "").trim();
  result.tools_timeout = Number(value("toolsTimeout", "")) || DEFAULT_CONFIG.tools_timeout;
  result.tools_max_result_chars = Number(value("toolsMaxResultChars", "")) || DEFAULT_CONFIG.tools_max_result_chars;
  return result;
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

/* ---- save ---- */

async function saveSettingsPage() {
  if (state.previewMode) {
    showToast("预览模式：无法保存配置", "info");
    return;
  }
  try {
    await saveConfig(collectConfig());

    for (const service of state.services) {
      await updateServiceConfig(service.id, collectProfileConfig(service.id));
    }

    await loadConfig();
    fillConfig(state.currentConfig);
    await loadServices();
    renderProfileList();
    await loadMemory();
    renderMemoryList();
    renderCapabilityStatus(state.currentConfig);
    showToast("配置已应用", "success");
  } catch (error) {
    showToast(`保存失败：${error.message}`, "error");
  }
}

/* ---- profile cards (service command editor) ---- */

function renderProfileList() {
  const host = $("#profileList");
  if (!host) return;
  host.innerHTML = "";
  for (const service of state.services) host.appendChild(createProfileCard(service));
  renderIcons();
  const hash = location.hash.replace("#", "");
  if (hash) document.getElementById(`profile-${hash}`)?.scrollIntoView({ block: "center" });
}

function createProfileCard(service) {
  const card = document.createElement("section");
  const port = service.health_url ? new URL(service.health_url).port || "--" : "--";
  const stateLabel = service.running ? "运行中" : service.external ? "外部运行" : "待启动";
  const health = service.health ? (service.health.ok ? "健康" : "异常") : "待检查";
  card.className = "profile-card";
  card.id = `profile-${service.id}`;
  card.dataset.serviceId = service.id;
  card.innerHTML = `
    <div class="profile-head">
      <div>
        <strong></strong>
        <span></span>
      </div>
      <button class="small-button test-log" type="button"><i data-lucide="scroll-text"></i>日志</button>
    </div>
    <div class="profile-summary">
      <span>${stateLabel}</span>
      <span>${health}</span>
      <span>本地端口 ${port}</span>
    </div>
    <details class="advanced-profile" ${true ? "open" : ""}>
      <summary><i data-lucide="sliders-horizontal"></i>高级启动参数</summary>
      <div class="inline-actions command-actions">
        <button class="small-button copy-command" type="button"><i data-lucide="copy"></i>复制命令</button>
        <button class="small-button test-log" type="button"><i data-lucide="scroll-text"></i>查看日志</button>
      </div>
      <div class="form-grid">
        <label><span>Working Directory</span><input class="profile-cwd" type="text" /></label>
        <label><span>Health URL</span><input class="profile-health" type="text" /></label>
        <label><span>Startup Wait sec</span><input class="profile-wait" type="number" min="0" max="180" step="1" /></label>
        <label class="wide"><span>Start Command</span><textarea class="profile-command"></textarea></label>
      </div>
    </details>
  `;
  card.querySelector("strong").textContent = service.label || service.id;
  card.querySelector(".profile-head span").textContent = service.description || "";
  card.querySelector(".profile-cwd").value = service.cwd || "";
  card.querySelector(".profile-health").value = service.health_url || "";
  card.querySelector(".profile-wait").value = service.startup_wait_sec ?? 0;
  card.querySelector(".profile-command").value = service.command || "";
  card.querySelectorAll(".test-log").forEach((btn) => btn.addEventListener("click", () => {
    location.href = `/static/services.html#logs-${service.id}`;
  }));
  card.querySelector(".copy-command")?.addEventListener("click", async () => {
    const command = card.querySelector(".profile-command")?.value || "";
    if (!command.trim()) return;
    try { await navigator.clipboard.writeText(command); showToast("命令已复制", "success"); }
    catch { showToast("复制失败", "error"); }
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

/* ---- memory UI ---- */

function renderMemoryList() {
  const host = $("#memoryList");
  if (!host) return;
  host.innerHTML = "";
  if (!state.memories.length) {
    host.innerHTML = `<div class="runtime-row"><div><strong>暂无记忆</strong><span>聊天或手动新增后会出现在这里。</span></div></div>`;
    return;
  }
  for (const item of state.memories) {
    const row = document.createElement("div");
    row.className = "runtime-row";
    row.innerHTML = `
      <div>
        <strong></strong>
        <span></span>
        <code></code>
      </div>
      <button class="small-button delete-memory" type="button"><i data-lucide="trash-2"></i>删除</button>
    `;
    row.querySelector("strong").textContent = `[${item.layer}] ${item.key}`;
    row.querySelector("span").textContent = item.value || "";
    row.querySelector("code").textContent = `记录 ${item.count || 0} 次 · 最近 ${item.last_seen_text || "--"}`;
    row.querySelector(".delete-memory").addEventListener("click", () => handleDeleteMemory(item.id));
    host.appendChild(row);
  }
  renderIcons();
}

async function handleAddMemory() {
  if (state.previewMode) return;
  const val = window.prompt("要记住什么？");
  if (!val?.trim()) return;
  try {
    await addMemory(val.trim());
    await loadMemory();
    renderMemoryList();
    showToast("记忆已新增", "success");
  } catch (error) { showToast(`新增记忆失败：${error.message}`, "error"); }
}

async function handleDeleteMemory(id) {
  if (!id || state.previewMode) return;
  if (!window.confirm("删除这条记忆？")) return;
  try {
    await deleteMemory(id);
    await loadMemory();
    renderMemoryList();
    showToast("记忆已删除", "success");
  } catch (error) { showToast(`删除记忆失败：${error.message}`, "error"); }
}
