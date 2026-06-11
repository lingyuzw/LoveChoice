import { state } from "../stores/state.js";
import {
  approveSticker,
  bulkStickerAction,
  deleteSticker,
  loadConfig,
  loadStickers,
  reanalyzeSticker,
  saveConfig,
  testSticker,
  testStickerVision,
  updateSticker,
  uploadStickerBatch,
} from "../api/index.js";
import { $, createIcon, renderIcons, setValue, showConfirm, showToast, value } from "../utils/dom.js";

let bound = false;
let selectedStickerId = "";
const selectedIds = new Set();

const STICKER_CONFIG_FIELDS = [
  ["stickers_enabled", "assetStickersEnabled", "bool"],
  ["sticker_activity", "assetStickerActivity", "string"],
  ["sticker_cooldown_sec", "assetStickerCooldownSec", "number"],
  ["sticker_daily_limit", "assetStickerDailyLimit", "number"],
  ["sticker_max_streak", "assetStickerMaxStreak", "number"],
  ["sticker_custom_probability", "assetStickerCustomProbability", "number"],
];

export async function initAssetsPage() {
  bindEvents();
  await loadConfig();
  fillAssetConfig();
  await refreshAssets();
}

export async function enterAssetsPage() {
  await refreshAssets();
}

export function leaveAssetsPage() {}

function bindEvents() {
  if (bound) return;
  bound = true;
  $("#assetStickerUploadBtn")?.addEventListener("click", () => $("#assetStickerFileInput")?.click());
  $("#assetStickerFileInput")?.addEventListener("change", uploadFiles);
  $("#assetStickerRefreshBtn")?.addEventListener("click", refreshAssets);
  $("#assetStickerVisionTestBtn")?.addEventListener("click", runVisionTest);
  $("#assetStickerConfigSaveBtn")?.addEventListener("click", saveAssetConfig);
  $("#assetStickerStatusFilter")?.addEventListener("change", refreshAssets);
  $("#assetStickerEmotionFilter")?.addEventListener("change", refreshAssets);
  $("#assetStickerSearchInput")?.addEventListener("input", () => {
    window.clearTimeout(state.assetSearchTimer);
    state.assetSearchTimer = window.setTimeout(refreshAssets, 220);
  });
  $("#assetStickerTestBtn")?.addEventListener("click", runPolicyTest);
  $("#assetSelectAllBtn")?.addEventListener("click", toggleSelectAllVisible);
  $("#assetBulkReanalyzeBtn")?.addEventListener("click", () => runBulk("reanalyze", false));
  $("#assetBulkApproveBtn")?.addEventListener("click", () => runBulk("approve", false));
  $("#assetBulkDeleteBtn")?.addEventListener("click", () => runBulk("delete", false));
  $("#assetBulkReanalyzeAllBtn")?.addEventListener("click", () => runBulk("reanalyze", true));
  $("#assetBulkApproveAllBtn")?.addEventListener("click", () => runBulk("approve", true));
  $("#assetBulkDeleteAllBtn")?.addEventListener("click", () => runBulk("delete", true));
}

function fillAssetConfig() {
  const config = state.currentConfig || {};
  setValue("assetStickerVisionEnabled", String(config.sticker_vision_enabled !== false));
  setValue("assetStickerVisionUrl", config.sticker_vision_url || config.vision_url || "");
  setValue("assetStickerVisionModel", config.sticker_vision_model || config.vision_model || "");
  setValue("assetStickerVisionTimeout", config.sticker_vision_timeout || 45);
  setValue("assetStickerVisionMaxTokens", config.sticker_vision_max_tokens || 420);
  const keyInput = $("#assetStickerVisionApiKey");
  if (keyInput) {
    keyInput.value = "";
    keyInput.placeholder = config.sticker_vision_api_key_masked || (config.sticker_vision_api_key_set ? "已保存，留空不修改" : "未设置");
  }
  setValue("assetStickersEnabled", String(config.stickers_enabled !== false));
  setValue("assetStickerActivity", config.sticker_activity || "active");
  setValue("assetStickerCooldownSec", config.sticker_cooldown_sec ?? 90);
  setValue("assetStickerDailyLimit", config.sticker_daily_limit ?? 60);
  setValue("assetStickerMaxStreak", config.sticker_max_streak ?? 2);
  setValue("assetStickerCustomProbability", config.sticker_custom_probability ?? 0.65);
}

