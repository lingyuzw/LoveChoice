import { fetchJson } from "./client";

export interface BotProfile {
  id: string;
  name?: string;
  avatar_url?: string;
  tools_enabled?: boolean;
  reply_style?: string;
  system?: string;
  [key: string]: unknown;
}

export async function loadBotProfiles() {
  return fetchJson<{ profiles: BotProfile[] }>("/api/bot-profiles");
}

export async function createBotProfile(profile: Partial<BotProfile>) {
  return fetchJson<{ profile: BotProfile; profiles: BotProfile[] }>("/api/bot-profiles", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(profile),
  });
}

export async function updateBotProfile(id: string, profile: Partial<BotProfile>) {
  return fetchJson<{ profile: BotProfile; profiles: BotProfile[] }>(`/api/bot-profiles/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(profile),
  });
}

export async function deleteBotProfile(id: string) {
  return fetchJson<{ ok: boolean; profiles: BotProfile[] }>(`/api/bot-profiles/${encodeURIComponent(id)}`, { method: "DELETE" });
}
