<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import { Activity, Clipboard, ClipboardCheck, Copy, MessageCircle, Network, Play, RotateCw, SearchCheck, Sparkles, Wrench } from "@lucide/vue";
import { loadConfig } from "@/api/config";
import { loadDiagnosticsSummary } from "@/api/diagnostics";
import { useAppStore } from "@/stores/app";
import { PROVIDER_LABELS, useToolsStore } from "@/stores/tools";
import { useAssetsStore } from "@/stores/assets";
import { useEngagementStore } from "@/stores/engagement";
import { useIntegrationsStore } from "@/stores/integrations";
import { useMemoryStore } from "@/stores/memory";

type CheckStatus = "idle" | "running" | "passed" | "warning" | "failed";

interface DiagnosticResult {
  status: CheckStatus;
  message: string;
  detail?: string;
  rawLog?: string;
  durationMs?: number;
  updatedAt?: string;
}

interface DiagnosticCheck {
  id: string;
  group: string;
  title: string;
  detail: string;
  action: () => Promise<string | void>;
}

const app = useAppStore();
const tools = useToolsStore();
const assets = useAssetsStore();
const engagement = useEngagementStore();
const integrations = useIntegrationsStore();
const memory = useMemoryStore();
const runningAll = ref(false);
const reportMessage = ref("");
const results = reactive<Record<string, DiagnosticResult>>({});

const providerChecks = computed<DiagnosticCheck[]>(() =>
  tools.providers.map((provider) => ({
    id: `tool-${provider.key}`,
    group: "联网工具",
    title: `${provider.label} Provider`,
    detail: provider.config?.enabled === false ? "当前 Provider 已关闭，检测会返回关闭状态。" : "调用后端工具测试接口，验证配置、密钥和返回格式。",
    action: async () => {
      if (provider.config?.enabled === false) {
        results[`tool-${provider.key}`] = { status: "warning", message: "Provider 已关闭", updatedAt: nowText() };
        return;
      }
      await tools.runProviderTest(provider.key);
      const text = tools.testResults[provider.key] || "";
      if (text.startsWith("测试失败")) throw new Error(text);
      return text;
    },
  })),
);

