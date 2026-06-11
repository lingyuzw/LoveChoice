<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from "vue";
import { Archive, Download, ImagePlus, MessageSquarePlus, Mic, MicOff, Search, Send, Square, Star, Trash2, Volume2, VolumeX } from "@lucide/vue";
import { uploadChatImage } from "@/api/assets";
import { conversationExportUrl, updateConversation, type ChatAttachment, type ChatMessage, type ConversationSummary } from "@/api/conversations";
import { useAppStore } from "@/stores/app";
import { useConversationsStore } from "@/stores/conversations";
import { appendMicSamples, createAudioRuntime, schedulePcm16, startMic, stopAssistantAudio, stopMic } from "@/utils/audio";
import { fileToDataUrl } from "@/utils/files";

type Scope = "recent" | "weixin";

const app = useAppStore();
const conversations = useConversationsStore();
const scroller = ref<HTMLElement | null>(null);
const imageInput = ref<HTMLInputElement | null>(null);
const draft = ref("");
const connected = ref(false);
const busy = ref(false);
const assistantActive = ref(false);
const micActive = ref(false);
const ttsEnabled = ref(true);
const level = ref(0);
const socket = ref<WebSocket | null>(null);
const socketGeneration = ref(0);
const activeScope = ref<Scope>("recent");
const liveMessages = ref<ChatMessage[]>([]);
const pendingAttachments = ref<ChatAttachment[]>([]);
const audio = createAudioRuntime();
const releaseTimer = ref<number | null>(null);
const manualClose = ref(false);
const metrics = reactive({
  status: "待机",
  trace: "--",
  vad: "--",
  asr: "--",
  llm: "--",
  tts: "--",
});

const visibleConversations = computed(() => (activeScope.value === "weixin" ? conversations.weixinChats : conversations.webChats));
const hasMessages = computed(() => liveMessages.value.length > 0);
const activeStoreKey = computed(() => {
  const active = conversations.active;
  if (!active) return "";
  const messages = active.messages || [];
  const last = messages[messages.length - 1];
  return `${active.id}|${active.updated_at || ""}|${messages.length}|${last?.id || ""}|${last?.content?.length || 0}|${last?.attachments?.length || 0}`;
});

onMounted(async () => {
  await conversations.reloadList();
  const first = visibleConversations.value[0] || conversations.items[0];
  if (first) await openConversation(first.id, { force: true });
  else newConversation();
  conversations.startPolling();
});

onUnmounted(() => {
  conversations.stopPolling();
  closeSocket();
  stopMic(audio);
  stopAssistantAudio(audio);
  if (releaseTimer.value) window.clearTimeout(releaseTimer.value);
});

watch(activeStoreKey, () => {
  const active = conversations.active;
  if (!active?.messages) return;
  const nextMessages = [...active.messages];
  if (transcriptKey(nextMessages) === transcriptKey(liveMessages.value)) return;
  if (busy.value && !isWeixinConversation(active)) return;
  const shouldFollow = isNearBottom();
  liveMessages.value = nextMessages;
  scrollToBottom({ force: shouldFollow || isWeixinConversation(active) });
});

function isWeixinConversation(item: ConversationSummary) {
  return Boolean(item.platform_id || item.source === "weixin_personal" || String(item.source || "").includes("weixin"));
}

function isNearBottom(threshold = 100) {
  const el = scroller.value;
  if (!el) return true;
  return el.scrollHeight - el.scrollTop - el.clientHeight <= threshold;
}

function scrollToBottom(options: { smooth?: boolean; force?: boolean } = {}) {
  const run = () => {
    const el = scroller.value;
    if (!el) return;
    const top = el.scrollHeight;
    if (options.smooth) el.scrollTo({ top, behavior: "smooth" });
    else el.scrollTop = top;
  };
  if (options.force || isNearBottom()) {
    void nextTick(() => {
      run();
      requestAnimationFrame(run);
    });
  }
}

async function openConversation(id: string, options: { force?: boolean } = {}) {
  if (!id) return;
  if (!options.force && conversations.active?.id === id) {
    return;
  }
  closeSocket();
  await conversations.select(id, { force: true });
  liveMessages.value = [...(conversations.active?.messages || [])];
  connectSocket(id);
  scrollToBottom({ force: true });
}

function newConversation() {
  closeSocket();
  conversations.active = null;
  liveMessages.value = [];
  pendingAttachments.value = [];
  resetMetrics("新对话");
  connectSocket("");
  scrollToBottom({ force: true });
}

