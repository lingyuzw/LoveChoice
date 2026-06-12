import { defineStore } from "pinia";
import {
  clearAllServiceLogs,
  clearServiceLogs,
  fetchServiceLogs,
  loadServices,
  loadSystemResources,
  startAllServices,
  startService,
  stopAllServices,
  stopService,
  updateServiceConfig,
  type ServiceSummary,
} from "@/api/services";

interface ServicesState {
  services: ServiceSummary[];
  resources: Record<string, unknown> | null;
  selectedId: string;
  logs: string;
  loading: boolean;
  logLoading: boolean;
  error: string;
  pending: Record<string, "starting" | "stopping" | "">;
  live: boolean;
  pollHandle: number | null;
  logHandle: number | null;
}

export const useServicesStore = defineStore("services", {
  state: (): ServicesState => ({
    services: [],
    resources: null,
    selectedId: "",
    logs: "",
    loading: false,
    logLoading: false,
    error: "",
    pending: {},
    live: true,
    pollHandle: null,
    logHandle: null,
  }),
  getters: {
    selected(state) {
      return state.services.find((item) => item.id === state.selectedId) || state.services[0] || null;
    },
  },
  actions: {
    async reload(quiet = false) {
      if (!quiet) this.loading = true;
      this.error = "";
      try {
        const [services, resources] = await Promise.allSettled([loadServices(), loadSystemResources()]);
        if (services.status === "fulfilled") {
          this.services = services.value.services || [];
          if (!this.selectedId || !this.services.some((item) => item.id === this.selectedId)) {
            this.selectedId = this.services[0]?.id || "";
          }
        }
        if (resources.status === "fulfilled") this.resources = resources.value;
      } catch (error) {
        this.error = error instanceof Error ? error.message : String(error);
      } finally {
        this.loading = false;
      }
    },
    async refreshLogs(quiet = false) {
      const id = this.selected?.id;
      if (!id) return;
      if (!quiet) this.logLoading = true;
      try {
        this.logs = await fetchServiceLogs(id);
      } catch (error) {
        this.logs = `日志读取失败：${error instanceof Error ? error.message : String(error)}`;
      } finally {
        this.logLoading = false;
      }
    },
    async select(id: string) {
      this.selectedId = id;
      await this.refreshLogs();
    },
    async start(id: string) {
      this.pending[id] = "starting";
      try {
        await startService(id);
        await this.trackUntilStable(id);
      } finally {
        this.pending[id] = "";
      }
    },
    async stop(id: string) {
      this.pending[id] = "stopping";
      try {
        await stopService(id);
        await this.trackUntilStable(id);
      } finally {
        this.pending[id] = "";
      }
    },
    async startAll() {
      await startAllServices();
      await this.trackUntilStable();
    },
    async stopAll() {
      await stopAllServices();
      await this.trackUntilStable();
    },
    async restartAll() {
      await stopAllServices();
      await startAllServices();
      await this.trackUntilStable();
    },
    async clearLogs() {
      const id = this.selected?.id;
      if (!id) return;
      await clearServiceLogs(id);
      await this.refreshLogs();
    },
    async clearAllLogs() {
      await clearAllServiceLogs();
      this.logs = "";
    },
    async updateConfig(service: ServiceSummary) {
      await updateServiceConfig(service.id, {
        cwd: service.cwd || "",
        health_url: service.health_url || "",
        startup_wait_sec: Number(service.startup_wait_sec || 0),
        command: service.command || "",
      });
    },
    async trackUntilStable(id = "") {
      for (let i = 0; i < 12; i += 1) {
        await new Promise((resolve) => window.setTimeout(resolve, i < 4 ? 500 : 1000));
        await this.reload(true);
        await this.refreshLogs(true);
        if (!id) continue;
        const item = this.services.find((service) => service.id === id);
        if (item && (item.running || item.status === "stopped" || item.status === "error")) break;
      }
    },
    startPolling() {
      this.stopPolling();
      this.pollHandle = window.setInterval(() => {
        void this.reload(true);
      }, 1800);
      this.logHandle = window.setInterval(() => {
        if (this.live) void this.refreshLogs(true);
      }, 1600);
    },
    stopPolling() {
      if (this.pollHandle) window.clearInterval(this.pollHandle);
      if (this.logHandle) window.clearInterval(this.logHandle);
      this.pollHandle = null;
      this.logHandle = null;
    },
  },
});
