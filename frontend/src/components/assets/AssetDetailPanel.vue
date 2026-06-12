<script setup lang="ts">
import { BadgeCheck, RefreshCw, Save, Trash2 } from "@lucide/vue";
import { reactive, watch } from "vue";
import type { Sticker } from "@/api/assets";
import { useAssetsStore } from "@/stores/assets";

const props = defineProps<{
  selected: Sticker | null;
}>();

const assets = useAssetsStore();
const form = reactive({
  name: "",
  tag: "",
  emotion: "",
  review_status: "pending",
  enabled: false,
  intensity: 3,
  channels: "",
  tags: "",
  scene: "",
  avoid: "",
  caption: "",
  ocr_text: "",
  error: "",
});

watch(
  () => props.selected,
  (selected) => {
    form.name = selected?.name || "";
    form.tag = selected?.tag || "";
    form.emotion = selected?.emotion || "";
    form.review_status = selected?.review_status || "pending";
    form.enabled = Boolean(selected?.enabled);
    form.intensity = Number(selected?.intensity || 3);
    form.channels = listToText(selected?.channels);
    form.tags = listToText(selected?.tags);
    form.scene = listToText(selected?.scene);
    form.avoid = listToText(selected?.avoid);
    form.caption = selected?.caption || "";
    form.ocr_text = selected?.ocr_text || "";
    form.error = selected?.error || "";
    assets.detailMessage = "";
  },
  { immediate: true },
);

function listToText(value?: string[]) {
  return (value || []).join("\n");
}

function textToList(value: string) {
  return String(value || "")
    .split(/[\n,，、]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

async function save() {
  if (!props.selected?.id) return;
  await assets.saveSticker(props.selected.id, {
    name: form.name,
    tag: form.tag,
    emotion: form.emotion,
    review_status: form.review_status,
    enabled: form.enabled,
    intensity: Number(form.intensity || 3),
    channels: textToList(form.channels),
    tags: textToList(form.tags),
    scene: textToList(form.scene),
    avoid: textToList(form.avoid),
    caption: form.caption,
    ocr_text: form.ocr_text,
    error: form.error,
  });
}

function confidenceText(value?: number) {
  const num = Number(value);
  return Number.isFinite(num) ? `${Math.round(num * 100)}%` : "--";
}

function formatTime(value?: string) {
  if (!value) return "--";
  return value.replace("T", " ").slice(0, 16);
}
</script>

<template>
  <aside class="asset-detail-panel" :class="{ empty: !selected }">
    <template v-if="selected">
      <div class="asset-detail-head">
        <img :src="selected.url || selected.thumbnail" :alt="selected.name" />
        <div>
          <strong>{{ selected.name }}</strong>
          <small>{{ selected.emotion || selected.tag || "-" }} · {{ selected.review_status || "pending" }}</small>
        </div>
      </div>

      <div class="asset-detail-meta">
        <span><b>原文件</b>{{ selected.original_name || selected.file_stem || selected.id }}</span>
        <span><b>置信度</b>{{ confidenceText(selected.confidence) }}</span>
        <span><b>渠道</b>{{ selected.channels?.join(" / ") || "all" }}</span>
        <span><b>格式</b>{{ selected.mime || "--" }}</span>
        <span><b>使用</b>{{ selected.use_count || 0 }} 次</span>
        <span><b>更新</b>{{ formatTime(selected.updated_at || selected.created_at) }}</span>
      </div>

      <div class="asset-detail-actions">
        <button class="primary-action" type="button" @click="save"><Save :size="15" />保存</button>
        <button class="secondary-action" type="button" @click="assets.approve([selected.id])"><BadgeCheck :size="15" />通过</button>
        <button class="secondary-action" type="button" @click="assets.reanalyzeOne(selected.id)"><RefreshCw :size="15" />重识别</button>
        <button class="secondary-action danger" type="button" @click="assets.remove([selected.id])"><Trash2 :size="15" />删除</button>
      </div>

      <span v-if="assets.detailMessage" class="asset-config-message">{{ assets.detailMessage }}</span>

      <div class="asset-detail-grid">
        <label><span>名称</span><input v-model="form.name" /></label>
        <label><span>主标签</span><input v-model="form.tag" /></label>
        <label><span>分类</span><input v-model="form.emotion" /></label>
        <label><span>状态</span><select v-model="form.review_status"><option value="pending">待审核</option><option value="approved">已通过</option><option value="failed">失败</option><option value="disabled">停用</option></select></label>
        <label><span>启用发送</span><select v-model="form.enabled"><option :value="true">启用</option><option :value="false">关闭</option></select></label>
        <label><span>强度</span><input v-model.number="form.intensity" type="number" min="1" max="5" step="1" /></label>
        <label class="wide"><span>发送渠道</span><textarea v-model="form.channels" placeholder="all / web / weixin，每行一个"></textarea></label>
        <label class="wide"><span>标签</span><textarea v-model="form.tags" placeholder="每行一个标签，或用逗号分隔"></textarea></label>
        <label class="wide"><span>适用场景</span><textarea v-model="form.scene" placeholder="例如：开玩笑、打招呼、安慰"></textarea></label>
        <label class="wide"><span>避免场景</span><textarea v-model="form.avoid" placeholder="不适合发送的语境"></textarea></label>
        <label class="wide"><span>说明</span><textarea v-model="form.caption"></textarea></label>
        <label class="wide"><span>OCR 文本</span><textarea v-model="form.ocr_text"></textarea></label>
        <label class="wide"><span>错误信息</span><textarea v-model="form.error"></textarea></label>
      </div>
    </template>
    <template v-else>
      选择一张素材后，可以复核分类、标签、适用场景，并执行保存、审核、重识别和删除。
    </template>
  </aside>
</template>
