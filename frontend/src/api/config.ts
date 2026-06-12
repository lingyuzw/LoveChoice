import { fetchJson } from "./client";

export interface PublicConfig {
  asr_mode?: string;
  asr_url?: string;
  asr_model?: string;
  asr_timeout?: number;
  asr_max_tokens?: number;
  dialog_mode: "local" | "api";
  llm_url: string;
  llm_model: string;
  temperature?: number;
  max_tokens?: number;
  history_turns?: number;
  api_llm_url: string;
  api_llm_model: string;
  api_llm_api_key?: string;
  api_llm_api_key_set?: boolean;
  api_llm_api_key_masked?: string;
  api_temperature?: number;
  api_max_tokens?: number;
  api_history_turns?: number;
  thinking_enabled?: boolean;
  system?: string;
  tts_enabled?: boolean;
  tts_url?: string;
  tts_speed?: number;
  tts_seed?: number;
  tts_volume?: number;
  tts_fade_ms?: number;
  tts_sample_rate?: number;
  vision_enabled?: boolean;
  vision_url?: string;
  vision_model?: string;
  vision_timeout?: number;
  vision_max_image_mb?: number;
  vision_memory_extract_enabled?: boolean;
  sticker_vision_enabled?: boolean;
  sticker_vision_url?: string;
  sticker_vision_model?: string;
  sticker_vision_api_key?: string;
  sticker_vision_api_key_set?: boolean;
  sticker_vision_api_key_masked?: string;
  sticker_vision_timeout?: number;
  sticker_vision_max_tokens?: number;
  stickers_enabled?: boolean;
  sticker_activity?: "off" | "low" | "standard" | "active" | "very_active" | "custom";
  sticker_cooldown_sec?: number;
  sticker_daily_limit?: number;
  sticker_max_streak?: number;
  sticker_custom_probability?: number;
  tools_enabled?: boolean;
  tools_auto_call?: boolean;
  tools_timeout?: number;
  tools_max_result_chars?: number;
  context_compaction_enabled?: boolean;
  context_window_tokens?: number;
  context_compaction_ratio?: number;
  context_keep_recent_turns?: number;
  context_summary_max_chars?: number;
  context_summary_max_layers?: number;
  web_user_name: string;
  web_user_avatar_url?: string;
  web_assistant_name: string;
  web_assistant_avatar_url?: string;
  ui_font_scale: number;
  vad_threshold?: number;
  vad_min_silence_ms?: number;
  vad_speech_pad_ms?: number;
  pre_speech_ms?: number;
  min_utterance_ms?: number;
  max_utterance_sec?: number;
  [key: string]: unknown;
}

export interface ModelFileEntry {
  name: string;
  path: string;
  size?: number;
  modified_at?: string;
}

export interface ModelFilesResponse {
  root: string;
  parent?: string;
  directories: ModelFileEntry[];
  files: ModelFileEntry[];
}

export async function loadConfig(): Promise<PublicConfig> {
  return fetchJson<PublicConfig>("/api/config");
}

export async function saveConfig(patch: Partial<PublicConfig>): Promise<PublicConfig> {
  return fetchJson<PublicConfig>("/api/config", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
}

export async function loadToolConfig() {
  return fetchJson<{ tools: Record<string, unknown> }>("/api/config/tools");
}

export async function saveToolConfig(patch: Record<string, unknown>) {
  return fetchJson<{ tools: Record<string, unknown> }>("/api/config/tools", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
}

export async function listModelFiles(root = "", query = "") {
  const params = new URLSearchParams();
  if (root.trim()) params.set("root", root.trim());
  if (query.trim()) params.set("query", query.trim());
  return fetchJson<ModelFilesResponse>(`/api/files/models${params.toString() ? `?${params}` : ""}`);
}
