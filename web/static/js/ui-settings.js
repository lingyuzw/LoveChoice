/* ============================================================
   ui-settings.js — Configuration page (settings.html)
   BranchWhisper · Precision Console
   ============================================================ */

import { state, DEFAULT_CONFIG } from "./state.js";
import { $, setValue, value, setText, setPlaceholder, renderIcons, showToast, createIcon, safePort } from "./utils.js";
import {
  createReminder,
  createBotProfile,
  deleteBotProfile,
  deleteReminder,
  loadBotProfiles,
  loadConfig,
  loadProactiveConfig,
  loadProactiveEvents,
  loadReminders,
  loadServices,
  loadStickers,
  loadToolConfig,
  resolveTool,
  saveConfig,
  saveProactiveConfig,
  saveToolConfig,
  testProactiveMessage,
  testTool,
  updateBotProfile,
  updateServiceConfig,
  uploadAvatar,
  uploadSticker,
  deleteSticker,
} from "./api.js";

/* ---- init ---- */

let eventsBound = false;
let toolProviderDraft = {};
let editingToolProvider = "";
let activeSettingsSection = "";
const SETTINGS_SECTION_LABELS = {
  appearance: "外观",
  engine: "本地模型",
  tools: "联网工具",
  dialogFeatures: "素材与对话",
  proactive: "主动性",
  botProfiles: "人格",
  prompt: "Prompt 配置",
  tts: "语音合成",
  vad: "语音检测",
  commands: "服务命令",
};
const MODAL_SETTING_SECTIONS = new Set(["dialogFeatures", "proactive", "botProfiles", "prompt", "tts", "vad", "commands"]);
const CHAT_IDENTITY = {
  user: {
    nameKey: "web_user_name",
    avatarKey: "web_user_avatar_url",
    nameId: "webUserName",
    fileId: "webUserAvatarFile",
    buttonId: "webUserAvatarBtn",
    clearId: "webUserAvatarClearBtn",
    previewId: "webUserAvatarPreview",
    fallbackName: "我",
    fallbackInitial: "我",
  },
  assistant: {
    nameKey: "web_assistant_name",
    avatarKey: "web_assistant_avatar_url",
    nameId: "webAssistantName",
    fileId: "webAssistantAvatarFile",
    buttonId: "webAssistantAvatarBtn",
    clearId: "webAssistantAvatarClearBtn",
    previewId: "webAssistantAvatarPreview",
    fallbackName: "枝语",
    fallbackInitial: "枝",
  },
};

export async function initSettings() {
  setupSettingsEvents();

  // 同步主题 toggle 初始状态
  const savedTheme = window.__branchwhisper?.getTheme?.() || "dark";
  document.querySelectorAll("#themeToggle button").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.theme === savedTheme);
  });

  const configResult = await loadConfig();
  await Promise.allSettled([loadToolConfig(), loadBotProfiles(), loadProactiveConfig(), loadProactiveEvents(), loadReminders(), loadStickers()]);
  syncToolProviderDraft();
  fillConfig(configResult.config);
  renderToolProviders();
  fillProactiveConfig();
  renderProactiveEvents();
  renderReminders();
  prepareSettingsSectionModal();
  refreshSettingsOverview();
  // 即使 loadConfig 失败也调 fillConfig(DEFAULT_CONFIG)
  if (!configResult.ok) fillConfig(configResult.config);
  await loadServices();
  renderProfileList();
  renderBotProfiles();
  renderStickerLibrary();
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
  $("#saveAllBtn")?.addEventListener("click", () => saveSettingsPage({ closeModal: false }));
  $("#saveAllHeroBtn")?.addEventListener("click", () => saveSettingsPage({ closeModal: false }));
  $("#settingsSectionSaveBtn")?.addEventListener("click", () => saveSettingsPage({ closeModal: true }));
  $("#settingsSectionCancelBtn")?.addEventListener("click", closeSettingsSectionModal);
  document.querySelector("#settingsSectionModal .modal-close")?.addEventListener("click", closeSettingsSectionModal);
  $("#settingsSectionModal")?.addEventListener("click", (event) => {
    if (event.target === event.currentTarget) closeSettingsSectionModal();
  });
  document.querySelectorAll("[data-open-config]").forEach((card) => {
    card.addEventListener("click", () => openSettingsSectionModal(card.dataset.openConfig));
  });
  $("#toolResolveBtn")?.addEventListener("click", runToolResolve);
  $("#toolResolveClearBtn")?.addEventListener("click", clearToolResolve);
  $("#proactiveTestBtn")?.addEventListener("click", runProactiveTest);
  $("#createReminderBtn")?.addEventListener("click", handleCreateReminder);
  $("#addBotProfileBtn")?.addEventListener("click", addBotProfile);
  $("#stickerUploadBtn")?.addEventListener("click", () => $("#stickerFileInput")?.click());
  $("#stickerFileInput")?.addEventListener("change", handleStickerUpload);
  bindChatIdentityEvents();
  $("#toolProviderCancelBtn")?.addEventListener("click", closeToolProviderModal);
  $("#toolProviderApplyBtn")?.addEventListener("click", applyToolProviderModal);
  document.querySelector("#toolProviderModal .modal-close")?.addEventListener("click", closeToolProviderModal);
  $("#toolProviderModal")?.addEventListener("click", (event) => {
    if (event.target === event.currentTarget) closeToolProviderModal();
  });
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
      if (id) {
        openSettingsSectionModal(id);
      }
      // 立即高亮，不等 scroll 事件触发
      document.querySelectorAll("[data-setting-nav]").forEach((l) => {
        l.classList.toggle("nav-active", l.dataset.settingNav === id);
      });
    });
  });
}

