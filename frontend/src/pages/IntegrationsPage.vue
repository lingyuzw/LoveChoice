<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import {
  Copy,
  Download,
  Edit3,
  Eraser,
  Link2,
  PackagePlus,
  Play,
  Plus,
  QrCode,
  RefreshCw,
  RotateCw,
  Save,
  Square,
  Trash2,
  X,
} from "@lucide/vue";
import type { IntegrationItem } from "@/api/integrations";
import { useIntegrationsStore } from "@/stores/integrations";
import { useProfilesStore } from "@/stores/profiles";

const integrations = useIntegrationsStore();
const profiles = useProfilesStore();
const configOpen = ref(false);
const actionMessage = ref("");

const selected = computed(() => integrations.selected);

onMounted(async () => {
  await Promise.all([integrations.reload(), profiles.reload()]);
  integrations.startPolling();
});

onUnmounted(() => {
  integrations.stopPolling();
});

function statusClass(status?: string) {
  if (["running", "login", "logged_in"].includes(status || "")) return "active";
  if (["starting", "installing"].includes(status || "")) return "loading";
  if (status === "failed") return "failed";
  return "stopped";
}

function qrImage(session: Record<string, any> | null) {
  const content = String(session?.qrcode_img_content || "");
  if (!content) return "";
  if (content.startsWith("data:")) return content;
  return `https://api.qrserver.com/v1/create-qr-code/?size=220x220&data=${encodeURIComponent(content)}`;
}

function accounts(item: Record<string, any> | null | undefined) {
  return Array.isArray(item?.accounts) ? item.accounts : [];
}

function timings(item: Record<string, any> | null | undefined) {
  return Array.isArray(item?.recent_timings) ? item.recent_timings.slice(0, 4) : [];
}

function profileName(id?: string) {
  const profile = profiles.profiles.find((item) => item.id === (id || "default"));
  return profile?.name || id || "default";
}

function openNew() {
  integrations.fillForm(null);
  configOpen.value = true;
}

function openEdit(item: IntegrationItem) {
  integrations.fillForm(item);
  configOpen.value = true;
}

async function saveConfig() {
  await integrations.saveForm();
  configOpen.value = false;
}

function showActionMessage(message: string) {
  actionMessage.value = message;
  window.setTimeout(() => {
    if (actionMessage.value === message) actionMessage.value = "";
  }, 1800);
}

async function copyLogs() {
  if (!integrations.logs.trim()) {
    showActionMessage("没有可复制日志");
    return;
  }
  try {
    await navigator.clipboard.writeText(integrations.logs);
    showActionMessage("日志已复制");
  } catch {
    showActionMessage("复制失败");
  }
}

