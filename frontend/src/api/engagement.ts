import { fetchJson } from "./client";

export interface Reminder {
  id: string;
  title: string;
  content?: string;
  due_at: string;
  channel?: string;
  status?: string;
  created_at?: string;
  fired_at?: string;
  last_error?: string;
}

export interface ProactiveEvent {
  id: string;
  kind?: string;
  title?: string;
  content?: string;
  channel?: string;
  status?: string;
  created_at?: string;
  fired_at?: string;
  conversation_id?: string;
  last_error?: string;
}

export type ProactiveConfig = Record<string, any>;

export async function loadReminders(status = "") {
  const params = new URLSearchParams();
  if (status) params.set("status", status);
  return fetchJson<{ reminders: Reminder[] }>(`/api/reminders${params.toString() ? `?${params}` : ""}`);
}

export async function createReminder(payload: Partial<Reminder>) {
  return fetchJson<{ reminder: Reminder; reminders: Reminder[] }>("/api/reminders", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export async function deleteReminder(id: string) {
  return fetchJson<{ ok: boolean; reminders: Reminder[] }>(`/api/reminders/${encodeURIComponent(id)}`, { method: "DELETE" });
}

export async function loadProactiveConfig() {
  return fetchJson<{ config: ProactiveConfig }>("/api/proactive/config");
}

export async function saveProactiveConfig(config: ProactiveConfig) {
  return fetchJson<{ config: ProactiveConfig }>("/api/proactive/config", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(config),
  });
}

export async function loadProactiveEvents(status = "", limit = 80) {
  const params = new URLSearchParams({ limit: String(limit) });
  if (status) params.set("status", status);
  return fetchJson<{ events: ProactiveEvent[] }>(`/api/proactive/events?${params}`);
}

export async function testProactiveMessage(content = "") {
  return fetchJson<{ event: ProactiveEvent; result: Record<string, unknown>; events: ProactiveEvent[] }>("/api/proactive/test", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content }),
  });
}

export async function dismissProactiveEvent(id: string) {
  return fetchJson<{ ok: boolean; events: ProactiveEvent[] }>(`/api/proactive/events/${encodeURIComponent(id)}/dismiss`, {
    method: "POST",
  });
}