function highlightNavSection() {
  const navLinks = document.querySelectorAll("[data-setting-nav]");
  navLinks.forEach((l) => {
    l.classList.toggle("nav-active", l.getAttribute("data-setting-nav") === activeSettingsSection);
  });
}

function prepareSettingsSectionModal() {
  document.querySelectorAll(".settings-content > .theme-section[id], .settings-content > .settings-panel[id]").forEach((section) => {
    section.classList.toggle("settings-section-detached", MODAL_SETTING_SECTIONS.has(section.id));
  });
}

function openSettingsSectionModal(id) {
  const section = document.getElementById(id);
  const body = $("#settingsSectionModalBody");
  if (!section) return;
  if (!MODAL_SETTING_SECTIONS.has(id)) {
    activeSettingsSection = id;
    section.scrollIntoView({ behavior: "smooth", block: "start" });
    highlightNavSection();
    return;
  }
  if (!body) return;
  activeSettingsSection = id;
  setText("settingsSectionModalTitle", SETTINGS_SECTION_LABELS[id] || "配置");
  body.innerHTML = "";
  body.appendChild(section);
  section.classList.remove("settings-section-detached");
  $("#settingsSectionModal").hidden = false;
  highlightNavSection();
  renderIcons();
}

function closeSettingsSectionModal() {
  const modal = $("#settingsSectionModal");
  const body = $("#settingsSectionModalBody");
  const grid = $("#settingsOverviewGrid");
  if (body && grid) {
    for (const section of Array.from(body.children)) {
      section.classList.add("settings-section-detached");
      grid.after(section);
    }
    restoreSettingsSectionOrder();
  }
  if (modal) modal.hidden = true;
  activeSettingsSection = "";
  highlightNavSection();
}

function restoreSettingsSectionOrder() {
  const grid = $("#settingsOverviewGrid");
  const content = grid?.parentElement;
  if (!grid || !content) return;
  let anchor = grid;
  for (const id of Object.keys(SETTINGS_SECTION_LABELS)) {
    const section = document.getElementById(id);
    if (!section || section.parentElement !== content) continue;
    if (MODAL_SETTING_SECTIONS.has(id)) section.classList.add("settings-section-detached");
    else section.classList.remove("settings-section-detached");
    anchor.after(section);
    anchor = section;
  }
}

