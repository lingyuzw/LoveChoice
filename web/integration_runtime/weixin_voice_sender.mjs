#!/usr/bin/env node

import crypto from "node:crypto";
import { execFile } from "node:child_process";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);

const DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com";
const DEFAULT_CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c";
const ILINK_APP_ID = "bot";
const OPENCLAW_WEIXIN_VERSION = "2.4.4";
const MESSAGE_TYPE_BOT = 2;
const MESSAGE_STATE_FINISH = 2;
const ITEM_IMAGE = 2;
const ITEM_VOICE = 3;
const UPLOAD_MEDIA_TYPE_IMAGE = 1;
const UPLOAD_MEDIA_TYPE_VOICE = 4;
const VOICE_ENCODE_SILK = 6;
const WEIXIN_VOICE_SAMPLE_RATE = 24_000;
const WEIXIN_VOICE_GAIN_DB = 8;
const MAX_VOICE_SECONDS = 60;
const MAX_IMAGE_BYTES = 8 * 1024 * 1024;
const MAX_THUMB_BYTES = 256 * 1024;

let silkModulePromise = null;

function npmExecutable() {
  return process.platform === "win32" ? "npm.cmd" : "npm";
}

function usage() {
  return [
    "Usage:",
    "  node weixin_voice_sender.mjs --base-url URL --token TOKEN --to USER_ID --voice-file FILE [--context-token TOKEN] [--text TEXT]",
    "  node weixin_voice_sender.mjs --base-url URL --token TOKEN --to USER_ID --image-file FILE [--context-token TOKEN]",
    "  node weixin_voice_sender.mjs --download-media --cdn-base-url URL --encrypt-query-param PARAM --aes-key KEY --output-file FILE",
    "",
    "Options:",
    "  --self-test   Check node crypto, ffmpeg, and ffprobe only.",
  ].join("\n");
}

function parseArgs(argv) {
  const args = {};
  for (let i = 0; i < argv.length; i += 1) {
    const item = argv[i];
    if (!item.startsWith("--")) continue;
    const key = item.slice(2).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
    if (key === "help" || key === "selfTest" || key === "downloadMedia") {
      args[key] = true;
    } else {
      args[key] = argv[i + 1] || "";
      i += 1;
    }
  }
  return args;
}

function fail(message, extra = {}) {
  process.stdout.write(JSON.stringify({ ok: false, error: String(message), stage: extra.stage || "unknown", ...extra }));
  process.exit(1);
}

function buildClientVersion(version) {
  const parts = String(version || "").split(".").slice(0, 3).map((part) => Number.parseInt(part, 10) || 0);
  while (parts.length < 3) parts.push(0);
  return ((parts[0] & 0xff) << 16) | ((parts[1] & 0xff) << 8) | (parts[2] & 0xff);
}

function buildBaseInfo() {
  return { channel_version: "branchwhisper-bridge", bot_agent: "BranchWhisper/1.0 (openclaw-weixin)" };
}

function buildHeaders(token = "") {
  const uin = Buffer.from(String(Math.floor(Math.random() * 0xffffffff))).toString("base64");
  const headers = {
    "Content-Type": "application/json",
    AuthorizationType: "ilink_bot_token",
    "X-WECHAT-UIN": uin,
    "iLink-App-Id": ILINK_APP_ID,
    "iLink-App-ClientVersion": String(buildClientVersion(OPENCLAW_WEIXIN_VERSION)),
  };
  if (token.trim()) headers.Authorization = `Bearer ${token.trim()}`;
  return headers;
}

function endpoint(baseUrl, apiPath) {
  return `${String(baseUrl || DEFAULT_BASE_URL).replace(/\/+$/, "")}/${apiPath.replace(/^\/+/, "")}`;
}

async function postJson({ baseUrl, apiPath, token, body, timeoutMs = 15_000 }) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(endpoint(baseUrl, apiPath), {
      method: "POST",
      headers: buildHeaders(token),
      body: JSON.stringify(body),
      signal: controller.signal,
    });
    const text = await response.text();
    if (!response.ok) {
      throw new Error(`${apiPath} HTTP ${response.status}: ${text.slice(0, 300)}`);
    }
    return text ? JSON.parse(text) : {};
  } finally {
    clearTimeout(timer);
  }
}

function aesEcbPaddedSize(plaintextSize) {
  return Math.ceil((plaintextSize + 1) / 16) * 16;
}

