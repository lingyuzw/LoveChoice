/* ============================================================
   ui-services.js — Service orchestration page (services.html)
   BranchWhisper · Precision Console
   ============================================================ */

import { state } from "./state.js";
import { $, setText, renderIcons, showToast, showSkeleton, showConfirm, createIcon, safePort } from "./utils.js";
import { loadConfig, loadServices, loadSystemResources, startService, stopService, startAllServices, stopAllServices, fetchServiceLogs, clearServiceLogs, clearAllServiceLogs } from "./api.js";

/* ---- init ---- */

let eventsBound = false;
let servicesMounted = false;
let logLiveRefresh = true;
let currentLogText = "";

export function initServices() {
  setupServiceEvents();
  syncLogFromHash();

  showSkeleton("serviceCards", 3);
  showSkeleton("resourceGrid", 3);
  loadConfig().then(() => {
    Promise.all([loadServices(), refreshResources({ quiet: true })]).then(() => {
      renderServiceCards();
      renderLogTabs();
      if (servicesMounted) startServicePolling();
      refreshLogs(state.selectedLogService, { quiet: true });
      setText("topStatus", serviceSummaryText());
    });
  });
}

export function enterServices() {
  servicesMounted = true;
  syncLogFromHash();
  startServicePolling();
}

export function leaveServices() {
  servicesMounted = false;
  stopServicePolling();
}

function setupServiceEvents() {
  if (eventsBound) return;
  eventsBound = true;
  $("#startAllBtn")?.addEventListener("click", handleStartAll);
  $("#stopAllBtn")?.addEventListener("click", handleStopAll);
  $("#restartAllBtn")?.addEventListener("click", handleRestartAll);
  $("#clearAllLogsBtn")?.addEventListener("click", handleClearAllLogs);
  $("#healthBtn")?.addEventListener("click", () => Promise.all([loadServices(), refreshResources()]).then(() => renderServiceCards()));
  $("#refreshLogsBtn")?.addEventListener("click", () => refreshLogs(state.selectedLogService));
  $("#toggleLogLiveBtn")?.addEventListener("click", toggleLogLiveRefresh);
  $("#copyLogBtn")?.addEventListener("click", copyCurrentLog);
  $("#downloadLogBtn")?.addEventListener("click", downloadCurrentLog);
}

