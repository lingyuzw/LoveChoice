/* ============================================================
   state.js — Global application state & default constants
   LoveChoice Voice Console
   ============================================================ */

export const ACTIVE_CONVERSATION_KEY = "lovechoice.activeConversationId";
export const PIPELINE_STEPS = ["vad", "asr", "llm", "tts"];

/* ---- default config (used as fallback when backend is unavailable) ---- */

export const DEFAULT_CONFIG = {
  asr_mode: "transcription",
  asr_url: "http://127.0.0.1:8001/v1/audio/transcriptions",
  asr_model: "qwen3-asr",
  llm_url: "http://127.0.0.1:8080/v1/chat/completions",
  llm_model: "qwen3.5-9b",
  llm_api_key: "",
  temperature: 0.35,
  max_tokens: 220,
  history_turns: 8,
  system: "",
  ui_font_scale: 1,
  memory_enabled: true,
  memory_extract_enabled: true,
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
  activeConversationId: localStorage.getItem(ACTIVE_CONVERSATION_KEY) || "",
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

  /* visualization */
  levels: Array.from({ length: 110 }, () => 0),
  latestLevel: 0,
  releaseTimer: 0,

  /* services page */
  selectedLogService: "asr",
  servicePollTimer: 0,
  systemResources: null,

  /* settings page */
  memories: [],
};