function encryptAesEcb(plaintext, key) {
  const cipher = crypto.createCipheriv("aes-128-ecb", key, null);
  return Buffer.concat([cipher.update(plaintext), cipher.final()]);
}

function decryptAesEcb(ciphertext, key) {
  const decipher = crypto.createDecipheriv("aes-128-ecb", key, null);
  return Buffer.concat([decipher.update(ciphertext), decipher.final()]);
}

function parseAesKey(value) {
  const raw = String(value || "").trim();
  if (!raw) throw new Error("missing aes key");
  if (/^[0-9a-fA-F]{32}$/.test(raw)) return Buffer.from(raw, "hex");
  const decoded = Buffer.from(raw, "base64");
  if (decoded.length === 16) return decoded;
  throw new Error("invalid aes key length");
}

function buildCdnUploadUrl({ cdnBaseUrl, uploadParam, filekey }) {
  const base = String(cdnBaseUrl || DEFAULT_CDN_BASE_URL).replace(/\/+$/, "");
  return `${base}/upload?encrypted_query_param=${encodeURIComponent(String(uploadParam || ""))}&filekey=${encodeURIComponent(filekey)}`;
}

function buildCdnDownloadUrl({ cdnBaseUrl, downloadParam }) {
  const param = String(downloadParam || "").trim();
  if (!param) throw new Error("missing encrypted query param");
  if (/^https?:\/\//i.test(param)) return param;
  const base = String(cdnBaseUrl || DEFAULT_CDN_BASE_URL).replace(/\/+$/, "");
  return `${base}/download?encrypted_query_param=${encodeURIComponent(param)}`;
}

function firstValue(...values) {
  for (const value of values) {
    if (typeof value === "string" && value.trim()) return value.trim();
    if (value && typeof value === "object") return value;
  }
  return "";
}

function compactResponseShape(value, depth = 0) {
  if (!value || typeof value !== "object") return typeof value;
  if (depth > 1) return Array.isArray(value) ? `array(${value.length})` : "object";
  const result = {};
  for (const [key, item] of Object.entries(value).slice(0, 24)) {
    if (/token|key|param|aes|authorization/i.test(key)) {
      result[key] = item ? `[${typeof item}]` : "";
    } else if (item && typeof item === "object") {
      result[key] = compactResponseShape(item, depth + 1);
    } else {
      result[key] = typeof item;
    }
  }
  return result;
}

async function uploadBufferToCdn({ buffer, uploadFullUrl, uploadParam, filekey, cdnBaseUrl, aeskey, label = "media" }) {
  const ciphertext = encryptAesEcb(buffer, aeskey);
  const urls = [];
  if (uploadFullUrl?.trim()) urls.push(uploadFullUrl.trim());
  if (uploadParam) urls.push(buildCdnUploadUrl({ cdnBaseUrl, uploadParam, filekey }));
  if (!urls.length) throw new Error("CDN upload missing upload URL");
  let lastError = null;
  for (const url of [...new Set(urls)]) {
    const methods = url.includes("/upload?") ? ["POST", "PUT"] : ["PUT", "POST"];
    for (const method of methods) {
      for (let attempt = 1; attempt <= 2; attempt += 1) {
        try {
          const response = await fetch(url, {
            method,
            headers: { "Content-Type": "application/octet-stream" },
            body: new Uint8Array(ciphertext),
          });
          if (response.status !== 200) {
            const body = await response.text().catch(() => "");
            const error = new Error(`${label} CDN ${method} HTTP ${response.status}: ${body.slice(0, 180)}`);
            error.status = response.status;
            error.urlKind = url.includes("/upload?") ? "param" : "full";
            throw error;
          }
          const downloadParam = response.headers.get("x-encrypted-param") || "";
          if (!downloadParam) throw new Error(`CDN ${method} response missing x-encrypted-param`);
          return {
            downloadParam,
            ciphertextSize: ciphertext.length,
            uploadMethod: method,
            uploadUrlKind: url.includes("/upload?") ? "param" : "full",
          };
        } catch (error) {
          lastError = error;
          if (attempt === 2 || error.status === 404 || error.status === 405) break;
        }
      }
    }
  }
  throw lastError || new Error("CDN upload failed");
}

async function downloadMedia(args) {
  const encryptQueryParam = String(args.encryptQueryParam || args.encryptedQueryParam || "").trim();
  const outputFile = String(args.outputFile || "").trim();
  if (!encryptQueryParam) throw new Error("missing --encrypt-query-param");
  if (!outputFile) throw new Error("missing --output-file");
  const aeskey = parseAesKey(args.aesKey || args.aeskey || "");
  const url = buildCdnDownloadUrl({ cdnBaseUrl: args.cdnBaseUrl || DEFAULT_CDN_BASE_URL, downloadParam: encryptQueryParam });
  const started = Date.now();
  const response = await fetch(url, { method: "GET" });
  if (response.status !== 200) {
    const body = await response.text().catch(() => "");
    const error = new Error(`CDN GET HTTP ${response.status}: ${body.slice(0, 180)}`);
    error.stage = "cdn_download";
    throw error;
  }
  const ciphertext = Buffer.from(await response.arrayBuffer());
  if (!ciphertext.length) throw new Error("downloaded media is empty");
  const plaintext = decryptAesEcb(ciphertext, aeskey);
  await fs.mkdir(path.dirname(outputFile), { recursive: true });
  await fs.writeFile(outputFile, plaintext);
  return {
    ok: true,
    stage: "downloaded",
    output_file: outputFile,
    ciphertext_size: ciphertext.length,
    plaintext_size: plaintext.length,
    download_url_kind: /^https?:\/\//i.test(encryptQueryParam) ? "full" : "param",
    download_ms: Date.now() - started,
  };
}

async function loadSilkWasm() {
  if (!silkModulePromise) {
    silkModulePromise = import("silk-wasm").catch(async (firstError) => {
      let lastError = firstError;
      const candidates = [];
      for (const entry of String(process.env.NODE_PATH || "").split(path.delimiter)) {
        if (entry.trim()) candidates.push(entry.trim());
      }
      const npmPrefix = process.env.npm_config_prefix || process.env.NPM_CONFIG_PREFIX;
      if (npmPrefix) candidates.push(path.join(npmPrefix, "node_modules"));
      if (process.execPath) {
        const nodeBin = path.dirname(process.execPath);
        candidates.push(path.resolve(nodeBin, "..", "lib", "node_modules"));
        candidates.push(path.resolve(nodeBin, "..", "node_modules"));
      }
      if (process.env.APPDATA) candidates.push(path.join(process.env.APPDATA, "npm", "node_modules"));
      if (process.env.LOCALAPPDATA) candidates.push(path.join(process.env.LOCALAPPDATA, "npm", "node_modules"));
      if (process.env.ProgramFiles) candidates.push(path.join(process.env.ProgramFiles, "nodejs", "node_modules"));
      try {
        const { stdout } = await execFileAsync(npmExecutable(), ["root", "-g"]);
        const globalRoot = stdout.trim();
        if (globalRoot) candidates.push(globalRoot);
      } catch {
        // npm may be missing from PATH even when node is available; keep the import error as the primary hint.
      }
      try {
        const { stdout } = await execFileAsync(npmExecutable(), ["config", "get", "prefix"]);
        const prefix = stdout.trim();
        if (prefix && prefix !== "undefined" && prefix !== "null") {
          candidates.push(path.join(prefix, "node_modules"));
        }
      } catch {
        // Same as above: package import failure is more actionable than a secondary npm lookup failure.
      }
      for (const root of [...new Set(candidates)]) {
        const modulePath = path.join(root, "silk-wasm", "lib", "index.mjs");
        try {
          await fs.access(modulePath);
          return await import(pathToFileURL(modulePath).href);
        } catch (error) {
          if (error?.code !== "ENOENT") lastError = error;
        }
      }
      const error = new Error(`silk-wasm is not available: ${(lastError || firstError).message}. Install it with: npm install -g silk-wasm`);
      error.stage = "silk_import";
      throw error;
    });
  }
  return silkModulePromise;
}

async function transcodeToPcm(inputPath) {
  const outputPath = path.join(os.tmpdir(), `branchwhisper-weixin-voice-${Date.now()}-${crypto.randomBytes(4).toString("hex")}.pcm`);
  await execFileAsync("ffmpeg", [
    "-hide_banner",
    "-loglevel",
    "error",
    "-y",
    "-i",
    inputPath,
    "-vn",
    "-sn",
    "-dn",
    "-t",
    String(MAX_VOICE_SECONDS),
    "-af",
    `aresample=${WEIXIN_VOICE_SAMPLE_RATE}:async=1:first_pts=0,volume=${WEIXIN_VOICE_GAIN_DB}dB`,
    "-ar",
    String(WEIXIN_VOICE_SAMPLE_RATE),
    "-ac",
    "1",
    "-f",
    "s16le",
    outputPath,
  ]);
  return outputPath;
}

async function probeAudioStats(filePath) {
  const [probe, volume] = await Promise.all([
    execFileAsync("ffprobe", [
      "-v",
      "error",
      "-show_entries",
      "stream=codec_name,sample_rate,channels:format=duration",
      "-of",
      "json",
      filePath,
    ]).then(({ stdout }) => JSON.parse(stdout || "{}")).catch(() => ({})),
    execFileAsync("ffmpeg", [
      "-hide_banner",
      "-nostats",
      "-i",
      filePath,
      "-af",
      "volumedetect",
      "-f",
      "null",
      "-",
    ]).then(({ stderr }) => parseVolumeDetect(stderr)).catch(() => ({})),
  ]);
  const stream = Array.isArray(probe.streams) ? probe.streams[0] || {} : {};
  return {
    codec: stream.codec_name || "",
    sample_rate: Number(stream.sample_rate || 0) || 0,
    channels: Number(stream.channels || 0) || 0,
    duration_ms: Math.round((Number(probe.format?.duration || 0) || 0) * 1000),
    ...volume,
  };
}

async function probePcmStats(filePath, sampleRate) {
  const { size } = await fs.stat(filePath);
  const durationMs = size > 0 ? Math.round((size / 2 / sampleRate) * 1000) : 0;
  const volume = await execFileAsync("ffmpeg", [
    "-hide_banner",
    "-nostats",
    "-f",
    "s16le",
    "-ar",
    String(sampleRate),
    "-ac",
    "1",
    "-i",
    filePath,
    "-af",
    "volumedetect",
    "-f",
    "null",
    "-",
  ]).then(({ stderr }) => parseVolumeDetect(stderr)).catch(() => ({}));
  return {
    codec: "pcm_s16le",
    sample_rate: sampleRate,
    channels: 1,
    duration_ms: durationMs,
    ...volume,
  };
}

function parseVolumeDetect(text) {
  const mean = String(text || "").match(/mean_volume:\s*(-?[\d.]+)\s*dB/i);
  const max = String(text || "").match(/max_volume:\s*(-?[\d.]+)\s*dB/i);
  return {
    mean_volume_db: mean ? Number(mean[1]) : null,
    max_volume_db: max ? Number(max[1]) : null,
  };
}

async function encodePcmToSilk(pcmPath) {
  const { encode, getDuration } = await loadSilkWasm();
  if (typeof encode !== "function") {
    const error = new Error("silk-wasm does not export encode()");
    error.stage = "silk_encode";
    throw error;
  }
  const pcm = await fs.readFile(pcmPath);
  const encoded = await encode(pcm, WEIXIN_VOICE_SAMPLE_RATE).catch((error) => {
    error.stage = "silk_encode";
    throw error;
  });
  const data = Buffer.from(encoded?.data?.buffer || encoded?.data || [], encoded?.data?.byteOffset || 0, encoded?.data?.byteLength || 0);
  if (!data.length) {
    const error = new Error("silk-wasm returned empty encoded data");
    error.stage = "silk_encode";
    throw error;
  }
  let durationMs = Number(encoded?.duration || 0);
  if ((!Number.isFinite(durationMs) || durationMs <= 0) && typeof getDuration === "function") {
    durationMs = Number(getDuration(data) || 0);
  }
  if (!Number.isFinite(durationMs) || durationMs <= 0) {
    durationMs = Math.max(1, Math.round((pcm.length / 2 / WEIXIN_VOICE_SAMPLE_RATE) * 1000));
  }
  return { data, durationMs: Math.round(durationMs) };
}

function makeSelfTestPcm() {
  const samples = Math.floor(WEIXIN_VOICE_SAMPLE_RATE / 10);
  const buffer = Buffer.alloc(samples * 2);
  for (let i = 0; i < samples; i += 1) {
    const value = Math.round(Math.sin((2 * Math.PI * 440 * i) / WEIXIN_VOICE_SAMPLE_RATE) * 12000);
    buffer.writeInt16LE(value, i * 2);
  }
  return buffer;
}

async function selfTest() {
  const key = Buffer.alloc(16);
  encryptAesEcb(Buffer.from("ok"), key);
  await execFileAsync("ffmpeg", ["-hide_banner", "-version"]);
  await execFileAsync("ffprobe", ["-hide_banner", "-version"]);
  let silkWasm = false;
  let silkError = "";
  try {
    const { encode } = await loadSilkWasm();
    const encoded = await encode(makeSelfTestPcm(), WEIXIN_VOICE_SAMPLE_RATE);
    silkWasm = Boolean(encoded?.data?.byteLength || encoded?.data?.length);
  } catch (error) {
    silkError = error?.message || String(error);
  }
  return {
    ok: true,
    ffmpeg: true,
    ffprobe: true,
    aes_128_ecb: true,
    silk_wasm: silkWasm,
    silk_error: silkError,
    voice_format: "silk",
    encode_type: VOICE_ENCODE_SILK,
    sample_rate: WEIXIN_VOICE_SAMPLE_RATE,
  };
}

async function sendVoice(args) {
  const baseUrl = args.baseUrl || DEFAULT_BASE_URL;
  const cdnBaseUrl = args.cdnBaseUrl || DEFAULT_CDN_BASE_URL;
  const token = String(args.token || "");
  const to = String(args.to || "");
  const voiceFile = String(args.voiceFile || "");
  const contextToken = String(args.contextToken || "");
  const text = String(args.text || "");
  if (!token) throw new Error("missing --token");
  if (!to) throw new Error("missing --to");
  if (!voiceFile) throw new Error("missing --voice-file");
  await fs.access(voiceFile);

  const started = Date.now();
  let pcmPath = "";
  try {
    const sourceStats = await probeAudioStats(voiceFile);
    pcmPath = await transcodeToPcm(voiceFile).catch((error) => {
      error.stage = "transcode";
      throw error;
    });
    const pcmStats = await probePcmStats(pcmPath, WEIXIN_VOICE_SAMPLE_RATE);
    const silk = await encodePcmToSilk(pcmPath);
    const playtimeMs = silk.durationMs;
    const transcodeStats = {
      ...pcmStats,
      codec: "silk",
      duration_ms: playtimeMs,
    };
    const plaintext = silk.data;
    const rawsize = plaintext.length;
    const rawfilemd5 = crypto.createHash("md5").update(plaintext).digest("hex");
    const filesize = aesEcbPaddedSize(rawsize);
    const filekey = crypto.randomBytes(16).toString("hex");
    const aeskey = crypto.randomBytes(16);
    const uploadStart = Date.now();
    const uploadUrlResp = await postJson({
      baseUrl,
      apiPath: "ilink/bot/getuploadurl",
      token,
      body: {
        filekey,
        media_type: UPLOAD_MEDIA_TYPE_VOICE,
        to_user_id: to,
        rawsize,
        rawfilemd5,
        filesize,
        no_need_thumb: true,
        aeskey: aeskey.toString("hex"),
      },
    }).catch((error) => {
      error.stage = "getuploadurl";
      throw error;
    });
    const uploadFullUrl = String(uploadUrlResp.upload_full_url || "").trim();
    const uploadParam = uploadUrlResp.upload_param;
    if (!uploadFullUrl && !uploadParam) throw new Error("getuploadurl returned no upload URL");
    const upload = await uploadBufferToCdn({
      buffer: plaintext,
      uploadFullUrl,
      uploadParam,
      filekey,
      cdnBaseUrl,
      aeskey,
    }).catch((error) => {
      error.stage = "cdn_upload";
      throw error;
    });
    const uploadMs = Date.now() - uploadStart;
    const sendStart = Date.now();
    const clientId = `branchwhisper-voice-${Date.now()}-${crypto.randomBytes(4).toString("hex")}`;
    await postJson({
      baseUrl,
      apiPath: "ilink/bot/sendmessage",
      token,
      body: {
        msg: {
          from_user_id: "",
          to_user_id: to,
          client_id: clientId,
          message_type: MESSAGE_TYPE_BOT,
          message_state: MESSAGE_STATE_FINISH,
          item_list: [
            {
              type: ITEM_VOICE,
              voice_item: {
                media: {
                  encrypt_query_param: upload.downloadParam,
                  aes_key: aeskey.toString("base64"),
                  encrypt_type: 1,
                },
                encode_type: VOICE_ENCODE_SILK,
                sample_rate: WEIXIN_VOICE_SAMPLE_RATE,
                playtime: playtimeMs,
                text,
              },
            },
          ],
          ...(contextToken ? { context_token: contextToken } : {}),
        },
        base_info: buildBaseInfo(),
      },
      timeoutMs: 20_000,
    }).catch((error) => {
      error.stage = "sendmessage";
      throw error;
    });
    return {
      ok: true,
      message_id: clientId,
      stage: "sent",
      transcode_format: "silk",
      encode_type: VOICE_ENCODE_SILK,
      sample_rate: WEIXIN_VOICE_SAMPLE_RATE,
      gain_db: WEIXIN_VOICE_GAIN_DB,
      playtime_ms: playtimeMs,
      source_audio: sourceStats,
      transcode_audio: transcodeStats,
      raw_size: rawsize,
      cipher_size: upload.ciphertextSize,
      upload_method: upload.uploadMethod,
      upload_url_kind: upload.uploadUrlKind,
      upload_ms: uploadMs,
      send_ms: Date.now() - sendStart,
      total_ms: Date.now() - started,
    };
  } finally {
    if (pcmPath) await fs.unlink(pcmPath).catch(() => {});
  }
}

async function probeImageStats(filePath) {
  const { size } = await fs.stat(filePath);
  let width = 0;
  let height = 0;
  try {
    const { stdout } = await execFileAsync("ffprobe", [
      "-v",
      "error",
      "-select_streams",
      "v:0",
      "-show_entries",
      "stream=width,height",
      "-of",
      "json",
      filePath,
    ]);
    const data = JSON.parse(stdout || "{}");
    const stream = Array.isArray(data.streams) ? data.streams[0] || {} : {};
    width = Number(stream.width || 0) || 0;
    height = Number(stream.height || 0) || 0;
  } catch {
    // Dimensions are diagnostics only.
  }
  return { size, width, height };
}

async function transcodeImageForWeixin(inputPath) {
  const outputPath = path.join(os.tmpdir(), `branchwhisper-weixin-image-${Date.now()}-${crypto.randomBytes(4).toString("hex")}.png`);
  await execFileAsync("ffmpeg", [
    "-hide_banner",
    "-loglevel",
    "error",
    "-y",
    "-i",
    inputPath,
    "-frames:v",
    "1",
    "-vf",
    "scale='min(1280,iw)':'min(1280,ih)':force_original_aspect_ratio=decrease,format=rgba",
    outputPath,
  ]);
  const { size } = await fs.stat(outputPath);
  if (!size) throw new Error("transcoded image is empty");
  if (size > MAX_IMAGE_BYTES) throw new Error("transcoded image exceeds 8 MB");
  return outputPath;
}

async function transcodeImageThumbForWeixin(inputPath) {
  const outputPath = path.join(os.tmpdir(), `branchwhisper-weixin-thumb-${Date.now()}-${crypto.randomBytes(4).toString("hex")}.jpg`);
  await execFileAsync("ffmpeg", [
    "-hide_banner",
    "-loglevel",
    "error",
    "-y",
    "-i",
    inputPath,
    "-frames:v",
    "1",
    "-vf",
    "scale='min(240,iw)':'min(240,ih)':force_original_aspect_ratio=decrease,format=yuvj420p",
    "-q:v",
    "4",
    outputPath,
  ]);
  const { size } = await fs.stat(outputPath);
  if (!size) throw new Error("transcoded thumbnail is empty");
  if (size > MAX_THUMB_BYTES) throw new Error("thumbnail exceeds 256 KB");
  return outputPath;
}

async function sendImage(args) {
  const baseUrl = args.baseUrl || DEFAULT_BASE_URL;
  const cdnBaseUrl = args.cdnBaseUrl || DEFAULT_CDN_BASE_URL;
  const token = String(args.token || "");
  const to = String(args.to || "");
  const imageFile = String(args.imageFile || "");
  const contextToken = String(args.contextToken || "");
  if (!token) throw new Error("missing --token");
  if (!to) throw new Error("missing --to");
  if (!imageFile) throw new Error("missing --image-file");
  await fs.access(imageFile);

  const started = Date.now();
  let normalizedPath = "";
  let thumbPath = "";
  try {
    const source = await fs.readFile(imageFile);
    if (!source.length) throw new Error("image file is empty");
    if (source.length > MAX_IMAGE_BYTES) throw new Error("image file exceeds 8 MB");
    const sourceStats = await probeImageStats(imageFile);
    normalizedPath = await transcodeImageForWeixin(imageFile).catch((error) => {
      error.stage = "image_transcode";
      throw error;
    });
    thumbPath = await transcodeImageThumbForWeixin(normalizedPath).catch((error) => {
      error.stage = "thumb_transcode";
      throw error;
    });

    const plaintext = await fs.readFile(normalizedPath);
    const thumbPlaintext = await fs.readFile(thumbPath);
    const stats = await probeImageStats(normalizedPath);
    const thumbStats = await probeImageStats(thumbPath);
    const rawsize = plaintext.length;
    const rawfilemd5 = crypto.createHash("md5").update(plaintext).digest("hex");
    const filesize = aesEcbPaddedSize(rawsize);
    const thumbRawsize = thumbPlaintext.length;
    const thumbRawfilemd5 = crypto.createHash("md5").update(thumbPlaintext).digest("hex");
    const thumbFilesize = aesEcbPaddedSize(thumbRawsize);
    const filekey = crypto.randomBytes(16).toString("hex");
    const aeskey = crypto.randomBytes(16);
    const uploadStart = Date.now();
    const uploadUrlResp = await postJson({
      baseUrl,
      apiPath: "ilink/bot/getuploadurl",
      token,
      body: {
        filekey,
        media_type: UPLOAD_MEDIA_TYPE_IMAGE,
        to_user_id: to,
        rawsize,
        rawfilemd5,
        filesize,
        aeskey: aeskey.toString("hex"),
        no_need_thumb: false,
        thumb_rawsize: thumbRawsize,
        thumb_rawfilemd5: thumbRawfilemd5,
        thumb_filesize: thumbFilesize,
        thumbnail_rawsize: thumbRawsize,
        thumbnail_rawfilemd5: thumbRawfilemd5,
        thumbnail_filesize: thumbFilesize,
      },
    }).catch((error) => {
      error.stage = "getuploadurl";
      throw error;
    });

    const nestedImage = uploadUrlResp.image || uploadUrlResp.media || uploadUrlResp.main || uploadUrlResp.file || {};
    const nestedThumb = uploadUrlResp.thumb || uploadUrlResp.thumbnail || uploadUrlResp.thumb_media || uploadUrlResp.thumbMedia || {};
    const uploadFullUrl = String(firstValue(uploadUrlResp.upload_full_url, uploadUrlResp.uploadFullUrl, nestedImage.upload_full_url, nestedImage.uploadFullUrl) || "").trim();
    const uploadParam = firstValue(uploadUrlResp.upload_param, uploadUrlResp.uploadParam, nestedImage.upload_param, nestedImage.uploadParam);
    const thumbUploadFullUrl = String(
      firstValue(
        uploadUrlResp.thumb_upload_full_url,
        uploadUrlResp.thumbUploadFullUrl,
        uploadUrlResp.thumb_full_url,
        uploadUrlResp.thumbFullUrl,
        uploadUrlResp.thumbnail_upload_full_url,
        uploadUrlResp.thumbnailUploadFullUrl,
        uploadUrlResp.thumbnail_full_url,
        uploadUrlResp.thumbnailFullUrl,
        nestedThumb.upload_full_url,
        nestedThumb.uploadFullUrl,
        nestedThumb.full_url,
        nestedThumb.fullUrl,
      ) || "",
    ).trim();
    const thumbUploadParam = firstValue(
      uploadUrlResp.thumb_upload_param,
      uploadUrlResp.thumbUploadParam,
      uploadUrlResp.thumb_param,
      uploadUrlResp.thumbParam,
      uploadUrlResp.thumbnail_upload_param,
      uploadUrlResp.thumbnailUploadParam,
      uploadUrlResp.thumbnail_param,
      uploadUrlResp.thumbnailParam,
      nestedThumb.upload_param,
      nestedThumb.uploadParam,
      nestedThumb.param,
    );
    if (!uploadFullUrl && !uploadParam) {
      const error = new Error("getuploadurl returned no image upload URL");
      error.response_shape = compactResponseShape(uploadUrlResp);
      throw error;
    }
    const canUploadThumb = Boolean(thumbUploadFullUrl || thumbUploadParam);

    const upload = await uploadBufferToCdn({
      buffer: plaintext,
      uploadFullUrl,
      uploadParam,
      filekey,
      cdnBaseUrl,
      aeskey,
      label: "image",
    }).catch((error) => {
      error.stage = "cdn_upload";
      throw error;
    });
    let thumbUpload = null;
    if (canUploadThumb) {
      thumbUpload = await uploadBufferToCdn({
        buffer: thumbPlaintext,
        uploadFullUrl: thumbUploadFullUrl,
        uploadParam: thumbUploadParam,
        filekey,
        cdnBaseUrl,
        aeskey,
        label: "thumbnail",
      }).catch((error) => {
        error.stage = "thumb_cdn_upload";
        throw error;
      });
    }
    const uploadMs = Date.now() - uploadStart;
    const sendStart = Date.now();
    const clientId = `branchwhisper-image-${Date.now()}-${crypto.randomBytes(4).toString("hex")}`;
    const imageItem = {
      media: {
        encrypt_query_param: upload.downloadParam,
        encrypted_query_param: upload.downloadParam,
        aes_key: aeskey.toString("base64"),
        aeskey: aeskey.toString("base64"),
        encrypt_type: 1,
      },
      filekey,
      rawsize,
      rawfilemd5,
      filesize,
      width: stats.width,
      height: stats.height,
    };
    if (thumbUpload) {
      imageItem.thumb_media = {
        encrypt_query_param: thumbUpload.downloadParam,
        encrypted_query_param: thumbUpload.downloadParam,
        aes_key: aeskey.toString("base64"),
        aeskey: aeskey.toString("base64"),
        encrypt_type: 1,
      };
      imageItem.thumb_filekey = filekey;
      imageItem.thumb_rawsize = thumbRawsize;
      imageItem.thumb_rawfilemd5 = thumbRawfilemd5;
      imageItem.thumb_filesize = thumbFilesize;
    }
    await postJson({
      baseUrl,
      apiPath: "ilink/bot/sendmessage",
      token,
      body: {
        msg: {
          from_user_id: "",
          to_user_id: to,
          client_id: clientId,
          message_type: MESSAGE_TYPE_BOT,
          message_state: MESSAGE_STATE_FINISH,
          item_list: [
            {
              type: ITEM_IMAGE,
              image_item: imageItem,
            },
          ],
          ...(contextToken ? { context_token: contextToken } : {}),
        },
        base_info: buildBaseInfo(),
      },
      timeoutMs: 20_000,
    }).catch((error) => {
      error.stage = "sendmessage";
      throw error;
    });
    return {
      ok: true,
      message_id: clientId,
      stage: "sent",
      media_type: "image",
      image_format: "png",
      source_image: sourceStats,
      image: stats,
      thumbnail: thumbStats,
      raw_size: rawsize,
      thumb_raw_size: thumbRawsize,
      cipher_size: upload.ciphertextSize,
      thumb_cipher_size: thumbUpload?.ciphertextSize || 0,
      upload_method: upload.uploadMethod,
      upload_url_kind: upload.uploadUrlKind,
      thumb_upload_method: thumbUpload?.uploadMethod || "",
      thumb_upload_url_kind: thumbUpload?.uploadUrlKind || "",
      thumbnail_skipped: !thumbUpload,
      getuploadurl_shape: compactResponseShape(uploadUrlResp),
      upload_ms: uploadMs,
      send_ms: Date.now() - sendStart,
      total_ms: Date.now() - started,
    };
  } finally {
    if (normalizedPath) await fs.unlink(normalizedPath).catch(() => {});
    if (thumbPath) await fs.unlink(thumbPath).catch(() => {});
  }
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.help) {
    process.stdout.write(usage());
    return;
  }
  try {
    const result = args.selfTest
      ? await selfTest()
      : (args.downloadMedia ? await downloadMedia(args) : (args.imageFile ? await sendImage(args) : await sendVoice(args)));
    process.stdout.write(JSON.stringify(result));
  } catch (error) {
    fail(error?.message || String(error), {
      stage: error?.stage || "unknown",
      response_shape: error?.response_shape,
    });
  }
}

await main();
