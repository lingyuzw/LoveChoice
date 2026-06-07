/* ============================================================
   ui-services.js — Service orchestration page (services.html)
   LoveChoice Voice Console · Precision Console
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
      setText("topStatus", serviceSummaryText());
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

function serviceSummaryText() {
  const running = state.services.filter((s) => s.running).length;
  return `${running}/${state.services.length} 运行`;
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
  const healthOk = service.health?.ok;
  const stateClass = service.running ? "active" : healthOk === false ? "failed" : "";
  card.className = `service-card ${stateClass}`;
  card.dataset.serviceCard = service.id;

  const stateLabel = service.external ? "External" : service.running ? "Running" : healthOk === false ? "Failed" : "Stopped";
  const health = service.health ? (service.health.ok ? "OK" : "Fail") : "--";
  const pid = service.external ? "external" : service.pid || "--";
  const port = service.health_url ? new URL(service.health_url).port || "--" : "--";

  card.innerHTML = `
    <div class="service-head">
      <span class="status-dot"></span>
      <div class="service-title"><strong>${service.label || service.id}</strong></div>
      <span class="service-badge ${service.running ? 'running' : healthOk === false ? 'failed' : ''}">${stateLabel}</span>
    </div>
    <div class="service-meta">
      <div class="meta-cell"><span>HEALTH</span><strong>${health}</strong></div>
      <div class="meta-cell"><span>PID</span><strong>${pid}</strong></div>
      <div class="meta-cell"><span>PORT</span><strong>${port}</strong></div>
    </div>
    <div class="service-actions">
      <button class="service-action start" type="button"><i data-lucide="play"></i>启动</button>
      <button class="service-action stop" type="button"><i data-lucide="square"></i>停止</button>
      <button class="service-action restart" type="button"><i data-lucide="refresh-ccw"></i>重启</button>
    </div>
  `;
  card.querySelector(".start").addEventListener("click", () => handleStart(service.id));
  card.querySelector(".stop").addEventListener("click", () => handleStop(service.id));
  card.querySelector(".restart").addEventListener("click", () => handleRestart(service.id));
  return card;
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
  if (!window.confirm("清空所有日志？")) return;
  try { await clearAllServiceLogs(); await refreshLogs(state.selectedLogService, { quiet: true }); }
  catch (e) { showToast(`失败：${e.message}`, "error"); }
}

/* ---- logs ---- */

export async function refreshLogs(serviceId, options = {}) {
  state.selectedLogService = serviceId || state.selectedLogService || "asr";
  renderLogTabs();
  if (state.previewMode) { if (!options.quiet) showLog("预览模式没有真实日志。"); return; }
  try { showLog(await fetchServiceLogs(state.selectedLogService) || "暂无日志。"); }
  catch (e) { if (!options.quiet) showLog(`读取失败：${e.message}`); }
}

function showLog(text) {
  const output = $("#logOutput"); if (!output) return;
  output.textContent = text || ""; output.scrollTop = output.scrollHeight;
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
  const text = $("#logOutput")?.textContent || ""; if (!text.trim()) return;
  try { await navigator.clipboard.writeText(text); showToast("已复制", "success"); } catch { showToast("复制失败", "error"); }
}

function downloadCurrentLog() {
  const text = $("#logOutput")?.textContent || ""; if (!text.trim()) return;
  const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
  const a = document.createElement("a"); a.href = URL.createObjectURL(blob);
  a.download = `lovechoice-${state.selectedLogService || "service"}.log`;
  document.body.appendChild(a); a.click(); a.remove();
  window.setTimeout(() => URL.revokeObjectURL(a.href), 200);
}

function startServicePolling() {
  window.clearInterval(state.servicePollTimer);
  state.servicePollTimer = window.setInterval(async () => {
    await loadServices(); renderServiceCards(); setText("topStatus", serviceSummaryText());
  }, 4500);
}

window.addEventListener("beforeunload", () => { window.clearInterval(state.servicePollTimer); });
