import { defineStore } from "pinia";
import {
  createReminder,
  deleteReminder,
  dismissProactiveEvent,
  loadProactiveConfig,
  loadProactiveEvents,
  loadReminders,
  saveProactiveConfig,
  testProactiveMessage,
  type ProactiveConfig,
  type ProactiveEvent,
  type Reminder,
} from "@/api/engagement";

interface EngagementState {
  config: ProactiveConfig;
  reminders: Reminder[];
  events: ProactiveEvent[];
  loading: boolean;
  saving: boolean;
  error: string;
  reminderTitle: string;
  reminderDueAt: string;
  reminderChannel: string;
}

function defaultConfig(): ProactiveConfig {
  return {
    enabled: false,
    daily_limit: 3,
    tone: "warm",
    followup_level: "restrained",
    ask_followup_enabled: false,
    channels: { web: true, weixin: false },
    quiet_hours_enabled: true,
    quiet_start: "23:00",
    quiet_end: "08:00",
    greetings: {
      enabled: false,
      good_morning: { enabled: false, window_start: "07:00", window_end: "09:30", with_weather: true, with_reminders: true, message: "" },
      noon: { enabled: false, window_start: "12:00", window_end: "13:30", with_weather: false, with_reminders: true, message: "" },
      good_night: { enabled: false, window_start: "22:00", window_end: "23:20", with_weather: false, with_reminders: false, message: "" },
      long_absence: { enabled: false, after_hours: 48 },
    },
    triggers: {
      reminders: true,
      service_alerts: true,
      weather: true,
      news_watch: false,
      emotion_care: false,
      long_goal_followup: false,
    },
  };
}

function mergeConfig(config: ProactiveConfig): ProactiveConfig {
  const base = defaultConfig();
  return {
    ...base,
    ...(config || {}),
    channels: { ...(base.channels || {}), ...((config || {}).channels || {}) },
    greetings: {
      ...(base.greetings || {}),
      ...((config || {}).greetings || {}),
      good_morning: {
        ...(base.greetings.good_morning || {}),
        ...(((config || {}).greetings || {}).good_morning || {}),
      },
      noon: {
        ...(base.greetings.noon || {}),
        ...(((config || {}).greetings || {}).noon || {}),
      },
      good_night: {
        ...(base.greetings.good_night || {}),
        ...(((config || {}).greetings || {}).good_night || {}),
      },
      long_absence: {
        ...(base.greetings.long_absence || {}),
        ...(((config || {}).greetings || {}).long_absence || {}),
      },
    },
    triggers: {
      ...(base.triggers || {}),
      ...((config || {}).triggers || {}),
    },
  };
}

export const useEngagementStore = defineStore("engagement", {
  state: (): EngagementState => ({
    config: defaultConfig(),
    reminders: [],
    events: [],
    loading: false,
    saving: false,
    error: "",
    reminderTitle: "",
    reminderDueAt: "",
    reminderChannel: "web",
  }),
  getters: {
    pendingReminders(state) {
      return state.reminders.filter((item) => (item.status || "pending") === "pending");
    },
    recentEvents(state) {
      return state.events.slice(0, 8);
    },
  },
  actions: {
    async reload() {
      this.loading = true;
      this.error = "";
      try {
        const [config, reminders, events] = await Promise.all([loadProactiveConfig(), loadReminders(), loadProactiveEvents()]);
        this.config = mergeConfig(config.config);
        this.reminders = reminders.reminders || [];
        this.events = events.events || [];
      } catch (error) {
        this.error = error instanceof Error ? error.message : String(error);
      } finally {
        this.loading = false;
      }
    },
    async save() {
      this.saving = true;
      try {
        this.config = mergeConfig((await saveProactiveConfig(this.config)).config);
      } finally {
        this.saving = false;
      }
    },
    async createReminder() {
      const title = this.reminderTitle.trim();
      const dueAt = this.reminderDueAt.trim();
      if (!title || !dueAt) return;
      const data = await createReminder({ title, content: title, due_at: dueAt, channel: this.reminderChannel || "web" });
      this.reminders = data.reminders || [];
      this.reminderTitle = "";
      this.reminderDueAt = "";
    },
    async removeReminder(id: string) {
      const data = await deleteReminder(id);
      this.reminders = data.reminders || [];
    },
    async dismissEvent(id: string) {
      const data = await dismissProactiveEvent(id);
      this.events = data.events || [];
    },
    async runTest() {
      const data = await testProactiveMessage("这是一条主动消息测试。它会按当前主动性通道发送；如果启用了微信，会发到已绑定的微信会话。");
      this.events = data.events || this.events;
    },
  },
});