const checks = computed<DiagnosticCheck[]>(() => [
  {
    id: "runtime-summary",
    group: "基础环境",
    title: "运行时总览",
    detail: "读取后端诊断摘要，确认 runtime、前端构建、命令依赖和基础数据链路。",
    action: async () => {
      const summary = await loadDiagnosticsSummary();
      if (summary.issues.length) {
        results["runtime-summary"] = { status: "warning", message: summary.issues.join("\n"), updatedAt: nowText() };
      }
      return JSON.stringify(summary, null, 2);
    },
  },
  {
    id: "backend-config",
    group: "基础环境",
    title: "后端 API 与配置读取",
    detail: "读取 /api/config，确认前端可以访问后端。",
    action: async () => {
      const config = await loadConfig();
      return `配置读取成功，当前模式：${config.dialog_mode || "local"}`;
    },
  },
  {
    id: "services-list",
    group: "基础环境",
    title: "服务清单",
    detail: "刷新服务状态，确认运行时服务管理接口可用。",
    action: async () => {
      await app.bootstrap();
      return `读取到 ${app.services.length || 0} 个服务。`;
    },
  },
  {
    id: "tool-route",
    group: "联网工具",
    title: "工具路由解析",
    detail: `用“${tools.resolveText}”测试工具路由是否能解析到合适 Provider。`,
    action: async () => {
      if (!tools.resolveText.trim()) throw new Error("请先填写工具路由句子。");
      await tools.runResolve();
      if (!tools.resolveResult) throw new Error("没有返回解析结果");
      return JSON.stringify(tools.resolveResult, null, 2);
    },
  },
  ...providerChecks.value,
  {
    id: "asset-list",
    group: "素材能力",
    title: "素材库读取",
    detail: "读取素材列表和素材配置，确认素材库接口可用。",
    action: async () => {
      await Promise.all([assets.reload(), assets.loadConfig()]);
      return `读取到 ${assets.stickers.length || 0} 张素材，发送策略：${assets.config.stickers_enabled ? "启用" : "关闭"}`;
    },
  },
  {
    id: "asset-policy",
    group: "素材能力",
    title: "表情策略命中",
    detail: `用“${assets.testText}”跑表情策略，验证是否能选出可发送素材。`,
    action: async () => {
      if (!assets.testText.trim()) throw new Error("请先填写素材策略文本。");
      await assets.runTest();
      return JSON.stringify(assets.testResult || {}, null, 2);
    },
  },
  {
    id: "asset-vision",
    group: "素材能力",
    title: "单张素材 Vision 自检",
    detail: "对当前选中素材执行识别服务自检。",
    action: async () => {
      if (!assets.selectedId) throw new Error("素材库没有可用素材，请先上传或选择一张素材。");
      await assets.runVisionTest(assets.selectedId);
      if (!assets.visionTestResult) throw new Error(assets.detailMessage || "没有返回识别结果");
      return JSON.stringify(assets.visionTestResult, null, 2);
    },
  },
  {
    id: "integration-state",
    group: "接入链路",
    title: "接入环境",
    detail: "刷新接入实例与 openclaw 环境信息。",
    action: async () => {
      await integrations.reload(true);
      return `${integrations.summary}，环境：${integrations.environmentReady ? "ready" : "not ready"}`;
    },
  },
  {
    id: "integration-dialog",
    group: "接入链路",
    title: "微信文本链路",
    detail: `向当前接入实例发送文本：“${integrations.testText}”。`,
    action: async () => {
      if (!integrations.selected?.id) throw new Error("没有可用接入实例。");
      if (!integrations.testText.trim()) throw new Error("请先填写微信文本。");
      await integrations.runDialogTest();
      return integrations.testResult || "已发送文本测试。";
    },
  },
  {
    id: "integration-voice",
    group: "接入链路",
    title: "微信语音链路",
    detail: `执行 TTS 合成并发送：“${integrations.voiceText}”。`,
    action: async () => {
      if (!integrations.selected?.id) throw new Error("没有可用接入实例。");
      if (!integrations.voiceText.trim()) throw new Error("请先填写微信语音文本。");
      await integrations.runVoiceTest();
      return integrations.voiceResult || "已发送语音测试。";
    },
  },
  {
    id: "integration-sticker",
    group: "接入链路",
    title: "微信表情链路",
    detail: `按表情策略发送：“${integrations.stickerText}”。`,
    action: async () => {
      if (!integrations.selected?.id) throw new Error("没有可用接入实例。");
      if (!integrations.stickerText.trim()) throw new Error("请先填写微信表情文本。");
      await integrations.runStickerTest();
      return integrations.stickerResult || "已发送表情测试。";
    },
  },
  {
    id: "memory-admission",
    group: "记忆系统",
    title: "记忆准入测试",
    detail: `检查“${memory.admissionText}”是否会进入记忆。`,
    action: async () => {
      if (!memory.admissionText.trim()) throw new Error("请先填写记忆准入文本。");
      await memory.testAdmission();
      if (!memory.admissionResults.length) throw new Error("没有返回准入结果");
      return JSON.stringify(memory.admissionResults, null, 2);
    },
  },
  {
    id: "proactive-config",
    group: "主动性",
    title: "主动性配置",
    detail: "读取主动消息配置、提醒和近期事件。",
    action: async () => {
      await engagement.reload();
      return `主动性：${engagement.config.enabled ? "启用" : "关闭"}，待触发提醒 ${engagement.pendingReminders.length} 条。`;
    },
  },
  {
    id: "proactive-message",
    group: "主动性",
    title: "主动消息测试",
    detail: "按当前主动性通道发送一条测试主动消息。",
    action: async () => {
      await engagement.runTest();
      return `主动消息测试已提交，最近事件 ${engagement.recentEvents.length} 条。`;
    },
  },
]);

const groups = computed(() => {
  const map = new Map<string, DiagnosticCheck[]>();
  for (const check of checks.value) {
    if (!map.has(check.group)) map.set(check.group, []);
    map.get(check.group)?.push(check);
  }
  return Array.from(map.entries()).map(([name, items]) => ({ name, items }));
});

const totals = computed(() => {
  const values = checks.value.map((check) => resultFor(check.id).status);
  return {
    total: checks.value.length,
    passed: values.filter((status) => status === "passed").length,
    failed: values.filter((status) => status === "failed").length,
    warning: values.filter((status) => status === "warning").length,
    running: values.filter((status) => status === "running").length,
  };
});

