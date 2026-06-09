/* ============================================================
   ui-settings.js — Configuration page (settings.html)
   BranchWhisper · Precision Console
   ============================================================ */

import { state, DEFAULT_CONFIG } from "./state.js";
import { $, setValue, value, setText, setPlaceholder, renderIcons, showToast, createIcon, safePort } from "./utils.js";
import {
  createBotProfile,
  deleteBotProfile,
  loadBotProfiles,
  loadConfig,
  loadServices,
  loadToolConfig,
  resolveTool,
  saveConfig,
  saveToolConfig,
  updateBotProfile,
  updateServiceConfig,
  uploadAvatar,
} from "./api.js";

/* ---- init ---- */

let eventsBound = false;
let toolProviderDraft = {};
let editingToolProvider = "";
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
  await Promise.allSettled([loadToolConfig(), loadBotProfiles()]);
  fillConfig(configResult.config);
  renderToolProviders();
  // 即使 loadConfig 失败也调 fillConfig(DEFAULT_CONFIG)
  if (!configResult.ok) fillConfig(configResult.config);
  await loadServices();
  renderProfileList();
  renderBotProfiles();
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
  $("#toolResolveBtn")?.addEventListener("click", runToolResolve);
  $("#addBotProfileBtn")?.addEventListener("click", addBotProfile);
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
  { key: "web_user_name", id: "webUserName" },
  { key: "web_user_avatar_url", id: "webUserAvatarFile", virtual: true },
  { key: "web_assistant_name", id: "webAssistantName" },
  { key: "web_assistant_avatar_url", id: "webAssistantAvatarFile", virtual: true },
  { key: "system", id: "systemPrompt" },
  { key: "tools_enabled", id: "toolsEnabled" },
  { key: "tools_auto_call", id: "toolsAutoCall" },
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

function renderToolProviders() {
  const host = $("#toolProviderGrid");
  if (!host) return;
  host.innerHTML = "";
  toolProviderDraft = structuredCloneSafe(state.toolConfig || {});
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
    const hasSecret = Boolean(provider.api_key_set || provider.webhook_url_set || provider.api_key || provider.webhook_url);
    status.innerHTML = `
      <span>${provider.enabled === false ? "已关闭" : "已启用"}</span>
      <span>${escapeHtml(providerName)}</span>
      <span>${hasSecret ? "密钥已配置" : "免密/未配置密钥"}</span>
    `;
    const action = document.createElement("button");
    action.className = "secondary-action";
    action.type = "button";
    action.append(createIcon("sliders-horizontal"), document.createTextNode("配置"));
    action.addEventListener("click", () => openToolProviderModal(key));
    card.append(head, status, action);
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
  for (const key of Object.keys(PROVIDER_FIELDS)) result[key] = { ...(toolProviderDraft[key] || {}) };
  return result;
}

function openToolProviderModal(key) {
  editingToolProvider = key;
  const provider = toolProviderDraft[key] || {};
  setText("toolProviderModalTitle", `${PROVIDER_LABELS[key] || key}配置`);
  const summary = $("#toolProviderModalSummary");
  if (summary) {
    summary.innerHTML = `
      <span>${provider.enabled === false ? "当前关闭" : "当前启用"}</span>
      <span>Provider: ${escapeHtml(provider.provider || "default")}</span>
      <span>${provider.api_key_set || provider.webhook_url_set ? "密钥已保存" : "未保存密钥"}</span>
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
      const input = document.createElement(field === "enabled" || field.endsWith("_enabled") ? "select" : "input");
      input.dataset.providerField = field;
      if (input.tagName === "SELECT") {
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

function applyToolProviderModal() {
  if (!editingToolProvider) return;
  const next = { ...(toolProviderDraft[editingToolProvider] || {}) };
  document.querySelectorAll("#toolProviderModalFields [data-provider-field]").forEach((input) => {
    const field = input.dataset.providerField;
    if (input.tagName === "SELECT") next[field] = input.value === "true";
    else if (input.value.trim()) next[field] = input.value.trim();
  });
  toolProviderDraft[editingToolProvider] = next;
  closeToolProviderModal();
  renderToolProviders();
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

async function saveSettingsPage() {
  if (state.previewMode) { showToast("预览模式：无法保存", "info"); return; }
  try {
    await saveConfig(collectConfig());
    await saveToolConfig(collectToolConfig());
    await saveBotProfiles();
    for (const s of state.services) { await updateServiceConfig(s.id, collectProfileConfig(s.id)); }
    await Promise.allSettled([loadConfig(), loadToolConfig(), loadBotProfiles()]);
    fillConfig(state.currentConfig);
    renderToolProviders();
    await loadServices(); renderProfileList();
    renderBotProfiles();
    window.dispatchEvent(new CustomEvent("branchwhisper:appearance-updated"));
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
