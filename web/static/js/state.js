/* ============================================================
   state.js — Global application state & default constants
   BranchWhisper
   ============================================================ */

export const ACTIVE_CONVERSATION_KEY = "branchwhisper.activeConversationId";
const LEGACY_ACTIVE_CONVERSATION_KEY = "lovechoice.activeConversationId";
export const PIPELINE_STEPS = ["vad", "asr", "llm", "tts"];

function readActiveConversationId() {
  const current = localStorage.getItem(ACTIVE_CONVERSATION_KEY);
  if (current !== null) return current;
  const legacy = localStorage.getItem(LEGACY_ACTIVE_CONVERSATION_KEY);
  if (legacy) {
    localStorage.setItem(ACTIVE_CONVERSATION_KEY, legacy);
    return legacy;
  }
  return "";
}

/* ---- default config (used as fallback when backend is unavailable) ---- */

export const DEFAULT_CONFIG = {
  asr_mode: "transcription",
  asr_url: "http://127.0.0.1:8001/v1/audio/transcriptions",
  asr_model: "qwen3-asr",
  llm_url: "http://127.0.0.1:8080/v1/chat/completions",
  llm_model: "qwen3.5-9b",
  temperature: 0.35,
  max_tokens: 220,
  history_turns: 8,
  system: "",
  ui_font_scale: 1,
  web_user_name: "我",
  web_user_avatar_url: "",
  web_assistant_name: "枝语",
  web_assistant_avatar_url: "",
  memory_enabled: true,
  memory_extract_enabled: true,
  memory_admission_enabled: true,
  memory_min_importance: 0.55,
  context_compaction_enabled: true,
  context_window_tokens: 8192,
  context_compaction_ratio: 0.7,
  context_keep_recent_turns: 10,
  context_summary_max_chars: 1200,
  context_summary_max_layers: 3,
  vision_enabled: true,
  vision_url: "http://127.0.0.1:8081/v1/chat/completions",
  vision_model: "qwen-vl",
  vision_timeout: 45,
  vision_max_image_mb: 8,
  vision_memory_extract_enabled: false,
  stickers_enabled: true,
  sticker_activity: "active",
  sticker_cooldown_sec: 90,
  sticker_daily_limit: 60,
  sticker_max_streak: 2,
  sticker_custom_probability: 0.65,
  memory_short_to_mid_days: 60,
  memory_short_to_mid_count: 3,
  memory_mid_to_long_days: 180,
  memory_mid_to_long_count: 5,
  memory_short_delete_days: 180,
  memory_mid_downgrade_days: 180,
  memory_long_downgrade_days: 365,
  memory_max_context_items: 12,
  tools_enabled: true,
  tools_auto_call: true,
  tools_timeout: 12,
  tools_max_result_chars: 4000,
  tts_url: "http://127.0.0.1:50000/tts",
  tts_sample_rate: 24000,
  tts_speed: 1.08,
  tts_seed: 42,
  tts_volume: 0.88,
  tts_fade_ms: 5,
  tts_enabled: true,
  vad_threshold: 0.5,
  vad_min_silence_ms: 350,
  vad_speech_pad_ms: 120,
  pre_speech_ms: 250,
  min_utterance_ms: 250,
  max_utterance_sec: 15,
};

/* ---- default service definitions (fallback) ---- */

export const DEFAULT_SERVICES = [
  {
    id: "asr",
    label: "Qwen3-ASR vLLM",
    description: "qwen-asr-serve speech recognition endpoint.",
    cwd: "",
    command: "",
    health_url: "http://127.0.0.1:8001/health",
    startup_wait_sec: 25,
    running: false,
  },
  {
    id: "llm",
    label: "llama.cpp Qwen3.5",
    description: "OpenAI-compatible llama.cpp chat server.",
    cwd: "",
    command: "",
    health_url: "http://127.0.0.1:8080/health",
    startup_wait_sec: 10,
    running: false,
  },
  {
    id: "tts",
    label: "CosyVoice3 TTS",
    description: "Trained CosyVoice3 streaming PCM API.",
    cwd: "",
    command: "",
    health_url: "http://127.0.0.1:50000/health",
    startup_wait_sec: 0,
    running: false,
  },
];

/* ---- global application state ---- */

export const state = {
  /* connection */
  previewMode: false,
  connected: false,
  ws: null,
  reconnectTimer: 0,
  manualSocketClose: false,

  /* config */
  currentConfig: { ...DEFAULT_CONFIG },
  services: DEFAULT_SERVICES.map((s) => ({ ...s })),
  conversations: [],
  activeConversationId: readActiveConversationId(),
  activeConversation: null,

  /* audio */
  audioCtx: null,
  playerGain: null,
  playheadTime: 0,
  playbackSources: new Set(),
  micActive: false,
  micStream: null,
  micSource: null,
  micProcessor: null,
  silentGain: null,
  micPending: new Float32Array(0),
  ttsSampleRate: 24000,
  ttsEnabled: true,

  /* dialogue state */
  busy: false,
  assistantActive: false,
  interrupting: false,
  dropAudioUntilNextAssistant: false,
  bargeInFrames: 0,
  lastInterruptAt: 0,
  currentAssistant: null,
  currentTraceId: "",
  pendingAttachments: [],

  /* visualization */
  levels: Array.from({ length: 110 }, () => 0),
  latestLevel: 0,
  releaseTimer: 0,

  /* services page */
  selectedLogService: "asr",
  servicePollTimer: 0,
  systemResources: null,

  /* integrations page */
  integrations: [],
  integrationEnv: null,
  selectedIntegrationId: "weixin_personal",
  integrationPollTimer: 0,
  integrationLoginPollTimer: 0,
  integrationLoginSession: null,

  /* settings page */
  memories: [],
  botProfiles: [],
  toolConfig: {},
  reminders: [],
  proactiveConfig: {},
  proactiveEvents: [],
  modelFileSearchTimer: 0,
  conversationFilter: "",
  conversationArchivedMode: "active",
  conversationScope: "recent",
  memoryPage: 1,
  memoryPageSize: 30,
};