onMounted(async () => {
  await Promise.allSettled([app.bootstrap(), tools.reload(), assets.reload(), assets.loadConfig(), integrations.reload(true), engagement.reload(), memory.reload()]);
});

function resultFor(id: string): DiagnosticResult {
  return results[id] || { status: "idle", message: "未检测" };
}

function nowText() {
  return new Date().toLocaleTimeString("zh-CN", { hour12: false });
}

function statusLabel(status: CheckStatus) {
  return { idle: "未检测", running: "检测中", passed: "通过", warning: "提示", failed: "失败" }[status];
}

function checkLog(check: DiagnosticCheck) {
  const result = resultFor(check.id);
  return [
    `[${statusLabel(result.status)}] ${check.group} / ${check.title}`,
    `说明：${check.detail}`,
    `结果：${result.message}`,
    result.durationMs ? `耗时：${result.durationMs}ms` : "",
    result.updatedAt ? `时间：${result.updatedAt}` : "",
    result.detail ? `详情：\n${result.detail}` : "",
    result.rawLog ? `原始日志：\n${result.rawLog}` : "",
  ]
    .filter(Boolean)
    .join("\n");
}

async function copyText(text: string) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const textArea = document.createElement("textarea");
    textArea.value = text;
    textArea.setAttribute("readonly", "true");
    textArea.style.position = "fixed";
    textArea.style.left = "-9999px";
    document.body.appendChild(textArea);
    textArea.select();
    const copied = document.execCommand("copy");
    document.body.removeChild(textArea);
    return copied;
  }
}

async function runCheck(check: DiagnosticCheck) {
  const start = Date.now();
  results[check.id] = { status: "running", message: "检测中...", updatedAt: nowText() };
  try {
    const detail = await check.action();
    if (resultFor(check.id).status === "warning") {
      results[check.id] = {
        ...resultFor(check.id),
        detail: detail ? String(detail) : resultFor(check.id).detail,
        rawLog: detail ? String(detail) : resultFor(check.id).rawLog,
        durationMs: Date.now() - start,
        updatedAt: nowText(),
      };
      return;
    }
    results[check.id] = {
      status: "passed",
      message: "检测通过",
      detail: detail ? String(detail) : "",
      rawLog: detail ? String(detail) : "检测通过",
      durationMs: Date.now() - start,
      updatedAt: nowText(),
    };
  } catch (error) {
    results[check.id] = {
      status: "failed",
      message: error instanceof Error ? error.message : String(error),
      rawLog: error instanceof Error ? error.stack || error.message : String(error),
      durationMs: Date.now() - start,
      updatedAt: nowText(),
    };
  }
}

async function runGroup(items: DiagnosticCheck[]) {
  for (const check of items) {
    await runCheck(check);
  }
}

async function runAll() {
  runningAll.value = true;
  try {
    for (const group of groups.value) {
      await runGroup(group.items);
    }
  } finally {
    runningAll.value = false;
  }
}

async function runFailed() {
  const failed = checks.value.filter((check) => resultFor(check.id).status === "failed");
  for (const check of failed) {
    await runCheck(check);
  }
}

async function copyReport() {
  const lines = checks.value.map((check) => checkLog(check));
  const copied = await copyText(lines.join("\n\n"));
  reportMessage.value = copied ? "检测报告已复制" : "复制失败，请手动选择日志";
  window.setTimeout(() => {
    reportMessage.value = "";
  }, 1800);
}

async function copyCheckLog(check: DiagnosticCheck) {
  const copied = await copyText(checkLog(check));
  reportMessage.value = copied ? `已复制：${check.title}` : "复制失败，请手动选择日志";
  window.setTimeout(() => {
    reportMessage.value = "";
  }, 1800);
}

function groupIcon(name: string) {
  return { 基础环境: Activity, 联网工具: Network, 素材能力: SearchCheck, 接入链路: MessageCircle, 记忆系统: Clipboard, 主动性: Sparkles }[name] || Wrench;
}
</script>

