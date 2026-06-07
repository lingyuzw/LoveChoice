/* ============================================================
   ui-services.js — Service orchestration page (services.html)
   LoveChoice Voice Console
   ============================================================ */

import { state } from "./state.js";
import { $, setText, renderIcons, showToast, showSkeleton } from "./utils.js";
import { loadConfig, loadServices, startService, stopService, startAllServices, stopAllServices, fetchServiceLogs, clearServiceLogs, clearAllServiceLogs } from "./api.js";

/* ---- init ---- */

export function initServices() {
  setupServiceEvents();
  const hashLog = location.hash.replace("#logs-", "");
  if (hashLog) state.selectedLogService = hashLog;

  showSkeleton("serviceCards", 3);
  loadConfig().then(() => {
    loadServices().then(() => {
      renderServiceCards();
      renderLogTabs();
      startServicePolling();
      refreshLogs(state.selectedLogService, { quiet: true });
      setText("systemState", serviceSummaryText());
    });
  });
}

function setupServiceEvents() {
  $("#startAllBtn")?.addEventListener("click", handleStartAll);
  $("#stopAllBtn")?.addEventListener("click", handleStopAll);
  $("#restartAllBtn")?.addEventListener("click", handleRestartAll);
  $("#clearAllLogsBtn")?.addEventListener("click", handleClearAllLogs);
  $("#healthBtn")?.addEventListener("click", () => loadServices().then(() => renderServiceCards()));
  $("#refreshLogsBtn")?.addEventListener("click", () => refreshLogs(state.selectedLogService));
  $("#copyLogBtn")?.addEventListener("click", copyCurrentLog);
  $("#downloadLogBtn")?.addEventListener("click", downloadCurrentLog);
}

/* ---- service status ---- */

function serviceSummaryText() {
  const running = state.services.filter((s) => s.running).length;
  return `${running}/${state.services.length} 运行`;
}

/* ---- service cards ---- */

function renderServiceCards() {
  const host = $("#serviceCards");
  if (!host) return;
  host.innerHTML = "";
  for (const service of state.services) host.appendChild(createServiceCard(service));
  renderIcons();
}

function createServiceCard(service) {
  const card = document.createElement("article");
  const healthOk = service.health?.ok;
  const stateClass = service.running ? "active" : healthOk === false ? "failed" : "";
  card.className = `service-card ${stateClass}`;
  card.dataset.serviceCard = service.id;

  const port = service.health_url ? new URL(service.health_url).port || "--" : "--";
  const pid = service.external ? "external" : service.pid || "--";
  const stateLabel = service.external ? "External" : service.running ? "Running" : healthOk === false ? "Failed" : "Stopped";
  const health = service.health ? (service.health.ok ? "OK" : "Fail") : "--";

  card.innerHTML = `
    <div class="service-top">
      <span class="status-dot"></span>
      <div class="service-title">
        <strong></strong>
        <span></span>
      </div>
      <b class="badge service-badge">${stateLabel}</b>
    </div>
    <div class="service-meta">
      <div class="meta-cell"><span>STATE</span><strong>${stateLabel}</strong></div>
      <div class="meta-cell"><span>PID</span><strong>${pid}</strong></div>
      <div class="meta-cell"><span>PORT</span><strong>${port}</strong></div>
      <div class="meta-cell"><span>HEALTH</span><strong>${health}</strong></div>
    </div>
    <div class="service-actions">
      <button class="service-action start" type="button"><i data-lucide="play"></i>启动</button>
      <button class="service-action stop" type="button"><i data-lucide="square"></i>停止</button>
      <button class="service-action restart" type="button"><i data-lucide="refresh-ccw"></i>重启</button>
    </div>
  `;
  card.querySelector(".service-title strong").textContent = service.label || service.id;
  card.querySelector(".service-title span").textContent = service.description || "";
  card.querySelector(".start").addEventListener("click", () => handleStart(service.id));
  card.querySelector(".stop").addEventListener("click", () => handleStop(service.id));
  card.querySelector(".restart").addEventListener("click", () => handleRestart(service.id));
  return card;
}

/* ---- service actions ---- */

async function handleStart(serviceId) {
  setServiceBusy(serviceId, "启动中...");
  if (state.previewMode) { showToast("预览模式：无法启动服务", "info"); return; }
  try {
    await startService(serviceId);
    await loadServices();
    renderServiceCards();
    await refreshLogs(serviceId);
  } catch (error) {
    showToast(`启动失败：${error.message}`, "error");
    setServiceBusy(serviceId, "");
  }
}

