<script setup lang="ts">
import { onMounted, onUnmounted } from "vue";
import { Power, RefreshCcw, RefreshCw, Square, Trash2 } from "@lucide/vue";
import ResourceSection from "@/components/services/ResourceSection.vue";
import ServiceCard from "@/components/services/ServiceCard.vue";
import ServiceLogsPanel from "@/components/services/ServiceLogsPanel.vue";
import { useServicesStore } from "@/stores/services";

const services = useServicesStore();

onMounted(async () => {
  await services.reload();
  await services.refreshLogs(true);
  services.startPolling();
});

onUnmounted(() => {
  services.stopPolling();
});
</script>

<template>
  <main class="page-view">
    <div class="ops-page services-page">
      <section class="page-head">
        <div><p class="eyebrow">Service Orchestration</p><h1>服务编排</h1></div>
        <div class="head-actions">
          <button class="primary-action" type="button" @click="services.startAll()"><Power :size="16" /> 一键启动</button>
          <button class="secondary-action" type="button" @click="services.stopAll()"><Square :size="16" /> 停止全部</button>
          <button class="secondary-action" type="button" @click="services.restartAll()"><RefreshCcw :size="16" /> 重启全部</button>
          <button class="secondary-action" type="button" @click="services.clearAllLogs()"><Trash2 :size="16" /> 清空日志</button>
          <button class="icon-button" type="button" title="刷新" @click="services.reload()"><RefreshCw :size="16" /></button>
        </div>
      </section>

      <ResourceSection :resources="services.resources" />

      <div class="service-list">
        <ServiceCard
          v-for="service in services.services"
          :key="service.id"
          :service="service"
          :pending="services.pending[service.id]"
          @select="services.select"
          @start="services.start"
          @stop="services.stop"
        />
      </div>

      <ServiceLogsPanel
        :services="services.services"
        :selected-id="services.selectedId"
        :logs="services.logs"
        :live="services.live"
        @select="services.select"
        @refresh="services.refreshLogs"
        @clear="services.clearLogs"
        @clear-all="services.clearAllLogs"
        @update:live="services.live = $event"
      />
    </div>
  </main>
</template>
