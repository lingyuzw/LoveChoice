/* ============================================================
   ui-integrations.js — OpenClaw / channel bot management
   BranchWhisper
   ============================================================ */

import { state } from "./state.js";
import {
  createIntegration,
  deleteIntegration,
  fetchIntegrationLogs,
  installIntegration,
  loadIntegrations,
  pollIntegrationQrLogin,
  restartIntegration,
  startIntegration,
  startIntegrationBridge,
  startIntegrationQrLogin,
  stopIntegration,
  testIntegrationDialog,
  updateIntegration,
  updateIntegrationContact,
  uploadAvatar,
} from "./api.js";
import { $, createIcon, renderIcons, setText, showConfirm, showSkeleton, showToast } from "./utils.js";

const DEFAULT_KEYWORDS = ["发语音", "说话", "念给我听", "语音回复", "我想听你说话"];

let eventsBound = false;
let integrationsMounted = false;
let editingIntegrationId = "";

export function initIntegrations() {
  setupIntegrationEvents();
  showSkeleton("integrationCards", 2);
  showSkeleton("integrationEnvGrid", 4);
  refreshIntegrations({ quiet: true });
}

export function enterIntegrations() {
  integrationsMounted = true;
  refreshIntegrations({ quiet: true });
  startPolling();
}

export function leaveIntegrations() {
  integrationsMounted = false;
  stopPolling();
  stopLoginPolling();
}

function setupIntegrationEvents() {
  if (eventsBound) return;
  eventsBound = true;
  $("#addIntegrationBtn")?.addEventListener("click", () => openIntegrationModal());
  $("#refreshIntegrationsBtn")?.addEventListener("click", () => refreshIntegrations());
  $("#integrationLoginBtn")?.addEventListener("click", () => beginQrLogin());
  $("#integrationInstallBtn")?.addEventListener("click", () => runSelectedAction("install"));
  $("#refreshIntegrationLogsBtn")?.addEventListener("click", () => refreshSelectedLogs());
  $("#integrationTestBtn")?.addEventListener("click", runDialogProbe);
  $("#integrationTestInput")?.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      runDialogProbe();
    }
  });
  $("#integrationForm")?.addEventListener("submit", saveIntegrationForm);
  $("#integrationCancelBtn")?.addEventListener("click", closeIntegrationModal);
  document.querySelector("#integrationModal .modal-close")?.addEventListener("click", closeIntegrationModal);
  $("#integrationModal")?.addEventListener("click", (event) => {
    if (event.target === event.currentTarget) closeIntegrationModal();
  });
}

async function refreshIntegrations(options = {}) {
  const result = await loadIntegrations();
  renderEnvironment();
  renderIntegrationCards();
  await refreshSelectedLogs({ quiet: true });
  setText("topStatus", integrationSummaryText());
  if (!result.ok && !options.quiet) showToast("接入状态读取失败", "error");
}

function integrationSummaryText() {
  const active = state.integrations.filter((item) => ["running", "login"].includes(item.status)).length;
  return `${active}/${state.integrations.length} 接入`;
}

function startPolling() {
  stopPolling();
  state.integrationPollTimer = window.setInterval(() => {
    if (!integrationsMounted) return;
    refreshIntegrations({ quiet: true });
  }, 6000);
}

function stopPolling() {
  if (state.integrationPollTimer) {
    window.clearInterval(state.integrationPollTimer);
    state.integrationPollTimer = 0;
  }
}

function startLoginPolling() {
  stopLoginPolling();
  state.integrationLoginPollTimer = window.setInterval(async () => {
    if (!integrationsMounted || !state.integrationLoginSession?.integrationId) return;
    await pollQrLogin({ quiet: true });
  }, 2600);
}

function stopLoginPolling() {
  if (state.integrationLoginPollTimer) {
    window.clearInterval(state.integrationLoginPollTimer);
    state.integrationLoginPollTimer = 0;
  }
}