async function handleStop(serviceId) {
  setServiceBusy(serviceId, "停止中...");
  if (state.previewMode) { showToast("预览模式：无法停止服务", "info"); return; }
  try {
    await stopService(serviceId);
    await loadServices();
    renderServiceCards();
  } catch (error) {
    showToast(`停止失败：${error.message}`, "error");
    setServiceBusy(serviceId, "");
  }
}

async function handleRestart(serviceId) {
  setServiceBusy(serviceId, "重启中...");
  if (state.previewMode) { showToast("预览模式：无法重启服务", "info"); return; }
  try {
    await stopService(serviceId);
    await startService(serviceId);
    await loadServices();
    renderServiceCards();
    await refreshLogs(serviceId);
  } catch (error) {
    showToast(`重启失败：${error.message}`, "error");
    setServiceBusy(serviceId, "");
  }
}

function setServiceBusy(serviceId, label) {
  const card = document.querySelector(`[data-service-card="${serviceId}"]`);
  if (!card) return;
  card.querySelectorAll("button").forEach((btn) => { btn.disabled = Boolean(label); });
  const badge = card.querySelector(".service-badge");
  if (badge && label) badge.textContent = label;
}

async function handleStartAll() {
  if (state.previewMode) { showToast("预览模式：无法启动服务", "info"); return; }
  showLog("启动 ASR...\nASR OK\n启动 LLM...\nLLM OK\n启动 TTS...");
  try {
    await startAllServices();
    await loadServices();
    renderServiceCards();
    showLog("启动流程已提交。请刷新状态或查看各服务日志确认模型加载进度。");
  } catch (error) { showToast(`一键启动失败：${error.message}`, "error"); }
}

async function handleStopAll() {
  if (state.previewMode) { showToast("预览模式：无法停止服务", "info"); return; }
  showLog("stopping all services...");
  try { await stopAllServices(); await loadServices(); renderServiceCards(); }
  catch (error) { showToast(`全部停止失败：${error.message}`, "error"); }
}

async function handleRestartAll() {
  if (state.previewMode) { showToast("预览模式：无法重启服务", "info"); return; }
  showLog("停止全部服务...\n重新启动 ASR → LLM → TTS...");
  try {
    await stopAllServices();
    await startAllServices();
    await loadServices();
    renderServiceCards();
  } catch (error) { showToast(`全部重启失败：${error.message}`, "error"); }
}

async function handleClearAllLogs() {
  if (state.previewMode) return;
  if (!window.confirm("清空所有服务的运行日志？")) return;
  try {
    showLog("clearing all service logs...");
    await clearAllServiceLogs();
    await refreshLogs(state.selectedLogService, { quiet: true });
  } catch (error) { showToast(`清空全部日志失败：${error.message}`, "error"); }
}

/* ---- logs ---- */

export async function refreshLogs(serviceId, options = {}) {
  state.selectedLogService = serviceId || state.selectedLogService || "asr";
  renderLogTabs();

  if (state.previewMode) {
    if (!options.quiet) showLog("预览模式没有真实日志。正式后端启动后这里会显示模型 stdout/stderr。");
    return;
  }
  try {
    const logs = await fetchServiceLogs(state.selectedLogService);
    showLog(logs || "暂无日志。");
  } catch (error) {
    if (!options.quiet) showLog(`日志读取失败：${error.message}`);
  }
}

function showLog(text) {
  const output = $("#logOutput");
  if (!output) return;
  output.textContent = text || "";
  output.scrollTop = output.scrollHeight;
}

function renderLogTabs() {
  const host = $("#logTabs");
  if (!host) return;
  host.innerHTML = "";
  for (const service of state.services) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `log-tab ${service.id === state.selectedLogService ? "active" : ""}`;
    button.textContent = service.id.toUpperCase();
    button.addEventListener("click", () => refreshLogs(service.id));
    host.appendChild(button);
  }
}

async function copyCurrentLog() {
  const text = $("#logOutput")?.textContent || "";
  if (!text.trim()) return;
  try { await navigator.clipboard.writeText(text); showToast("日志已复制", "success"); }
  catch { showToast("复制失败", "error"); }
}

function downloadCurrentLog() {
  const text = $("#logOutput")?.textContent || "";
  if (!text.trim()) return;
  const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = `lovechoice-${state.selectedLogService || "service"}.log`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(link.href), 200);
}

/* ---- polling ---- */

function startServicePolling() {
  window.clearInterval(state.servicePollTimer);
  state.servicePollTimer = window.setInterval(async () => {
    await loadServices();
    renderServiceCards();
    setText("systemState", serviceSummaryText());
  }, 4500);
}

// stop polling when leaving page
window.addEventListener("beforeunload", () => {
  window.clearInterval(state.servicePollTimer);
});