async function removeConversation(item: ConversationSummary) {
  const wasActive = conversations.active?.id === item.id;
  await conversations.remove(item.id);
  if (wasActive) newConversation();
}

async function toggleFavorite(item: ConversationSummary) {
  await updateConversation(item.id, { favorite: !item.favorite });
  await conversations.reloadList(true);
}

async function archiveConversation(item: ConversationSummary) {
  await updateConversation(item.id, { archived: !item.archived });
  if (conversations.active?.id === item.id) newConversation();
  await conversations.reloadList(true);
}

function exportConversation(item: ConversationSummary) {
  window.open(conversationExportUrl(item.id), "_blank");
}

function switchScope(scope: Scope) {
  activeScope.value = scope;
  if (conversations.active && visibleConversations.value.some((item) => item.id === conversations.active?.id)) return;
  newConversation();
}

function connectSocket(conversationId = conversations.active?.id || "") {
  if (socket.value && socket.value.readyState <= WebSocket.OPEN) {
    const currentId = (socket.value as WebSocket & { datasetConversationId?: string }).datasetConversationId || "";
    if (currentId === conversationId) return;
    closeSocket();
  }
  const generation = socketGeneration.value + 1;
  socketGeneration.value = generation;
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const query = conversationId ? `?conversation_id=${encodeURIComponent(conversationId)}` : "";
  const ws = new WebSocket(`${scheme}://${location.host}/ws/dialog${query}`);
  (ws as WebSocket & { datasetConversationId?: string }).datasetConversationId = conversationId;
  ws.binaryType = "arraybuffer";
  socket.value = ws;
  ws.addEventListener("open", () => {
    if (generation !== socketGeneration.value || socket.value !== ws) return;
    connected.value = true;
    metrics.status = micActive.value ? "监听中" : "待机";
    sendRuntimeSettings();
  });
  ws.addEventListener("close", () => {
    if (generation !== socketGeneration.value || socket.value !== ws) return;
    connected.value = false;
    busy.value = false;
    assistantActive.value = false;
    stopAssistantAudio(audio);
    if (!manualClose.value) window.setTimeout(() => connectSocket(conversations.active?.id || ""), 1200);
  });
  ws.addEventListener("error", () => {
    if (generation !== socketGeneration.value || socket.value !== ws) return;
    metrics.status = "连接异常";
  });
  ws.addEventListener("message", (event) => {
    if (generation !== socketGeneration.value || socket.value !== ws) return;
    void handleSocketMessage(event);
  });
}

function closeSocket() {
  manualClose.value = true;
  socketGeneration.value += 1;
  socket.value?.close();
  socket.value = null;
  connected.value = false;
  busy.value = false;
  assistantActive.value = false;
  window.setTimeout(() => {
    manualClose.value = false;
  }, 100);
}

function sendRuntimeSettings() {
  if (socket.value?.readyState !== WebSocket.OPEN) return;
  const settings = { ...(app.config || {}) };
  delete settings.llm_api_key;
  delete settings.api_llm_api_key;
  settings.tts_enabled = ttsEnabled.value;
  socket.value.send(JSON.stringify({ type: "settings", settings }));
}

async function handleSocketMessage(event: MessageEvent) {
  if (event.data instanceof ArrayBuffer) {
    await schedulePcm16(audio, event.data);
    return;
  }
  if (event.data instanceof Blob) {
    await schedulePcm16(audio, await event.data.arrayBuffer());
    return;
  }
  if (typeof event.data !== "string") return;
  let data: Record<string, any>;
  try {
    data = JSON.parse(event.data);
  } catch {
    return;
  }
  handleSocketEvent(data);
}