async function saveAssetConfig() {
  const payload = {
    sticker_vision_enabled: value("assetStickerVisionEnabled", "true") === "true",
    sticker_vision_url: value("assetStickerVisionUrl", "").trim(),
    sticker_vision_model: value("assetStickerVisionModel", "").trim(),
    sticker_vision_timeout: Number(value("assetStickerVisionTimeout", 45)) || 45,
    sticker_vision_max_tokens: Number(value("assetStickerVisionMaxTokens", 420)) || 420,
  };
  const key = value("assetStickerVisionApiKey", "").trim();
  if (key) payload.sticker_vision_api_key = key;
  for (const [configKey, id, kind] of STICKER_CONFIG_FIELDS) {
    const raw = value(id, "");
    payload[configKey] = kind === "number" ? Number(raw) : (kind === "bool" ? raw === "true" : raw);
  }
  state.currentConfig = await saveConfig(payload);
  fillAssetConfig();
  showToast("素材配置已保存", "success");
}

async function refreshAssets() {
  const filters = currentFilters();
  await loadStickers(filters);
  selectedIds.forEach((id) => {
    if (!state.stickers.some((item) => item.id === id)) selectedIds.delete(id);
  });
  if (selectedStickerId && !state.stickers.some((item) => item.id === selectedStickerId)) selectedStickerId = "";
  renderStats();
  renderBulkBar();
  renderGallery();
  renderDetail(selectedSticker());
  renderIcons();
}

function currentFilters() {
  return {
    status: value("assetStickerStatusFilter", ""),
    emotion: value("assetStickerEmotionFilter", ""),
    q: value("assetStickerSearchInput", "").trim(),
  };
}

function selectedSticker() {
  return (state.stickers || []).find((item) => item.id === selectedStickerId) || null;
}

function renderStats() {
  const host = $("#assetStatsGrid");
  if (!host) return;
  const items = state.stickers || [];
  const count = (status) => items.filter((item) => item.review_status === status).length;
  host.innerHTML = "";
  for (const card of [
    ["当前视图", items.length],
    ["待审核", count("pending")],
    ["已通过", count("approved")],
    ["失败", count("failed")],
  ]) {
    const el = document.createElement("article");
    el.className = "asset-stat-card";
    el.innerHTML = `<small>${card[0]}</small><strong>${card[1]}</strong>`;
    host.appendChild(el);
  }
}

function renderBulkBar() {
  const count = selectedIds.size;
  const label = $("#assetSelectedCount");
  if (label) label.textContent = count ? `已选 ${count} 张` : "未选择";
  for (const id of ["assetBulkReanalyzeBtn", "assetBulkApproveBtn", "assetBulkDeleteBtn"]) {
    const button = $(`#${id}`);
    if (button) button.disabled = count === 0;
  }
}

function renderGallery() {
  const host = $("#assetStickerGallery");
  if (!host) return;
  const items = state.stickers || [];
  host.innerHTML = "";
  if (!items.length) {
    const empty = document.createElement("div");
    empty.className = "asset-empty";
    empty.textContent = "还没有素材。点击右上角批量上传 PNG、JPG 或 WebP。";
    host.appendChild(empty);
    return;
  }
  for (const sticker of items) {
    const card = document.createElement("article");
    card.className = `asset-card review-${sticker.review_status || "pending"}`;
    card.classList.toggle("active", sticker.id === selectedStickerId);
    card.classList.toggle("selected", selectedIds.has(sticker.id));

    const check = document.createElement("button");
    check.type = "button";
    check.className = "asset-card-check";
    check.title = selectedIds.has(sticker.id) ? "取消选择" : "选择";
    check.innerHTML = selectedIds.has(sticker.id) ? '<i data-lucide="check-square"></i>' : '<i data-lucide="square"></i>';
    check.addEventListener("click", (event) => {
      event.stopPropagation();
      toggleStickerSelection(sticker.id);
    });

    const preview = document.createElement("button");
    preview.type = "button";
    preview.className = "asset-card-preview";
    preview.addEventListener("click", () => {
      selectedStickerId = sticker.id;
      renderGallery();
      renderDetail(sticker);
      renderIcons();
    });
    const img = document.createElement("img");
    img.src = sticker.thumbnail || sticker.url || sticker.file || sticker.send_file;
    img.alt = sticker.caption || sticker.name || "表情包";
    const meta = document.createElement("span");
    meta.innerHTML = `<strong>${escapeHtml(sticker.emotion || sticker.tag || "sticker")}</strong><small>${escapeHtml(statusLabel(sticker.review_status))} · 强度 ${Number(sticker.intensity || 3)}</small>`;
    preview.append(img, meta);
    card.append(check, preview);
    host.appendChild(card);
  }
}

function toggleStickerSelection(id) {
  if (selectedIds.has(id)) selectedIds.delete(id);
  else selectedIds.add(id);
  renderBulkBar();
  renderGallery();
  renderIcons();
}

