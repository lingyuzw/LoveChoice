/* ============================================================
   audio.js — Audio capture, PCM playback, level metering
   BranchWhisper
   ============================================================ */

import { state } from "./state.js";
import { $, setText, createIcon, renderIcons } from "./utils.js";

/* ---- AudioContext lifecycle ---- */

export async function ensureAudioContext() {
  if (!state.audioCtx) {
    state.audioCtx = new AudioContext();
    state.playerGain = state.audioCtx.createGain();
    state.playerGain.gain.value = 1;
    state.playerGain.connect(state.audioCtx.destination);
    state.playheadTime = state.audioCtx.currentTime;
  }
  if (state.audioCtx.state === "suspended") await state.audioCtx.resume();
}

/* ---- microphone capture ---- */

export async function startMic({ onSendSamples }) {
  await ensureAudioContext();

  state.micStream = await navigator.mediaDevices.getUserMedia({
    audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: false },
  });

  state.micSource = state.audioCtx.createMediaStreamSource(state.micStream);
  state.micProcessor = state.audioCtx.createScriptProcessor(1024, 1, 1);
  state.silentGain = state.audioCtx.createGain();
  state.silentGain.gain.value = 0;

  state.micProcessor.onaudioprocess = (event) => {
    const input = event.inputBuffer.getChannelData(0);
    updateLevel(input);
    if (!state.micActive || state.ws?.readyState !== WebSocket.OPEN) return;
    const samples = downsample(input, state.audioCtx.sampleRate, 16000);
    onSendSamples(samples);
  };

  state.micSource.connect(state.micProcessor);
  state.micProcessor.connect(state.silentGain);
  state.silentGain.connect(state.audioCtx.destination);

  state.micActive = true;
  // update both mic buttons (welcome + bottom composer)
  document.querySelectorAll("#micBtn, #micBtnWelcome").forEach((btn) => {
    btn.classList.add("active");
    btn.replaceChildren(createIcon("mic-off"));
  });
  renderIcons();
}

export function stopMic() {
  state.micActive = false;
  state.micPending = new Float32Array(0);
  state.micProcessor?.disconnect();
  state.micSource?.disconnect();
  state.silentGain?.disconnect();
  for (const track of state.micStream?.getTracks() || []) track.stop();
  state.micStream = null;
  state.micSource = null;
  state.micProcessor = null;
  state.silentGain = null;
  document.querySelectorAll("#micBtn, #micBtnWelcome").forEach((btn) => {
    btn.classList.remove("active");
    btn.replaceChildren(createIcon("mic"));
  });
  renderIcons();
}

/* ---- sample rate conversion ---- */

function downsample(input, inputRate, outputRate) {
  if (inputRate === outputRate) return new Float32Array(input);
  const ratio = inputRate / outputRate;
  const newLength = Math.floor(input.length / ratio);
  const result = new Float32Array(newLength);
  let offsetInput = 0;
  for (let i = 0; i < newLength; i += 1) {
    const nextOffset = Math.round((i + 1) * ratio);
    let sum = 0;
    let count = 0;
    for (let j = offsetInput; j < nextOffset && j < input.length; j += 1) {
      sum += input[j];
      count += 1;
    }
    result[i] = count ? sum / count : 0;
    offsetInput = nextOffset;
  }
  return result;
}

/* ---- buffered mic send ---- */

function appendFloat32(left, right) {
  const merged = new Float32Array(left.length + right.length);
  merged.set(left, 0);
  merged.set(right, left.length);
  return merged;
}

export function sendMicSamples(samples) {
  state.micPending = appendFloat32(state.micPending, samples);
  while (state.micPending.length >= 512) {
    const chunk = state.micPending.slice(0, 512);
    state.micPending = state.micPending.slice(512);
    state.ws.send(chunk.buffer);
  }
}

/* ---- level metering ---- */

function updateLevel(input) {
  let sum = 0;
  for (let i = 0; i < input.length; i += 4) sum += input[i] * input[i];
  const rms = Math.sqrt(sum / Math.max(1, input.length / 4));
  state.latestLevel = Math.min(1, rms * 8);
  const level = Math.round(state.latestLevel * 100);
  const bar = $("#levelBar");
  if (bar) bar.style.width = `${level}%`;
  setText("levelText", `${level}%`);
}

/* ---- barge-in detection ---- */

export function shouldTriggerBargeIn() {
  const now = performance.now();
  if (state.interrupting || now - state.lastInterruptAt < 900) return false;
  if (!state.audioCtx) return false;
  const playbackPending = state.playheadTime > state.audioCtx.currentTime + 0.08;
  if (!playbackPending && !state.assistantActive) return false;

  if (state.latestLevel >= 0.28) state.bargeInFrames += 1;
  else state.bargeInFrames = Math.max(0, state.bargeInFrames - 1);
  return state.bargeInFrames >= 3;
}

/* ---- PCM16 playback ---- */

export async function schedulePcm16(arrayBuffer) {
  await ensureAudioContext();
  if (state.dropAudioUntilNextAssistant) return;
  if (!arrayBuffer.byteLength || arrayBuffer.byteLength < 2) return;
  const view = new DataView(arrayBuffer);
  const sampleCount = Math.floor(view.byteLength / 2);
  const sampleRate = Number(state.ttsSampleRate) || 24000;
  const samples = new Float32Array(sampleCount);
  for (let i = 0; i < sampleCount; i += 1) samples[i] = view.getInt16(i * 2, true) / 32768;

  // gentle fade-in/out to reduce clicks
  const fadeSamples = Math.min(Math.floor(sampleRate * 0.004), Math.floor(sampleCount / 4));
  for (let i = 0; i < fadeSamples; i += 1) {
    const gain = (i + 1) / fadeSamples;
    samples[i] *= gain;
    samples[sampleCount - 1 - i] *= gain;
  }

  const buffer = state.audioCtx.createBuffer(1, sampleCount, sampleRate);
  buffer.copyToChannel(samples, 0);
  const source = state.audioCtx.createBufferSource();
  source.buffer = buffer;
  source.connect(state.playerGain);

  const startAt = Math.max(state.audioCtx.currentTime + 0.06, state.playheadTime || 0);
  state.playbackSources.add(source);
  source.onended = () => state.playbackSources.delete(source);
  source.start(startAt);
  state.playheadTime = startAt + buffer.duration;
}

export function stopAssistantAudio() {
  window.clearTimeout(state.releaseTimer);
  for (const source of state.playbackSources) {
    try { source.stop(); } catch { /* already ended */ }
  }
  state.playbackSources.clear();
  if (state.audioCtx) state.playheadTime = state.audioCtx.currentTime;
}

export function releaseAfterPlayback({ onReleased }) {
  window.clearTimeout(state.releaseTimer);
  const remaining = state.audioCtx ? Math.max(0, state.playheadTime - state.audioCtx.currentTime) : 0;
  state.releaseTimer = window.setTimeout(() => {
    state.busy = false;
    state.assistantActive = false;
    state.interrupting = false;
    state.dropAudioUntilNextAssistant = false;
    if (onReleased) onReleased();
  }, remaining * 1000 + 140);
}