function renderEnvironment() {
  const host = $("#integrationEnvGrid");
  if (!host) return;
  host.innerHTML = "";
  const env = state.integrationEnv;
  setText("integrationEnvBadge", env?.ready ? "环境可用" : "需要配置");
  const tools = env?.tools || {};
  for (const name of ["node", "npm", "openclaw", "ffmpeg"]) {
    host.appendChild(createEnvCard(name, tools[name]));
  }
  renderIcons();
}

function createEnvCard(name, tool) {
  const card = document.createElement("article");
  card.className = `integration-env-card ${tool?.available ? "ready" : "missing"}`;
  const icon = document.createElement("span");
  icon.className = "integration-env-icon";
  icon.append(createIcon(tool?.available ? "check" : "circle-alert"));
  const title = document.createElement("strong");
  title.textContent = name;
  const version = document.createElement("small");
  version.textContent = tool?.version || "未检测到";
  const path = document.createElement("span");
  path.textContent = tool?.path || "PATH 中不可用";
  card.append(icon, title, version, path);
  return card;
}

function renderIntegrationCards() {
  const host = $("#integrationCards");
  if (!host) return;
  host.innerHTML = "";
  if (!state.integrations.length) {
    const empty = document.createElement("div");
    empty.className = "integration-empty";
    empty.textContent = "还没有接入实例。";
    host.appendChild(empty);
    renderIcons();
    return;
  }
  for (const integration of state.integrations) {
    host.appendChild(createIntegrationCard(integration));
  }
  if (!state.integrations.some((item) => item.id === state.selectedIntegrationId)) {
    state.selectedIntegrationId = state.integrations[0]?.id || "";
  }
  renderSelectedPanel();
  renderIcons();
}

function createIntegrationCard(integration) {
  const card = document.createElement("article");
  const active = integration.id === state.selectedIntegrationId;
  card.className = `integration-card ${statusClass(integration.status)}${active ? " selected" : ""}`;
  card.dataset.integrationId = integration.id;
  card.addEventListener("click", () => selectIntegration(integration.id));

  const head = document.createElement("div");
  head.className = "integration-card-head";
  const title = document.createElement("div");
  title.className = "integration-title";
  const dot = document.createElement("span");
  dot.className = "status-dot";
  const name = document.createElement("strong");
  name.textContent = integration.id;
  const desc = document.createElement("small");
  desc.textContent = `微信个人号 · OpenClaw ${integration.openclaw_profile || "branchwhisper"} · Bot ${integration.bot_profile_id || "default"}`;
  title.append(dot, name, document.createElement("br"), desc);
  const badge = document.createElement("span");
  badge.className = `service-badge ${statusClass(integration.status)}`;
  badge.textContent = statusText(integration.status);
  head.append(title, badge);

  const meta = document.createElement("div");
  meta.className = "integration-meta";
  const lastError = integration.last_error || integration.runtime?.last_error || "--";
  for (const [label, value] of [
    ["ENABLED", integration.enabled ? "on" : "off"],
    ["MODE", integration.reply_mode || "text"],
    ["ACCOUNTS", integration.runtime?.account_count ?? 0],
    ["PID", integration.pid || "--"],
    ["ERROR", lastError],
  ]) {
    meta.appendChild(metaCell(label, value));
  }

  const actions = document.createElement("div");
  actions.className = "integration-actions";
  actions.append(
    actionButton("启动", "play", () => handleCardAction(integration.id, "start")),
    actionButton("停止", "square", () => handleCardAction(integration.id, "stop")),
    actionButton("重启", "refresh-ccw", () => handleCardAction(integration.id, "restart")),
    actionButton("编辑", "settings-2", () => openIntegrationModal(integration)),
    actionButton("桥接", "cable", () => handleCardAction(integration.id, "bridge")),
    actionButton("删除", "trash-2", () => handleDelete(integration.id)),
  );

  card.append(head, meta, actions);
  return card;
}