function toggleSelectAllVisible() {
  const visibleIds = (state.stickers || []).map((item) => item.id).filter(Boolean);
  const allSelected = visibleIds.length && visibleIds.every((id) => selectedIds.has(id));
  if (allSelected) visibleIds.forEach((id) => selectedIds.delete(id));
  else visibleIds.forEach((id) => selectedIds.add(id));
  renderBulkBar();
  renderGallery();
  renderIcons();
}

function renderDetail(sticker) {
  const host = $("#assetStickerDetail");
  if (!host) return;
  if (!sticker) {
    host.className = "asset-detail-panel empty";
    host.textContent = "选择一张素材后，可以复核分类、标签和适用场景。";
    return;
  }
  host.className = "asset-detail-panel";
  host.innerHTML = `
    <div class="asset-detail-head">
      <img src="${escapeAttr(sticker.thumbnail || sticker.url || sticker.file || "")}" alt="preview" />
      <div><strong>${escapeHtml(sticker.name || sticker.id)}</strong><small>${escapeHtml(sticker.id)} · ${escapeHtml(statusLabel(sticker.review_status))}</small></div>
    </div>
    <label><span>名称</span><input data-asset-field="name" value="${escapeAttr(sticker.name || "")}" /></label>
    <label><span>主分类</span><input data-asset-field="emotion" value="${escapeAttr(sticker.emotion || "laugh")}" /></label>
    <label><span>标签</span><input data-asset-field="tags" value="${escapeAttr((sticker.tags || []).join("，"))}" /></label>
    <label><span>适用场景</span><textarea data-asset-field="scene">${escapeHtml((sticker.scene || []).join("，"))}</textarea></label>
    <label><span>禁用场景</span><textarea data-asset-field="avoid">${escapeHtml((sticker.avoid || []).join("，"))}</textarea></label>
    <label><span>说明</span><textarea data-asset-field="caption">${escapeHtml(sticker.caption || "")}</textarea></label>
    <label><span>OCR</span><textarea data-asset-field="ocr_text">${escapeHtml(sticker.ocr_text || "")}</textarea></label>
    <div class="asset-detail-actions">
      <button class="primary-action" data-asset-action="save" type="button"><i data-lucide="save"></i>保存</button>
      <button class="secondary-action" data-asset-action="approve" type="button"><i data-lucide="check"></i>通过</button>
      <button class="secondary-action" data-asset-action="reanalyze" type="button"><i data-lucide="scan-eye"></i>重新识别</button>
      <button class="icon-button danger" data-asset-action="delete" type="button" title="删除"><i data-lucide="trash-2"></i></button>
    </div>
    ${sticker.error ? `<div class="asset-error">${escapeHtml(sticker.error)}</div>` : ""}
  `;
  host.querySelector('[data-asset-action="save"]')?.addEventListener("click", () => saveDetail(sticker.id));
  host.querySelector('[data-asset-action="approve"]')?.addEventListener("click", () => approveAndRefresh(sticker.id));
  host.querySelector('[data-asset-action="reanalyze"]')?.addEventListener("click", () => reanalyzeAndRefresh(sticker.id));
  host.querySelector('[data-asset-action="delete"]')?.addEventListener("click", () => deleteAndRefresh(sticker.id));
}

async function uploadFiles(event) {
  const files = Array.from(event.target.files || []);
  if (!files.length) return;
  const valid = files.filter((file) => ["image/png", "image/jpeg", "image/webp"].includes(String(file.type || "").toLowerCase()));
  if (!valid.length) {
    showToast("请选择 PNG、JPG 或 WebP", "error");
    return;
  }
  const button = $("#assetStickerUploadBtn");
  try {
    setBusy(button, `正在入库 ${valid.length} 张...`);
    const payload = [];
    for (const file of valid.slice(0, 120)) payload.push({ name: file.name, data_url: await fileToDataUrl(file) });
    const result = await uploadStickerBatch(payload, "all");
    const ok = (result.results || []).filter((item) => item.ok).length;
    const pending = (result.results || []).filter((item) => item.ok && item.analyzed === false && !item.duplicate).length;
    const duplicate = (result.results || []).filter((item) => item.duplicate).length;
    const failed = (result.results || []).filter((item) => !item.ok).length;
    showToast(`入库 ${ok} 张${duplicate ? `，重复 ${duplicate} 张` : ""}${pending ? `，${pending} 张待识别` : ""}，失败 ${failed} 张`, failed || pending ? "info" : "success");
    await refreshAssets();
  } finally {
    clearBusy(button, '<i data-lucide="image-plus"></i>批量上传');
    event.target.value = "";
  }
}

