import { defineStore } from "pinia";
import { loadConfig, saveConfig, type PublicConfig } from "@/api/config";
import { loadServices, type ServiceSummary } from "@/api/services";

interface AppState {
  config: PublicConfig | null;
  services: ServiceSummary[];
  loading: boolean;
  error: string;
}

function applyUiPreferences(config: PublicConfig | null) {
  const scale = Number(config?.ui_font_scale || 1);
  document.documentElement.style.setProperty("--ui-font-scale", String(Number.isFinite(scale) ? scale : 1));
  document.documentElement.classList.toggle("theme-light", window.localStorage.getItem("branchwhisper:theme") === "light");
}

export const useAppStore = defineStore("app", {
  state: (): AppState => ({
    config: null,
    services: [],
    loading: false,
    error: "",
  }),
  actions: {
    async bootstrap() {
      this.loading = true;
      this.error = "";
      try {
        const [config, services] = await Promise.all([loadConfig(), loadServices()]);
        this.config = config;
        this.services = services.services || [];
        applyUiPreferences(config);
      } catch (error) {
        this.error = error instanceof Error ? error.message : String(error);
      } finally {
        this.loading = false;
      }
    },
    async saveConfig(patch: Partial<PublicConfig>) {
      this.loading = true;
      this.error = "";
      try {
        this.config = await saveConfig(patch);
        applyUiPreferences(this.config);
        window.dispatchEvent(new CustomEvent("branchwhisper:config-updated", { detail: { config: this.config } }));
      } catch (error) {
        this.error = error instanceof Error ? error.message : String(error);
        throw error;
      } finally {
        this.loading = false;
      }
    },
  },
});
