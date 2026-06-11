import { state } from "../../stores/state.js";
import { loadConversation, loadConversations } from "../../api/index.js";
import { renderExternalConversation } from "../../dialog.js";

const CONVERSATION_POLL_MS = 2000;
let callbacks = {};

export function setupConversationRefresh(nextCallbacks = {}) {
  callbacks = nextCallbacks;
}

export function startConversationRefresh() {
  if (state.conversationPollTimer) return;
  state.conversationPollTimer = window.setInterval(() => {
    refreshConversationsNow({ reason: "poll" });
  }, CONVERSATION_POLL_MS);
}

export function stopConversationRefresh() {
  if (!state.conversationPollTimer) return;
  window.clearInterval(state.conversationPollTimer);
  state.conversationPollTimer = 0;
}

export function markConversationSnapshot() {
  state.conversationSnapshot = Object.fromEntries(
    (state.conversations || []).map((conversation) => [
      conversation.id,
      conversationKey(conversation),
    ]),
  );
}

export function conversationKey(conversation) {
  return `${conversation?.updated_at || ""}|${conversation?.message_count || 0}|${conversation?.last_message || ""}`;
}

export function conversationsChanged(snapshot, conversations) {
  if (!snapshot || Object.keys(snapshot).length !== conversations.length) return true;
  return conversations.some((conversation) => snapshot[conversation.id] !== conversationKey(conversation));
}

export function pauseConversationRefresh(ms = 1200) {
  state.conversationRefreshPausedUntil = Date.now() + ms;
}

function conversationMatchesCurrentScope(conversation) {
  if (!conversation) return false;
  if (state.conversationArchivedMode === "archived") return true;
  const isWeixin = Boolean(callbacks.isWeixinConversation?.(conversation));
  return (state.conversationScope || "recent") === "weixin" ? isWeixin : !isWeixin;
}

export async function refreshConversationsNow(options = {}) {
  if (document.body.dataset.page !== "dashboard") return false;
  if (state.previewMode) return false;
  if (Date.now() < Number(state.conversationRefreshPausedUntil || 0) && !options.force) return false;

  const before = { ...(state.conversationSnapshot || {}) };
  try {
    await loadConversations();
  } catch {
    return false;
  }

  const changed = options.force || conversationsChanged(before, state.conversations || []);
  if (!changed) return false;

  callbacks.renderConversationList?.();
  if (!options.skipActive) {
    await refreshActiveConversation({ previousSnapshot: before, force: Boolean(options.force) });
  }
  callbacks.syncChatView?.();
  return true;
}

export async function refreshActiveConversation(options = {}) {
  const activeId = state.activeConversationId;
  if (!activeId) return null;
  const active = (state.conversations || []).find((conversation) => conversation.id === activeId);
  if (!active && !options.force) return null;
  if (active && !conversationMatchesCurrentScope(active) && !callbacks.isWeixinConversation?.(active)) return null;
  if (active && !options.force && !callbacks.isWeixinConversation?.(active)) return null;
  if (active && !options.force && options.previousSnapshot?.[active.id] === conversationKey(active)) return null;

  try {
    const full = await loadConversation(activeId);
    if (!full || full.id !== state.activeConversationId) return null;
    if (!conversationMatchesCurrentScope(full) && !callbacks.isWeixinConversation?.(full)) return null;
    if (callbacks.isWeixinConversation?.(full) || options.force) {
      renderExternalConversation(full);
    }
    return full;
  } catch {
    return null;
  }
}
