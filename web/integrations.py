from __future__ import annotations

import asyncio
import base64
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

from audio_pipeline import clean_for_tts, extract_chat_message_text
from config import SessionSettings, llm_headers
from conversations import ConversationStore
from direct_answers import direct_answer_from_tool
from profiles import BotProfileStore
from runtime_brain import MemoryStore, ToolManager


DEFAULT_VOICE_TRIGGERS = ["发语音", "说话", "念给我听", "语音回复", "我想听你说话"]
DEFAULT_WEIXIN_OC_BASE_URL = "https://ilinkai.weixin.qq.com"
DEFAULT_WEIXIN_OC_BOT_TYPE = "3"
DEFAULT_WEIXIN_OC_VERSION = "2.4.4"
ILINK_APP_ID = "bot"
SUPPORTED_TYPES = {"weixin_oc"}
RUNNING_STATES = {"starting", "running", "login"}


def now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def now_ts() -> float:
    return time.time()


def safe_id(value: str, fallback: str = "weixin_personal") -> str:
    value = re.sub(r"[^a-zA-Z0-9_\-]", "_", str(value or "")).strip("_")
    return value[:48] or fallback


def compact_text(text: str, limit: int = 600) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "..."


def openclaw_state_dir(profile: str) -> Path:
    env_dir = os.environ.get("OPENCLAW_STATE_DIR") or os.environ.get("CLAWDBOT_STATE_DIR")
    if env_dir:
        return Path(env_dir).expanduser()
    profile = safe_id(profile, fallback="branchwhisper")
    if profile and profile not in {"default", "main"}:
        return Path.home() / f".openclaw-{profile}"
    return Path.home() / ".openclaw"


def read_json_file(path: Path, fallback):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback


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


def is_story_request(text: str) -> bool:
    return any(keyword in text for keyword in ("故事", "睡前", "童话"))