<template>
  <main class="page-view">
    <div class="diagnostics-page">
      <section class="diagnostics-hero">
        <div>
          <p class="eyebrow">Diagnostics</p>
          <h1>检测中心</h1>
          <p>把分散在配置、接入、素材库里的自检集中到这里，按链路定位问题。</p>
        </div>
        <div class="diagnostics-actions">
          <span v-if="reportMessage" class="soft-badge">{{ reportMessage }}</span>
          <button class="secondary-action" type="button" :disabled="!totals.failed" @click="runFailed"><RotateCw :size="16" />重跑失败项</button>
          <button class="secondary-action" type="button" @click="copyReport"><Copy :size="16" />复制报告</button>
          <button class="primary-action" type="button" :disabled="runningAll" @click="runAll"><Play :size="16" />一键检测</button>
        </div>
      </section>

      <section class="diagnostics-summary">
        <article><small>检测项</small><strong>{{ totals.total }}</strong></article>
        <article class="passed"><small>通过</small><strong>{{ totals.passed }}</strong></article>
        <article class="warning"><small>提示</small><strong>{{ totals.warning }}</strong></article>
        <article class="failed"><small>失败</small><strong>{{ totals.failed }}</strong></article>
        <article class="running"><small>检测中</small><strong>{{ totals.running }}</strong></article>
      </section>

      <section class="diagnostics-input-console">
        <div class="diagnostics-input-head">
          <div>
            <p class="eyebrow">Probe Inputs</p>
            <h2>检测输入</h2>
          </div>
          <small>测试文本集中在这里维护，其他页面只保留业务管理。</small>
        </div>
        <div class="diagnostics-input-grid">
          <label><span>工具路由句子</span><input v-model="tools.resolveText" /></label>
          <label><span>素材策略文本</span><input v-model="assets.testText" /></label>
          <label><span>素材渠道</span><select v-model="assets.testChannel"><option value="web">Web</option><option value="weixin">微信</option></select></label>
          <label><span>微信文本</span><input v-model="integrations.testText" /></label>
          <label><span>微信语音</span><input v-model="integrations.voiceText" /></label>
          <label><span>微信表情</span><input v-model="integrations.stickerText" /></label>
          <label class="wide"><span>记忆准入文本</span><textarea v-model="memory.admissionText" rows="3"></textarea></label>
        </div>
      </section>

      <section class="diagnostics-groups">
        <article v-for="group in groups" :key="group.name" class="diagnostics-group">
          <header class="diagnostics-group-head">
            <div>
              <component :is="groupIcon(group.name)" :size="18" />
              <strong>{{ group.name }}</strong>
              <small>{{ group.items.length }} 项</small>
            </div>
            <button class="secondary-action" type="button" @click="runGroup(group.items)"><ClipboardCheck :size="15" />检测本组</button>
          </header>

          <div class="diagnostics-list">
            <section v-for="check in group.items" :key="check.id" class="diagnostic-row" :class="resultFor(check.id).status">
              <div class="diagnostic-main">
                <span class="status-dot" :class="{ active: resultFor(check.id).status === 'passed', loading: resultFor(check.id).status === 'running', failed: resultFor(check.id).status === 'failed' }"></span>
                <div>
                  <strong>{{ check.title }}</strong>
                  <small>{{ check.detail }}</small>
                </div>
              </div>
              <div class="diagnostic-result">
                <span class="diagnostic-state">{{ statusLabel(resultFor(check.id).status) }}</span>
                <small v-if="resultFor(check.id).durationMs">{{ resultFor(check.id).durationMs }}ms · {{ resultFor(check.id).updatedAt }}</small>
                <small v-else-if="resultFor(check.id).updatedAt">{{ resultFor(check.id).updatedAt }}</small>
              </div>
              <div class="diagnostic-actions">
                <button class="secondary-action" type="button" @click="copyCheckLog(check)"><Copy :size="14" />复制日志</button>
                <button class="secondary-action" type="button" @click="runCheck(check)"><Play :size="14" />运行</button>
              </div>
              <pre v-if="resultFor(check.id).message !== '未检测' || resultFor(check.id).detail" class="diagnostic-detail">{{ resultFor(check.id).message }}<template v-if="resultFor(check.id).detail">
{{ resultFor(check.id).detail }}</template></pre>
            </section>
          </div>
        </article>
      </section>
    </div>
  </main>
</template>
