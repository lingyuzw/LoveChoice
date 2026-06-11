from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import random
import re
import shutil
import subprocess
import time
import uuid
import wave
from dataclasses import asdict
from pathlib import Path
from typing import Any

import httpx

from service_runtime.audio_pipeline import clean_for_tts, clean_reply_text, extract_chat_message_text, extract_finish_reason
from core.config import (
    SessionSettings,
    active_history_turns,
    active_llm_model,
    active_llm_url,
    active_max_tokens,
    active_temperature,
    llm_headers,
    memory_mode,
)
from data.conversations import ConversationStore
from tools.direct_answers import direct_answer_from_tool
from data.profiles import BotProfileStore
from tools.runtime_brain import MemoryStore, ToolManager
from integration_runtime.weixin_media import WeixinImageSendError, WeixinVoiceSendError, send_weixin_image, send_weixin_voice
from media.assets import StickerStore
from media.sticker_vision import ChatImageAnalyzer
from media.sticker_policy import StickerPolicy
from core.io_utils import read_json_file
from core.text_utils import compact_text, extract_repeat_text, format_reply_paragraphs, is_story_request, split_reply_messages
from core.time_utils import now_text, now_ts


DEFAULT_VOICE_TRIGGERS = [
    "发语音",
    "发条语音",
    "发句语音",
    "说话",
    "你说话",
    "说两句",
    "说句话",
    "讲句话",
    "想听你说",
    "念给我听",
    "读出来",
    "语音回复",
    "我想听你说话",
    "听听",
]
VOICE_INTENT_RE = re.compile(
    r"(发|来|给我|要|想听)(一|1)?(条|段|句)?语音"
    r"|(一|1)?(条|段|句)语音"
    r"|再(发|来|说|讲)(一|1)?(条|段|句)?"
    r"|(我)?想听(你|妳)?(说|讲|念|读)"
    r"|语音回复"
    r"|(你|妳)?(快点|快|赶紧|马上)?说话"
    r"|(你|妳)?(快点|快|赶紧|马上)?说(句|句话|一句|一段|两句|几句|一下|给我听|呀|啊|嘛|吗|吧)"
    r"|说(给我听|我听着|出来)"
    r"|讲(句|句话|一句|一段|两句|几句|一下|给我听)"
    r"|念(一下|给我听)?"
    r"|读出来"
    r"|开口(说话)?"
    r"|出声"
    r"|听听$"
)
VOICE_NEGATIVE_RE = re.compile(r"(别|不要|不用|不想|先别|别再)(发语音|语音|说话|开口|出声)")
DEFAULT_WEIXIN_OC_BASE_URL = "https://ilinkai.weixin.qq.com"
DEFAULT_WEIXIN_CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
DEFAULT_WEIXIN_OC_BOT_TYPE = "3"
DEFAULT_WEIXIN_OC_VERSION = "2.4.4"
ILINK_APP_ID = "bot"
SUPPORTED_TYPES = {"weixin_oc"}
RUNNING_STATES = {"starting", "running", "login"}


def safe_id(value: str, fallback: str = "weixin_personal") -> str:
    value = re.sub(r"[^a-zA-Z0-9_\-]", "_", str(value or "")).strip("_")
    return value[:48] or fallback


def openclaw_state_dir(profile: str) -> Path:
    env_dir = os.environ.get("OPENCLAW_STATE_DIR") or os.environ.get("CLAWDBOT_STATE_DIR")
    if env_dir:
        return Path(env_dir).expanduser()
    profile = safe_id(profile, fallback="branchwhisper")
    if profile and profile not in {"default", "main"}:
        return Path.home() / f".openclaw-{profile}"
    return Path.home() / ".openclaw"


def build_client_version(version: str) -> int:
    parts = []
    for part in str(version or "").split(".")[:3]:
        try:
            parts.append(int(part))
        except ValueError:
            parts.append(0)
    while len(parts) < 3:
        parts.append(0)
    major, minor, patch = parts
    return ((major & 0xFF) << 16) | ((minor & 0xFF) << 8) | (patch & 0xFF)


def weixin_api_headers(token: str = "", version: str = DEFAULT_WEIXIN_OC_VERSION) -> dict[str, str]:
    uin = base64.b64encode(str(random.getrandbits(32)).encode("utf-8")).decode("ascii")
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": uin,
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(build_client_version(version)),
    }
    if token:
        headers["Authorization"] = f"Bearer {token.strip()}"
    return headers


def normalize_weixin_account_id(account_id: str) -> str:
    value = str(account_id or "").strip()
    if value.endswith("@im.bot"):
        return f"{value[:-7]}-im-bot"
    if value.endswith("@im.wechat"):
        return f"{value[:-10]}-im-wechat"
    value = re.sub(r"[^a-zA-Z0-9_\-]", "-", value).strip("-")
    return value or "weixin-account"


def derive_raw_account_id(account_id: str) -> str | None:
    if account_id.endswith("-im-bot"):
        return f"{account_id[:-7]}@im.bot"
    if account_id.endswith("-im-wechat"):
        return f"{account_id[:-10]}@im.wechat"
    return None


def ilink_endpoint(base_url: str, path: str) -> str:
    return f"{str(base_url or DEFAULT_WEIXIN_OC_BASE_URL).rstrip('/')}/{path.lstrip('/')}"


