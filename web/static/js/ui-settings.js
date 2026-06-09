/* ============================================================
   ui-settings.js — Configuration page (settings.html)
   BranchWhisper · Precision Console
   ============================================================ */

import { state, DEFAULT_CONFIG } from "./state.js";
import { $, setValue, value, setText, setPlaceholder, renderIcons, showToast, createIcon, safePort } from "./utils.js";
import { loadConfig, saveConfig, loadServices, updateServiceConfig } from "./api.js";

/* ---- init ---- */

let eventsBound = false;

export async function initSettings() {
  setupSettingsEvents();

  // 同步主题 toggle 初始状态
  const savedTheme = window.__branchwhisper?.getTheme?.() || "dark";
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

export function leaveSettings() {
  window.removeEventListener("scroll", highlightNavSection);
}

function setupSettingsEvents() {
  if (eventsBound) {
    window.addEventListener("scroll", highlightNavSection, { passive: true });
    return;
  }
  eventsBound = true;
  $("#saveAllBtn")?.addEventListener("click", saveSettingsPage);
  document.querySelectorAll("#themeToggle button").forEach((btn) => {
    btn.addEventListener("click", () => {
      const theme = btn.dataset.theme;
      if (window.__branchwhisper) {
        window.__branchwhisper.setTheme(theme);
      } else {
        // 降级方案：直接操作 DOM + localStorage
        if (theme === "light") {
          document.documentElement.classList.add("theme-light");
        } else {
          document.documentElement.classList.remove("theme-light");
        }
        localStorage.setItem("branchwhisper.theme", theme);
        document.querySelectorAll("#themeToggle button").forEach((b) => {
          b.classList.toggle("active", b.dataset.theme === theme);
        });
      }
    });
  });
  window.addEventListener("scroll", highlightNavSection, { passive: true });
  // nav link clicks: scroll to section + 立即高亮避免 smooth scroll 延迟
  document.querySelectorAll("[data-setting-nav]").forEach((link) => {
    link.addEventListener("click", (e) => {
      e.preventDefault();
      const id = link.dataset.settingNav;
      const target = document.getElementById(id);
      if (!target) return;
      // 立即高亮，不等 scroll 事件触发
      document.querySelectorAll("[data-setting-nav]").forEach((l) => {
        l.classList.toggle("nav-active", l.dataset.settingNav === id);
      });
      target.scrollIntoView({ behavior: "smooth", block: "start" });
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
  { key: "ui_font_scale", id: "uiFontScale" },
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
  { key: "tools_timeout", id: "toolsTimeout" },
  { key: "tools_max_result_chars", id: "toolsMaxResultChars" },
];

const NUM_FIELDS = new Set(["temperature", "max_tokens", "history_turns", "ui_font_scale", "tts_speed", "tts_seed", "tts_volume", "tts_fade_ms", "tts_sample_rate", "vad_threshold", "vad_min_silence_ms", "vad_speech_pad_ms", "pre_speech_ms", "min_utterance_ms", "max_utterance_sec", "tools_timeout", "tools_max_result_chars"]);

function fillConfig(config) {
  setPlaceholder("llmApiKey", config.llm_api_key_masked || "");
  for (const f of CONFIG_FIELD_MAP) {
    if (!document.getElementById(f.id)) continue;
    const val = config[f.key] !== undefined ? config[f.key] : DEFAULT_CONFIG[f.key];
    setValue(f.id, val);
  }
  const keyState = config.llm_api_key_set ? "已保存" : "未保存";
  setText("apiKeyState", keyState);
}

function collectConfig() {
  const result = {};
  for (const f of CONFIG_FIELD_MAP) {
    if (!document.getElementById(f.id)) continue;
    const raw = value(f.id, "");
    if (NUM_FIELDS.has(f.key)) {
      const parsed = Number(raw);
      result[f.key] = Number.isFinite(parsed) ? parsed : DEFAULT_CONFIG[f.key];
    } else {
      result[f.key] = raw || state.currentConfig[f.key] || DEFAULT_CONFIG[f.key];
    }
  }
  const apiKey = value("llmApiKey", "").trim();
  if (apiKey) {
    result.llm_api_key = apiKey;
  }
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
  const port = safePort(service.health_url);
  card.className = "profile-card"; card.id = `profile-${service.id}`; card.dataset.serviceId = service.id;
  const head = document.createElement("div");
  head.className = "profile-head";
  const title = document.createElement("div");
  const strong = document.createElement("strong");
  strong.textContent = service.label || service.id;
  const desc = document.createElement("span");
  desc.textContent = service.description || "";
  title.append(strong, desc);
  const logBtn = document.createElement("button");
  logBtn.className = "small-button test-log";
  logBtn.type = "button";
  logBtn.append(createIcon("scroll-text"), document.createTextNode("日志"));
  head.append(title, logBtn);

  const summary = document.createElement("div");
  summary.className = "profile-summary";
  const running = document.createElement("span");
  running.textContent = service.running ? "运行中" : "待启动";
  const portSpan = document.createElement("span");
  portSpan.textContent = `端口 ${port}`;
  summary.append(running, portSpan);

  const details = document.createElement("details");
  details.className = "advanced-profile";
  const summaryToggle = document.createElement("summary");
  summaryToggle.append(createIcon("sliders-horizontal"), document.createTextNode("高级启动参数"));
  const commandActions = document.createElement("div");
  commandActions.className = "inline-actions command-actions";
  commandActions.style.marginBottom = "6px";
  const copyBtn = document.createElement("button");
  copyBtn.className = "small-button copy-command";
  copyBtn.type = "button";
  copyBtn.append(createIcon("copy"), document.createTextNode("复制命令"));
  commandActions.appendChild(copyBtn);
  const form = document.createElement("div");
  form.className = "form-grid";
  form.append(
    profileField("Working Directory", "input", "profile-cwd"),
    profileField("Health URL", "input", "profile-health"),
    profileField("Startup Wait sec", "input", "profile-wait", { type: "number", min: "0", max: "180", step: "1" }),
    profileField("Start Command", "textarea", "profile-command", { wide: true }),
  );
  details.append(summaryToggle, commandActions, form);
  card.append(head, summary, details);
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

function profileField(labelText, tag, className, options = {}) {
  const label = document.createElement("label");
  if (options.wide) label.className = "wide";
  const span = document.createElement("span");
  span.textContent = labelText;
  const field = document.createElement(tag);
  field.className = className;
  if (options.type) field.type = options.type;
  for (const attr of ["min", "max", "step"]) {
    if (options[attr]) field.setAttribute(attr, options[attr]);
  }
  label.append(span, field);
  return label;
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