function metaCell(label, value) {
  const cell = document.createElement("div");
  cell.className = "meta-cell";
  const span = document.createElement("span");
  span.textContent = label;
  const strong = document.createElement("strong");
  strong.title = String(value || "");
  strong.textContent = compact(String(value || "--"), 34);
  cell.append(span, strong);
  return cell;
}

function actionButton(label, icon, handler) {
  const button = document.createElement("button");
  button.className = "service-action";
  button.type = "button";
  button.append(createIcon(icon), document.createTextNode(label));
  button.addEventListener("click", (event) => {
    event.stopPropagation();
    handler();
  });
  return button;
}

async function handleCardAction(id, action) {
  state.selectedIntegrationId = id;
  try {
    if (action === "start") await startIntegration(id);
    if (action === "stop") await stopIntegration(id);
    if (action === "restart") await restartIntegration(id);
    if (action === "bridge") await startIntegrationBridge(id);
    await refreshIntegrations({ quiet: true });
    showToast("操作已发送", "success");
  } catch (error) {
    showToast(`操作失败：${error.message}`, "error");
  }
}

async function handleDelete(id) {
  const ok = await showConfirm(`删除接入实例 ${id}？`);
  if (!ok) return;
  try {
    await deleteIntegration(id);
    state.selectedIntegrationId = state.integrations[0]?.id || "";
    await refreshIntegrations({ quiet: true });
    showToast("实例已删除", "success");
  } catch (error) {
    showToast(`删除失败：${error.message}`, "error");
  }
}

function selectIntegration(id) {
  state.selectedIntegrationId = id;
  renderIntegrationCards();
  refreshSelectedLogs({ quiet: true });
}

function renderSelectedPanel() {
  const selected = selectedIntegration();
  setText("selectedIntegrationBadge", selected?.id || "--");
  renderLoginBox(selected);
  renderAccountList(selected);
  renderTimingList(selected);
  renderContactList(selected);
}

function renderLoginBox(selected) {
  const box = $("#integrationLoginBox");
  if (!box) return;
  box.innerHTML = "";
  if (!selected) {
    box.textContent = "请选择一个接入实例。";
    return;
  }
  const session = state.integrationLoginSession?.integrationId === selected.id ? state.integrationLoginSession : null;
  if (selected.status === "logged_in" && !session) {
    const text = document.createElement("div");
    text.className = "integration-login-placeholder";
    text.innerHTML = `<strong>${escapeHtml(selected.id)}</strong><span>已登录。二维码会在扫码成功后自动隐藏。</span>`;
    box.appendChild(text);
    return;
  }
  if (!session) {
    const text = document.createElement("div");
    text.className = "integration-login-placeholder";
    text.innerHTML = `<strong>${escapeHtml(selected.id)}</strong><span>点击“扫码登录”后在这里显示二维码；登录凭证保存在本机 OpenClaw profile 中。</span>`;
    box.appendChild(text);
    return;
  }
  if (["created", "binded_redirect"].includes(session.status)) {
    const text = document.createElement("div");
    text.className = "integration-login-placeholder";
    text.innerHTML = `<strong>登录成功</strong><span>${escapeHtml(session.message || "账号已保存，二维码已隐藏。")}</span>`;
    box.appendChild(text);
    return;
  }
  if (session.qrcode_img_content) {
    const image = document.createElement("img");
    image.className = "integration-qr-image";
    image.alt = "微信扫码登录二维码";
    image.src = session.qrcode_img_content.startsWith("data:")
      ? session.qrcode_img_content
      : `https://api.qrserver.com/v1/create-qr-code/?size=220x220&data=${encodeURIComponent(session.qrcode_img_content)}`;
    box.appendChild(image);
  }
  const meta = document.createElement("div");
  meta.className = "integration-login-meta";
  const title = document.createElement("strong");
  title.textContent = loginStatusText(session.status);
  const message = document.createElement("span");
  message.textContent = session.message || "等待微信扫码。";
  const expire = document.createElement("small");
  expire.textContent = session.expires_at ? `有效期至 ${new Date(session.expires_at * 1000).toLocaleTimeString()}` : "";
  meta.append(title, message, expire);
  box.appendChild(meta);
}