function handleSocketEvent(data: Record<string, any>) {
  const shouldFollow = isNearBottom();
  if (data.type === "ready") resetMetrics("待机");
  if (data.type === "settings") {
    ttsEnabled.value = Boolean((data.settings || {}).tts_enabled ?? ttsEnabled.value);
  }
  if (data.type === "conversation") {
    conversations.active = data.conversation;
    liveMessages.value = [...(data.conversation?.messages || [])];
    scrollToBottom({ force: true });
  }
  if (data.type === "conversation_saved") {
    conversations.active = data.conversation;
    if (data.conversation?.messages?.length) liveMessages.value = [...data.conversation.messages];
    void conversations.reloadList(true);
    scrollToBottom({ force: true });
  }
  if (data.type === "trace") {
    metrics.trace = data.trace_id ? String(data.trace_id).slice(-10) : "--";
  }
  if (data.type === "status") {
    metrics.status = statusLabel(data.label || data.status || data.stage || "");
  }
  if (data.type === "vad_start") {
    metrics.vad = "speech";
    metrics.status = "收音";
  }
  if (data.type === "vad_end") {
    metrics.vad = `${data.duration_ms || 0}ms`;
    metrics.status = "识别";
    busy.value = true;
  }
  if (data.type === "user") {
    liveMessages.value.push({ role: "user", content: data.text || "", attachments: data.attachments || [], created_at: "刚刚" });
    metrics.status = "思考中";
  }
  if (data.type === "assistant_start") {
    if (releaseTimer.value) window.clearTimeout(releaseTimer.value);
    busy.value = true;
    assistantActive.value = true;
    liveMessages.value.push({ role: "assistant", content: "", attachments: [], created_at: "生成中" });
    metrics.status = "生成";
  }
  if (data.type === "llm_delta") {
    const last = liveMessages.value[liveMessages.value.length - 1];
    if (last?.role === "assistant") last.content += data.text || "";
    metrics.status = "输出中";
  }
  if (data.type === "assistant_attachment") {
    const last = liveMessages.value[liveMessages.value.length - 1];
    if (last?.role === "assistant") last.attachments = [...(last.attachments || []), ...((data.attachments || []) as ChatAttachment[])];
  }
  if (data.type === "audio_format") {
    audio.ttsSampleRate = Number(data.sample_rate || 24000);
    metrics.status = "播放";
  }
  if (data.type === "metric") {
    setMetric(data.name, data.value);
  }
  if (data.type === "interrupted") {
    stopAssistantAudio(audio);
    busy.value = false;
    assistantActive.value = false;
    metrics.status = micActive.value ? "监听中" : "待机";
  }
  if (data.type === "turn_done") {
    releaseAfterPlayback();
    void conversations.reloadList(true);
  }
  if (data.type === "error") {
    busy.value = false;
    assistantActive.value = false;
    metrics.status = data.message || "出错";
  }
  if (shouldFollow || ["user", "assistant_start", "llm_delta", "assistant_attachment", "turn_done"].includes(String(data.type))) {
    scrollToBottom({ force: shouldFollow });
  }
}

function setMetric(name: string, value: unknown) {
  const text = Number.isFinite(Number(value)) ? `${value}ms` : "--";
  if (name === "asr_ms") metrics.asr = text;
  if (name === "llm_first_token_ms") metrics.llm = text;
  if (name === "tts_first_audio_ms") metrics.tts = text;
}

function statusLabel(label: string) {
  return { loading: "加载中", ready: "就绪", running: "运行中", warming: "预热中" }[label] || label || "待机";
}

function resetMetrics(status = "待机") {
  metrics.status = status;
  metrics.trace = "--";
  metrics.vad = "--";
  metrics.asr = "--";
  metrics.llm = "--";
  metrics.tts = "--";
}

function transcriptKey(messages: ChatMessage[]) {
  return messages
    .map((message, index) => {
      const content = message.content || "";
      return `${message.id || index}:${message.role}:${content.length}:${content.slice(0, 24)}:${content.slice(-24)}:${message.attachments?.length || 0}`;
    })
    .join("|");
}

function releaseAfterPlayback() {
  if (releaseTimer.value) window.clearTimeout(releaseTimer.value);
  const remaining = audio.audioCtx ? Math.max(0, audio.playheadTime - audio.audioCtx.currentTime) : 0;
  releaseTimer.value = window.setTimeout(() => {
    busy.value = false;
    assistantActive.value = false;
    metrics.status = micActive.value ? "监听中" : "待机";
  }, remaining * 1000 + 140);
}

function sendMessage(text: string, attachments: ChatAttachment[] = []) {
  if ((!text.trim() && !attachments.length) || socket.value?.readyState !== WebSocket.OPEN) return;
  if (busy.value && assistantActive.value) interruptAssistant("text");
  busy.value = true;
  metrics.status = "发送";
  if (attachments.length) socket.value.send(JSON.stringify({ type: "message", text, attachments }));
  else socket.value.send(JSON.stringify({ type: "text", text }));
}

function sendText() {
  const text = draft.value.trim();
  const attachments = [...pendingAttachments.value];
  if (!text && !attachments.length) return;
  sendMessage(text, attachments);
  draft.value = "";
  pendingAttachments.value = [];
}