def wav_bytes_from_pcm16(pcm: bytes, sample_rate: int) -> bytes:
    buffer = bytearray()
    import io

    stream = io.BytesIO()
    with wave.open(stream, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(int(sample_rate))
        wav.writeframes(pcm)
    buffer.extend(stream.getvalue())
    return bytes(buffer)


def sniff_image_mime(path: Path, fallback: str = "image/jpeg") -> str:
    try:
        header = path.read_bytes()[:16]
    except Exception:
        return fallback
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    if header.startswith(b"GIF87a") or header.startswith(b"GIF89a"):
        return "image/gif"
    return fallback


def voice_fallback_reply(user_text: str) -> str:
    normalized = re.sub(r"\s+", "", user_text or "")
    if normalized in {"说话", "你说话", "发语音", "语音", "说两句"}:
        return "我在呢，听得到的话我们继续聊。"
    return "我在呢，这句我直接说给你听。"


class IntegrationManager:
    def __init__(self, config_path: Path, log_dir: Path, media_dir: Path):
        self.config_path = config_path
        self.log_dir = log_dir
        self.media_dir = media_dir
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.processes: dict[str, subprocess.Popen] = {}
        self.runtime: dict[str, dict[str, Any]] = {}
        self._environment_cache: dict | None = None
        self._environment_cache_at = 0.0
        self._login_sessions: dict[str, dict[str, Any]] = {}
        if not self.config_path.exists():
            self.save_config(self.default_config())

    def default_config(self) -> dict:
        return {
            "integrations": [
                {
                    "id": "weixin_personal",
                    "type": "weixin_oc",
                    "enabled": False,
                    "chat_name": "我的微信聊天",
                    "openclaw_profile": "branchwhisper",
                    "reply_mode": "text",
                    "voice_trigger_keywords": list(DEFAULT_VOICE_TRIGGERS),
                    "status": "stopped",
                    "last_error": "",
                    "last_login_at": "",
                }
            ],
            "sessions": {},
            "my_weixin_session": {},
        }

    def load_config(self) -> dict:
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except Exception:
            data = {}
        base = self.default_config()
        integrations = data.get("integrations")
        if isinstance(integrations, list):
            base["integrations"] = [self.normalize_integration(item) for item in integrations if isinstance(item, dict)]
        sessions = data.get("sessions")
        if isinstance(sessions, dict):
            base["sessions"] = {str(k): str(v) for k, v in sessions.items() if k and v}
        if isinstance(data.get("my_weixin_session"), dict):
            base["my_weixin_session"] = dict(data["my_weixin_session"])
        return base

    def save_config(self, data: dict) -> dict:
        payload = {
            "integrations": [self.normalize_integration(item) for item in data.get("integrations", []) if isinstance(item, dict)],
            "sessions": data.get("sessions") if isinstance(data.get("sessions"), dict) else {},
            "my_weixin_session": data.get("my_weixin_session") if isinstance(data.get("my_weixin_session"), dict) else {},
        }
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.config_path.with_suffix(self.config_path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.config_path)
        return payload

    def normalize_integration(self, item: dict) -> dict:
        integration_id = safe_id(str(item.get("id") or "weixin_personal"))
        integration_type = str(item.get("type") or "weixin_oc")
        if integration_type not in SUPPORTED_TYPES:
            integration_type = "weixin_oc"
        keywords = item.get("voice_trigger_keywords")
        if not isinstance(keywords, list):
            keywords = DEFAULT_VOICE_TRIGGERS
        keywords = [str(keyword).strip() for keyword in keywords if str(keyword).strip()]
        return {
            "id": integration_id,
            "type": integration_type,
            "enabled": bool(item.get("enabled", False)),
            "chat_name": compact_text(str(item.get("chat_name") or "我的微信聊天"), 48),
            "openclaw_profile": safe_id(str(item.get("openclaw_profile") or "branchwhisper"), fallback="branchwhisper"),
            "bot_profile_id": safe_id(str(item.get("bot_profile_id") or "default"), fallback="default"),
            "reply_mode": str(item.get("reply_mode") or "text") if str(item.get("reply_mode") or "text") in {"text", "voice"} else "text",
            "voice_trigger_keywords": keywords or list(DEFAULT_VOICE_TRIGGERS),
            "status": str(item.get("status") or "stopped"),
            "last_error": str(item.get("last_error") or ""),
            "last_login_at": str(item.get("last_login_at") or ""),
        }

    def list_integrations(self) -> dict:
        data = self.load_config()
        items = []
        for integration in data["integrations"]:
            items.append(self.with_runtime(integration))
        return {"integrations": items, "environment": self.environment_status()}

    def with_runtime(self, integration: dict) -> dict:
        item = dict(integration)
        proc = self.processes.get(item["id"])
        runtime = self.runtime.get(item["id"], {})
        accounts = self.weixin_accounts(integration)
        if proc and proc.poll() is None:
            item["status"] = runtime.get("status") or item.get("status") or "running"
            item["pid"] = proc.pid
        elif proc:
            return_code = proc.poll()
            self.processes.pop(item["id"], None)
            runtime = self.runtime.setdefault(item["id"], dict(runtime))
            runtime["pid"] = None
            runtime["last_exit_code"] = return_code
            runtime["ended_at"] = now_text()
            if item.get("status") in RUNNING_STATES or runtime.get("status") in RUNNING_STATES:
                if return_code == 0:
                    item["status"] = "stopped"
                    runtime["status"] = "stopped"
                    runtime["last_error"] = ""
                else:
                    item["status"] = "failed"
                    error = f"process exited with code {return_code}"
                    runtime["status"] = "failed"
                    runtime["last_error"] = error
                    item["last_error"] = error
                    self.append_log(item["id"], f"[process] {error}")
            item["pid"] = None
        elif item.get("status") in RUNNING_STATES:
            item["status"] = "stopped"
            item["pid"] = None
        if accounts and item.get("status") == "stopped":
            item["status"] = "logged_in"
        item["runtime"] = {
            **runtime,
            "log_file": str(self.log_path(item["id"])),
            "media_dir": str(self.media_dir),
            "state_dir": str(openclaw_state_dir(integration.get("openclaw_profile") or "branchwhisper")),
            "account_count": len(accounts),
        }
        item["accounts"] = accounts
        item["account"] = runtime.get("account") or (accounts[0]["id"] if accounts else "")
        item["last_message_at"] = runtime.get("last_message_at") or ""
        item["recent_timings"] = runtime.get("recent_timings") or []
        item["my_weixin_session"] = self.my_weixin_session(item["id"])
        return item

    def weixin_accounts(self, integration: dict) -> list[dict]:
        state_dir = openclaw_state_dir(integration.get("openclaw_profile") or "branchwhisper")
        weixin_dir = state_dir / "openclaw-weixin"
        index = read_json_file(weixin_dir / "accounts.json", [])
        if not isinstance(index, list):
            index = []
        accounts = []
        for account_id in [str(item) for item in index if str(item).strip()]:
            account_path = weixin_dir / "accounts" / f"{account_id}.json"
            data = read_json_file(account_path, None)
            raw_id = derive_raw_account_id(account_id)
            if (not isinstance(data, dict) or not data) and raw_id:
                account_path = weixin_dir / "accounts" / f"{raw_id}.json"
                data = read_json_file(account_path, None)
            if not isinstance(data, dict):
                data = {}
            accounts.append(
                {
                    "id": account_id,
                    "base_url": str(data.get("baseUrl") or data.get("base_url") or ""),
                    "user_id": str(data.get("userId") or data.get("user_id") or ""),
                    "saved_at": str(data.get("savedAt") or data.get("saved_at") or ""),
                    "token_set": bool(str(data.get("token") or "").strip()),
                    "path": str(account_path),
                }
            )
        return accounts

    def weixin_account_credentials(self, integration: dict, account_id: str = "") -> dict:
        accounts = self.weixin_accounts(integration)
        selected = next((item for item in accounts if item.get("id") == account_id), None) if account_id else (accounts[0] if accounts else None)
        if not selected:
            return {}
        data = read_json_file(Path(selected.get("path") or ""), {})
        if not isinstance(data, dict):
            return {}
        token = str(data.get("token") or "").strip()
        if not token:
            return {}
        return {
            "id": selected.get("id") or account_id,
            "token": token,
            "base_url": str(data.get("baseUrl") or data.get("base_url") or selected.get("base_url") or DEFAULT_WEIXIN_OC_BASE_URL),
            "cdn_base_url": str(data.get("cdnBaseUrl") or data.get("cdn_base_url") or DEFAULT_WEIXIN_CDN_BASE_URL),
            "user_id": str(data.get("userId") or data.get("user_id") or selected.get("user_id") or ""),
        }

    def context_tokens_path(self, integration: dict, account_id: str) -> Path:
        state_dir = openclaw_state_dir(integration.get("openclaw_profile") or "branchwhisper")
        return state_dir / "openclaw-weixin" / "accounts" / f"{account_id}.context-tokens.json"

    def recent_weixin_targets(self, integration_id: str, within_hours: float = 24.0) -> list[dict]:
        integration = self.get_integration(integration_id)
        if not integration:
            return []
        targets: list[dict] = []
        for account in self.weixin_accounts(integration):
            account_id = str(account.get("id") or "")
            if not account_id:
                continue
            credentials = self.weixin_account_credentials(integration, account_id)
            if not credentials:
                continue
            token_path = self.context_tokens_path(integration, account_id)
            data = read_json_file(token_path, {})
            if not isinstance(data, dict):
                continue
            modified_at = token_path.stat().st_mtime if token_path.exists() else 0
            age_hours = (time.time() - modified_at) / 3600 if modified_at else 999999
            if age_hours > within_hours:
                continue
            for sender_id, context_token in data.items():
                sender = str(sender_id or "")
                token = str(context_token or "")
                if sender and token:
                    targets.append(
                        {
                            "account_id": account_id,
                            "sender_id": sender,
                            "context_token": token,
                            "base_url": credentials["base_url"],
                            "cdn_base_url": credentials.get("cdn_base_url") or DEFAULT_WEIXIN_CDN_BASE_URL,
                            "token": credentials["token"],
                            "age_hours": round(age_hours, 2),
                        }
                    )
        return targets

    def select_weixin_target(self, integration_id: str, sender_id: str = "", account_id: str = "") -> dict | None:
        my_session = self.my_weixin_session(integration_id)
        sender_id = sender_id or str(my_session.get("sender_id") or "")
        account_id = account_id or str(my_session.get("account_id") or "")
        targets = self.recent_weixin_targets(integration_id)
        for item in targets:
            if item.get("sender_id") == sender_id and (not account_id or item.get("account_id") == account_id):
                return item
        return targets[0] if targets else None

    async def send_weixin_text(self, integration_id: str, text: str, sender_id: str = "", account_id: str = "") -> dict:
        integration = self.require_integration(integration_id)
        target = self.select_weixin_target(integration_id, sender_id=sender_id, account_id=account_id)
        if not target:
            error = "我的微信会话未绑定或已超过 24 小时可触达窗口；请先用你的微信给 BranchWhisper 发一条消息。"
            self.append_log(integration_id, f"[proactive] send skipped: {error}")
            return {"ok": False, "error": error}

        client_id = f"branchwhisper-proactive-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
        payload = {
            "msg": {
                "from_user_id": "",
                "to_user_id": target["sender_id"],
                "client_id": client_id,
                "message_type": 2,
                "message_state": 2,
                "item_list": [{"type": 1, "text_item": {"text": str(text or "")}}],
                "context_token": target["context_token"],
            },
            "base_info": {"channel_version": "branchwhisper-server", "bot_agent": "BranchWhisper/1.0 (proactive)"},
        }
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                ilink_endpoint(target["base_url"], "ilink/bot/sendmessage"),
                json=payload,
                headers=weixin_api_headers(target["token"]),
            )
            resp.raise_for_status()
        self.append_log(
            integration_id,
            f"[proactive] sent text account={target['account_id']} to={target['sender_id']} client_id={client_id}",
        )
        return {"ok": True, "client_id": client_id, "account_id": target["account_id"], "sender_id": target["sender_id"]}

    def my_weixin_session(self, integration_id: str = "") -> dict:
        data = self.load_config()
        session = data.get("my_weixin_session") if isinstance(data.get("my_weixin_session"), dict) else {}
        if integration_id and session and session.get("platform_id") != safe_id(integration_id):
            return {}
        if not session:
            return {}
        updated_at = float(session.get("updated_at_ts") or 0)
        remaining = max(0, int(24 * 3600 - (time.time() - updated_at))) if updated_at else 0
        return {
            **session,
            "bound": bool(session.get("sender_id") and session.get("account_id")),
            "reachable": remaining > 0,
            "reachable_remaining_sec": remaining,
        }

    def bind_my_weixin_session(
        self,
        platform_id: str,
        *,
        account_id: str,
        session_id: str,
        sender_id: str,
        conversation_id: str,
        context_token: str = "",
    ) -> dict:
        data = self.load_config()
        item = {
            "platform_id": safe_id(platform_id),
            "account_id": str(account_id or ""),
            "session_id": str(session_id or ""),
            "sender_id": str(sender_id or ""),
            "conversation_id": str(conversation_id or ""),
            "context_token_set": bool(str(context_token or "")),
            "updated_at": now_text(),
            "updated_at_ts": time.time(),
        }
        data["my_weixin_session"] = item
        self.save_config(data)
        self.runtime.setdefault(item["platform_id"], {})["my_weixin_session"] = item
        self.append_log(item["platform_id"], f"[weixin-session] bound my session sender={item['sender_id']} conversation={conversation_id}")
        return self.my_weixin_session(item["platform_id"])

    def get_integration(self, integration_id: str) -> dict | None:
        integration_id = safe_id(integration_id)
        for item in self.load_config()["integrations"]:
            if item["id"] == integration_id:
                return item
        return None

    def create_integration(self, payload: dict) -> dict:
        data = self.load_config()
        item = self.normalize_integration({**payload, "type": "weixin_oc"})
        if any(existing["id"] == item["id"] for existing in data["integrations"]):
            raise ValueError(f"integration already exists: {item['id']}")
        data["integrations"].append(item)
        self.save_config(data)
        self.append_log(item["id"], f"[config] created integration {item['id']}")
        return item

    def update_integration(self, integration_id: str, payload: dict) -> dict:
        data = self.load_config()
        integration_id = safe_id(integration_id)
        for index, existing in enumerate(data["integrations"]):
            if existing["id"] != integration_id:
                continue
            updated = self.normalize_integration({**existing, **payload, "id": existing["id"], "type": existing["type"]})
            data["integrations"][index] = updated
            self.save_config(data)
            self.append_log(updated["id"], "[config] updated integration")
            return updated
        raise KeyError(integration_id)

    def delete_integration(self, integration_id: str) -> bool:
        data = self.load_config()
        integration_id = safe_id(integration_id)
        next_items = [item for item in data["integrations"] if item["id"] != integration_id]
        if len(next_items) == len(data["integrations"]):
            return False
        self.stop_process(integration_id)
        data["integrations"] = next_items
        prefix = f"{integration_id}:"
        data["sessions"] = {key: value for key, value in data["sessions"].items() if not key.startswith(prefix)}
        self.save_config(data)
        self.append_log(integration_id, "[config] deleted integration")
        return True

    def record_message_timing(self, integration_id: str, timing: dict) -> None:
        runtime = self.runtime.setdefault(safe_id(integration_id), {})
        items = list(runtime.get("recent_timings") or [])
        items.insert(0, timing)
        runtime["recent_timings"] = items[:10]

    def update_message_timing(self, integration_id: str, trace_id: str, patch: dict) -> dict:
        if not trace_id:
            raise ValueError("trace_id is required")
        runtime = self.runtime.setdefault(safe_id(integration_id), {})
        items = list(runtime.get("recent_timings") or [])
        allowed = {
            "receive_ms",
            "tool_ms",
            "llm_ms",
            "tts_ms",
            "send_ms",
            "total_ms",
            "dialog_ms",
            "bridge_ms",
            "send_status",
            "send_error",
            "text_parts",
            "voice_send_ms",
            "voice_send_status",
            "voice_error",
            "voice_message_id",
            "voice_stage",
            "voice_format",
            "voice_diagnostic",
            "sticker_send_ms",
            "sticker_send_status",
            "sticker_count",
            "sticker_error",
            "sticker_sent_ids",
        }
        sanitized = {
            key: value
            for key, value in (patch or {}).items()
            if key in allowed and isinstance(value, (str, int, float, bool))
        }
        sanitized["updated_at"] = now_text()
        for item in items:
            if str(item.get("trace_id") or "") == str(trace_id):
                item.update(sanitized)
                runtime["recent_timings"] = items[:10]
                self.mark_stickers_sent_from_timing(item)
                return item
        timing = {"trace_id": trace_id, "created_at": now_text(), **sanitized}
        items.insert(0, timing)
        runtime["recent_timings"] = items[:10]
        self.mark_stickers_sent_from_timing(timing)
        return timing

    def mark_stickers_sent_from_timing(self, timing: dict) -> None:
        if not getattr(self, "sticker_store", None) or not getattr(self, "sticker_policy", None):
            return
        if timing.get("sticker_usage_marked"):
            return
        sent_ids = [
            str(item).strip()
            for item in re.split(r"[,;\s]+", str(timing.get("sticker_sent_ids") or ""))
            if str(item).strip()
        ]
        if not sent_ids or str(timing.get("sticker_send_status") or "") != "sent":
            return
        session_id = str(timing.get("conversation_id") or timing.get("session_id") or "")
        if not session_id:
            return
        for sticker_id in sent_ids:
            self.sticker_policy.mark_sent(session_id, sticker_id)
        self.sticker_store.mark_used_many(sent_ids)
        timing["sticker_usage_marked"] = True

    def mark_status(self, integration_id: str, status: str, error: str = "") -> None:
        integration = self.get_integration(integration_id)
        if not integration:
            return
        patch = {"status": status, "last_error": error}
        if status == "login":
            patch["last_login_at"] = now_text()
        try:
            self.update_integration(integration_id, patch)
        except Exception:
            pass
        self.runtime.setdefault(integration_id, {})["status"] = status
        if error:
            self.runtime[integration_id]["last_error"] = error
        if status in RUNNING_STATES:
            self.runtime[integration_id]["manual_stop"] = False

    def environment_status(self) -> dict:
        if self._environment_cache and now_ts() - self._environment_cache_at < 15:
            return self._environment_cache
        tools = {}
        for name in ("node", "npm", "openclaw", "ffmpeg"):
            path = shutil.which(name)
            tools[name] = {
                "available": bool(path),
                "path": path or "",
                "version": self.tool_version(name) if path else "",
            }
        silk_version = self.npm_installed_package_version("silk-wasm")
        tools["silk-wasm"] = {
            "available": bool(silk_version),
            "path": "npm package" if silk_version else "",
            "version": silk_version,
        }
        package = self.npm_package_version("@tencent-weixin/openclaw-weixin")
        cli_package = self.npm_package_version("@tencent-weixin/openclaw-weixin-cli")
        ready = all(tools[name]["available"] for name in ("node", "npm", "openclaw", "ffmpeg", "silk-wasm"))
        data = {"ready": ready, "tools": tools, "packages": {"openclaw_weixin": package, "openclaw_weixin_cli": cli_package, "silk_wasm": silk_version}}
        self._environment_cache = data
        self._environment_cache_at = now_ts()
        return data

    def tool_version(self, name: str) -> str:
        cmd = [name, "-version"] if name == "ffmpeg" else [name, "--version"]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5)
        except Exception as exc:
            return str(exc)
        text = (result.stdout or result.stderr or "").strip().splitlines()
        return text[0][:160] if text else ""

    def npm_package_version(self, package_name: str) -> str:
        if not shutil.which("npm"):
            return ""
        try:
            result = subprocess.run(
                ["npm", "view", package_name, "version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=8,
            )
        except Exception:
            return ""
        if result.returncode != 0:
            return ""
        return (result.stdout or "").strip().splitlines()[0] if (result.stdout or "").strip() else ""

    def npm_installed_package_version(self, package_name: str) -> str:
        if not shutil.which("npm"):
            return ""
        commands = [
            ["npm", "list", "-g", package_name, "--depth=0", "--json"],
            ["npm", "list", package_name, "--depth=0", "--json"],
        ]
        for command in commands:
            try:
                result = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=8,
                )
            except Exception:
                continue
            text = (result.stdout or "").strip()
            if not text:
                continue
            try:
                data = json.loads(text)
            except Exception:
                continue
            version = str((data.get("dependencies") or {}).get(package_name, {}).get("version") or "")
            if version:
                return version
        return ""

    def log_path(self, integration_id: str) -> Path:
        return self.log_dir / f"integration-{safe_id(integration_id)}.log"

    def append_log(self, integration_id: str, text: str) -> None:
        path = self.log_path(integration_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", errors="replace") as stream:
            stream.write(f"\n===== {now_text()} =====\n{text.rstrip()}\n")

    def read_logs(self, integration_id: str, max_bytes: int = 36000) -> str:
        path = self.log_path(integration_id)
        if not path.exists():
            return ""
        size = path.stat().st_size
        with path.open("rb") as stream:
            if size > max_bytes:
                stream.seek(max(0, size - max_bytes))
            data = stream.read()
        return data.decode("utf-8", errors="replace")

    def read_logs_scoped(self, integration_id: str, max_bytes: int = 36000, scope: str = "all") -> str:
        text = self.read_logs(integration_id, max_bytes=max_bytes)
        if scope != "current" or not text:
            return text
        markers = [
            "\n===== ",
        ]
        start_tokens = ("[session] bridge started", "[process] start:")
        start_index = -1
        for token in start_tokens:
            idx = text.rfind(token)
            if idx > start_index:
                start_index = idx
        if start_index == -1:
            return ""
        header_index = text.rfind(markers[0], 0, start_index)
        return text[header_index + 1 if header_index >= 0 else start_index :]

    def clear_logs(self, integration_id: str) -> dict:
        path = self.log_path(integration_id)
        if path.exists():
            path.unlink()
        self.append_log(integration_id, "[logs] cleared")
        return {"ok": True, "id": safe_id(integration_id)}

    async def install_weixin_cli(self, integration_id: str) -> dict:
        integration = self.require_integration(integration_id)
        return await self.run_command(integration, ["npx", "-y", "@tencent-weixin/openclaw-weixin-cli", "install"], timeout=600)

    async def gateway_action(self, integration_id: str, action: str) -> dict:
        integration = self.require_integration(integration_id)
        if action not in {"start", "stop", "restart", "status"}:
            raise ValueError("unsupported gateway action")
        result = await self.run_openclaw(integration, ["gateway", action], timeout=45)
        disabled_hint = self.gateway_disabled_hint(result)
        if disabled_hint:
            result = {**result, "ok": False, "gateway_disabled": True, "hint": disabled_hint}
            next_status = "logged_in" if self.weixin_accounts(integration) else "stopped"
            self.mark_status(integration["id"], next_status, disabled_hint)
        elif action == "start" and result["ok"]:
            self.mark_status(integration["id"], "running")
        elif action == "stop" and result["ok"]:
            self.mark_status(integration["id"], "stopped")
        elif action == "restart" and result["ok"]:
            self.mark_status(integration["id"], "running")
        elif not result["ok"]:
            self.mark_status(integration["id"], "failed", result["stderr"] or result["stdout"])
        return result

    def gateway_disabled_hint(self, result: dict) -> str:
        text = f"{result.get('stdout') or ''}\n{result.get('stderr') or ''}"
        lowered = text.lower()
        if "gateway service disabled" in lowered or "systemd user services are unavailable" in lowered:
            return (
                "OpenClaw gateway systemd 服务不可用。容器环境下这是常见情况，"
                "请使用“启动桥接”运行 BranchWhisper 的前台桥接进程。"
            )
        return ""

    async def login(self, integration_id: str) -> dict:
        integration = self.require_integration(integration_id)
        command = self.openclaw_command(integration, ["channels", "login", "--channel", "openclaw-weixin"])
        return self.start_background_process(integration, command, status="login")

    async def start_bridge(self, integration_id: str, branchwhisper_url: str = "http://127.0.0.1:7860") -> dict:
        integration = self.require_integration(integration_id)
        script = Path(__file__).resolve().parent / "openclaw_bridge.py"
        command = [
            os.environ.get("PYTHON", "python"),
            "-u",
            str(script),
            "--integration-id",
            integration["id"],
            "--profile",
            integration["openclaw_profile"],
            "--state-dir",
            str(openclaw_state_dir(integration["openclaw_profile"])),
            "--branchwhisper-url",
            branchwhisper_url,
        ]
        return self.start_background_process(integration, command, status="running")

    def start_background_process(self, integration: dict, command: list[str], status: str) -> dict:
        integration_id = integration["id"]
        existing = self.processes.get(integration_id)
        if existing and existing.poll() is None:
            return {"ok": True, "status": "already_running", "pid": existing.pid}
        path = self.log_path(integration_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.append_log(integration_id, f"[process] start: {' '.join(command)}")
        stream = path.open("ab")
        try:
            proc = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        except Exception as exc:
            stream.close()
            self.mark_status(integration_id, "failed", str(exc))
            self.append_log(integration_id, f"[process] failed: {exc}")
            return {"ok": False, "error": str(exc)}
        stream.close()
        self.processes[integration_id] = proc
        self.runtime.setdefault(integration_id, {})["status"] = status
        self.runtime[integration_id]["manual_stop"] = False
        self.runtime[integration_id]["started_at"] = now_text()
        self.runtime[integration_id]["command"] = command
        self.mark_status(integration_id, status)
        self.append_log(integration_id, f"[session] bridge started pid={proc.pid}")
        return {"ok": True, "status": status, "pid": proc.pid, "log_file": str(path)}

    def stop_process(self, integration_id: str) -> dict:
        integration_id = safe_id(integration_id)
        proc = self.processes.get(integration_id)
        if not proc or proc.poll() is not None:
            self.runtime.setdefault(integration_id, {})["manual_stop"] = True
            self.mark_status(integration_id, "stopped")
            return {"ok": True, "status": "stopped"}
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        self.mark_status(integration_id, "stopped")
        self.runtime.setdefault(integration_id, {})["manual_stop"] = True
        self.append_log(integration_id, "[process] stopped")
        return {"ok": True, "status": "stopped"}

    async def run_openclaw(self, integration: dict, args: list[str], timeout: int = 60) -> dict:
        return await self.run_command(integration, self.openclaw_command(integration, args), timeout=timeout)

    def openclaw_command(self, integration: dict, args: list[str]) -> list[str]:
        return ["openclaw", "--profile", integration["openclaw_profile"], *args]

    async def run_command(self, integration: dict, command: list[str], timeout: int = 60) -> dict:
        integration_id = integration["id"]
        self.append_log(integration_id, f"[command] {' '.join(command)}")

        def _run() -> subprocess.CompletedProcess:
            return subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )

        try:
            result = await asyncio.to_thread(_run)
            stdout = result.stdout or ""
            stderr = result.stderr or ""
            self.append_log(integration_id, f"[exit {result.returncode}]\n{stdout}\n{stderr}".strip())
            return {"ok": result.returncode == 0, "returncode": result.returncode, "stdout": stdout, "stderr": stderr}
        except Exception as exc:
            self.append_log(integration_id, f"[command:error] {exc}")
            return {"ok": False, "returncode": -1, "stdout": "", "stderr": str(exc)}

    def weixin_base_url(self, integration: dict) -> str:
        return str(integration.get("weixin_oc_base_url") or DEFAULT_WEIXIN_OC_BASE_URL).rstrip("/")

    async def request_weixin_login_qr(self, integration_id: str, force: bool = False) -> dict:
        integration = self.require_integration(integration_id)
        session = self._login_sessions.get(integration_id)
        now = now_ts()
        if session and not force and now - float(session.get("started_at", 0)) < 290:
            return {"ok": True, "login": self.public_login_session(integration_id)}

        base_url = self.weixin_base_url(integration)
        payload = {"local_token_list": self.local_token_list(integration)}
        params = {"bot_type": str(integration.get("weixin_oc_bot_type") or DEFAULT_WEIXIN_OC_BOT_TYPE)}
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(
                    f"{base_url}/ilink/bot/get_bot_qrcode",
                    params=params,
                    json=payload,
                    headers=weixin_api_headers(),
                )
                response.raise_for_status()
                data = response.json()
        except Exception as exc:
            error = f"request QR failed: {exc}"
            self.mark_status(integration_id, "failed", error)
            self.append_log(integration_id, f"[login] {error}")
            return {"ok": False, "error": error}

        qrcode = str(data.get("qrcode") or "").strip()
        qrcode_img = str(data.get("qrcode_img_content") or "").strip()
        if not qrcode or not qrcode_img:
            error = "QR response missing qrcode or qrcode_img_content"
            self.mark_status(integration_id, "failed", error)
            self.append_log(integration_id, f"[login] {error}: {compact_text(json.dumps(data, ensure_ascii=False), 500)}")
            return {"ok": False, "error": error}

        session_key = uuid.uuid4().hex
        session = {
            "session_key": session_key,
            "base_url": base_url,
            "qrcode": qrcode,
            "qrcode_img_content": qrcode_img,
            "started_at": now,
            "expires_at": now + 300,
            "status": "wait",
            "message": "请使用手机微信扫码，并在微信内确认登录。",
        }
        self._login_sessions[integration_id] = session
        self.mark_status(integration_id, "login")
        self.append_log(integration_id, f"[login] QR session started, session={session_key}, image={qrcode_img[:160]}")
        return {"ok": True, "login": self.public_login_session(integration_id)}

    async def poll_weixin_login(self, integration_id: str, verify_code: str = "") -> dict:
        integration = self.require_integration(integration_id)
        session = self._login_sessions.get(integration_id)
        if not session:
            return {"ok": False, "login": {"status": "idle", "message": "当前没有进行中的扫码登录。"}}
        if now_ts() > float(session.get("expires_at", 0)):
            session["status"] = "expired"
            session["message"] = "二维码已过期，请重新发起扫码登录。"
            self.mark_status(integration_id, "failed", session["message"])
            return {"ok": False, "login": self.public_login_session(integration_id)}

        params = {"qrcode": session["qrcode"]}
        if verify_code:
            params["verify_code"] = verify_code
        try:
            async with httpx.AsyncClient(timeout=36.0) as client:
                response = await client.get(
                    f"{session['base_url']}/ilink/bot/get_qrcode_status",
                    params=params,
                    headers=weixin_api_headers(version="0.0.1"),
                )
                response.raise_for_status()
                data = response.json()
        except Exception as exc:
            session["status"] = "wait"
            session["message"] = f"等待扫码确认中：{exc}"
            self.append_log(integration_id, f"[login] poll warning: {exc}")
            return {"ok": True, "login": self.public_login_session(integration_id)}

        status = str(data.get("status") or "wait").strip()
        session["status"] = status
        if status in {"wait", ""}:
            session["message"] = "等待扫码。"
        elif status == "scaned":
            session["message"] = "已扫码，等待手机端确认。"
        elif status == "need_verifycode":
            session["message"] = "手机端需要输入验证数字；请在日志或手机提示中完成验证后重试。"
        elif status == "scaned_but_redirect":
            redirect_host = str(data.get("redirect_host") or "").strip()
            if redirect_host:
                session["base_url"] = f"https://{redirect_host}"
                session["message"] = f"已扫码，切换到微信分区 {redirect_host} 后继续等待。"
            else:
                session["message"] = "已扫码，等待重定向信息。"
        elif status == "binded_redirect":
            session["message"] = "该微信号已绑定当前 OpenClaw。"
            session["status"] = "created"
            self.mark_status(integration_id, "logged_in")
        elif status == "expired":
            session["message"] = "二维码已过期，请重新生成。"
            self.mark_status(integration_id, "failed", session["message"])
        elif status in {"cancel", "canceled", "denied", "verify_code_blocked"}:
            session["message"] = "登录被取消或验证失败，请重新发起扫码。"
            self.mark_status(integration_id, "failed", session["message"])
        elif status == "confirmed":
            token = str(data.get("bot_token") or "").strip()
            account_id = str(data.get("ilink_bot_id") or "").strip()
            if not token or not account_id:
                session["status"] = "error"
                session["message"] = "登录成功但微信接口未返回 token 或账号 ID。"
                self.mark_status(integration_id, "failed", session["message"])
            else:
                normalized_id = normalize_weixin_account_id(account_id)
                base_url = str(data.get("baseurl") or session.get("base_url") or DEFAULT_WEIXIN_OC_BASE_URL).rstrip("/")
                user_id = str(data.get("ilink_user_id") or "").strip()
                self.save_weixin_account(integration, normalized_id, token=token, base_url=base_url, user_id=user_id)
                session.update(
                    {
                        "status": "created",
                        "message": "登录成功，账号已保存。",
                        "account_id": normalized_id,
                        "base_url": base_url,
                        "user_id": user_id,
                    }
                )
                self.mark_status(integration_id, "logged_in")
                self.append_log(integration_id, f"[login] confirmed account={normalized_id} user={user_id or '-'}")
        else:
            session["message"] = f"微信返回状态：{status}"

        return {"ok": True, "login": self.public_login_session(integration_id)}

    def public_login_session(self, integration_id: str) -> dict:
        session = self._login_sessions.get(integration_id) or {}
        if not session:
            return {"status": "idle", "message": "当前没有进行中的扫码登录。"}
        return {
            "session_key": session.get("session_key", ""),
            "status": session.get("status", "wait"),
            "message": session.get("message", ""),
            "qrcode_img_content": session.get("qrcode_img_content", ""),
            "expires_at": session.get("expires_at", 0),
            "account_id": session.get("account_id", ""),
            "base_url": session.get("base_url", ""),
            "user_id": session.get("user_id", ""),
        }

    def local_token_list(self, integration: dict) -> list[str]:
        tokens = []
        for account in self.weixin_accounts(integration):
            path = account.get("path")
            if not path:
                continue
            data = read_json_file(Path(path), {})
            token = str(data.get("token") or "").strip() if isinstance(data, dict) else ""
            if token:
                tokens.append(token)
        return tokens[-10:]

    def save_weixin_account(self, integration: dict, account_id: str, *, token: str, base_url: str, user_id: str = "") -> None:
        state_dir = openclaw_state_dir(integration.get("openclaw_profile") or "branchwhisper")
        weixin_dir = state_dir / "openclaw-weixin"
        accounts_path = weixin_dir / "accounts.json"
        account_dir = weixin_dir / "accounts"
        account_dir.mkdir(parents=True, exist_ok=True)
        index = read_json_file(accounts_path, [])
        if not isinstance(index, list):
            index = []
        ids = [str(item) for item in index if str(item).strip()]
        if account_id not in ids:
            ids.append(account_id)
        accounts_path.write_text(json.dumps(ids, ensure_ascii=False, indent=2), encoding="utf-8")
        account_payload = {
            "token": token,
            "savedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "baseUrl": base_url or DEFAULT_WEIXIN_OC_BASE_URL,
            **({"userId": user_id} if user_id else {}),
        }
        account_file = account_dir / f"{account_id}.json"
        account_file.write_text(json.dumps(account_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def require_integration(self, integration_id: str) -> dict:
        integration = self.get_integration(integration_id)
        if not integration:
            raise KeyError(integration_id)
        return integration

    def session_conversation_id(self, platform_id: str, session_id: str, sender_id: str, store: ConversationStore) -> str:
        data = self.load_config()
        platform = safe_id(platform_id, fallback="platform")
        raw_key = f"{platform}:{session_id or sender_id or 'default'}:{sender_id or 'user'}"
        key = compact_text(raw_key, 180)
        existing_id = data["sessions"].get(key)
        if existing_id and store.load(existing_id):
            integration = self.get_integration(platform) or {}
            chat_name = compact_text(str(integration.get("chat_name") or ""), 48)
            if chat_name:
                store.update(existing_id, {"title": chat_name})
            return existing_id
        integration = self.get_integration(platform) or {}
        title = compact_text(str(integration.get("chat_name") or "我的微信聊天"), 48)
        conversation = store.create(
            title,
            metadata={"source": platform, "platform_id": platform, "sender_id": sender_id or ""},
        )
        data["sessions"][key] = conversation["id"]
        my_session = data.get("my_weixin_session") if isinstance(data.get("my_weixin_session"), dict) else {}
        if my_session.get("platform_id") == platform and my_session.get("sender_id") == (sender_id or ""):
            my_session["conversation_id"] = conversation["id"]
            data["my_weixin_session"] = my_session
        self.save_config(data)
        return conversation["id"]

    def forget_conversation(self, conversation_id: str) -> None:
        if not conversation_id:
            return
        data = self.load_config()
        changed = False
        sessions = data.get("sessions") if isinstance(data.get("sessions"), dict) else {}
        next_sessions = {key: value for key, value in sessions.items() if value != conversation_id}
        if len(next_sessions) != len(sessions):
            data["sessions"] = next_sessions
            changed = True
        my_session = data.get("my_weixin_session") if isinstance(data.get("my_weixin_session"), dict) else {}
        if my_session.get("conversation_id") == conversation_id:
            my_session["conversation_id"] = ""
            data["my_weixin_session"] = my_session
            changed = True
        if changed:
            self.save_config(data)


class ExternalDialogEngine:
    def __init__(
        self,
        integration_manager: IntegrationManager,
        conversation_store: ConversationStore,
        memory_store: MemoryStore,
        tool_manager: ToolManager,
        bot_profiles: BotProfileStore,
        media_dir: Path,
        sticker_store: StickerStore | None = None,
        sticker_policy: StickerPolicy | None = None,
    ):
        self.integration_manager = integration_manager
        self.conversation_store = conversation_store
        self.memory_store = memory_store
        self.tool_manager = tool_manager
        self.bot_profiles = bot_profiles
        self.media_dir = media_dir
        self.sticker_store = sticker_store
        self.sticker_policy = sticker_policy
        self.media_dir.mkdir(parents=True, exist_ok=True)

    def run_background(self, platform_id: str, trace_id: str, coro, label: str) -> None:
        task = asyncio.create_task(coro)

        def _log_failure(done: asyncio.Task) -> None:
            with contextlib.suppress(asyncio.CancelledError):
                exc = done.exception()
                if exc:
                    self.integration_manager.append_log(platform_id, f"[dialog:{trace_id}] {label} background failed: {exc}")

        task.add_done_callback(_log_failure)

    async def handle(self, payload: dict, settings: SessionSettings) -> dict:
        platform_id = safe_id(str(payload.get("platform_id") or payload.get("integration_id") or "weixin_personal"))
        session_id = str(payload.get("session_id") or payload.get("sender_id") or "default")
        sender_id = str(payload.get("sender_id") or session_id)
        text = str(payload.get("text") or "").strip()
        if not text:
            raise ValueError("text is required")

        integration = self.integration_manager.get_integration(platform_id) or self.integration_manager.get_integration("weixin_personal")
        keywords = (integration or {}).get("voice_trigger_keywords") or DEFAULT_VOICE_TRIGGERS
        bot_profile = self.bot_profiles.get((integration or {}).get("bot_profile_id") or "default")
        runtime_settings = self.settings_for_profile(settings, bot_profile)
        conversation_id = self.integration_manager.session_conversation_id(platform_id, session_id, sender_id, self.conversation_store)
        conversation = self.conversation_store.load(conversation_id)
        if not conversation:
            conversation = self.conversation_store.create(
                f"{platform_id} / {sender_id}",
                metadata={"source": platform_id, "platform_id": platform_id, "sender_id": sender_id},
            )
            conversation_id = conversation["id"]
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        if platform_id and sender_id and metadata.get("account_id"):
            self.integration_manager.bind_my_weixin_session(
                platform_id,
                account_id=str(metadata.get("account_id") or ""),
                session_id=session_id,
                sender_id=sender_id,
                conversation_id=conversation_id,
                context_token=str(metadata.get("context_token") or ""),
            )

        trace_id = f"ext_{uuid.uuid4().hex[:10]}"
        started_at = time.perf_counter()
        timings = {"receive_ms": 0, "tool_ms": 0, "llm_ms": 0, "tts_ms": 0, "memory_ms": 0, "send_ms": 0, "total_ms": 0}
        self.integration_manager.append_log(platform_id, f"[dialog:{trace_id}] recv sender={sender_id} text={compact_text(text, 220)}")
        image_context, image_attachments = await self.prepare_inbound_images(platform_id, trace_id, payload.get("images"), runtime_settings)
        image_vision_failed = any(
            item.get("ok") and str(item.get("error") or "").startswith("图片理解失败")
            for item in image_attachments
            if isinstance(item, dict)
        )
        image_has_description = any(
            bool(item.get("description"))
            for item in image_attachments
            if isinstance(item, dict)
        )
        model_text = text
        if image_context:
            guidance = ""
            if image_vision_failed and not image_has_description:
                guidance = "\n\n注意：微信图片已经下载成功，但图片理解服务没有连上。请直接说明“图片已收到，但图片理解服务没连上”，不要说微信密钥不对、不要把责任推给微信官方，也不要猜图片内容。"
            model_text = f"{text}\n\n{image_context}{guidance}" if text else f"{image_context}{guidance}"
        voice_requested = self.should_send_voice(text, keywords, integration)
        image_only_failed = image_vision_failed and not image_has_description and str(text or "").strip() in {"", "[图片]"}
        if image_only_failed:
            reply_text = "图片我已经收到了，但图片理解服务现在没连上，所以暂时看不出内容。这个不是微信密钥问题，先检查 Vision URL、模型服务和端口。"
            tool_result = None
            direct_answer = True
            tool_ms = 0
            llm_ms = 0
            reply_diag = {"image_vision_failed_direct_reply": True}
        else:
            reply_text, tool_result, direct_answer, tool_ms, llm_ms, reply_diag = await self.reply_text(runtime_settings, conversation, model_text, voice_requested=voice_requested)
        if voice_requested and not clean_for_tts(reply_text):
            reply_text = voice_fallback_reply(text)
            reply_diag["voice_empty_fallback"] = True
            self.integration_manager.append_log(platform_id, f"[dialog:{trace_id}] voice reply fallback used after empty cleanup")
        reply_parts = split_reply_messages(reply_text)
        timings["tool_ms"] = tool_ms
        timings["llm_ms"] = llm_ms
        messages_to_store = [
            {
                "role": "user",
                "content": model_text,
                "source": platform_id,
                "platform_id": platform_id,
                "sender_id": sender_id,
                "attachments": image_attachments,
            }
        ]
        assistant_attachments = [] if image_vision_failed else self.choose_reply_sticker(runtime_settings, conversation["id"], text, reply_text)
        if reply_text.strip():
            stored_parts = reply_parts or [reply_text]
            for index, part in enumerate(stored_parts):
                messages_to_store.append(
                    {
                        "role": "assistant",
                        "content": part,
                        "source": platform_id,
                        "platform_id": platform_id,
                        "bot_profile_id": bot_profile.get("id") or "default",
                        "attachments": assistant_attachments if index == len(stored_parts) - 1 else [],
                    }
                )
        self.conversation_store.append_messages(conversation["id"], messages_to_store, title_hint=text)
        if reply_text.strip():
            self.run_background(platform_id, trace_id, self.remember_turn(runtime_settings, text, reply_text), "memory")

        send_voice = voice_requested
        voice_file = ""
        voice_error = ""
        if send_voice:
            try:
                tts_start = time.perf_counter()
                voice_file = await self.synthesize_voice(runtime_settings, reply_text, trace_id)
                timings["tts_ms"] = int((time.perf_counter() - tts_start) * 1000)
            except Exception as exc:
                send_voice = False
                voice_error = str(exc)
                self.integration_manager.append_log(platform_id, f"[dialog:{trace_id}] tts failed: {exc}")

        timings["total_ms"] = int((time.perf_counter() - started_at) * 1000)
        self.integration_manager.runtime.setdefault(platform_id, {})["last_message_at"] = now_text()
        self.integration_manager.record_message_timing(
            platform_id,
            {
                "trace_id": trace_id,
                "sender_id": sender_id,
                "text": compact_text(text, 80),
                "conversation_id": conversation["id"],
                "session_id": session_id,
                "reply_len": len(reply_text),
                "tool": (tool_result or {}).get("tool") or "",
                "direct_answer": bool(direct_answer),
                "created_at": now_text(),
                **timings,
            },
        )
        self.integration_manager.append_log(
            platform_id,
            f"[dialog:{trace_id}] reply text_len={len(reply_text)} parts={len(reply_parts)} clean_len={len(clean_for_tts(reply_text))} diag={reply_diag} tool={(tool_result or {}).get('tool') or '-'} direct={bool(direct_answer)} timings={timings} voice_requested={voice_requested} send_voice={send_voice} voice_file={voice_file} voice_error={voice_error[:180]}",
        )
        return {
            "ok": True,
            "trace_id": trace_id,
            "platform_id": platform_id,
            "session_id": session_id,
            "sender_id": sender_id,
            "conversation_id": conversation["id"],
            "reply_text": reply_text,
            "reply_parts": reply_parts,
            "attachments": assistant_attachments,
            "voice_requested": voice_requested,
            "send_voice": send_voice,
            "voice_file": voice_file,
            "voice_format": "wav" if voice_file else "",
            "voice_error": voice_error,
            "tool_used": (tool_result or {}).get("tool") or "",
            "direct_answer": bool(direct_answer),
            "timings": timings,
        }

    def choose_reply_sticker(self, settings: SessionSettings, session_id: str, user_text: str, reply_text: str) -> list[dict]:
        if not self.sticker_store or not self.sticker_policy:
            return []
        intent = self.sticker_policy.choose_intent(
            settings,
            session_id=session_id or "weixin",
            user_text=user_text,
            reply_text=reply_text,
            source="weixin",
        )
        if not intent.get("send"):
            self.sticker_policy.mark_text_only(session_id or "weixin")
            return []
        sticker = self.sticker_store.choose(
            str(intent.get("tag") or ""),
            avoid_id=str(intent.get("avoid_id") or ""),
            channel="weixin",
        )
        if not sticker:
            self.sticker_policy.mark_text_only(session_id or "weixin")
            return []
        return [
            {
                "type": "sticker",
                "asset_id": sticker["id"],
                "url": sticker["url"],
                "path": sticker.get("send_path") or sticker.get("path") or "",
                "send_path": sticker.get("send_path") or "",
                "send_file": sticker.get("send_file") or "",
                "mime": sticker.get("mime") or "image/png",
                "tag": sticker.get("tag") or "",
                "pending_mark_used": True,
                "name": sticker.get("name") or "表情包",
            }
        ]

    def settings_for_profile(self, settings: SessionSettings, profile: dict) -> SessionSettings:
        snapshot = SessionSettings(**asdict(settings))
        if profile.get("system"):
            snapshot.system = str(profile["system"])
        if profile.get("tools_enabled") is False:
            snapshot.tools_enabled = False
        return snapshot

    async def prepare_inbound_images(self, platform_id: str, trace_id: str, images: Any, settings: SessionSettings) -> tuple[str, list[dict]]:
        if not isinstance(images, list) or not images:
            return "", []
        analyzer = ChatImageAnalyzer(settings)
        lines = []
        attachments = []
        for index, item in enumerate(images[:4], start=1):
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "").strip()
            mime = str(item.get("mime") or "image/jpeg")
            if item.get("ok") and path:
                mime = sniff_image_mime(Path(path), mime)
            attachment = {
                "type": "image",
                "path": path,
                "mime": mime,
                "source": "weixin",
                "ok": bool(item.get("ok")),
                "error": str(item.get("error") or ""),
            }
            if not item.get("ok"):
                error = str(item.get("error") or "微信图片下载失败")
                lines.append(f"第 {index} 张微信图片未能解析：{error}")
                attachments.append(attachment)
                continue
            try:
                description = await analyzer.describe(Path(path), mime=mime)
                attachment["description"] = description
                lines.append(f"第 {index} 张微信图片内容摘要：{description}")
            except Exception as exc:
                error = f"图片理解失败：{exc}"
                attachment["error"] = error
                lines.append(f"第 {index} 张微信图片已下载，但{error}。这不是微信图片密钥问题，而是图片理解 API 未连通或不可用。")
                self.integration_manager.append_log(platform_id, f"[dialog:{trace_id}] inbound image vision failed path={path} error={exc}")
            attachments.append(attachment)
        if not lines:
            return "", attachments
        return "微信图片解析结果：\n" + "\n".join(lines), attachments

    async def voice_test(self, integration_id: str, settings: SessionSettings, text: str, sender_id: str = "", account_id: str = "") -> dict:
        platform_id = safe_id(integration_id)
        text = clean_for_tts(text) or str(text or "").strip()
        if not text:
            raise ValueError("text is required")
        integration = self.integration_manager.require_integration(platform_id)
        target = self.integration_manager.select_weixin_target(platform_id, sender_id=sender_id, account_id=account_id)
        if not target:
            error = "我的微信会话未绑定或已超过 24 小时可触达窗口；请先用你的微信给 BranchWhisper 发一条消息。"
            self.integration_manager.append_log(platform_id, f"[voice-test] skipped: {error}")
            return {"ok": False, "stage": "target", "error": error}

        trace_id = f"voice_test_{uuid.uuid4().hex[:10]}"
        started_at = time.perf_counter()
        result: dict[str, Any] = {
            "ok": False,
            "trace_id": trace_id,
            "stage": "start",
            "text": text,
            "target": {
                "account_id": target["account_id"],
                "sender_id": target["sender_id"],
                "age_hours": target.get("age_hours"),
            },
            "tts_done": False,
            "send_done": False,
        }
        self.integration_manager.append_log(platform_id, f"[voice-test:{trace_id}] start account={target['account_id']} to={target['sender_id']} text={compact_text(text, 120)}")
        try:
            tts_started = time.perf_counter()
            voice_file = await self.synthesize_voice(settings, text, trace_id)
            result.update(
                {
                    "stage": "tts_done",
                    "tts_done": True,
                    "voice_file": voice_file,
                    "tts_ms": int((time.perf_counter() - tts_started) * 1000),
                }
            )
            send_started = time.perf_counter()
            sent = send_weixin_voice(
                base_url=target["base_url"],
                token=target["token"],
                to_user_id=target["sender_id"],
                voice_file=voice_file,
                text=text[:240],
                context_token=target["context_token"],
                cdn_base_url=str(target.get("cdn_base_url") or DEFAULT_WEIXIN_CDN_BASE_URL),
            )
            result.update(
                {
                    "ok": True,
                    "stage": "accepted",
                    "send_done": True,
                    "send_ms": int((time.perf_counter() - send_started) * 1000),
                    "total_ms": int((time.perf_counter() - started_at) * 1000),
                    "voice_message_id": sent.get("message_id") or "",
                    "voice_stage": sent.get("stage") or "accepted",
                    "voice_format": sent.get("transcode_format") or "",
                    "voice_diagnostic": {
                        "encode_type": sent.get("encode_type"),
                        "sample_rate": sent.get("sample_rate"),
                        "gain_db": sent.get("gain_db"),
                        "playtime_ms": sent.get("playtime_ms"),
                        "source_audio": sent.get("source_audio"),
                        "transcode_audio": sent.get("transcode_audio"),
                        "upload_ms": sent.get("upload_ms"),
                        "upload_method": sent.get("upload_method"),
                        "upload_url_kind": sent.get("upload_url_kind"),
                    },
                    "client_delivery": "unconfirmed",
                }
            )
            self.integration_manager.append_log(
                platform_id,
                f"[voice-test:{trace_id}] voice api accepted account={target['account_id']} to={target['sender_id']} "
                f"message_id={result['voice_message_id']} format={result['voice_format']} diagnostic={result['voice_diagnostic']}",
            )
            return result
        except (WeixinVoiceSendError, Exception) as exc:
            result.update(
                {
                    "ok": False,
                    "error": str(exc),
                    "total_ms": int((time.perf_counter() - started_at) * 1000),
                }
            )
            self.integration_manager.append_log(platform_id, f"[voice-test:{trace_id}] failed stage={result.get('stage')} error={exc}")
            return result

    async def sticker_test(self, integration_id: str, settings: SessionSettings, text: str, sender_id: str = "", account_id: str = "") -> dict:
        platform_id = safe_id(integration_id)
        text = str(text or "").strip() or "哈哈哈哈"
        target = self.integration_manager.select_weixin_target(platform_id, sender_id=sender_id, account_id=account_id)
        if not target:
            error = "我的微信会话未绑定或已超过 24 小时可触达窗口；请先用你的微信给 BranchWhisper 发一条消息。"
            self.integration_manager.append_log(platform_id, f"[sticker-test] skipped: {error}")
            return {"ok": False, "stage": "target", "error": error}
        if not self.sticker_store or not self.sticker_policy:
            return {"ok": False, "stage": "store", "error": "sticker store is not available"}

        trace_id = f"sticker_test_{uuid.uuid4().hex[:10]}"
        started_at = time.perf_counter()
        intent = self.sticker_policy.simulate(
            settings,
            session_id=f"sticker_test:weixin",
            user_text=text,
            reply_text="",
            source="weixin",
        )
        sticker = None
        if intent.get("send"):
            sticker = self.sticker_store.choose(
                str(intent.get("tag") or ""),
                avoid_id=str(intent.get("avoid_id") or ""),
                channel="weixin",
            )
        if not sticker:
            return {"ok": False, "stage": "sticker", "intent": intent, "error": "没有匹配当前语境的微信表情素材。请检查素材 OCR、标签和适用场景。"}

        result: dict[str, Any] = {
            "ok": False,
            "trace_id": trace_id,
            "stage": "start",
            "target": {
                "account_id": target["account_id"],
                "sender_id": target["sender_id"],
                "age_hours": target.get("age_hours"),
            },
            "intent": intent,
            "sticker": {
                "id": sticker.get("id"),
                "name": sticker.get("name"),
                "tag": sticker.get("tag"),
                "mime": sticker.get("mime"),
                "url": sticker.get("url"),
                "send_file": sticker.get("send_file") or "",
            },
        }
        self.integration_manager.append_log(
            platform_id,
            f"[sticker-test:{trace_id}] start account={target['account_id']} to={target['sender_id']} sticker={sticker.get('id')}",
        )
        try:
            send_started = time.perf_counter()
            sent = send_weixin_image(
                base_url=target["base_url"],
                token=target["token"],
                to_user_id=target["sender_id"],
                image_file=str(sticker.get("send_path") or sticker.get("path") or ""),
                context_token=target["context_token"],
                cdn_base_url=str(target.get("cdn_base_url") or DEFAULT_WEIXIN_CDN_BASE_URL),
            )
            self.sticker_policy.mark_sent(f"sticker_test:weixin", str(sticker.get("id") or ""))
            self.sticker_store.mark_used(str(sticker.get("id") or ""))
            result.update(
                {
                    "ok": True,
                    "stage": "accepted",
                    "send_done": True,
                    "send_ms": int((time.perf_counter() - send_started) * 1000),
                    "total_ms": int((time.perf_counter() - started_at) * 1000),
                    "image_message_id": sent.get("message_id") or "",
                    "image_stage": sent.get("stage") or "accepted",
                    "image_diagnostic": {
                        "image_format": sent.get("image_format"),
                        "source_image": sent.get("source_image"),
                        "image": sent.get("image"),
                        "thumbnail": sent.get("thumbnail"),
                        "thumbnail_skipped": sent.get("thumbnail_skipped"),
                        "cipher_size": sent.get("cipher_size"),
                        "mid_size": sent.get("mid_size"),
                        "media_aes_key_format": sent.get("media_aes_key_format"),
                        "upload_ms": sent.get("upload_ms"),
                        "upload_method": sent.get("upload_method"),
                        "upload_url_kind": sent.get("upload_url_kind"),
                        "send_ms": sent.get("send_ms"),
                    },
                    "client_delivery": "unconfirmed",
                }
            )
            self.integration_manager.append_log(
                platform_id,
                f"[sticker-test:{trace_id}] image api accepted account={target['account_id']} to={target['sender_id']} "
                f"message_id={result['image_message_id']} diagnostic={result['image_diagnostic']}",
            )
            return result
        except (WeixinImageSendError, Exception) as exc:
            payload = getattr(exc, "payload", {}) if isinstance(exc, Exception) else {}
            result.update(
                {
                    "ok": False,
                    "error": str(exc),
                    "image_diagnostic": payload if isinstance(payload, dict) else {},
                    "total_ms": int((time.perf_counter() - started_at) * 1000),
                }
            )
            self.integration_manager.append_log(platform_id, f"[sticker-test:{trace_id}] failed stage={result.get('stage')} error={exc}")
            return result

    async def reply_text(self, settings: SessionSettings, conversation: dict, user_text: str, *, voice_requested: bool = False) -> tuple[str, dict | None, str, int, int, dict]:
        diag: dict[str, Any] = {}
        repeat_text = extract_repeat_text(user_text)
        if repeat_text:
            reply = format_reply_paragraphs(clean_reply_text(repeat_text) or repeat_text)
            return reply, None, "", 0, 0, diag

        request_text = self.build_request_text(user_text, conversation)
        if voice_requested:
            request_text += "\n\n本轮任务：用户要听语音。请直接写一段会被朗读的自然内容，不要拒绝，不要说自己是文字 AI。"
        tool_started = time.perf_counter()
        tool_result = await self.maybe_execute_tool(settings, user_text)
        tool_ms = int((time.perf_counter() - tool_started) * 1000) if tool_result else 0
        direct_answer = direct_answer_from_tool(tool_result)
        if direct_answer:
            reply = format_reply_paragraphs(clean_reply_text(direct_answer) or direct_answer)
            return reply, tool_result, direct_answer, tool_ms, 0, diag
        if tool_result:
            request_text += (
                "\n\n联网/API 工具结果如下。请基于结果回答用户；如果结果不足或失败，要自然说明不确定，不要编造：\n"
                + json.dumps(tool_result, ensure_ascii=False)
            )
        messages = self.build_messages(settings, conversation, user_text, request_text, voice_requested=voice_requested)
        llm_started = time.perf_counter()
        answer = await self.complete_llm_text(settings, messages, temperature=active_temperature(settings), max_tokens=active_max_tokens(settings), diag=diag)
        llm_ms = int((time.perf_counter() - llm_started) * 1000)
        reply = format_reply_paragraphs(clean_reply_text(answer) or answer.strip())
        if voice_requested and not clean_for_tts(reply):
            reply = voice_fallback_reply(user_text)
            diag["voice_empty_fallback"] = True
        diag["reply_len"] = len(reply)
        diag["reply_clean_len"] = len(clean_for_tts(reply))
        return reply, tool_result, "", tool_ms, llm_ms, diag

    def build_messages(self, settings: SessionSettings, conversation: dict, user_text: str, request_text: str, *, voice_requested: bool = False) -> list[dict[str, str]]:
        messages = [{"role": "system", "content": settings.system}]
        history = conversation.get("messages") or []
        max_history = max(2, active_history_turns(settings) * 2)
        for item in history[-max_history:]:
            role = item.get("role")
            content = item.get("content")
            if role in {"user", "assistant"} and content:
                messages.append({"role": role, "content": content})

        memory_context = self.memory_store.format_context(settings, user_text, mode=memory_mode(settings))
        system_text = messages[0]["content"]
        if memory_context:
            system_text += "\n\n" + memory_context
        system_text += "\n\n当前消息来自外部平台接入。回复要短、自然，默认适合文字聊天；除非上下文需要，不要解释系统实现。"
        if voice_requested:
            system_text += (
                "\n\n本轮用户明确请求语音回复。系统会把你的回复转换成语音并发送，"
                "你只需要自然输出要说出口的内容。不要说自己不能发语音，不要说只有打字形态，"
                "不要说自己是文字 AI，不要说用户听不见，不要解释技术实现，也不要拒绝。回复尽量口语化、短句。"
            )

        recent_user = [compact_text(item.get("content", "")) for item in history[-6:] if item.get("role") == "user"]
        if recent_user and compact_text(user_text) == recent_user[-1]:
            system_text += "\n用户重复了上一轮问题，请换一种说法回答，避免原句复用。"
        recent_assistant = [compact_text(item.get("content", ""), 90) for item in history[-8:] if item.get("role") == "assistant"]
        if recent_assistant:
            system_text += "\n最近你已经说过这些回复片段，请避免原句复用：\n" + "\n".join(f"- {item}" for item in recent_assistant[-3:])
        messages[0] = {"role": "system", "content": system_text}
        messages.append({"role": "user", "content": request_text})
        return messages

    def build_request_text(self, text: str, conversation: dict) -> str:
        if not is_story_request(text):
            return text
        return (
            text
            + "\n\nTask: The user is asking for a bedtime/story. Directly tell a warm, coherent Chinese story for voice reading. "
            "Start with one short Chinese sentence under 8 Chinese characters. Then continue the story in 100 to 180 Chinese characters. "
            "Do not give sleep advice, do not ask the user a question, and do not output END."
        )

    async def maybe_execute_tool(self, settings: SessionSettings, user_text: str) -> dict | None:
        if not settings.tools_enabled:
            return None
        call = self.tool_manager.suggest_from_text(user_text)
        if not call:
            return None
        tool_id = call.get("id") or ""
        if not self.tool_manager.tool_exists(tool_id):
            return None
        arguments = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
        try:
            result = await self.tool_manager.execute(
                tool_id,
                arguments,
                timeout=settings.tools_timeout,
                max_chars=settings.tools_max_result_chars,
            )
        except Exception as exc:
            result = {"ok": False, "error": str(exc)}
        return {"tool": tool_id, "arguments": arguments, "result": result}

    async def remember_turn(self, settings: SessionSettings, user_text: str, assistant_text: str) -> None:
        try:
            await self.memory_store.observe_turn(
                settings,
                user_text,
                assistant_text,
                lambda prompt: self.extract_memories(settings, prompt),
                mode=memory_mode(settings),
            )
        except Exception as exc:
            print(f"[integration-memory] update failed: {exc}", flush=True)

    async def extract_memories(self, settings: SessionSettings, prompt: str) -> str:
        messages = [
            {"role": "system", "content": "你是记忆抽取器。只输出 JSON 数组，不输出解释、Markdown 或多余文字。"},
            {"role": "user", "content": prompt},
        ]
        return await self.complete_llm_text(settings, messages, temperature=0.0, max_tokens=420, timeout=10.0)

    async def complete_llm_text(
        self,
        settings: SessionSettings,
        messages: list[dict[str, str]],
        temperature: float = 0.0,
        max_tokens: int = 260,
        timeout: float | None = None,
        diag: dict | None = None,
    ) -> str:
        settings_snapshot = SessionSettings(**asdict(settings))
        payload = {
            "model": active_llm_model(settings_snapshot),
            "messages": messages,
            "stream": False,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        thinking_requested = bool(getattr(settings_snapshot, "thinking_enabled", False))
        if thinking_requested:
            payload["enable_thinking"] = True
        async with httpx.AsyncClient(timeout=timeout or settings_snapshot.tools_timeout) as client:
            resp = await client.post(active_llm_url(settings_snapshot), json=payload, headers=llm_headers(settings_snapshot))
            if resp.status_code == 400 and payload.pop("enable_thinking", None):
                resp = await client.post(active_llm_url(settings_snapshot), json=payload, headers=llm_headers(settings_snapshot))
            resp.raise_for_status()
            data = resp.json()
            text = clean_reply_text(extract_chat_message_text(data))
            finish_reason = extract_finish_reason(data)
            if thinking_requested and not clean_for_tts(text):
                if diag is not None:
                    diag["thinking_empty_retry"] = True
                payload.pop("enable_thinking", None)
                resp = await client.post(active_llm_url(settings_snapshot), json=payload, headers=llm_headers(settings_snapshot))
                resp.raise_for_status()
                data = resp.json()
                text = clean_reply_text(extract_chat_message_text(data))
                finish_reason = extract_finish_reason(data)
        if finish_reason == "length":
            try:
                continuation = await self.complete_llm_continuation(
                    settings_snapshot,
                    messages,
                    text,
                    temperature=temperature,
                    timeout=timeout,
                )
            except Exception as exc:
                continuation = ""
                if diag is not None:
                    diag["finish_length_retry_error"] = str(exc)[:160]
            if continuation:
                text = clean_reply_text(text + continuation)
                if diag is not None:
                    diag["finish_length_retry"] = True
        if diag is not None:
            diag["finish_reason"] = finish_reason or "-"
            diag["llm_reply_len"] = len(text)
            diag["llm_clean_len"] = len(clean_for_tts(text))
        return text

    async def complete_llm_continuation(
        self,
        settings: SessionSettings,
        messages: list[dict[str, str]],
        partial_text: str,
        temperature: float = 0.0,
        timeout: float | None = None,
    ) -> str:
        if not partial_text:
            return ""
        retry_messages = list(messages) + [
            {"role": "assistant", "content": partial_text},
            {"role": "user", "content": "上一条回复在中间断掉了。请只从断掉处继续补完最后一句，不要重复前文。"},
        ]
        payload = {
            "model": active_llm_model(settings),
            "messages": retry_messages,
            "stream": False,
            "temperature": temperature,
            "max_tokens": min(220, max(80, active_max_tokens(settings))),
        }
        async with httpx.AsyncClient(timeout=timeout or settings.tools_timeout) as client:
            resp = await client.post(active_llm_url(settings), json=payload, headers=llm_headers(settings))
        resp.raise_for_status()
        return clean_reply_text(extract_chat_message_text(resp.json()))

    def should_send_voice(self, user_text: str, keywords: list[str], integration: dict | None) -> bool:
        if (integration or {}).get("reply_mode") == "voice":
            return True
        normalized = re.sub(r"\s+", "", user_text or "")
        if VOICE_NEGATIVE_RE.search(normalized):
            return False
        for keyword in keywords:
            normalized_keyword = re.sub(r"\s+", "", str(keyword))
            if not normalized_keyword:
                continue
            if normalized_keyword in normalized:
                return True
        if normalized in {"语音", "说话", "你说话", "说两句", "说一句", "说呀", "说嘛", "说吗", "快说", "那你快说呀"}:
            return True
        return bool(VOICE_INTENT_RE.search(normalized))

    async def synthesize_voice(self, settings: SessionSettings, text: str, trace_id: str) -> str:
        text = clean_for_tts(text)
        if not text:
            raise ValueError("empty text after tts cleanup")
        if not settings.tts_enabled:
            raise RuntimeError("TTS is disabled")
        payload = {
            "text": text,
            "stream": True,
            "speed": settings.tts_speed,
            "seed": settings.tts_seed,
        }
        pcm = bytearray()
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("POST", settings.tts_url, json=payload) as resp:
                resp.raise_for_status()
                async for chunk in resp.aiter_bytes():
                    if chunk:
                        pcm.extend(chunk)
        if len(pcm) % 2:
            pcm = pcm[:-1]
        if not pcm:
            raise RuntimeError("TTS returned empty audio")
        wav = wav_bytes_from_pcm16(bytes(pcm), settings.tts_sample_rate)
        path = self.media_dir / f"{trace_id}.wav"
        path.write_bytes(wav)
        return str(path)