function syncLogFromHash() {
  const hash = location.hash.replace(/^#/, "");
  if (hash.startsWith("logs-")) {
    state.selectedLogService = hash.slice(5) || state.selectedLogService;
  }
}

function serviceSummaryText() {
  const running = state.services.filter((s) => s.running).length;
  return `${running}/${state.services.length} 运行`;
}

/* ---- system resources ---- */

async function refreshResources(options = {}) {
  const result = await loadSystemResources();
  renderResourceCards();
  if (!result.ok && !options.quiet) showToast("资源状态读取失败", "error");
}

function renderResourceCards() {
  const host = $("#resourceGrid");
  if (!host) return;
  host.innerHTML = "";

  const resources = state.systemResources;
  if (!resources) {
    setText("resourcePlatform", "资源不可用");
    host.appendChild(createResourceCard("资源状态", "--", "后端暂未返回数据", null, "circle-alert"));
    renderIcons();
    return;
  }

  setText("resourcePlatform", shortPlatform(resources.platform));

  const cpu = resources.cpu || {};
  host.appendChild(createResourceCard(
    "CPU",
    formatPercent(cpu.percent),
    `${cpu.cores || "--"} 核 · ${formatLoad(cpu)}`,
    cpu.percent,
    "cpu",
  ));

  const memory = resources.memory || {};
  host.appendChild(createResourceCard(
    "内存",
    formatPercent(memory.percent),
    `${formatBytes(memory.used_bytes)} / ${formatBytes(memory.total_bytes)}`,
    memory.percent,
    "memory-stick",
  ));

  const gpus = Array.isArray(resources.gpus) ? resources.gpus : [];
  if (!gpus.length) {
    host.appendChild(createResourceCard("GPU", "--", "未检测到 nvidia-smi", null, "monitor-cog"));
  } else {
    for (const gpu of gpus) {
      host.appendChild(createResourceCard(
        `GPU ${Number(gpu.index ?? 0) + 1}`,
        formatPercent(gpu.util_percent),
        `${gpu.name || "GPU"} · ${formatGpuMemory(gpu)} · ${formatTemp(gpu.temperature_c)}`,
        gpu.util_percent,
        "gauge",
      ));
    }
  }
  renderIcons();
}

function createResourceCard(title, valueText, detailText, percent, iconName) {
  const card = document.createElement("article");
  card.className = "resource-card";

  const head = document.createElement("div");
  head.className = "resource-card-head";
  const icon = document.createElement("span");
  icon.className = "resource-icon";
  icon.append(createIcon(iconName));
  const titleEl = document.createElement("strong");
  titleEl.textContent = title;
  head.append(icon, titleEl);

  const value = document.createElement("div");
  value.className = "resource-value";
  value.textContent = valueText;

  const detail = document.createElement("small");
  detail.textContent = detailText;

  const meter = document.createElement("div");
  meter.className = "resource-meter";
  const bar = document.createElement("span");
  const pct = clampPercent(percent);
  bar.style.width = pct === null ? "0%" : `${pct}%`;
  meter.classList.toggle("warning", pct !== null && pct >= 75);
  meter.classList.toggle("danger", pct !== null && pct >= 90);
  meter.appendChild(bar);

  card.append(head, value, detail, meter);
  return card;
}

function clampPercent(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return null;
  return Math.max(0, Math.min(100, num));
}

function formatPercent(value) {
  const pct = clampPercent(value);
  return pct === null ? "--" : `${pct.toFixed(pct % 1 ? 1 : 0)}%`;
}

function formatLoad(cpu) {
  const loads = [cpu.load_1m, cpu.load_5m, cpu.load_15m]
    .map((v) => Number(v))
    .filter((v) => Number.isFinite(v))
    .map((v) => v.toFixed(2));
  return loads.length ? `负载 ${loads.join(" / ")}` : "负载 --";
}

function formatBytes(value) {
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes <= 0) return "--";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = bytes;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size.toFixed(unit >= 3 ? 1 : 0)} ${units[unit]}`;
}

function formatGpuMemory(gpu) {
  const used = Number(gpu.memory_used_mb);
  const total = Number(gpu.memory_total_mb);
  if (!Number.isFinite(used) || !Number.isFinite(total) || total <= 0) return "显存 --";
  return `显存 ${(used / 1024).toFixed(1)} / ${(total / 1024).toFixed(1)} GB`;
}

function formatTemp(value) {
  const temp = Number(value);
  return Number.isFinite(temp) ? `${temp.toFixed(0)}°C` : "温度 --";
}

function shortPlatform(value) {
  const text = String(value || "--");
  return text.length > 42 ? `${text.slice(0, 39)}...` : text;
}

/* ---- cards (slim: status dot + name + 3 buttons) ---- */

function renderServiceCards() {
  const host = $("#serviceCards");
  if (!host) return;
  host.innerHTML = "";
  for (const service of state.services) host.appendChild(createServiceCard(service));
  renderIcons();
}

function createServiceCard(service) {
  const card = document.createElement("article");
  const runtimeState = serviceState(service);
  const stateClass = serviceStateClass(runtimeState);
  card.className = `service-card ${stateClass}`;
  card.dataset.serviceCard = service.id;

  const healthPayload = normalizedHealthPayload(service);
  const stateLabel = serviceStateLabel(runtimeState, service);
  const health = serviceHealthLabel(service, healthPayload);
  const warmup = warmupLabel(service.warmup, healthPayload);
  const pid = service.external ? "external" : service.pid || "--";
  const port = safePort(service.health_url);

  const head = document.createElement("div");
  head.className = "service-head";
  const dot = document.createElement("span");
  dot.className = "status-dot";
  const title = document.createElement("div");
  title.className = "service-title";
  const name = document.createElement("strong");
  name.textContent = service.label || service.id;
  const desc = document.createElement("small");
  desc.textContent = serviceDescription(service, runtimeState, healthPayload);
  title.append(name, document.createElement("br"), desc);
  const badge = document.createElement("span");
  badge.className = `service-badge ${stateClass || "stopped"}`;
  badge.textContent = stateLabel;
  head.append(dot, title, badge);

  const meta = document.createElement("div");
  meta.className = "service-meta";
  for (const [label, value] of [["HEALTH", health], ["WARM", warmup], ["PID", pid], ["PORT", port]]) {
    const cell = document.createElement("div");
    cell.className = "meta-cell";
    const span = document.createElement("span");
    span.textContent = label;
    const strong = document.createElement("strong");
    strong.textContent = String(value);
    cell.append(span, strong);
    meta.appendChild(cell);
  }

  const actions = document.createElement("div");
  actions.className = "service-actions";
  const startBtn = serviceActionButton("start", "play", "启动");
  const stopBtn = serviceActionButton("stop", "square", "停止");
  const restartBtn = serviceActionButton("restart", "refresh-ccw", "重启");
  actions.append(startBtn, stopBtn, restartBtn);
  card.append(head, meta, actions);
  card.querySelector(".start").addEventListener("click", () => handleStart(service.id));
  card.querySelector(".stop").addEventListener("click", () => handleStop(service.id));
  card.querySelector(".restart").addEventListener("click", () => handleRestart(service.id));
  return card;
}

function normalizedHealthPayload(service) {
  const payload = service.health?.payload || {};
  if (payload.detail && typeof payload.detail === "object") return { ...payload, ...payload.detail };
  return payload;
}

function serviceState(service) {
  if (service.state) return String(service.state);
  const healthPayload = normalizedHealthPayload(service);
  if (healthPayload.status === "warming") return "warming";
  if (healthPayload.ready === false) return "starting";
  if (service.health?.ok === false && !service.port_open) return "failed";
  if (service.running) return "ready";
  return "stopped";
}

function serviceStateClass(runtimeState) {
  if (["ready", "running"].includes(runtimeState)) return "active";
  if (["starting", "warming", "queued", "retrying"].includes(runtimeState)) return "loading";
  if (runtimeState === "failed") return "failed";
  return "";
}

function serviceStateLabel(runtimeState, service) {
  const labels = {
    ready: service.external ? "External" : "Ready",
    running: "Running",
    starting: "Starting",
    warming: "Warming",
    stopped: "Stopped",
    failed: "Failed",
  };
  return labels[runtimeState] || runtimeState || "--";
}

function serviceHealthLabel(service, payload) {
  if (payload.ready === false && payload.status) return payload.status;
  if (!service.health) return "--";
  if (service.health.ok) return "OK";
  return service.error || service.health.error || "Fail";
}

function warmupLabel(warmup, payload = {}) {
  if (!warmup && payload.ready === false && payload.status) return payload.status;
  if (!warmup) return "--";
  const labels = {
    queued: "Queued",
    warming: `Try ${warmup.attempt || 1}`,
    retrying: `Retry ${warmup.attempt || 1}`,
    ready: "Ready",
    failed: "Failed",
  };
  return labels[warmup.state] || warmup.state || "--";
}

function serviceDescription(service, runtimeState, payload) {
  const base = service.description || "";
  if (service.warmup?.state === "retrying" && service.warmup?.error) {
    return `预热重试中：${shortText(service.warmup.error, 48)}`;
  }
  if (service.warmup?.state === "failed" && service.warmup?.error) {
    return `预热失败：${shortText(service.warmup.error, 48)}`;
  }
  if (payload.ready === false && payload.status) {
    return `模型${payload.status} · ${base}`;
  }
  if (runtimeState === "starting") return `服务启动中 · ${base}`;
  if (runtimeState === "warming") return `模型预热中 · ${base}`;
  if (runtimeState === "failed" && service.error) return `异常：${shortText(service.error, 56)}`;
  return base;
}

function shortText(value, length) {
  const text = String(value || "");
  return text.length > length ? `${text.slice(0, length - 3)}...` : text;
}

function serviceActionButton(className, icon, label) {
  const button = document.createElement("button");
  button.className = `service-action ${className}`;
  button.type = "button";
  button.append(createIcon(icon), document.createTextNode(label));
  return button;
}

/* ---- actions ---- */

async function handleStart(id) { setBusy(id, "启动中..."); if (state.previewMode) return showToast("预览模式", "info"); try { await startService(id); await loadServices(); renderServiceCards(); } catch (e) { showToast(`失败：${e.message}`, "error"); setBusy(id, ""); } }
async function handleStop(id)  { setBusy(id, "停止中..."); if (state.previewMode) return showToast("预览模式", "info"); try { await stopService(id); await loadServices(); renderServiceCards(); } catch (e) { showToast(`失败：${e.message}`, "error"); setBusy(id, ""); } }
async function handleRestart(id) { setBusy(id, "重启中..."); if (state.previewMode) return showToast("预览模式", "info"); try { await stopService(id); await startService(id); await loadServices(); renderServiceCards(); } catch (e) { showToast(`失败：${e.message}`, "error"); setBusy(id, ""); } }

function setBusy(id, label) {
  const card = document.querySelector(`[data-service-card="${id}"]`);
  if (!card) return;
  card.querySelectorAll("button").forEach((b) => { b.disabled = Boolean(label); });
}

async function handleStartAll() {
  if (state.previewMode) return showToast("预览模式", "info");
  showLog("启动 ASR...\nASR OK\n启动 LLM...\nLLM OK\n启动 TTS...");
  try { await startAllServices(); await loadServices(); renderServiceCards(); showLog("启动流程已提交。"); }
  catch (e) { showToast(`失败：${e.message}`, "error"); }
}

async function handleStopAll() {
  if (state.previewMode) return showToast("预览模式", "info");
  showLog("stopping all services...");
  try { await stopAllServices(); await loadServices(); renderServiceCards(); }
  catch (e) { showToast(`失败：${e.message}`, "error"); }
}

async function handleRestartAll() {
  if (state.previewMode) return showToast("预览模式", "info");
  showLog("停止全部...\n重新启动...");
  try { await stopAllServices(); await startAllServices(); await loadServices(); renderServiceCards(); }
  catch (e) { showToast(`失败：${e.message}`, "error"); }
}

async function handleClearAllLogs() {
  if (state.previewMode) return;
  if (!(await showConfirm("清空所有日志？"))) return;
  try { await clearAllServiceLogs(); showLog("日志已清空。"); await refreshLogs(state.selectedLogService, { quiet: true }); }
  catch (e) { showToast(`失败：${e.message}`, "error"); }
}

/* ---- logs ---- */

export async function refreshLogs(serviceId, options = {}) {
  state.selectedLogService = serviceId || state.selectedLogService || "asr";
  renderLogTabs();
  syncLogLiveButton();
  if (state.previewMode) { if (!options.quiet) showLog("预览模式没有真实日志。", options); return; }
  try { showLog(await fetchServiceLogs(state.selectedLogService) || "暂无日志。", options); }
  catch (e) { if (!options.quiet) showLog(`读取失败：${e.message}`, options); }
}

function showLog(text, options = {}) {
  const output = $("#logOutput");
  if (!output) return;

  const nextText = String(text || "");
  const nearBottom = output.scrollHeight - output.scrollTop - output.clientHeight < 48;
  currentLogText = nextText;
  output.replaceChildren();

  const sections = splitLogSections(nextText);
  for (const section of sections) output.appendChild(createLogSection(section));

  if (!sections.length) output.textContent = "暂无日志。";
  if (!options.preserveScroll || nearBottom) output.scrollTop = output.scrollHeight;
}

function splitLogSections(text) {
  const lines = String(text || "").split(/\r?\n/);
  const sections = [];
  let current = { title: "当前日志", lines: [] };
  const startRe = /^=+\s*start\s+(.+?)\s*=+$/i;

  for (const line of lines) {
    const match = line.match(startRe);
    if (match) {
      if (current.lines.length) sections.push(current);
      current = { title: `启动 ${match[1]}`, lines: [line] };
    } else {
      current.lines.push(line);
    }
  }
  if (current.lines.length) sections.push(current);
  return sections;
}

function createLogSection(section) {
  const block = document.createElement("section");
  block.className = "log-run";

  const head = document.createElement("div");
  head.className = "log-run-head";
  const title = document.createElement("strong");
  title.textContent = section.title;
  const count = document.createElement("span");
  count.textContent = `${section.lines.length} 行`;
  head.append(title, count);

  const body = document.createElement("pre");
  body.className = "log-run-body";
  body.textContent = section.lines.join("\n").trim() || "--";
  block.append(head, body);
  return block;
}

function renderLogTabs() {
  const host = $("#logTabs"); if (!host) return;
  host.innerHTML = "";
  for (const s of state.services) {
    const btn = document.createElement("button"); btn.type = "button";
    btn.className = `log-tab ${s.id === state.selectedLogService ? "active" : ""}`;
    btn.textContent = s.id.toUpperCase();
    btn.addEventListener("click", () => refreshLogs(s.id));
    host.appendChild(btn);
  }
}

async function copyCurrentLog() {
  const text = currentLogText || $("#logOutput")?.textContent || ""; if (!text.trim()) return;
  try { await navigator.clipboard.writeText(text); showToast("已复制", "success"); } catch { showToast("复制失败", "error"); }
}

function downloadCurrentLog() {
  const text = currentLogText || $("#logOutput")?.textContent || ""; if (!text.trim()) return;
  const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
  const a = document.createElement("a"); a.href = URL.createObjectURL(blob);
  a.download = `branchwhisper-${state.selectedLogService || "service"}.log`;
  document.body.appendChild(a); a.click(); a.remove();
  window.setTimeout(() => URL.revokeObjectURL(a.href), 200);
}

function toggleLogLiveRefresh() {
  logLiveRefresh = !logLiveRefresh;
  syncLogLiveButton();
  if (logLiveRefresh) refreshLogs(state.selectedLogService, { quiet: true });
}

function syncLogLiveButton() {
  const btn = $("#toggleLogLiveBtn");
  if (!btn) return;
  btn.replaceChildren(createIcon(logLiveRefresh ? "pause" : "play"));
  btn.title = logLiveRefresh ? "暂停实时刷新" : "继续实时刷新";
  btn.classList.toggle("off", !logLiveRefresh);
  renderIcons();
}

function startServicePolling() {
  window.clearInterval(state.servicePollTimer);
  state.servicePollTimer = window.setInterval(async () => {
    if (!servicesMounted) return;
    await Promise.all([
      loadServices(),
      refreshResources({ quiet: true }),
      logLiveRefresh ? refreshLogs(state.selectedLogService, { quiet: true, preserveScroll: true }) : Promise.resolve(),
    ]);
    renderServiceCards();
    setText("topStatus", serviceSummaryText());
  }, 4500);
}

function stopServicePolling() {
  window.clearInterval(state.servicePollTimer);
  state.servicePollTimer = 0;
}

window.addEventListener("beforeunload", stopServicePolling);
