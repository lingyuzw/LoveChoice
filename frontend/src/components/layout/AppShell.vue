<script setup lang="ts">
import { Activity, Bot, Brain, ClipboardCheck, Library, MessagesSquare, Settings2 } from "@lucide/vue";
import { onMounted, watchEffect } from "vue";
import { RouterLink, RouterView, useRoute } from "vue-router";
import { useAppStore } from "@/stores/app";

const app = useAppStore();
const route = useRoute();

const navItems = [
  { to: "/", label: "对话", icon: MessagesSquare },
  { to: "/services", label: "服务", icon: Activity },
  { to: "/integrations", label: "接入", icon: Bot },
  { to: "/diagnostics", label: "检测", icon: ClipboardCheck },
  { to: "/memory", label: "记忆", icon: Brain },
  { to: "/assets", label: "素材库", icon: Library },
  { to: "/settings", label: "配置", icon: Settings2 },
];

onMounted(() => {
  void app.bootstrap();
});

watchEffect(() => {
  const page = route.path === "/" ? "dashboard" : route.path.replace(/^\//, "") || "dashboard";
  document.body.dataset.page = page;
});
</script>

<template>
  <div class="app-shell">
    <header class="topbar">
      <RouterLink class="brand" to="/">
        <span class="brand-mark">BW</span>
        <span>
          <strong>BranchWhisper</strong>
          <small>Local Voice AI</small>
        </span>
      </RouterLink>

      <nav class="nav-tabs" aria-label="主导航">
        <RouterLink v-for="item in navItems" :key="item.to" :to="item.to" active-class="active">
          <component :is="item.icon" :size="16" />
          {{ item.label }}
        </RouterLink>
      </nav>

      <div class="topbar-status">
        <span class="status-pill" :class="{ danger: app.error }">
          {{ app.error ? "连接异常" : app.loading ? "加载中" : "待机" }}
        </span>
      </div>
    </header>

    <RouterView />
  </div>
</template>
