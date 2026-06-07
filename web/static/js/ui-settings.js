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

  // 同步主题 toggle 初始状态
  const savedTheme = localStorage.getItem("lovechoice.theme") || "dark";
  document.querySelectorAll("#themeToggle button").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.theme === savedTheme);
  });

  const configResult = await loadConfig();
  fillConfig(configResult.config);
  // 即使 loadConfig 失败也调 fillConfig(DEFAULT_CONFIG)
  if (!configResult.ok) fillConfig(configResult.config);
  await loadServices();
  renderProfileList();
  setText("topStatus", configResult.ok ? "后端在线" : "静态预览");
}

function setupSettingsEvents() {
  $("#saveAllBtn")?.addEventListener("click", saveSettingsPage);
  document.querySelectorAll("#themeToggle button").forEach((btn) => {
    btn.addEventListener("click", () => {
      const theme = btn.dataset.theme;
      if (window.__lovechoice) {
        window.__lovechoice.setTheme(theme);
      } else {
        // 降级方案：直接操作 DOM + localStorage
        if (theme === "light") {
          document.documentElement.classList.add("theme-light");
        } else {
          document.documentElement.classList.remove("theme-light");
        }
        localStorage.setItem("lovechoice.theme", theme);
        document.querySelectorAll("#themeToggle button").forEach((b) => {
          b.classList.toggle("active", b.dataset.theme === theme);
        });
      }
    });
  });
  window.addEventListener("scroll", highlightNavSection, { passive: true });
  // nav link clicks: scroll to section
  document.querySelectorAll("[data-setting-nav]").forEach((link) => {
    link.addEventListener("click", (e) => {
      e.preventDefault();
      const id = link.dataset.settingNav;
      document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  });
}

function highlightNavSection() {
  const sections = document.querySelectorAll(".settings-panel[id], .theme-section[id]");
  const navLinks = document.querySelectorAll("[data-setting-nav]");
  let currentId = "";
  for (const s of sections) {
    if (s.getBoundingClientRect().top < 160) currentId = s.id;
  }
  navLinks.forEach((l) => {
    const isActive = l.getAttribute("data-setting-nav") === currentId;
    l.classList.toggle("nav-active", isActive);
  });
}

/* ---- checkbox helpers ---- */

function setChecked(id, val) {
  const el = document.getElementById(id);
  if (el) el.checked = Boolean(val);
}

function checked(id, defaultVal) {
  const el = document.getElementById(id);
  if (el) return el.checked;
  return defaultVal;
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
  { key: "tts_sample_rate", id: "ttsSampleRate" },
  { key: "vad_threshold", id: "vadThreshold" },
  { key: "vad_min_silence_ms", id: "vadMinSilence" },
  { key: "vad_speech_pad_ms", id: "vadSpeechPad" },
  { key: "pre_speech_ms", id: "preSpeech" },
  { key: "min_utterance_ms", id: "minUtterance" },
  { key: "max_utterance_sec", id: "maxUtterance" },
  { key: "memory_short_to_mid_days", id: "memoryShortToMidDays" },
  { key: "memory_short_to_mid_count", id: "memoryShortToMidCount" },
  { key: "memory_mid_to_long_days", id: "memoryMidToLongDays" },
  { key: "memory_mid_to_long_count", id: "memoryMidToLongCount" },
  { key: "memory_short_delete_days", id: "memoryShortDeleteDays" },
  { key: "memory_mid_downgrade_days", id: "memoryMidDowngradeDays" },
  { key: "memory_long_downgrade_days", id: "memoryLongDowngradeDays" },
  { key: "memory_max_context_items", id: "memoryMaxContextItems" },
  { key: "tools_timeout", id: "toolsTimeout" },
  { key: "tools_max_result_chars", id: "toolsMaxResultChars" },
];

const NUM_FIELDS = new Set(["temperature", "max_tokens", "history_turns", "tts_speed", "tts_seed", "tts_volume", "tts_fade_ms", "tts_sample_rate", "vad_threshold", "vad_min_silence_ms", "vad_speech_pad_ms", "pre_speech_ms", "min_utterance_ms", "max_utterance_sec", "memory_short_to_mid_days", "memory_short_to_mid_count", "memory_mid_to_long_days", "memory_mid_to_long_count", "memory_short_delete_days", "memory_mid_downgrade_days", "memory_long_downgrade_days", "memory_max_context_items", "tools_timeout", "tools_max_result_chars"]);

function fillConfig(config) {
  setPlaceholder("llmApiKey", config.llm_api_key_masked || "");
  for (const f of CONFIG_FIELD_MAP) {
    const val = config[f.key] !== undefined ? config[f.key] : DEFAULT_CONFIG[f.key];
    setValue(f.id, val);
  }
  setChecked("memoryEnabled", config.memory_enabled ?? true);
  setChecked("memoryExtractEnabled", config.memory_extract_enabled ?? true);
  setChecked("toolsEnabled", config.tools_enabled ?? true);
  setChecked("toolsAutoCall", config.tools_auto_call ?? true);
}

function collectConfig() {
  const result = {};
  for (const f of CONFIG_FIELD_MAP) {
    const raw = value(f.id, "");
    result[f.key] = NUM_FIELDS.has(f.key) ? (Number(raw) || DEFAULT_CONFIG[f.key]) : (raw || state.currentConfig[f.key] || DEFAULT_CONFIG[f.key]);
  }
  result.llm_api_key = value("llmApiKey", "").trim();
  result.memory_enabled = checked("memoryEnabled", true);
  result.memory_extract_enabled = checked("memoryExtractEnabled", true);
  result.tools_enabled = checked("toolsEnabled", true);
  result.tools_auto_call = checked("toolsAutoCall", true);
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
    <details class="advanced-profile">
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
