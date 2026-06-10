/* ============================================================
   ui-memory.js - Memory center
   BranchWhisper
   ============================================================ */

import { state } from "./state.js";
import { $, createIcon, renderIcons, setText, showConfirm, showToast } from "./utils.js";
import { addMemory, deleteMemory, loadMemory } from "./api.js";

let eventsBound = false;
let memorySearchTimer = 0;

export async function initMemoryPage() {
  setupMemoryEvents();
  await refreshMemoryPage({ quiet: true });
}

export async function enterMemoryPage() {
  await refreshMemoryPage({ quiet: true });
}

function setupMemoryEvents() {
  if (eventsBound) return;
  eventsBound = true;
  $("#memoryRefreshPageBtn")?.addEventListener("click", () => refreshMemoryPage());
  $("#memoryLayerFilter")?.addEventListener("change", () => {
    state.memoryPage = 1;
    renderMemoryPage();
  });
  $("#memorySearchInput")?.addEventListener("input", () => {
    window.clearTimeout(memorySearchTimer);
    memorySearchTimer = window.setTimeout(() => {
      state.memoryPage = 1;
      renderMemoryPage();
    }, 160);
  });
  $("#memoryAddPageBtn")?.addEventListener("click", handleAddMemory);
  $("#memoryAddPageInput")?.addEventListener("keydown", (event) => {
    if (event.key === "Enter") handleAddMemory();
  });
  $("#memoryPrevPageBtn")?.addEventListener("click", () => {
    state.memoryPage = Math.max(1, Number(state.memoryPage || 1) - 1);
    renderMemoryPage();
  });
  $("#memoryNextPageBtn")?.addEventListener("click", () => {
    state.memoryPage = Number(state.memoryPage || 1) + 1;
    renderMemoryPage();
  });
}

async function refreshMemoryPage(options = {}) {
  try {
    await loadMemory(240);
    renderMemoryPage();
    setText("topStatus", `${state.memories.length} 条记忆`);
  } catch (error) {
    if (!options.quiet) showToast(`记忆读取失败：${error.message}`, "error");
  }
}

function renderMemoryPage() {
  renderMemoryStats();
  renderMemoryList();
  renderIcons();
}

function renderMemoryStats() {
  const host = $("#memoryStatsGrid");
  if (!host) return;
  const items = state.memories || [];
  const counts = {
    total: items.length,
    short: items.filter((item) => item.layer === "short").length,
    mid: items.filter((item) => item.layer === "mid").length,
    long: items.filter((item) => item.layer === "long").length,
  };
  host.innerHTML = "";
  host.append(
    memoryStatCard("全部记忆", counts.total, "当前可用于上下文的记忆", ""),
    memoryStatCard("短期", counts.short, "最近对话里的临时偏好", "short"),
    memoryStatCard("中期", counts.mid, "重复出现的稳定信息", "mid"),
    memoryStatCard("长期", counts.long, "置顶或高置信信息", "long"),
  );
}

function memoryStatCard(title, value, detail, layer) {
  const card = document.createElement("button");
  card.type = "button";
  card.className = `memory-stat-card ${($("#memoryLayerFilter")?.value || "") === layer ? "active" : ""}`;
  card.dataset.memoryLayer = layer;
  card.innerHTML = `<span>${title}</span><strong>${value}</strong><small>${detail}</small>`;
  card.addEventListener("click", () => {
    const select = $("#memoryLayerFilter");
    if (select) select.value = layer;
    state.memoryPage = 1;
    renderMemoryPage();
  });
  return card;
}

function renderMemoryList() {
  const host = $("#memoryPageList");
  if (!host) return;
  host.innerHTML = "";
  const items = filteredMemories();
  const pageSize = Number(state.memoryPageSize || 30);
  const pageCount = Math.max(1, Math.ceil(items.length / pageSize));
  state.memoryPage = Math.max(1, Math.min(Number(state.memoryPage || 1), pageCount));
  updateMemoryPagination(items.length, pageCount);
  if (!items.length) {
    const empty = document.createElement("p");
    empty.className = "conversation-empty memory-empty";
    empty.textContent = "没有匹配的记忆。";
    host.appendChild(empty);
    return;
  }
  const start = (state.memoryPage - 1) * pageSize;
  for (const item of items.slice(start, start + pageSize)) host.appendChild(createMemoryRow(item));
}

function filteredMemories() {
  const query = ($("#memorySearchInput")?.value || "").trim().toLowerCase();
  const layer = $("#memoryLayerFilter")?.value || "";
  return (state.memories || []).filter((item) => {
    if (layer && item.layer !== layer) return false;
    if (!query) return true;
    return `${item.key || ""} ${item.value || ""}`.toLowerCase().includes(query);
  });
}

function updateMemoryPagination(total, pageCount) {
  const page = Number(state.memoryPage || 1);
  setText("memoryPageInfo", `${total} 条 · 第 ${page} / ${pageCount} 页`);
  const prev = $("#memoryPrevPageBtn");
  const next = $("#memoryNextPageBtn");
  if (prev) prev.disabled = page <= 1;
  if (next) next.disabled = page >= pageCount;
}

function createMemoryRow(item) {
  const row = document.createElement("article");
  row.className = `memory-row layer-${item.layer || "short"}`;
  const body = document.createElement("div");
  body.className = "memory-row-body";
  const key = document.createElement("strong");
  key.textContent = item.key || "记忆";
  const value = document.createElement("p");
  value.textContent = item.value || "";
  const meta = document.createElement("small");
  meta.textContent = `${layerLabel(item.layer)} · ${item.count || 1} 次 · 置信度 ${percent(item.confidence)} · ${formatTime(item.last_seen_at || item.last_changed_at)}`;
  body.append(key, value, meta);

  const actions = document.createElement("div");
  actions.className = "memory-row-actions";
  const del = document.createElement("button");
  del.className = "icon-button danger";
  del.type = "button";
  del.title = "删除记忆";
  del.append(createIcon("trash-2"));
  del.addEventListener("click", () => handleDeleteMemory(item.id));
  actions.appendChild(del);
  row.append(body, actions);
  return row;
}

async function handleAddMemory() {
  const input = $("#memoryAddPageInput");
  const text = input?.value.trim();
  if (!text) return;
  try {
    await addMemory(text);
    input.value = "";
    await refreshMemoryPage({ quiet: true });
    showToast("记忆已添加", "success");
  } catch (error) {
    showToast(`添加失败：${error.message}`, "error");
  }
}

async function handleDeleteMemory(id) {
  if (!id) return;
  if (!(await showConfirm("删除这条记忆？"))) return;
  try {
    await deleteMemory(id);
    await refreshMemoryPage({ quiet: true });
    showToast("记忆已删除", "success");
  } catch (error) {
    showToast(`删除失败：${error.message}`, "error");
  }
}

function layerLabel(layer) {
  return { short: "短期", mid: "中期", long: "长期" }[layer] || "未分层";
}

function percent(value) {
  const num = Number(value);
  return Number.isFinite(num) ? `${Math.round(num * 100)}%` : "--";
}

function formatTime(value) {
  if (!value) return "--";
  return String(value).replace("T", " ").slice(0, 16);
}