function interruptAssistant(reason = "manual") {
  if (socket.value?.readyState !== WebSocket.OPEN) return;
  stopAssistantAudio(audio);
  busy.value = false;
  assistantActive.value = false;
  metrics.status = "打断";
  socket.value.send(JSON.stringify({ type: "interrupt", reason }));
}

async function toggleMic() {
  if (micActive.value) {
    micActive.value = false;
    stopMic(audio);
    metrics.status = "待机";
    return;
  }
  if (!connected.value) return;
  await startMic(
    audio,
    (samples) => {
      if (busy.value) return;
      appendMicSamples(audio, samples, (chunk) => {
        if (socket.value?.readyState === WebSocket.OPEN) socket.value.send(chunk);
      });
    },
    (nextLevel) => {
      level.value = nextLevel;
    },
  );
  micActive.value = true;
  metrics.status = "监听中";
}

function toggleTts() {
  ttsEnabled.value = !ttsEnabled.value;
  sendRuntimeSettings();
}

async function handleImageSelected(event: Event) {
  const input = event.target as HTMLInputElement;
  const files = Array.from(input.files || []).filter((file) => file.type.startsWith("image/")).slice(0, 4);
  try {
    for (const file of files) {
      const dataUrl = await fileToDataUrl(file);
      const result = await uploadChatImage(dataUrl);
      pendingAttachments.value.push({ ...result.asset, name: file.name });
    }
    pendingAttachments.value = pendingAttachments.value.slice(-4);
  } finally {
    input.value = "";
  }
}

function removeAttachment(assetId?: string) {
  pendingAttachments.value = pendingAttachments.value.filter((item) => item.asset_id !== assetId);
}

function displayName(role: string, message: ChatMessage) {
  if (message.source || message.attachments?.some((item) => item.type === "image")) return role === "user" ? "我" : app.config?.web_assistant_name || "枝语";
  return role === "user" ? app.config?.web_user_name || "我" : app.config?.web_assistant_name || "枝语";
}
</script>