def extract_repeat_text(text: str) -> str | None:
    prefixes = (
        "跟着我说",
        "跟我说",
        "跟我念",
        "照着我说",
        "复读",
        "重复",
        "请你重复",
        "请你跟着我说",
        "你跟着我说",
    )
    for prefix in prefixes:
        index = text.find(prefix)
        if index == -1:
            continue
        value = text[index + len(prefix) :].strip().lstrip("\u3000 \t\r\n，,:：")
        return value or None
    return None


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
                    "openclaw_profile": "branchwhisper",
                    "reply_mode": "text",
                    "voice_trigger_keywords": list(DEFAULT_VOICE_TRIGGERS),
                    "status": "stopped",
                    "last_error": "",
                    "last_login_at": "",
                }
            ],
            "sessions": {},
            "contacts": {},
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
        contacts = data.get("contacts")
        if isinstance(contacts, dict):
            base["contacts"] = {str(k): self.normalize_contact(v) for k, v in contacts.items() if isinstance(v, dict)}
        return base

    def save_config(self, data: dict) -> dict:
        payload = {
            "integrations": [self.normalize_integration(item) for item in data.get("integrations", []) if isinstance(item, dict)],
            "sessions": data.get("sessions") if isinstance(data.get("sessions"), dict) else {},
            "contacts": data.get("contacts") if isinstance(data.get("contacts"), dict) else {},
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
        item["contacts"] = self.integration_contacts(item["id"])
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

    def contact_key(self, integration_id: str, sender_id: str, account_id: str = "") -> str:
        return f"{safe_id(integration_id)}:{safe_id(account_id or 'account')}:{str(sender_id or 'unknown')}"

    def normalize_contact(self, item: dict) -> dict:
        return {
            "sender_id": str(item.get("sender_id") or ""),
            "account_id": str(item.get("account_id") or ""),
            "remark_name": str(item.get("remark_name") or ""),
            "display_name": str(item.get("display_name") or item.get("nickname") or ""),
            "avatar_url": str(item.get("avatar_url") or ""),
            "auto_avatar_url": str(item.get("auto_avatar_url") or ""),
            "updated_at": str(item.get("updated_at") or now_text()),
        }

    def integration_contacts(self, integration_id: str) -> list[dict]:
        data = self.load_config()
        prefix = f"{safe_id(integration_id)}:"
        items = []
        for key, item in (data.get("contacts") or {}).items():
            if str(key).startswith(prefix):
                items.append({"key": key, **self.normalize_contact(item)})
        items.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
        return items

    def get_contact(self, integration_id: str, sender_id: str, account_id: str = "") -> dict:
        data = self.load_config()
        key = self.contact_key(integration_id, sender_id, account_id)
        return self.normalize_contact((data.get("contacts") or {}).get(key, {"sender_id": sender_id, "account_id": account_id}))

    def update_contact(self, integration_id: str, sender_id: str, payload: dict, account_id: str = "") -> dict:
        data = self.load_config()
        key = self.contact_key(integration_id, sender_id, account_id or str(payload.get("account_id") or ""))
        current = self.normalize_contact((data.get("contacts") or {}).get(key, {"sender_id": sender_id, "account_id": account_id}))
        updated = self.normalize_contact({**current, **payload, "sender_id": sender_id, "updated_at": now_text()})
        data.setdefault("contacts", {})[key] = updated
        self.save_config(data)
        return {"key": key, **updated}

    def touch_contact_from_message(self, integration_id: str, sender_id: str, metadata: dict, account_id: str = "") -> dict:
        data = self.load_config()
        key = self.contact_key(integration_id, sender_id, account_id)
        current = self.normalize_contact((data.get("contacts") or {}).get(key, {"sender_id": sender_id, "account_id": account_id}))
        display_name = str(
            metadata.get("display_name")
            or metadata.get("nickname")
            or metadata.get("nick_name")
            or current.get("display_name")
            or ""
        )
        auto_avatar_url = str(
            metadata.get("avatar_url")
            or metadata.get("head_img_url")
            or metadata.get("portrait")
            or current.get("auto_avatar_url")
            or ""
        )
        updated = self.normalize_contact(
            {
                **current,
                "sender_id": sender_id,
                "account_id": account_id or current.get("account_id") or "",
                "display_name": display_name,
                "auto_avatar_url": auto_avatar_url,
                "updated_at": now_text(),
            }
        )
        data.setdefault("contacts", {})[key] = updated
        self.save_config(data)
        return {"key": key, **updated}

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
                return item
        timing = {"trace_id": trace_id, "created_at": now_text(), **sanitized}
        items.insert(0, timing)
        runtime["recent_timings"] = items[:10]
        return timing

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

    def environment_status(self) -> dict:
        if self._environment_cache and now_ts() - self._environment_cache_at < 180:
            return self._environment_cache
        tools = {}
        for name in ("node", "npm", "openclaw", "ffmpeg"):
            path = shutil.which(name)
            tools[name] = {
                "available": bool(path),
                "path": path or "",
                "version": self.tool_version(name) if path else "",
            }
        package = self.npm_package_version("@tencent-weixin/openclaw-weixin")
        cli_package = self.npm_package_version("@tencent-weixin/openclaw-weixin-cli")
        ready = all(tools[name]["available"] for name in ("node", "npm", "openclaw", "ffmpeg"))
        data = {"ready": ready, "tools": tools, "packages": {"openclaw_weixin": package, "openclaw_weixin_cli": cli_package}}
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

    async def install_weixin_cli(self, integration_id: str) -> dict:
        integration = self.require_integration(integration_id)
        return await self.run_command(integration, ["npx", "-y", "@tencent-weixin/openclaw-weixin-cli", "install"], timeout=600)

    async def gateway_action(self, integration_id: str, action: str) -> dict:
        integration = self.require_integration(integration_id)
        if action not in {"start", "stop", "restart", "status"}:
            raise ValueError("unsupported gateway action")
        result = await self.run_openclaw(integration, ["gateway", action], timeout=45)
        if action == "start" and result["ok"]:
            self.mark_status(integration["id"], "running")
        elif action == "stop" and result["ok"]:
            self.mark_status(integration["id"], "stopped")
        elif action == "restart" and result["ok"]:
            self.mark_status(integration["id"], "running")
        elif not result["ok"]:
            self.mark_status(integration["id"], "failed", result["stderr"] or result["stdout"])
        return result

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
        self.runtime[integration_id]["started_at"] = now_text()
        self.runtime[integration_id]["command"] = command
        self.mark_status(integration_id, status)
        return {"ok": True, "status": status, "pid": proc.pid, "log_file": str(path)}

    def stop_process(self, integration_id: str) -> dict:
        integration_id = safe_id(integration_id)
        proc = self.processes.get(integration_id)
        if not proc or proc.poll() is not None:
            self.mark_status(integration_id, "stopped")
            return {"ok": True, "status": "stopped"}
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        self.mark_status(integration_id, "stopped")
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
            return existing_id
        title = compact_text(f"{platform} / {sender_id or session_id or '外部会话'}", 36)
        conversation = store.create(title)
        data["sessions"][key] = conversation["id"]
        self.save_config(data)
        return conversation["id"]


class ExternalDialogEngine:
    def __init__(
        self,
        integration_manager: IntegrationManager,
        conversation_store: ConversationStore,
        memory_store: MemoryStore,
        tool_manager: ToolManager,
        bot_profiles: BotProfileStore,
        media_dir: Path,
    ):
        self.integration_manager = integration_manager
        self.conversation_store = conversation_store
        self.memory_store = memory_store
        self.tool_manager = tool_manager
        self.bot_profiles = bot_profiles
        self.media_dir = media_dir
        self.media_dir.mkdir(parents=True, exist_ok=True)

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
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        account_id = str(metadata.get("account_id") or "")
        contact = self.integration_manager.touch_contact_from_message(platform_id, sender_id, metadata, account_id)
        conversation_id = self.integration_manager.session_conversation_id(platform_id, session_id, sender_id, self.conversation_store)
        conversation = self.conversation_store.load(conversation_id) or self.conversation_store.create(f"{platform_id} / {sender_id}")

        trace_id = f"ext_{uuid.uuid4().hex[:10]}"
        started_at = time.perf_counter()
        timings = {"receive_ms": 0, "tool_ms": 0, "llm_ms": 0, "tts_ms": 0, "send_ms": 0, "total_ms": 0}
        self.integration_manager.append_log(platform_id, f"[dialog:{trace_id}] recv sender={sender_id} text={compact_text(text, 220)}")
        reply_text, tool_result, direct_answer, tool_ms, llm_ms = await self.reply_text(runtime_settings, conversation, text)
        timings["tool_ms"] = tool_ms
        timings["llm_ms"] = llm_ms
        self.conversation_store.append_messages(
            conversation["id"],
            [
                {
                    "role": "user",
                    "content": text,
                    "source": platform_id,
                    "platform_id": platform_id,
                    "sender_id": sender_id,
                    "display_name": contact.get("remark_name") or contact.get("display_name") or sender_id,
                    "avatar_url": contact.get("avatar_url") or contact.get("auto_avatar_url") or "",
                },
                {
                    "role": "assistant",
                    "content": reply_text,
                    "source": platform_id,
                    "platform_id": platform_id,
                    "display_name": bot_profile.get("name") or "枝语",
                    "avatar_url": bot_profile.get("avatar_url") or "",
                    "bot_profile_id": bot_profile.get("id") or "default",
                },
            ],
            title_hint=text,
        )
        await self.remember_turn(runtime_settings, text, reply_text)

        send_voice = self.should_send_voice(text, keywords, integration)
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
                "reply_len": len(reply_text),
                "tool": (tool_result or {}).get("tool") or "",
                "direct_answer": bool(direct_answer),
                "created_at": now_text(),
                **timings,
            },
        )
        self.integration_manager.append_log(
            platform_id,
            f"[dialog:{trace_id}] reply text_len={len(reply_text)} tool={(tool_result or {}).get('tool') or '-'} direct={bool(direct_answer)} timings={timings} send_voice={send_voice} voice_file={voice_file}",
        )
        return {
            "ok": True,
            "trace_id": trace_id,
            "platform_id": platform_id,
            "session_id": session_id,
            "sender_id": sender_id,
            "conversation_id": conversation["id"],
            "reply_text": reply_text,
            "send_voice": send_voice,
            "voice_file": voice_file,
            "voice_error": voice_error,
            "tool_used": (tool_result or {}).get("tool") or "",
            "direct_answer": bool(direct_answer),
            "timings": timings,
        }

    def settings_for_profile(self, settings: SessionSettings, profile: dict) -> SessionSettings:
        snapshot = SessionSettings(**asdict(settings))
        if profile.get("system"):
            snapshot.system = str(profile["system"])
        if profile.get("tools_enabled") is False:
            snapshot.tools_enabled = False
        return snapshot

    async def reply_text(self, settings: SessionSettings, conversation: dict, user_text: str) -> tuple[str, dict | None, str, int, int]:
        repeat_text = extract_repeat_text(user_text)
        if repeat_text:
            return clean_for_tts(repeat_text) or repeat_text, None, "", 0, 0

        request_text = self.build_request_text(user_text, conversation)
        tool_started = time.perf_counter()
        tool_result = await self.maybe_execute_tool(settings, user_text)
        tool_ms = int((time.perf_counter() - tool_started) * 1000) if tool_result else 0
        direct_answer = direct_answer_from_tool(tool_result)
        if direct_answer:
            return clean_for_tts(direct_answer) or direct_answer, tool_result, direct_answer, tool_ms, 0
        if tool_result:
            request_text += (
                "\n\n联网/API 工具结果如下。请基于结果回答用户；如果结果不足或失败，要自然说明不确定，不要编造：\n"
                + json.dumps(tool_result, ensure_ascii=False)
            )
        messages = self.build_messages(settings, conversation, user_text, request_text)
        llm_started = time.perf_counter()
        answer = await self.complete_llm_text(settings, messages, temperature=settings.temperature, max_tokens=settings.max_tokens)
        llm_ms = int((time.perf_counter() - llm_started) * 1000)
        return clean_for_tts(answer) or answer.strip(), tool_result, "", tool_ms, llm_ms

    def build_messages(self, settings: SessionSettings, conversation: dict, user_text: str, request_text: str) -> list[dict[str, str]]:
        messages = [{"role": "system", "content": settings.system}]
        history = conversation.get("messages") or []
        max_history = max(2, settings.history_turns * 2)
        for item in history[-max_history:]:
            role = item.get("role")
            content = item.get("content")
            if role in {"user", "assistant"} and content:
                messages.append({"role": role, "content": content})

        memory_context = self.memory_store.format_context(settings, user_text)
        system_text = messages[0]["content"]
        if memory_context:
            system_text += "\n\n" + memory_context
        system_text += "\n\n当前消息来自外部平台接入。回复要短、自然，默认适合文字聊天；除非上下文需要，不要解释系统实现。"

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
            await self.memory_store.observe_turn(settings, user_text, assistant_text, lambda prompt: self.extract_memories(settings, prompt))
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
    ) -> str:
        settings_snapshot = SessionSettings(**asdict(settings))
        payload = {
            "model": settings_snapshot.llm_model,
            "messages": messages,
            "stream": False,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        async with httpx.AsyncClient(timeout=timeout or settings_snapshot.tools_timeout) as client:
            resp = await client.post(settings_snapshot.llm_url, json=payload, headers=llm_headers(settings_snapshot))
        resp.raise_for_status()
        return extract_chat_message_text(resp.json())

    def should_send_voice(self, user_text: str, keywords: list[str], integration: dict | None) -> bool:
        if (integration or {}).get("reply_mode") == "voice":
            return True
        return any(keyword and keyword in user_text for keyword in keywords)

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