function renderTimingList(selected) {
  const host = $("#integrationTimingList");
  if (!host) return;
  host.innerHTML = "";
  const items = selected?.recent_timings || [];
  if (!items.length) {
    const empty = document.createElement("div");
    empty.className = "integration-empty compact";
    empty.textContent = "还没有消息耗时。";
    host.appendChild(empty);
    return;
  }
  for (const item of items.slice(0, 10)) {
    const row = document.createElement("div");
    row.className = "integration-timing-item";
    const total = Number(item.bridge_ms || item.total_ms || 0);
    row.innerHTML = `
      <strong>${escapeHtml(item.text || item.trace_id || "--")}</strong>
      <span>总耗时 ${total}ms${item.send_status ? ` · ${escapeHtml(item.send_status)}` : ""}</span>
      <small>receive ${Number(item.receive_ms || 0)} · tool ${Number(item.tool_ms || 0)} · llm ${Number(item.llm_ms || 0)} · tts ${Number(item.tts_ms || 0)} · send ${Number(item.send_ms || 0)}</small>
    `;
    host.appendChild(row);
  }
}

function renderContactList(selected) {
  const host = $("#integrationContactList");
  if (!host) return;
  host.innerHTML = "";
  const contacts = selected?.contacts || [];
  if (!contacts.length) {
    const empty = document.createElement("div");
    empty.className = "integration-empty compact";
    empty.textContent = "收到微信消息后会自动出现联系人。";
    host.appendChild(empty);
    return;
  }
  for (const contact of contacts.slice(0, 12)) {
    const row = document.createElement("div");
    row.className = "integration-contact-item";
    const avatar = document.createElement("div");
    avatar.className = "integration-contact-avatar";
    const avatarUrl = contact.avatar_url || contact.auto_avatar_url || "";
    if (avatarUrl) {
      const img = document.createElement("img");
      img.src = avatarUrl;
      avatar.appendChild(img);
    } else {
      avatar.textContent = "微";
    }
    const body = document.createElement("div");
    body.className = "integration-contact-body";
    body.innerHTML = `<strong>${escapeHtml(contact.remark_name || contact.display_name || contact.sender_id || "--")}</strong><small>${escapeHtml(contact.sender_id || "")}</small>`;
    const edit = document.createElement("div");
    edit.className = "integration-contact-edit";
    const remark = document.createElement("input");
    remark.type = "text";
    remark.placeholder = "联系人备注";
    remark.value = contact.remark_name || "";
    const file = document.createElement("input");
    file.type = "file";
    file.accept = "image/png,image/jpeg,image/webp,image/gif";
    const save = document.createElement("button");
    save.className = "secondary-action compact-action";
    save.type = "button";
    save.append(createIcon("save"), document.createTextNode("保存"));
    save.addEventListener("click", () => saveContactProfile(selected.id, contact, remark, file));
    edit.append(remark, file, save);
    row.append(avatar, body, edit);
    host.appendChild(row);
  }
}

async function saveContactProfile(integrationId, contact, remarkInput, fileInput) {
  try {
    let avatarUrl = contact.avatar_url || "";
    if (fileInput?.files?.[0]) {
      const uploaded = await uploadAvatar(await fileToDataUrl(fileInput.files[0]));
      avatarUrl = uploaded.asset?.url || avatarUrl;
    }
    await updateIntegrationContact(integrationId, contact.sender_id, {
      account_id: contact.account_id || "",
      remark_name: remarkInput?.value.trim() || "",
      avatar_url: avatarUrl,
    });
    await refreshIntegrations({ quiet: true });
    showToast("联系人资料已保存", "success");
  } catch (error) {
    showToast(`联系人保存失败：${error.message}`, "error");
  }
}