async function saveDetail(id) {
  const field = (name) => $(`[data-asset-field="${name}"]`)?.value || "";
  await updateSticker(id, {
    name: field("name"),
    emotion: field("emotion"),
    tags: splitList(field("tags")),
    scene: splitList(field("scene")),
    avoid: splitList(field("avoid")),
    caption: field("caption"),
    ocr_text: field("ocr_text"),
  });
  showToast("素材已保存", "success");
  await refreshAssets();
}

async function approveAndRefresh(id) {
  await approveSticker(id);
  showToast("素材已通过审核", "success");
  await refreshAssets();
}

async function reanalyzeAndRefresh(id) {
  showToast("正在重新识别...", "info");
  await reanalyzeSticker(id);
  await refreshAssets();
}

async function deleteAndRefresh(id) {
  if (!(await showConfirm("删除这张素材？"))) return;
  await deleteSticker(id);
  selectedIds.delete(id);
  selectedStickerId = "";
  await refreshAssets();
}

async function runBulk(action, includeFiltered) {
  const ids = includeFiltered ? [] : Array.from(selectedIds);
  const filters = currentFilters();
  const scopeText = includeFiltered ? "当前筛选结果" : `${ids.length} 张选中素材`;
  if (!includeFiltered && !ids.length) {
    showToast("先选择素材", "info");
    return;
  }
  if (action === "delete") {
    const ok = await showConfirm(`删除${scopeText}？这个操作会移除素材文件。`);
    if (!ok) return;
  }
  const button = bulkButton(action, includeFiltered);
  try {
    setBusy(button, actionBusyText(action));
    const result = await bulkStickerAction(action, ids, { include_filtered: includeFiltered, filters });
    if (action === "delete") {
      selectedIds.clear();
      selectedStickerId = "";
    }
    showToast(`${actionLabel(action)}完成：成功 ${result.success || 0}，失败 ${result.failed || 0}`, result.failed ? "info" : "success");
    await refreshAssets();
  } finally {
    clearBusy(button, button?.dataset.label || "");
  }
}

function bulkButton(action, includeFiltered) {
  const id = {
    "reanalyze:false": "assetBulkReanalyzeBtn",
    "approve:false": "assetBulkApproveBtn",
    "delete:false": "assetBulkDeleteBtn",
    "reanalyze:true": "assetBulkReanalyzeAllBtn",
    "approve:true": "assetBulkApproveAllBtn",
    "delete:true": "assetBulkDeleteAllBtn",
  }[`${action}:${includeFiltered}`];
  return id ? $(`#${id}`) : null;
}

async function runPolicyTest() {
  const host = $("#assetStickerTestResult");
  if (host) {
    host.className = "asset-test-result loading";
    host.textContent = "测试中...";
  }
  const result = await testSticker(value("assetStickerTestInput", ""), value("assetStickerTestChannel", "web"));
  const sticker = result.sticker;
  if (!host) return;
  host.className = `asset-test-result ${sticker ? "hit" : "miss"}`;
  if (sticker) {
    host.innerHTML = `<strong>命中</strong><span>${escapeHtml(sticker.name || sticker.id)} · ${escapeHtml(sticker.tag || sticker.emotion)}</span>`;
  } else {
    host.innerHTML = `<strong>未命中</strong><span>${escapeHtml(result.intent?.reason || "unknown")}</span>`;
  }
}

async function runVisionTest() {
  const sticker = selectedSticker();
  if (!sticker) {
    showToast("先选择一张素材再测试识别 API", "info");
    return;
  }
  const result = await testStickerVision({ sticker_id: sticker.id });
  if (result.ok) {
    showToast(`识别 API 可用：${result.vision_model || "model"}`, "success");
  } else {
    showToast(`识别 API 不可用：${result.error}`, "error");
  }
}

function setBusy(button, label) {
  if (!button) return;
  button.dataset.label = button.innerHTML;
  button.disabled = true;
  button.textContent = label;
}

function clearBusy(button, fallbackHtml) {
  if (!button) return;
  button.disabled = false;
  button.innerHTML = button.dataset.label || fallbackHtml;
  delete button.dataset.label;
  renderIcons();
}

function actionLabel(action) {
  return { reanalyze: "识别", approve: "通过", delete: "删除" }[action] || action;
}

function actionBusyText(action) {
  return { reanalyze: "识别中...", approve: "通过中...", delete: "删除中..." }[action] || "处理中...";
}

function statusLabel(status) {
  return { pending: "待审核", approved: "已通过", failed: "失败", disabled: "停用" }[status] || (status || "待审核");
}

function fileToDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

function splitList(text) {
  return String(text || "").split(/[,，、\s]+/).map((item) => item.trim()).filter(Boolean).slice(0, 10);
}

function escapeHtml(input) {
  return String(input || "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function escapeAttr(input) {
  return escapeHtml(input).replace(/`/g, "&#96;");
}