<template>
  <main class="page-view">
    <div class="voice-console">
      <aside class="sidebar">
      <div class="conversation-top">
        <button class="conversation-action-row" type="button" @click="newConversation">
          <MessageSquarePlus :size="16" /> 新建对话
        </button>
        <label class="conversation-search-row">
          <Search :size="16" />
          <input v-model="conversations.query" placeholder="搜索聊天" @input="conversations.reloadList(true)" />
        </label>
        <div class="conversation-tabs">
          <button type="button" :class="{ active: activeScope === 'recent' }" @click="switchScope('recent')">最近</button>
          <button type="button" :class="{ active: activeScope === 'weixin' }" @click="switchScope('weixin')">微信聊天</button>
        </div>
        <button class="conversation-action-row subtle" type="button">
          <Archive :size="16" /> 归档
        </button>
      </div>

      <div class="conversation-rail">
        <div class="rail-head">
          <p class="eyebrow rail-label">{{ activeScope === "weixin" ? "微信聊天" : "最近" }}</p>
          <span>{{ visibleConversations.length }}</span>
        </div>

        <section class="conversation-list">
          <article
            v-for="item in visibleConversations"
            :key="item.id"
            class="conversation-item"
            :class="{ active: conversations.active?.id === item.id }"
          >
            <button class="conversation-open" type="button" @click="openConversation(item.id)">
              <strong>{{ item.title || (isWeixinConversation(item) ? "微信聊天" : "新的对话") }}</strong>
              <span>{{ item.summary || item.last_message || "空会话" }}</span>
              <small>{{ item.favorite ? "★ " : "" }}{{ isWeixinConversation(item) ? "微信 · " : "" }}{{ item.updated_at?.slice(0, 16) || "--" }}</small>
            </button>
            <div class="conversation-actions">
              <button class="conversation-icon" type="button" title="收藏" @click.stop="toggleFavorite(item)"><Star :size="14" /></button>
              <button class="conversation-icon" type="button" title="导出" @click.stop="exportConversation(item)"><Download :size="14" /></button>
              <button class="conversation-icon" type="button" title="归档" @click.stop="archiveConversation(item)"><Archive :size="14" /></button>
              <button class="conversation-icon danger" type="button" title="删除" @click.stop="removeConversation(item)"><Trash2 :size="14" /></button>
            </div>
          </article>
          <p v-if="!visibleConversations.length" class="conversation-empty">
            {{ activeScope === "weixin" ? "还没有微信聊天。先在微信里发一条消息。" : "还没有保存的对话。发送第一条消息后会出现在这里。" }}
          </p>
        </section>
      </div>

      <section class="sidebar-scope">
        <div class="scope-header"><span>{{ metrics.status }}</span><span>{{ Math.round(level * 100) }}%</span></div>
        <div class="level-track"><span class="level-bar" :style="{ width: `${Math.round(level * 100)}%` }"></span></div>
      </section>

      <section class="pipeline-compact">
        <div class="pipeline-row" :class="{ active: metrics.status === '收音' }"><span class="pdot"></span><strong>VAD</strong><small>{{ metrics.vad }}</small></div>
        <div class="pipeline-row"><span class="pdot"></span><strong>ASR</strong><small>{{ metrics.asr }}</small></div>
        <div class="pipeline-row"><span class="pdot"></span><strong>LLM</strong><small>{{ metrics.llm }}</small></div>
        <div class="pipeline-row"><span class="pdot"></span><strong>TTS</strong><small>{{ metrics.tts }}</small></div>
      </section>

      <section class="runtime-chips sidebar-chips">
        <span><b>ASR</b><strong>{{ metrics.asr }}</strong></span>
        <span><b>LLM</b><strong>{{ metrics.llm }}</strong></span>
        <span><b>TTS</b><strong>{{ metrics.tts }}</strong></span>
        <span><b>TRACE</b><strong>{{ metrics.trace }}</strong></span>
      </section>
    </aside>

    <section class="chat-area">
      <div v-if="!hasMessages" class="chat-welcome">
        <div class="welcome-brand">
          <span class="welcome-icon">BW</span>
          <h1>有什么我可以帮你的？</h1>
          <p>打开麦克风开始语音对话，或输入文字发送消息</p>
        </div>
      </div>

      <div v-show="hasMessages" class="chat-messages">
        <div ref="scroller" class="transcript">
          <article v-for="(message, index) in liveMessages" :key="message.id || `${index}-${message.role}`" class="message-row" :class="message.role">
            <div class="message-avatar">{{ message.role === "user" ? "我" : "枝" }}</div>
            <div class="message-body">
              <small class="message-name">{{ displayName(message.role, message) }}</small>
              <div v-if="message.content" class="message" :class="message.role">{{ message.content }}</div>
              <div v-if="message.attachments?.length" class="message-attachments">
                <template v-for="attachment in message.attachments" :key="attachment.asset_id || attachment.url">
                  <figure v-if="attachment.type === 'image'" class="message-image">
                    <img :src="attachment.url" :alt="attachment.summary || attachment.name || '图片'" />
                    <figcaption v-if="attachment.summary">{{ attachment.summary }}</figcaption>
                  </figure>
                  <div v-else class="message-sticker">
                    <img :src="attachment.url" :alt="attachment.tag || attachment.name || '表情包'" />
                  </div>
                </template>
              </div>
            </div>
          </article>
        </div>
      </div>

      <footer class="chat-composer">
        <div v-if="pendingAttachments.length" class="attachment-preview-strip">
          <div v-for="item in pendingAttachments" :key="item.asset_id || item.url" class="attachment-preview-chip">
            <img :src="item.url" :alt="item.name || '图片'" />
            <span>{{ item.name || "图片" }}</span>
            <button type="button" @click="removeAttachment(item.asset_id)">×</button>
          </div>
        </div>
        <div class="composer">
          <button class="icon-button attach-image-btn" type="button" title="添加图片" @click="imageInput?.click()"><ImagePlus :size="18" /></button>
          <button class="icon-button" :class="{ active: micActive }" type="button" title="语音输入" @click="toggleMic">
            <component :is="micActive ? MicOff : Mic" :size="18" />
          </button>
          <textarea v-model="draft" :disabled="!connected" placeholder="有问题，尽管问" rows="1" @keydown.enter.exact.prevent="sendText"></textarea>
          <button class="icon-button" type="button" :title="ttsEnabled ? '语音开启' : '语音关闭'" @click="toggleTts">
            <component :is="ttsEnabled ? Volume2 : VolumeX" :size="18" />
          </button>
          <button class="icon-button" type="button" title="打断" @click="interruptAssistant('manual')"><Square :size="16" /></button>
          <button class="primary-action send-button" type="button" :disabled="!connected || (!draft.trim() && !pendingAttachments.length)" @click="sendText">
            <Send :size="16" /> 发送
          </button>
        </div>
        <input ref="imageInput" type="file" accept="image/*" multiple hidden @change="handleImageSelected" />
      </footer>
    </section>
    </div>
  </main>
</template>
