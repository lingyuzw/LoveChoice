import { defineStore } from "pinia";
import { loadToolsConfig, resolveTool, saveToolsConfig, testTool, type ToolProviderConfig, type ToolResolveResult } from "@/api/tools";

export const PROVIDER_FIELDS: Record<string, string[]> = {
  weather: ["enabled", "provider", "base_url", "api_key", "default_location"],
  search: ["enabled", "provider", "base_url", "api_key", "limit"],
  news: ["enabled", "provider", "base_url", "api_key", "region", "limit"],
  finance: ["enabled", "provider", "base_url", "api_key"],
  map: ["enabled", "provider", "base_url", "api_key"],
  url_fetch: ["enabled", "user_agent", "max_chars"],
  reminder: ["enabled", "web_enabled", "weixin_enabled", "webhook_url"],
};

export const PROVIDER_LABELS: Record<string, string> = {
  weather: "天气",
  search: "搜索",
  news: "新闻",
  finance: "财经",
  map: "地图",
  url_fetch: "网页读取",
  reminder: "提醒通知",
};

export const PROVIDER_OPTIONS: Record<string, Array<[string, string]>> = {
  weather: [["gaode", "高德天气"], ["wttr", "wttr.in 免密天气"]],
  search: [["gaode", "高德地点搜索"], ["duckduckgo", "DuckDuckGo 网页搜索"]],
  news: [["google_rss", "Google News RSS"], ["search", "网页搜索兜底"]],
  finance: [["search", "网页搜索兜底"]],
  map: [["gaode", "高德地图 Web服务"]],
  url_fetch: [["built-in", "内置网页读取"]],
  reminder: [["default", "内置提醒"]],
};

interface ToolsState {
  config: ToolProviderConfig;
  loading: boolean;
  saving: boolean;
  error: string;
  resolveText: string;
  resolveResult: ToolResolveResult | null;
  testResults: Record<string, string>;
}

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value ?? {}));
}

export const useToolsStore = defineStore("tools", {
  state: (): ToolsState => ({
    config: {},
    loading: false,
    saving: false,
    error: "",
    resolveText: "漳州今天天气怎么样",
    resolveResult: null,
    testResults: {},
  }),
  getters: {
    providers(state) {
      return Object.keys(PROVIDER_FIELDS).map((key) => ({ key, label: PROVIDER_LABELS[key] || key, config: state.config[key] || {} }));
    },
  },
  actions: {
    async reload() {
      this.loading = true;
      this.error = "";
      try {
        this.config = await loadToolsConfig();
      } catch (error) {
        this.error = error instanceof Error ? error.message : String(error);
      } finally {
        this.loading = false;
      }
    },
    async save() {
      this.saving = true;
      this.error = "";
      try {
        this.config = await saveToolsConfig(clone(this.config));
      } catch (error) {
        this.error = error instanceof Error ? error.message : String(error);
        throw error;
      } finally {
        this.saving = false;
      }
    },
    setProviderField(providerKey: string, field: string, value: unknown) {
      const provider = { ...(this.config[providerKey] || {}) };
      provider[field] = value;
      this.config = { ...this.config, [providerKey]: provider };
    },
    async runResolve() {
      const text = this.resolveText.trim();
      if (!text) return;
      this.resolveResult = await resolveTool(text);
    },
    clearResolve() {
      this.resolveText = "";
      this.resolveResult = null;
    },
    async runProviderTest(providerKey: string) {
      const args: Record<string, Record<string, unknown>> = {
        weather: { location: "北京" },
        search: { query: "BranchWhisper" },
        news: { query: "AI" },
        finance: { symbol: "AAPL" },
        map: { origin: "北京站", destination: "天安门" },
        url_fetch: { url: "https://example.com" },
        reminder: { title: "测试提醒", due_at: new Date(Date.now() + 3600_000).toISOString() },
      };
      this.testResults[providerKey] = "测试中...";
      try {
        const toolByProvider: Record<string, string> = { url_fetch: "url_fetch", reminder: "reminder" };
        const result = await testTool(toolByProvider[providerKey] || providerKey, args[providerKey] || {});
        this.testResults[providerKey] = JSON.stringify(result, null, 2);
      } catch (error) {
        this.testResults[providerKey] = `测试失败：${error instanceof Error ? error.message : String(error)}`;
      }
    },
  },
});
