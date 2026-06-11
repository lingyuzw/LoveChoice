import { state } from "../stores/state.js";
import { fetchJson } from "./client.js";

export async function uploadAvatar(dataUrl) {
  return fetchJson("/api/assets/avatar", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ data_url: dataUrl }),
  });
}

export async function uploadChatImage(dataUrl) {
  return fetchJson("/api/assets/chat-image", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ data_url: dataUrl }),
  });
}

export async function loadStickers(filters = {}) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(filters || {})) {
    if (value !== undefined && value !== null && String(value).trim()) params.set(key, value);
  }
  const data = await fetchJson(`/api/stickers${params.toString() ? `?${params}` : ""}`);
  state.stickers = data.stickers || [];
  return state.stickers;
}

export async function uploadSticker(dataUrl, tag = "默认", name = "", channels = "all") {
  const data = await fetchJson("/api/stickers", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ data_url: dataUrl, tag, name, channels }),
  });
  state.stickers = data.stickers || state.stickers || [];
  return data.sticker;
}

export async function testSticker(text, channel = "web", replyText = "") {
  return fetchJson("/api/stickers/test", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text, channel, reply_text: replyText }),
  });
}

export async function uploadStickerBatch(files, channels = "all") {
  const data = await fetchJson("/api/stickers/batch", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ files, channels }),
  });
  state.stickers = data.stickers || state.stickers || [];
  return data;
}

export async function updateSticker(stickerId, patch) {
  const data = await fetchJson(`/api/stickers/${encodeURIComponent(stickerId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch || {}),
  });
  state.stickers = data.stickers || state.stickers || [];
  return data.sticker;
}

export async function approveSticker(stickerId) {
  const data = await fetchJson(`/api/stickers/${encodeURIComponent(stickerId)}/approve`, { method: "POST" });
  state.stickers = data.stickers || state.stickers || [];
  return data.sticker;
}

export async function reanalyzeSticker(stickerId) {
  const data = await fetchJson(`/api/stickers/${encodeURIComponent(stickerId)}/reanalyze`, { method: "POST" });
  state.stickers = data.stickers || state.stickers || [];
  return data.sticker;
}

export async function testStickerVision(payload = {}) {
  return fetchJson("/api/stickers/vision-test", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {}),
  });
}

export async function bulkStickerAction(action, ids = [], options = {}) {
  const data = await fetchJson("/api/stickers/bulk", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, ids, ...(options || {}) }),
  });
  state.stickers = data.stickers || state.stickers || [];
  return data;
}

export async function deleteSticker(stickerId) {
  const data = await fetchJson(`/api/stickers/${encodeURIComponent(stickerId)}`, { method: "DELETE" });
  state.stickers = data.stickers || state.stickers || [];
  return data.ok;
}