function renderAccountList(selected) {
  const host = $("#integrationAccountList");
  if (!host) return;
  host.innerHTML = "";
  if (!selected) return;
  const stateDir = document.createElement("div");
  stateDir.className = "integration-account-item";
  stateDir.innerHTML = `<span>STATE</span><strong>${escapeHtml(selected.runtime?.state_dir || "--")}</strong>`;
  host.appendChild(stateDir);
  const accounts = Array.isArray(selected.accounts) ? selected.accounts : [];
  if (!accounts.length) {
    const empty = document.createElement("div");
    empty.className = "integration-account-item muted";
    empty.innerHTML = "<span>ACCOUNT</span><strong>未发现已登录账号</strong>";
    host.appendChild(empty);
    return;
  }
  for (const account of accounts) {
    const item = document.createElement("div");
    item.className = "integration-account-item";
    item.innerHTML = `<span>ACCOUNT</span><strong>${escapeHtml(account.id || "--")}</strong><small>${escapeHtml(account.user_id || account.saved_at || "")}</small>`;
    host.appendChild(item);
  }
}

async function runSelectedAction(action) {
  const selected = selectedIntegration();
  if (!selected) {
    showToast("请先选择实例", "error");
    return;
  }
  try {
    if (action === "install") await installIntegration(selected.id);
    await refreshIntegrations({ quiet: true });
    await refreshSelectedLogs();
    showToast("安装命令已执行", "success");
  } catch (error) {
    showToast(`操作失败：${error.message}`, "error");
  }
}

async function beginQrLogin(force = true) {
  const selected = selectedIntegration();
  if (!selected) {
    showToast("请先选择实例", "error");
    return;
  }
  try {
    const data = await startIntegrationQrLogin(selected.id, force);
    const login = data.result?.login || {};
    state.integrationLoginSession = { ...login, integrationId: selected.id };
    renderSelectedPanel();
    await refreshSelectedLogs({ quiet: true });
    if (data.result?.ok === false) {
      showToast(login.message || data.result.error || "二维码生成失败", "error");
      return;
    }
    showToast("二维码已生成，请用手机微信扫码", "success");
    startLoginPolling();
  } catch (error) {
    showToast(`扫码登录失败：${error.message}`, "error");
  }
}

async function pollQrLogin(options = {}) {
  const selected = selectedIntegration();
  if (!selected || state.integrationLoginSession?.integrationId !== selected.id) return;
  try {
    const data = await pollIntegrationQrLogin(selected.id);
    const login = data.result?.login || {};
    state.integrationLoginSession = { ...state.integrationLoginSession, ...login, integrationId: selected.id };
    renderSelectedPanel();
    if (["created", "expired", "denied", "cancel", "canceled", "verify_code_blocked", "error"].includes(state.integrationLoginSession.status)) {
      stopLoginPolling();
      await refreshIntegrations({ quiet: true });
      await refreshSelectedLogs({ quiet: true });
      if (state.integrationLoginSession.status === "created") {
        state.integrationLoginSession = null;
        showToast("微信登录成功，账号已保存", "success");
        renderSelectedPanel();
      } else if (!options.quiet) {
        showToast(state.integrationLoginSession.message || "扫码登录结束", "error");
      }
    }
  } catch (error) {
    if (!options.quiet) showToast(`登录状态读取失败：${error.message}`, "error");
  }
}

async function refreshSelectedLogs(options = {}) {
  const selected = selectedIntegration();
  const output = $("#integrationLogOutput");
  if (!selected || !output) return;
  try {
    const logs = await fetchIntegrationLogs(selected.id);
    output.textContent = logs || "暂无日志。";
    output.scrollTop = output.scrollHeight;
  } catch (error) {
    if (!options.quiet) showToast(`日志读取失败：${error.message}`, "error");
  }
}

async function runDialogProbe() {
  const selected = selectedIntegration();
  const text = $("#integrationTestInput")?.value.trim();
  if (!selected || !text) {
    showToast("请选择实例并输入测试消息", "error");
    return;
  }
  const resultBox = $("#integrationTestResult");
  if (resultBox) resultBox.textContent = "请求中...";
  try {
    const result = await testIntegrationDialog(selected.id, text);
    if (resultBox) {
      resultBox.textContent = `${result.reply_text || ""}${result.send_voice ? `\n语音文件：${result.voice_file || "--"}` : ""}`;
    }
    await refreshSelectedLogs({ quiet: true });
  } catch (error) {
    if (resultBox) resultBox.textContent = `失败：${error.message}`;
    showToast(`测试失败：${error.message}`, "error");
  }
}

