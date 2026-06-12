<script setup lang="ts">
import { computed, onMounted } from "vue";
import { Brain, ChevronLeft, ChevronRight, RefreshCw, Search, Trash2 } from "@lucide/vue";
import { useMemoryStore } from "@/stores/memory";
import type { MemoryLayer } from "@/api/memory";

const memory = useMemoryStore();

const statCards = computed(() => [
  { label: "全部记忆", value: memory.stats.total, detail: "当前可用于上下文的记忆", layer: "" as MemoryLayer },
  { label: "短期", value: memory.stats.short, detail: "最近对话里的临时偏好", layer: "short" as MemoryLayer },
  { label: "中期", value: memory.stats.mid, detail: "重复出现的稳定信息", layer: "mid" as MemoryLayer },
  { label: "长期", value: memory.stats.long, detail: "置顶或高置信信息", layer: "long" as MemoryLayer },
]);

onMounted(() => {
  void memory.reload();
});

function percent(value: unknown) {
  const num = Number(value);
  return Number.isFinite(num) ? `${Math.round(num * 100)}%` : "--";
}

function layerLabel(layer?: string) {
  return { short: "短期", mid: "中期", long: "长期" }[layer || ""] || "未分层";
}

function formatTime(value?: string) {
  if (!value) return "--";
  return value.replace("T", " ").slice(0, 16);
}
</script>

<template>
  <main class="page-view">
    <div class="ops-page memory-page">
      <section class="page-head">
        <div>
          <p class="eyebrow">Memory Center</p>
          <h1>记忆中心</h1>
          <small>本地模型和 API 模式的记忆分开管理，点击左侧概况可以直接筛选。</small>
        </div>
        <div class="head-actions">
          <button class="secondary-action" type="button" :disabled="memory.loading" @click="memory.reload">
            <RefreshCw :size="16" /> 刷新
          </button>
        </div>
      </section>

      <section class="memory-workbench">
      <aside class="memory-side-panel">
        <div class="memory-side-head">
          <strong>记忆概况</strong>
          <small>{{ memory.mode || "当前模式" }} · {{ memory.dbPath || "runtime/memory" }}</small>
        </div>

        <div class="memory-stats-grid">
          <button
            v-for="card in statCards"
            :key="card.label"
            class="memory-stat-card"
            :class="{ active: memory.layer === card.layer }"
            type="button"
            @click="memory.setLayer(card.layer)"
          >
            <span>{{ card.label }}</span>
            <strong>{{ card.value }}</strong>
            <small>{{ card.detail }}</small>
          </button>
        </div>

        <div class="memory-filter-stack">
          <label>
            模式
            <select v-model="memory.mode" @change="memory.reload()">
              <option value="">跟随当前配置</option>
              <option value="local">本地模型</option>
              <option value="api">API 模型</option>
            </select>
          </label>
          <label>
            分层
            <select v-model="memory.layer" @change="memory.page = 1">
              <option value="">全部</option>
              <option value="short">短期</option>
              <option value="mid">中期</option>
              <option value="long">长期</option>
            </select>
          </label>
          <button class="secondary-action full" type="button" @click="memory.decay">
            <Brain :size="16" /> 衰减清理
          </button>
        </div>
      </aside>

      <section class="memory-page-panel">
        <header class="memory-toolbar">
          <label class="memory-search-box">
            <Search :size="16" />
            <input :value="memory.query" placeholder="搜索 key / 内容 / 类型" @input="memory.setQuery(($event.target as HTMLInputElement).value)" />
          </label>
          <div class="memory-add-box">
            <input v-model="memory.addText" placeholder="手动添加一条稳定记忆" @keydown.enter="memory.create" />
            <button class="primary-action" type="button" @click="memory.create">添加</button>
          </div>
        </header>

        <div class="memory-list-shell">
          <div class="memory-page-list">
            <article
              v-for="item in memory.paged"
              :key="item.id"
              class="memory-row"
              :class="`layer-${item.layer || 'short'}`"
              @click="memory.selectedId = item.id"
            >
              <div class="memory-row-body">
                <strong>{{ item.key || "记忆" }}</strong>
                <p>{{ item.value }}</p>
                <small>
                  {{ layerLabel(item.layer) }} · {{ item.count || 1 }} 次 · 置信度 {{ percent(item.confidence) }} ·
                  {{ formatTime(item.last_seen_at || item.last_changed_at || item.created_at) }}
                </small>
              </div>
              <div class="memory-row-actions">
                <button class="icon-button danger" type="button" title="删除记忆" @click.stop="memory.remove(item.id)">
                  <Trash2 :size="16" />
                </button>
              </div>
            </article>
            <p v-if="!memory.paged.length" class="conversation-empty memory-empty">没有匹配的记忆。</p>
          </div>
        </div>

        <footer class="memory-pagination">
          <button class="secondary-action" type="button" :disabled="memory.page <= 1" @click="memory.page -= 1">
            <ChevronLeft :size="16" /> 上一页
          </button>
          <span>{{ memory.filtered.length }} 条 · 第 {{ memory.page }} / {{ memory.pageCount }} 页</span>
          <button class="secondary-action" type="button" :disabled="memory.page >= memory.pageCount" @click="memory.page += 1">
            下一页 <ChevronRight :size="16" />
          </button>
          <button class="secondary-action" type="button" :disabled="memory.loading" @click="memory.reload">
            <RefreshCw :size="16" /> 刷新
          </button>
        </footer>
      </section>
      </section>
    </div>
  </main>
</template>
