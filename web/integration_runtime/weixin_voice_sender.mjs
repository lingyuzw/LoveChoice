#!/usr/bin/env node

import crypto from "node:crypto";
import { execFile } from "node:child_process";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);

const DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com";
const DEFAULT_CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c";
const ILINK_APP_ID = "bot";
const OPENCLAW_WEIXIN_VERSION = "2.4.4";
const MESSAGE_TYPE_BOT = 2;
const MESSAGE_STATE_FINISH = 2;
const ITEM_VOICE = 3;
const UPLOAD_MEDIA_TYPE_VOICE = 4;
const VOICE_ENCODE_OGG_OPUS = 8;
const MAX_VOICE_SECONDS = 60;

function usage() {
  return [
    "Usage:",
    "  node weixin_voice_sender.mjs --base-url URL --token TOKEN --to USER_ID --voice-file FILE [--context-token TOKEN] [--text TEXT]",
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
    if (key === "help" || key === "selfTest") {
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

function buildCdnUploadUrl({ cdnBaseUrl, uploadParam, filekey }) {
  const base = String(cdnBaseUrl || DEFAULT_CDN_BASE_URL).replace(/\/+$/, "");
  return `${base}/upload?encrypted_query_param=${encodeURIComponent(String(uploadParam || ""))}&filekey=${encodeURIComponent(filekey)}`;
}

async function uploadBufferToCdn({ buffer, uploadFullUrl, uploadParam, filekey, cdnBaseUrl, aeskey }) {
  const ciphertext = encryptAesEcb(buffer, aeskey);
  const url = uploadFullUrl?.trim() || buildCdnUploadUrl({ cdnBaseUrl, uploadParam, filekey });
  let lastError = null;
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    try {
      const response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/octet-stream" },
        body: new Uint8Array(ciphertext),
      });
      if (response.status !== 200) {
        const body = await response.text().catch(() => "");
        throw new Error(`CDN HTTP ${response.status}: ${body.slice(0, 180)}`);
      }
      const downloadParam = response.headers.get("x-encrypted-param") || "";
      if (!downloadParam) throw new Error("CDN response missing x-encrypted-param");
      return { downloadParam, ciphertextSize: ciphertext.length };
    } catch (error) {
      lastError = error;
      if (attempt === 3) break;
    }
  }
  throw lastError || new Error("CDN upload failed");
}

async function transcodeToOggOpus(inputPath) {
  const outputPath = path.join(os.tmpdir(), `branchwhisper-weixin-voice-${Date.now()}-${crypto.randomBytes(4).toString("hex")}.ogg`);
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
    "-ar",
    "48000",
    "-ac",
    "1",
    "-c:a",
    "libopus",
    "-b:a",
    "64k",
    "-f",
    "ogg",
    outputPath,
  ]);
  return outputPath;
}

async function probeDurationMs(filePath) {
  const { stdout } = await execFileAsync("ffprobe", [
    "-v",
    "error",
    "-show_entries",
    "format=duration",
    "-of",
    "default=noprint_wrappers=1:nokey=1",
    filePath,
  ]);
  const seconds = Number.parseFloat(stdout.trim());
  if (!Number.isFinite(seconds) || seconds <= 0) throw new Error("ffprobe returned invalid duration");
  return Math.max(1, Math.round(seconds * 1000));
}

async function selfTest() {
  const key = Buffer.alloc(16);
  encryptAesEcb(Buffer.from("ok"), key);
  await execFileAsync("ffmpeg", ["-hide_banner", "-version"]);
  await execFileAsync("ffprobe", ["-hide_banner", "-version"]);
  return { ok: true, ffmpeg: true, ffprobe: true, aes_128_ecb: true };
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
  let oggPath = "";
  try {
    oggPath = await transcodeToOggOpus(voiceFile).catch((error) => {
      error.stage = "transcode";
      throw error;
    });
    const playtimeMs = await probeDurationMs(oggPath);
    const plaintext = await fs.readFile(oggPath);
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
                encode_type: VOICE_ENCODE_OGG_OPUS,
                sample_rate: 48000,
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
      transcode_format: "ogg_opus",
      encode_type: VOICE_ENCODE_OGG_OPUS,
      sample_rate: 48000,
      playtime_ms: playtimeMs,
      raw_size: rawsize,
      cipher_size: upload.ciphertextSize,
      upload_ms: uploadMs,
      send_ms: Date.now() - sendStart,
      total_ms: Date.now() - started,
    };
  } finally {
    if (oggPath) await fs.unlink(oggPath).catch(() => {});
  }
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.help) {
    process.stdout.write(usage());
    return;
  }
  try {
    const result = args.selfTest ? await selfTest() : await sendVoice(args);
    process.stdout.write(JSON.stringify(result));
  } catch (error) {
    fail(error?.message || String(error), { stage: error?.stage || "unknown" });
  }
}

await main();