function downloadLogs() {
  if (!integrations.logs.trim()) {
    showActionMessage("没有可下载日志");
    return;
  }
  const timestamp = new Date().toISOString().replace(/[:.]/g, "-");
  const blob = new Blob([integrations.logs], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `${selected.value?.id || "integration"}-${timestamp}.log`;
  link.click();
  URL.revokeObjectURL(url);
  showActionMessage("日志已下载");
}
</script>

<template>
  <main class="page-view">
    <div class="ops-page integrations-page">
      <section class="page-head">
        <div>
          <p class="eyebrow">Channel Bots</p>
          <h1>接入管理</h1>
          <small>微信桥接、扫码登录、人格绑定和运行日志。当前 {{ integrations.summary }}</small>
        </div>
        <div class="head-actions">
          <button class="primary-action" type="button" @click="openNew">
            <Plus :size="16" /> 添加微信个人号
          </button>
          <button class="secondary-action" type="button" @click="integrations.reload()">
            <RefreshCw :size="16" /> 刷新
          </button>
        </div>
      </section>

      <section class="integration-shell">
        <div class="integration-list">
          <article
            v-for="item in integrations.items"
            :key="item.id"
            class="integration-card"
            :class="[statusClass(item.status), { selected: item.id === integrations.selectedId }]"
            @click="integrations.select(item.id)"
          >
            <div class="integration-card-head">
              <div class="integration-title">
                <span class="status-dot"></span>
                <strong>{{ item.chat_name || item.id }}</strong>
                <small>微信个人号 · {{ item.id }} · OpenClaw {{ item.openclaw_profile || "branchwhisper" }}</small>
              </div>
              <span class="service-badge">{{ integrations.humanStatus(item) }}</span>
            </div>

            <div class="integration-meta">
              <span class="meta-cell"><span>启用</span><strong>{{ item.enabled ? "自动守护" : "手动" }}</strong></span>
              <span class="meta-cell"><span>人格</span><strong>{{ profileName(item.bot_profile_id) }}</strong></span>
              <span class="meta-cell"><span>回复</span><strong>{{ item.reply_mode || "text" }}</strong></span>
              <span class="meta-cell"><span>账号</span><strong>{{ item.runtime?.account_count ?? item.accounts?.length ?? 0 }}</strong></span>
              <span class="meta-cell"><span>PID</span><strong>{{ item.pid || "--" }}</strong></span>
              <span class="meta-cell"><span>提示</span><strong>{{ item.last_error ? "有错误" : "--" }}</strong></span>
            </div>

            <p class="integration-state-note" :class="statusClass(item.status)">
              {{ item.last_error || item.runtime?.last_error || (item.status === "running" ? "桥接进程运行中，微信消息会进入 BranchWhisper。" : "首次使用请先扫码登录。") }}
            </p>

            <div class="integration-actions" @click.stop>
              <button class="secondary-action" type="button" @click="openEdit(item)">
                <Edit3 :size="15" /> 编辑
              </button>
              <button class="secondary-action" type="button" @click="integrations.selectedId = item.id; integrations.run('start')">
                <Play :size="15" /> 启动
              </button>
              <button class="secondary-action" type="button" @click="integrations.selectedId = item.id; integrations.run('stop')">
                <Square :size="15" /> 停止
              </button>
              <button class="secondary-action" type="button" @click="integrations.selectedId = item.id; integrations.run('restart')">
                <RefreshCw :size="15" /> 重启
              </button>
              <button class="secondary-action danger" type="button" @click="integrations.remove(item.id)">
                <Trash2 :size="15" /> 删除
              </button>
            </div>
          </article>
          <p v-if="!integrations.items.length" class="integration-empty">还没有接入实例。添加微信个人号后会显示在这里。</p>
        </div>

        <aside class="integration-side">
          <section class="integration-panel">
            <div class="panel-head">
              <div>
                <p class="eyebrow">Login & Logs</p>
                <h2>登录与日志</h2>
              </div>
              <span class="soft-badge">{{ selected?.id || "--" }}</span>
            </div>
            <div class="integration-qr">
              <img v-if="qrImage(integrations.qrSession)" class="integration-qr-image" :src="qrImage(integrations.qrSession)" alt="微信扫码二维码" />
              <div v-else class="integration-login-placeholder">
                <strong>{{ selected?.status === "running" ? "桥接运行中" : selected ? "等待扫码" : "请选择实例" }}</strong>
                <span>{{ integrations.qrSession?.message || (selected?.runtime?.manual_stop ? "实例已手动停止。" : "点击扫码登录后，这里会显示二维码。") }}</span>
              </div>
            </div>
            <div v-if="integrations.qrSession" class="integration-login-meta">
              <strong>{{ integrations.qrSession.status || "login" }}</strong>
              <span>{{ integrations.qrSession.message || integrations.qrSession.qrcode_url || "--" }}</span>
              <small v-if="integrations.qrSession.expire_at">过期时间 {{ integrations.qrSession.expire_at }}</small>
            </div>
            <div class="integration-account-list">
              <div v-for="account in accounts(selected)" :key="account.account_id || account.id" class="integration-account-item">
                <span>账号</span>
                <strong>{{ account.nickname || account.name || account.account_id || account.id }}</strong>
                <small>{{ account.account_id || account.id || "--" }}</small>
              </div>
              <div v-if="!accounts(selected).length" class="integration-account-item muted">
                <span>账号</span>
                <strong>暂无账号</strong>
                <small>扫码登录成功后显示</small>
              </div>
            </div>
            <div v-if="timings(selected).length" class="integration-timing-summary">
              <span v-for="timing in timings(selected)" :key="timing.message_id || timing.created_at || timing.total_ms">
                <b>{{ timing.total_ms || timing.branch_ms || "--" }}ms</b>
                <small>{{ timing.text || timing.message || "最近消息" }}</small>
              </span>
            </div>
            <div class="inline-actions">
              <button class="secondary-action" type="button" :disabled="!selected" @click="integrations.startQrLogin(true)">
                <QrCode :size="16" /> 扫码登录
              </button>
              <button class="secondary-action" type="button" :disabled="!selected" @click="integrations.run('install')">
                <PackagePlus :size="16" /> 安装适配器
              </button>
            </div>
            <div class="integration-bridge-row">
              <input v-model="integrations.bridgeUrl" type="text" placeholder="http://127.0.0.1:7860" />
              <button class="secondary-action" type="button" :disabled="!selected || integrations.actioning" @click="integrations.startBridge">
                <Link2 :size="16" /> 启动桥接
              </button>
            </div>
            <div class="integration-bridge-row">
              <input v-model="integrations.verifyCode" type="text" placeholder="验证码 / verify_code" @keydown.enter="integrations.pollQrLogin(false)" />
              <button class="secondary-action" type="button" :disabled="!integrations.qrSession" @click="integrations.pollQrLogin(false)">
                <RefreshCw :size="16" /> 轮询登录
              </button>
            </div>
            <div class="integration-log-toolbar">
              <select v-model="integrations.logScope" @change="integrations.refreshLogs()">
                <option value="current">本次启动</option>
                <option value="all">全部日志</option>
              </select>
              <button class="icon-button" type="button" title="刷新日志" @click="integrations.refreshLogs()"><RotateCw :size="16" /></button>
              <button class="icon-button" type="button" title="复制日志" @click="copyLogs"><Copy :size="16" /></button>
              <button class="icon-button" type="button" title="下载日志" @click="downloadLogs"><Download :size="16" /></button>
              <button class="icon-button danger" type="button" title="清空日志" @click="integrations.clearLogs()"><Eraser :size="16" /></button>
            </div>
            <span v-if="actionMessage" class="soft-badge integration-action-message">{{ actionMessage }}</span>
            <div class="log-viewer integration-log" role="log" aria-live="polite">{{ integrations.logs || "暂无日志。" }}</div>
          </section>

        </aside>
      </section>

      <div v-if="configOpen" class="modal-overlay" @click.self="configOpen = false">
        <section class="modal-panel integration-modal-panel" role="dialog" aria-modal="true" aria-label="接入实例配置">
          <div class="modal-head">
            <div>
              <p class="eyebrow">Instance Config</p>
              <h2>{{ integrations.editingId ? "编辑实例" : "新增实例" }}</h2>
            </div>
            <button class="icon-button modal-close" type="button" title="关闭" @click="configOpen = false"><X :size="16" /></button>
          </div>
          <div class="modal-body">
            <div class="form-grid compact">
              <label><span>实例 ID</span><input v-model="integrations.form.id" :disabled="!!integrations.editingId" /></label>
              <label><span>微信聊天名</span><input v-model="integrations.form.chat_name" /></label>
              <label><span>OpenClaw profile</span><input v-model="integrations.form.openclaw_profile" /></label>
              <label><span>Bot 人格</span><select v-model="integrations.form.bot_profile_id"><option v-for="profile in profiles.profiles" :key="profile.id" :value="profile.id">{{ profile.name || profile.id }}</option></select></label>
              <label><span>回复模式</span><select v-model="integrations.form.reply_mode"><option value="text">文字默认</option><option value="voice">语音优先</option></select></label>
              <label class="switch-label"><span>启用后台守护</span><input v-model="integrations.form.enabled" type="checkbox" /></label>
              <label class="wide"><span>语音触发词</span><textarea v-model="integrations.form.voice_trigger_keywords" rows="5"></textarea></label>
            </div>
            <p v-if="integrations.error" class="asset-error">{{ integrations.error }}</p>
          </div>
          <div class="modal-actions">
            <button class="secondary-action" type="button" @click="configOpen = false">取消</button>
            <button class="primary-action" type="button" @click="saveConfig"><Save :size="16" /> 保存</button>
          </div>
        </section>
      </div>
    </div>
  </main>
</template>