function refreshSettingsOverview() {
  setText("appearanceSummary", `${window.__branchwhisper?.getTheme?.() === "light" ? "浅色" : "深色"} · 字号 ${value("uiFontScale", 1)}`);
  setText("engineSummary", `${value("llmModel", "本地模型")} · ${value("historyTurns", 8)} 轮`);
  setText("toolsSummary", `${value("toolsEnabled", "true") === "true" ? "已启用" : "已关闭"} · ${Object.keys(toolProviderDraft || {}).filter((key) => (toolProviderDraft[key] || {}).enabled !== false).length} 项`);
  setText("dialogFeaturesSummary", `${value("visionEnabled", "true") === "true" ? "图片开" : "图片关"} · 表情 ${value("stickerActivity", "active")} · 压缩 ${value("contextCompactionEnabled", "true") === "true" ? "开" : "关"}`);
  setText("proactiveSummary", `${value("proactiveEnabled", "false") === "true" ? "已开启" : "已关闭"} · ${value("followupLevel", "restrained")}`);
  setText("botProfileSummary", `${(state.botProfiles || []).length || 1} 个 Profile`);
  setText("promptSummary", `${(value("systemPrompt", "") || "").length} 字`);
  setText("ttsSummary", `${value("ttsSpeed", 1)}x · ${value("ttsSampleRate", 24000)}Hz`);
  setText("vadSummary", `阈值 ${value("vadThreshold", 0.5)} · 静音 ${value("vadMinSilence", 500)}ms`);
  setText("commandsSummary", `${(state.services || []).length} 个服务`);
  const stickerCount = (state.stickers || []).length;
  setText("stickerLibrarySummary", `${stickerCount} 个素材 · 按标签管理，后续可扩展角色素材包`);
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
  { key: "web_user_name", id: "webUserName" },
  { key: "web_user_avatar_url", id: "webUserAvatarFile", virtual: true },
  { key: "web_assistant_name", id: "webAssistantName" },
  { key: "web_assistant_avatar_url", id: "webAssistantAvatarFile", virtual: true },
  { key: "system", id: "systemPrompt" },
  { key: "tools_enabled", id: "toolsEnabled" },
  { key: "tools_auto_call", id: "toolsAutoCall" },
  { key: "vision_enabled", id: "visionEnabled" },
  { key: "vision_url", id: "visionUrl" },
  { key: "vision_model", id: "visionModel" },
  { key: "vision_timeout", id: "visionTimeout" },
  { key: "vision_max_image_mb", id: "visionMaxImageMb" },
  { key: "vision_memory_extract_enabled", id: "visionMemoryExtractEnabled" },
  { key: "stickers_enabled", id: "stickersEnabled" },
  { key: "sticker_activity", id: "stickerActivity" },
  { key: "sticker_cooldown_sec", id: "stickerCooldownSec" },
  { key: "sticker_daily_limit", id: "stickerDailyLimit" },
  { key: "sticker_max_streak", id: "stickerMaxStreak" },
  { key: "sticker_custom_probability", id: "stickerCustomProbability" },
  { key: "context_compaction_enabled", id: "contextCompactionEnabled" },
  { key: "context_window_tokens", id: "contextWindowTokens" },
  { key: "context_compaction_ratio", id: "contextCompactionRatio" },
  { key: "context_keep_recent_turns", id: "contextKeepRecentTurns" },
  { key: "context_summary_max_chars", id: "contextSummaryMaxChars" },
  { key: "context_summary_max_layers", id: "contextSummaryMaxLayers" },
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

const NUM_FIELDS = new Set(["temperature", "max_tokens", "history_turns", "ui_font_scale", "tts_speed", "tts_seed", "tts_volume", "tts_fade_ms", "tts_sample_rate", "vad_threshold", "vad_min_silence_ms", "vad_speech_pad_ms", "pre_speech_ms", "min_utterance_ms", "max_utterance_sec", "tools_timeout", "tools_max_result_chars", "vision_timeout", "vision_max_image_mb", "sticker_cooldown_sec", "sticker_daily_limit", "sticker_max_streak", "sticker_custom_probability", "context_window_tokens", "context_compaction_ratio", "context_keep_recent_turns", "context_summary_max_chars", "context_summary_max_layers"]);

function fillConfig(config) {
  for (const f of CONFIG_FIELD_MAP) {
    if (f.virtual) continue;
    if (!document.getElementById(f.id)) continue;
    const val = config[f.key] !== undefined ? config[f.key] : DEFAULT_CONFIG[f.key];
    setValue(f.id, val);
  }
  renderChatIdentities(config);
}

function collectConfig() {
  const result = {};
  for (const f of CONFIG_FIELD_MAP) {
    if (f.virtual) continue;
    if (!document.getElementById(f.id)) continue;
    const raw = value(f.id, "");
    if (NUM_FIELDS.has(f.key)) {
      const parsed = Number(raw);
      result[f.key] = Number.isFinite(parsed) ? parsed : DEFAULT_CONFIG[f.key];
    } else {
      result[f.key] = raw || state.currentConfig[f.key] || DEFAULT_CONFIG[f.key];
    }
  }
  for (const def of Object.values(CHAT_IDENTITY)) {
    result[def.nameKey] = value(def.nameId, state.currentConfig[def.nameKey] || def.fallbackName).trim() || def.fallbackName;
    result[def.avatarKey] = state.currentConfig[def.avatarKey] || "";
  }
  return result;
}

function bindChatIdentityEvents() {
  for (const [role, def] of Object.entries(CHAT_IDENTITY)) {
    const file = $(`#${def.fileId}`);
    const button = $(`#${def.buttonId}`);
    const clear = $(`#${def.clearId}`);
    button?.addEventListener("click", () => file?.click());
    file?.addEventListener("change", (event) => handleChatAvatar(role, event));
    clear?.addEventListener("click", () => {
      state.currentConfig[def.avatarKey] = "";
      if (file) file.value = "";
      renderChatIdentity(role, state.currentConfig);
    });
  }
}

function renderChatIdentities(config = state.currentConfig) {
  for (const role of Object.keys(CHAT_IDENTITY)) renderChatIdentity(role, config);
}

function renderChatIdentity(role, config = state.currentConfig) {
  const def = CHAT_IDENTITY[role];
  if (!def) return;
  const name = String(config[def.nameKey] || def.fallbackName).trim() || def.fallbackName;
  const avatarUrl = String(config[def.avatarKey] || "").trim();
  const preview = $(`#${def.previewId}`);
  if (!preview) return;
  preview.innerHTML = "";
  if (avatarUrl) {
    const img = document.createElement("img");
    img.src = avatarUrl;
    img.alt = name;
    preview.appendChild(img);
  } else {
    preview.textContent = firstIdentityChar(name, def.fallbackInitial);
  }
}

async function handleChatAvatar(role, event) {
  const def = CHAT_IDENTITY[role];
  const file = event.target.files?.[0];
  if (!def || !file) return;
  try {
    const dataUrl = await fileToDataUrl(file);
    const result = await uploadAvatar(dataUrl);
    state.currentConfig[def.avatarKey] = result.asset?.url || "";
    renderChatIdentity(role, state.currentConfig);
  } catch (e) {
    showToast(`头像上传失败：${e.message}`, "error");
  }
}

function firstIdentityChar(name, fallback) {
  const chars = Array.from(String(name || "").trim());
  return chars[0] || fallback;
}

const PROVIDER_FIELDS = {
  weather: ["enabled", "provider", "base_url", "api_key", "default_location"],
  search: ["enabled", "provider", "base_url", "api_key", "limit"],
  news: ["enabled", "provider", "base_url", "api_key", "region", "limit"],
  finance: ["enabled", "provider", "base_url", "api_key"],
  map: ["enabled", "provider", "base_url", "api_key"],
  url_fetch: ["enabled", "user_agent", "max_chars"],
  reminder: ["enabled", "web_enabled", "weixin_enabled", "webhook_url"],
};

const PROVIDER_LABELS = {
  weather: "天气",
  search: "搜索",
  news: "新闻",
  finance: "财经",
  map: "地图",
  url_fetch: "网页读取",
  reminder: "提醒通知",
};

const PROVIDER_OPTIONS = {
  weather: [
    ["gaode", "高德天气"],
    ["wttr", "wttr.in 免密天气"],
  ],
  search: [
    ["gaode", "高德地点搜索"],
    ["duckduckgo", "DuckDuckGo 网页搜索"],
  ],
  news: [
    ["google_rss", "Google News RSS"],
    ["search", "网页搜索兜底"],
  ],
  finance: [
    ["search", "网页搜索兜底"],
  ],
  map: [
    ["gaode", "高德地图 Web服务"],
  ],
  url_fetch: [
    ["built-in", "内置网页读取"],
  ],
  reminder: [
    ["default", "内置提醒"],
  ],
};

const PROVIDER_DEFAULTS = {
  weather: {
    wttr: { base_url: "https://wttr.in" },
    gaode: { base_url: "https://restapi.amap.com/v3" },
  },
  search: {
    duckduckgo: { base_url: "https://duckduckgo.com/html/" },
    gaode: { base_url: "https://restapi.amap.com/v3" },
  },
  news: {
    google_rss: { base_url: "https://news.google.com/rss" },
    search: { base_url: "" },
  },
  finance: {
    search: { base_url: "" },
  },
  map: {
    gaode: { base_url: "https://restapi.amap.com/v3" },
  },
};

function renderToolProviders() {
  const host = $("#toolProviderGrid");
  if (!host) return;
  host.innerHTML = "";
  for (const key of Object.keys(PROVIDER_FIELDS)) {
    const provider = toolProviderDraft[key] || {};
    const card = document.createElement("section");
    card.className = `tool-provider-card overview-card ${provider.enabled === false ? "disabled" : "enabled"}`;
    card.dataset.providerKey = key;
    const head = document.createElement("div");
    head.className = "tool-provider-head";
    head.innerHTML = `<strong>${PROVIDER_LABELS[key] || key}</strong><small>${key}</small>`;
    const status = document.createElement("div");
    status.className = "tool-provider-status";
    const providerName = provider.provider || (key === "url_fetch" ? "built-in" : "default");
    const secretState = providerSecretState(key, provider);
    status.innerHTML = `
      <span>${provider.enabled === false ? "已关闭" : "已启用"}</span>
      <span>${escapeHtml(providerName)}</span>
      <span>${secretState}</span>
    `;
    const action = document.createElement("button");
    action.className = "secondary-action";
    action.type = "button";
    action.append(createIcon("sliders-horizontal"), document.createTextNode("配置"));
    action.addEventListener("click", () => openToolProviderModal(key));
    const test = document.createElement("button");
    test.className = "secondary-action tool-provider-test";
    test.type = "button";
    test.append(createIcon("activity"), document.createTextNode("测试"));
    test.addEventListener("click", () => runProviderTest(key));
    const actions = document.createElement("div");
    actions.className = "tool-provider-actions";
    actions.append(action, test);
    card.append(head, status, actions);
    host.appendChild(card);
  }
  renderIcons();
}

function collectToolConfig() {
  const result = {
    enabled: value("toolsEnabled", "true") === "true",
    auto_call: value("toolsAutoCall", "true") === "true",
    timeout: Number(value("toolsTimeout", state.currentConfig.tools_timeout || 12)),
    max_result_chars: Number(value("toolsMaxResultChars", state.currentConfig.tools_max_result_chars || 4000)),
  };
  for (const key of Object.keys(PROVIDER_FIELDS)) {
    const provider = structuredCloneSafe(toolProviderDraft[key] || {});
    stripProviderRuntimeFields(provider);
    result[key] = provider;
  }
  return result;
}

function syncToolProviderDraft() {
  toolProviderDraft = structuredCloneSafe(state.toolConfig || {});
}

function openToolProviderModal(key) {
  editingToolProvider = key;
  const provider = normalizeProviderDraft(key, toolProviderDraft[key] || {});
  setText("toolProviderModalTitle", `${PROVIDER_LABELS[key] || key}配置`);
  const summary = $("#toolProviderModalSummary");
  if (summary) {
    const secretState = providerSecretState(key, provider);
    summary.innerHTML = `
      <span>${provider.enabled === false ? "当前关闭" : "当前启用"}</span>
      <span>Provider: ${escapeHtml(provider.provider || "default")}</span>
      <span>${secretState}</span>
    `;
  }
  const host = $("#toolProviderModalFields");
  if (host) {
    host.innerHTML = "";
    for (const field of PROVIDER_FIELDS[key] || []) {
      const label = document.createElement("label");
      const span = document.createElement("span");
      span.textContent = fieldLabel(field);
      const current = provider[field];
      const input = document.createElement(field === "enabled" || field.endsWith("_enabled") || field === "provider" ? "select" : "input");
      input.dataset.providerField = field;
      if (field === "provider") {
        const options = PROVIDER_OPTIONS[key] || [["default", "默认"]];
        input.innerHTML = options.map(([value, label]) => `<option value="${escapeAttr(value)}">${escapeHtml(label)}</option>`).join("");
        input.value = String(current || options[0]?.[0] || "default");
        input.addEventListener("change", () => applyProviderSelectionDefaults(key, input.value));
      } else if (input.tagName === "SELECT") {
        input.innerHTML = `<option value="true">启用</option><option value="false">关闭</option>`;
        input.value = String(current ?? true);
      } else {
        input.type = field.includes("key") || field.includes("webhook") ? "password" : "text";
        input.placeholder = provider[`${field}_masked`] || "";
        input.value = field.includes("key") || field.includes("webhook") ? "" : (current ?? "");
      }
      label.append(span, input);
      host.appendChild(label);
    }
  }
  $("#toolProviderModal").hidden = false;
  renderIcons();
}

async function applyToolProviderModal() {
  if (!editingToolProvider) return;
  const button = $("#toolProviderApplyBtn");
  if (button) {
    button.disabled = true;
    button.dataset.originalText = button.textContent || "保存工具配置";
    button.textContent = "保存中...";
  }
  const next = normalizeProviderDraft(editingToolProvider, toolProviderDraft[editingToolProvider] || {});
  document.querySelectorAll("#toolProviderModalFields [data-provider-field]").forEach((input) => {
    const field = input.dataset.providerField;
    if (field === "provider") next[field] = input.value;
    else if (input.tagName === "SELECT") next[field] = input.value === "true";
    else if (input.value.trim()) next[field] = input.value.trim();
    else if (field === "api_key" || field.includes("webhook")) delete next[field];
  });
  applyProviderDefaults(editingToolProvider, next);
  toolProviderDraft[editingToolProvider] = next;
  if (state.previewMode) {
    closeToolProviderModal();
    renderToolProviders();
    resetToolProviderApplyButton();
    return;
  }
  try {
    await saveToolConfig(collectToolConfig());
    syncToolProviderDraft();
    renderToolProviders();
    closeToolProviderModal();
    showToast("工具配置已保存", "success");
  } catch (e) {
    showToast(`工具配置保存失败：${e.message}`, "error");
  } finally {
    resetToolProviderApplyButton();
  }
}

function resetToolProviderApplyButton() {
  const button = $("#toolProviderApplyBtn");
  if (!button) return;
  button.disabled = false;
  button.innerHTML = '<i data-lucide="check"></i>保存工具配置';
  renderIcons();
}

function applyProviderDefaults(key, next) {
  const defaults = PROVIDER_DEFAULTS[key]?.[next.provider];
  if (defaults && "base_url" in defaults) {
    next.base_url = defaults.base_url;
  }
}

function applyProviderSelectionDefaults(key, provider) {
  const defaults = PROVIDER_DEFAULTS[key]?.[provider];
  if (!defaults) return;
  for (const [field, val] of Object.entries(defaults)) {
    const input = document.querySelector(`#toolProviderModalFields [data-provider-field="${field}"]`);
    if (input) input.value = val;
  }
}

function normalizeProviderDraft(key, provider) {
  const next = { ...(provider || {}) };
  const options = PROVIDER_OPTIONS[key] || [];
  if (!next.provider && options.length) next.provider = options[0][0];
  return next;
}

function stripProviderRuntimeFields(provider) {
  for (const key of Object.keys(provider)) {
    if (key.endsWith("_set") || key.endsWith("_masked")) delete provider[key];
  }
}

function providerSecretState(key, provider) {
  if (providerHasDirectSecret(provider)) return "密钥已配置";
  if (provider.provider === "gaode" && gaodeSharedSecretAvailable(key)) return "高德 Key 可复用";
  if (key === "url_fetch" || provider.provider === "wttr" || provider.provider === "duckduckgo" || provider.provider === "google_rss" || provider.provider === "built-in" || provider.provider === "default") {
    return "免密";
  }
  return "未配置密钥";
}

function providerHasDirectSecret(provider = {}) {
  return Boolean(provider.api_key_set || provider.webhook_url_set || provider.api_key || provider.webhook_url || provider.api_key_masked || provider.webhook_url_masked);
}

function gaodeSharedSecretAvailable(currentKey) {
  return ["weather", "search", "map"]
    .filter((key) => key !== currentKey)
    .some((key) => {
      const provider = toolProviderDraft[key] || {};
      return provider.provider === "gaode" && providerHasDirectSecret(provider);
    });
}

function closeToolProviderModal() {
  $("#toolProviderModal").hidden = true;
  editingToolProvider = "";
}

function fieldLabel(field) {
  return {
    enabled: "启用",
    provider: "Provider",
    base_url: "接口地址",
    api_key: "API Key",
    default_location: "默认城市",
    limit: "返回条数",
    region: "区域",
    user_agent: "User-Agent",
    max_chars: "正文长度",
    web_enabled: "Web 提醒",
    weixin_enabled: "微信提醒",
    webhook_url: "Webhook 地址",
  }[field] || field;
}

function structuredCloneSafe(value) {
  return JSON.parse(JSON.stringify(value || {}));
}

async function runToolResolve() {
  const text = value("toolResolveInput", "").trim();
  if (!text) return;
  try {
    const result = await resolveTool(text);
    setText("toolResolveResult", JSON.stringify(result, null, 2));
  } catch (e) {
    setText("toolResolveResult", `测试失败：${e.message}`);
  }
}

function clearToolResolve() {
  setValue("toolResolveInput", "");
  setText("toolResolveResult", "等待测试。");
}

async function runProviderTest(key) {
  const providerArgs = {
    weather: { location: toolProviderDraft.weather?.default_location || "北京" },
    search: { query: "漳州", limit: 3 },
    news: { topic: "科技", region: "CN", limit: 3 },
    finance: { query: "人民币 美元 汇率", limit: 3 },
    map: { query: "漳州在哪个省份" },
    url_fetch: { url: "https://example.com" },
    reminder: {},
  };
  const toolByProvider = {
    weather: "weather",
    search: "web_search",
    news: "hot_news",
    finance: "finance",
    map: "map",
    url_fetch: "url_fetch",
    reminder: "time",
  };
  const tool = toolByProvider[key] || key;
  setText("toolResolveResult", `正在测试 ${tool} ...`);
  try {
    const result = await testTool(tool, providerArgs[key] || {});
    setText("toolResolveResult", JSON.stringify(result, null, 2));
  } catch (e) {
    setText("toolResolveResult", `测试失败：${e.message}`);
  }
}

function fillProactiveConfig() {
  const cfg = state.proactiveConfig || {};
  const greetings = cfg.greetings || {};
  const morning = greetings.good_morning || {};
  setValue("proactiveEnabled", String(cfg.enabled === true));
  setValue("proactiveDailyLimit", cfg.daily_limit || 3);
  setValue("proactiveTone", cfg.tone || "warm");
  setValue("followupLevel", cfg.followup_level || "restrained");
  setChecked("askFollowupEnabled", cfg.ask_followup_enabled !== false);
  setChecked("proactiveWebChannel", (cfg.channels || {}).web !== false);
  setChecked("proactiveWeixinChannel", (cfg.channels || {}).weixin === true);
  setValue("quietHoursEnabled", String(cfg.quiet_hours_enabled !== false));
  setValue("quietStart", cfg.quiet_start || "23:00");
  setValue("quietEnd", cfg.quiet_end || "08:00");
  setChecked("greetingsEnabled", greetings.enabled === true);
  setChecked("morningEnabled", morning.enabled !== false);
  setValue("morningStart", morning.window_start || "07:00");
  setValue("morningEnd", morning.window_end || "09:30");
  setChecked("morningWeather", morning.with_weather === true);
  setChecked("morningReminders", morning.with_reminders === true);
  setChecked("noonEnabled", (greetings.noon || {}).enabled === true);
  setChecked("nightEnabled", (greetings.good_night || {}).enabled === true);
  setChecked("absenceEnabled", (greetings.long_absence || {}).enabled === true);
  setValue("morningMessage", morning.message || "");
}

function collectProactiveConfig() {
  const current = structuredCloneSafe(state.proactiveConfig || {});
  current.enabled = value("proactiveEnabled", "false") === "true";
  current.daily_limit = Number(value("proactiveDailyLimit", 3)) || 3;
  current.tone = value("proactiveTone", "warm");
  current.followup_level = value("followupLevel", "restrained");
  current.ask_followup_enabled = isChecked("askFollowupEnabled");
  current.channels = { web: isChecked("proactiveWebChannel"), weixin: isChecked("proactiveWeixinChannel") };
  current.quiet_hours_enabled = value("quietHoursEnabled", "true") === "true";
  current.quiet_start = value("quietStart", "23:00");
  current.quiet_end = value("quietEnd", "08:00");
  current.greetings = current.greetings || {};
  current.greetings.enabled = isChecked("greetingsEnabled");
  current.greetings.good_morning = {
    ...(current.greetings.good_morning || {}),
    enabled: isChecked("morningEnabled"),
    window_start: value("morningStart", "07:00"),
    window_end: value("morningEnd", "09:30"),
    with_weather: isChecked("morningWeather"),
    with_reminders: isChecked("morningReminders"),
    message: value("morningMessage", ""),
  };
  current.greetings.noon = { ...(current.greetings.noon || {}), enabled: isChecked("noonEnabled") };
  current.greetings.good_night = { ...(current.greetings.good_night || {}), enabled: isChecked("nightEnabled") };
  current.greetings.long_absence = { ...(current.greetings.long_absence || {}), enabled: isChecked("absenceEnabled") };
  return current;
}

function renderProactiveEvents() {
  const host = $("#proactiveEvents");
  if (!host) return;
  host.innerHTML = "";
  const events = state.proactiveEvents || [];
  if (!events.length) {
    const empty = document.createElement("p");
    empty.className = "conversation-empty";
    empty.textContent = "暂无主动事件。";
    host.appendChild(empty);
    return;
  }
  for (const event of events.slice(0, 8)) {
    const item = document.createElement("div");
    item.className = `proactive-event ${event.status || "pending"}`;
    const body = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = event.title || event.kind || "主动事件";
    const content = document.createElement("span");
    content.textContent = event.content || "";
    const meta = document.createElement("small");
    meta.textContent = `${event.status || "pending"} · ${event.last_error || event.created_at || ""}`;
    body.append(title, content, meta);
    item.appendChild(body);
    host.appendChild(item);
  }
}

async function runProactiveTest() {
  try {
    const event = await testProactiveMessage("这是一条主动消息测试。它会按当前主动性通道发送；如果启用了微信，会发到已绑定的“我的微信会话”。");
    await loadProactiveEvents();
    renderProactiveEvents();
    showToast(event?.status === "failed" ? "主动测试已记录，但发送失败，请看事件原因。" : "主动消息测试已发送", event?.status === "failed" ? "error" : "success");
  } catch (e) {
    showToast(`主动消息测试失败：${e.message}`, "error");
  }
}

function renderReminders() {
  const host = $("#reminderList");
  if (!host) return;
  host.innerHTML = "";
  const reminders = (state.reminders || []).filter((item) => item.status === "pending").slice(0, 8);
  if (!reminders.length) {
    const empty = document.createElement("p");
    empty.className = "conversation-empty";
    empty.textContent = "暂无待触发提醒。";
    host.appendChild(empty);
    return;
  }
  for (const reminder of reminders) {
    const item = document.createElement("div");
    item.className = "reminder-item";
    const body = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = reminder.title || "提醒";
    const meta = document.createElement("small");
    meta.textContent = `${formatReminderTime(reminder.due_at)} · ${reminder.channel || "web"}`;
    body.append(title, meta);
    const del = document.createElement("button");
    del.type = "button";
    del.className = "icon-button";
    del.title = "删除提醒";
    del.append(createIcon("trash-2"));
    del.addEventListener("click", () => handleDeleteReminder(reminder.id));
    item.append(body, del);
    host.appendChild(item);
  }
  renderIcons();
}

async function handleCreateReminder() {
  const title = value("reminderTitleInput", "").trim();
  const dueAt = value("reminderTimeInput", "").trim();
  const channel = value("reminderChannelInput", "web");
  if (!title || !dueAt) {
    showToast("请填写提醒内容和时间", "error");
    return;
  }
  try {
    await createReminder({ title, content: title, due_at: dueAt, channel });
    setValue("reminderTitleInput", "");
    setValue("reminderTimeInput", "");
    await loadReminders();
    renderReminders();
    showToast("提醒已添加", "success");
  } catch (e) {
    showToast(`添加提醒失败：${e.message}`, "error");
  }
}

async function handleDeleteReminder(id) {
  if (!id) return;
  try {
    await deleteReminder(id);
    await loadReminders();
    renderReminders();
    showToast("提醒已删除", "success");
  } catch (e) {
    showToast(`删除提醒失败：${e.message}`, "error");
  }
}

function formatReminderTime(value) {
  const text = String(value || "");
  return text ? text.replace("T", " ").slice(0, 16) : "--";
}

function setChecked(id, checked) {
  const el = document.getElementById(id);
  if (el) el.checked = Boolean(checked);
}

function isChecked(id) {
  return Boolean(document.getElementById(id)?.checked);
}

/* ---- sticker library ---- */

function renderStickerLibrary() {
  const host = $("#stickerLibraryList");
  if (!host) return;
  host.replaceChildren();
  const stickers = state.stickers || [];
  if (!stickers.length) {
    const empty = document.createElement("div");
    empty.className = "sticker-library-empty";
    empty.textContent = "还没有表情包。上传几张后，枝语会按标签自动挑。";
    host.appendChild(empty);
    return;
  }
  for (const sticker of stickers.slice(0, 24)) {
    const item = document.createElement("div");
    item.className = "sticker-library-item";
    const img = document.createElement("img");
    img.src = sticker.url;
    img.alt = sticker.tag || sticker.name || "表情包";
    const meta = document.createElement("span");
    meta.textContent = `${sticker.tag || "默认"} · ${sticker.use_count || 0}`;
    const del = document.createElement("button");
    del.type = "button";
    del.title = "删除表情包";
    del.appendChild(createIcon("trash-2"));
    del.addEventListener("click", async () => {
      await deleteSticker(sticker.id);
      renderStickerLibrary();
    });
    item.append(img, meta, del);
    host.appendChild(item);
  }
  renderIcons();
}

async function handleStickerUpload(event) {
  const file = event.target.files?.[0];
  if (!file) return;
  try {
    const dataUrl = await fileToDataUrl(file);
    const tag = value("stickerTagInput", "默认").trim() || "默认";
    await uploadSticker(dataUrl, tag, file.name);
    renderStickerLibrary();
    showToast("表情包已加入素材库", "success");
  } catch (error) {
    showToast(`表情包上传失败：${error.message}`, "error");
  } finally {
    event.target.value = "";
  }
}

function renderBotProfiles() {
  const host = $("#botProfileList");
  if (!host) return;
  host.innerHTML = "";
  for (const profile of state.botProfiles || []) {
    host.appendChild(createBotProfileCard(profile));
  }
  renderIcons();
}

function createBotProfileCard(profile) {
  const card = document.createElement("section");
  card.className = "bot-profile-card";
  card.dataset.profileId = profile.id;
  const form = document.createElement("div");
  form.className = "form-grid";
  form.innerHTML = `
    <label><span>工具</span><select class="bot-tools"><option value="true">启用</option><option value="false">关闭</option></select></label>
    <label><span>风格</span><input class="bot-style" type="text" value="${escapeAttr(profile.reply_style || "natural")}"></label>
    <label class="wide"><span>System Prompt</span><textarea class="bot-system">${escapeHtml(profile.system || "")}</textarea></label>
  `;
  form.querySelector(".bot-tools").value = String(profile.tools_enabled !== false);
  const actions = document.createElement("div");
  actions.className = "inline-actions";
  const del = document.createElement("button");
  del.className = "small-button";
  del.type = "button";
  del.append(createIcon("trash-2"), document.createTextNode("删除"));
  del.disabled = profile.id === "default";
  del.addEventListener("click", () => handleDeleteBotProfile(profile.id));
  card.append(form, actions);
  actions.appendChild(del);
  return card;
}

async function addBotProfile() {
  const id = `profile_${Date.now().toString(36)}`;
  await createBotProfile({ id, name: "新人格", system: state.currentConfig.system || "" });
  await loadBotProfiles();
  renderBotProfiles();
}

async function saveBotProfiles() {
  for (const card of document.querySelectorAll("[data-profile-id]")) {
    const id = card.dataset.profileId;
    const previous = state.botProfiles.find((p) => p.id === id) || {};
    await updateBotProfile(id, {
      name: previous.name || "枝语",
      avatar_url: previous.avatar_url || "",
      tools_enabled: card.querySelector(".bot-tools")?.value !== "false",
      reply_style: card.querySelector(".bot-style")?.value.trim() || "natural",
      system: card.querySelector(".bot-system")?.value || "",
    });
  }
}

async function handleDeleteBotProfile(id) {
  if (!id || id === "default") return;
  await deleteBotProfile(id);
  await loadBotProfiles();
  renderBotProfiles();
}

function fileToDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

function escapeHtml(value) {
  return String(value || "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function escapeAttr(value) {
  return escapeHtml(value).replace(/`/g, "&#96;");
}

/* ---- save ---- */

async function saveSettingsPage(options = {}) {
  if (state.previewMode) { showToast("预览模式：无法保存", "info"); return; }
  try {
    await saveConfig(collectConfig());
    await saveToolConfig(collectToolConfig());
    await saveProactiveConfig(collectProactiveConfig());
    await saveBotProfiles();
    for (const s of state.services) { await updateServiceConfig(s.id, collectProfileConfig(s.id)); }
    await Promise.allSettled([loadConfig(), loadToolConfig(), loadBotProfiles(), loadProactiveConfig(), loadProactiveEvents(), loadReminders(), loadStickers()]);
    syncToolProviderDraft();
    fillConfig(state.currentConfig);
    renderToolProviders();
    fillProactiveConfig();
    renderProactiveEvents();
    renderReminders();
    renderStickerLibrary();
    await loadServices(); renderProfileList();
    renderBotProfiles();
    refreshSettingsOverview();
    window.dispatchEvent(new CustomEvent("branchwhisper:appearance-updated"));
    if (options.closeModal) closeSettingsSectionModal();
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