function openIntegrationModal(integration = null) {
  editingIntegrationId = integration?.id || "";
  setText("integrationModalTitle", integration ? "编辑微信个人号" : "添加微信个人号");
  const idInput = $("#integrationIdInput");
  if (idInput) {
    idInput.value = integration?.id || "weixin_personal";
    idInput.disabled = Boolean(integration);
  }
  if ($("#integrationProfileInput")) $("#integrationProfileInput").value = integration?.openclaw_profile || "branchwhisper";
  if ($("#integrationBotProfileInput")) $("#integrationBotProfileInput").value = integration?.bot_profile_id || "default";
  if ($("#integrationReplyMode")) $("#integrationReplyMode").value = integration?.reply_mode || "text";
  if ($("#integrationEnabledInput")) $("#integrationEnabledInput").checked = Boolean(integration?.enabled);
  if ($("#integrationKeywordsInput")) {
    $("#integrationKeywordsInput").value = (integration?.voice_trigger_keywords || DEFAULT_KEYWORDS).join("\n");
  }
  $("#integrationModal").hidden = false;
  renderIcons();
}

function closeIntegrationModal() {
  $("#integrationModal").hidden = true;
  editingIntegrationId = "";
}

async function saveIntegrationForm(event) {
  event.preventDefault();
  const payload = {
    id: $("#integrationIdInput")?.value.trim() || "weixin_personal",
    enabled: Boolean($("#integrationEnabledInput")?.checked),
    openclaw_profile: $("#integrationProfileInput")?.value.trim() || "branchwhisper",
    bot_profile_id: $("#integrationBotProfileInput")?.value.trim() || "default",
    reply_mode: $("#integrationReplyMode")?.value || "text",
    voice_trigger_keywords: ($("#integrationKeywordsInput")?.value || "")
      .split(/\r?\n|[,，]/)
      .map((item) => item.trim())
      .filter(Boolean),
  };
  try {
    if (editingIntegrationId) {
      await updateIntegration(editingIntegrationId, payload);
      state.selectedIntegrationId = editingIntegrationId;
    } else {
      await createIntegration(payload);
      state.selectedIntegrationId = payload.id;
    }
    closeIntegrationModal();
    await refreshIntegrations({ quiet: true });
    showToast("接入配置已保存", "success");
  } catch (error) {
    showToast(`保存失败：${error.message}`, "error");
  }
}

function selectedIntegration() {
  return state.integrations.find((item) => item.id === state.selectedIntegrationId) || state.integrations[0] || null;
}

function statusClass(status) {
  if (["running", "login", "logged_in"].includes(status)) return "active";
  if (["starting", "installing"].includes(status)) return "loading";
  if (status === "failed") return "failed";
  return "stopped";
}

function statusText(status) {
  return {
    running: "运行中",
    login: "登录中",
    logged_in: "已登录",
    starting: "启动中",
    installing: "安装中",
    failed: "失败",
    stopped: "已停止",
  }[status] || "未知";
}

function loginStatusText(status) {
  return {
    idle: "未开始",
    wait: "等待扫码",
    scaned: "已扫码",
    scaned_but_redirect: "切换分区",
    binded_redirect: "已绑定",
    need_verifycode: "需要验证",
    created: "登录成功",
    confirmed: "已确认",
    expired: "已过期",
    denied: "已取消",
    error: "登录失败",
  }[status] || "登录中";
}

function compact(text, limit) {
  return text.length > limit ? `${text.slice(0, limit - 3)}...` : text;
}

function fileToDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(reader.error || new Error("文件读取失败"));
    reader.readAsDataURL(file);
  });
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}
