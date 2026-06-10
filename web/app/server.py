from __future__ import annotations

import argparse
import asyncio
import json
import os
import contextlib
import time
from dataclasses import asdict
from pathlib import Path
import sys

import httpx
from fastapi import Body, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from media.avatars import AvatarStore
from core.config import (
    SessionSettings,
    add_settings_args,
    enable_default_capabilities,
    llm_headers,
    load_persisted_settings,
    public_settings,
    save_persisted_settings,
    update_llm_api_key,
)
from data.conversations import ConversationStore
from dialog.session import DialogSession
from integration_runtime.manager import ExternalDialogEngine, IntegrationManager
from media.assets import ChatImageStore, StickerStore
from data.profiles import BotProfileStore
from engagement.proactive import FollowupPolicy, ProactiveStore
from engagement.reminders import ReminderStore
from tools.runtime_brain import MemoryStore, ToolManager
from service_runtime.services import ServiceManager, check_service, health_url_from
from media.sticker_policy import StickerPolicy
from service_runtime.system_resources import collect_system_resources
from core.tool_config import ToolProviderConfig
from service_runtime.vad import VadModelStore


# Backend data flow:
# 1. Browser sends 16 kHz float32 PCM blocks over WebSocket.
# 2. This server runs Silero VAD and cuts the stream into utterances.
# 3. Each utterance is sent to Qwen3-ASR, then the text goes to llama.cpp.
# 4. LLM text is split into small TTS segments and CosyVoice PCM is streamed
#    back to the browser as binary WebSocket frames.
STATIC_DIR = APP_DIR / "static"
RUNTIME_DIR = APP_DIR / "runtime"
LOG_DIR = RUNTIME_DIR / "logs"
CONVERSATION_DIR = RUNTIME_DIR / "conversations"
SETTINGS_CONFIG = RUNTIME_DIR / "settings.json"
SERVICE_PROFILES_CONFIG = RUNTIME_DIR / "service_profiles.json"
MEMORY_DB = RUNTIME_DIR / "memory.sqlite3"
TOOLS_CONFIG = RUNTIME_DIR / "tools.json"
INTEGRATIONS_CONFIG = RUNTIME_DIR / "integrations.json"
INTEGRATION_MEDIA_DIR = RUNTIME_DIR / "integration_media"
TOOL_PROVIDERS_CONFIG = RUNTIME_DIR / "tool_providers.json"
BOT_PROFILES_CONFIG = RUNTIME_DIR / "bot_profiles.json"
REMINDERS_DB = RUNTIME_DIR / "reminders.sqlite3"
PROACTIVE_CONFIG = RUNTIME_DIR / "proactive_config.json"
PROACTIVE_DB = RUNTIME_DIR / "proactive.sqlite3"
UPLOAD_DIR = RUNTIME_DIR / "uploads"
AVATAR_DIR = UPLOAD_DIR / "avatars"
CHAT_IMAGE_DIR = UPLOAD_DIR / "chat_images"
STICKER_DIR = UPLOAD_DIR / "stickers"
STICKERS_CONFIG = RUNTIME_DIR / "stickers.json"

SERVICE_WARMUP_LOCK = asyncio.Lock()
SERVICE_WARMUP_TASKS: dict[str, asyncio.Task] = {}
SERVICE_WARMUP_DONE: set[str] = set()
SERVICE_WARMUP_STATUS: dict[str, dict] = {}
LOCALHOST_NAMES = {"127.0.0.1", "::1", "localhost"}

# TTS output from the local CosyVoice endpoint is expected to be raw PCM16LE.
# These defaults reduce clipping/pops when the HTTP stream is forwarded to the
# browser over WebSocket. They do not change the model, only the transported PCM.
def is_local_request(request: Request) -> bool:
    host = request.client.host if request.client else ""
    return host in LOCALHOST_NAMES


def require_local_service_control(request: Request) -> None:
    if (
        is_local_request(request)
        or os.environ.get("BRANCHWHISPER_ALLOW_REMOTE_SERVICE_CONTROL") == "1"
        or os.environ.get("BUDING_ALLOW_REMOTE_SERVICE_CONTROL") == "1"
    ):
        return
    raise HTTPException(
        status_code=403,
        detail=(
            "Service control is restricted to localhost. "
            "Set BRANCHWHISPER_ALLOW_REMOTE_SERVICE_CONTROL=1 to override."
        ),
    )


def require_integration_dialog_access(request: Request) -> None:
    if is_local_request(request) or os.environ.get("BRANCHWHISPER_ALLOW_REMOTE_INTEGRATION_DIALOG") == "1":
        return
    raise HTTPException(
        status_code=403,
        detail=(
            "Integration dialog is restricted to localhost. "
            "Set BRANCHWHISPER_ALLOW_REMOTE_INTEGRATION_DIALOG=1 to allow remote bridge calls."
        ),
    )


def local_branchwhisper_url(request: Request) -> str:
    override = (
        os.environ.get("BRANCHWHISPER_BRIDGE_URL")
        or os.environ.get("BUDING_BRIDGE_URL")
        or os.environ.get("BRANCHWHISPER_PUBLIC_URL")
        or os.environ.get("BUDING_PUBLIC_URL")
    )
    if override:
        return override.rstrip("/")
    app_port = getattr(request.app.state, "server_port", None)
    if app_port:
        return f"http://127.0.0.1:{int(app_port)}"
    server = request.scope.get("server") or ("", 7860)
    port = server[1] if isinstance(server, (tuple, list)) and len(server) > 1 else request.url.port
    return f"http://127.0.0.1:{int(port or 7860)}"


def unique_urls(urls: list[str]) -> list[str]:
    seen = set()
    result = []
    for url in urls:
        normalized = str(url or "").rstrip("/")
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


async def resolve_branchwhisper_url(request: Request, preferred: str = "") -> str:
    override = os.environ.get("BRANCHWHISPER_BRIDGE_URL") or os.environ.get("BUDING_BRIDGE_URL")
    if override:
        return override.rstrip("/")

    candidates = unique_urls(
        [
            preferred,
            local_branchwhisper_url(request),
            "http://127.0.0.1:7860",
        ]
    )
    for url in candidates:
        try:
            async with httpx.AsyncClient(timeout=1.5) as client:
                response = await client.get(f"{url}/api/health")
            if response.status_code < 500:
                return url
        except Exception:
            continue
    return candidates[0] if candidates else "http://127.0.0.1:7860"


def service_warmup_key(service_id: str, settings: SessionSettings) -> str:
    if service_id == "asr":
        return f"asr:{settings.asr_url}:{settings.asr_model}"
    if service_id == "llm":
        return f"llm:{settings.llm_url}:{settings.llm_model}"
    return service_id


def set_warmup_status(
    service_id: str,
    key: str,
    state: str,
    *,
    attempt: int = 0,
    error: str = "",
) -> None:
    SERVICE_WARMUP_STATUS[service_id] = {
        "service": service_id,
        "key": key,
        "state": state,
        "attempt": attempt,
        "error": error,
        "updated_at": time.time(),
    }


def warmup_statuses() -> dict:
    return {service_id: dict(status) for service_id, status in SERVICE_WARMUP_STATUS.items()}


def clear_warmup_status(service_id: str | None = None) -> None:
    service_ids = {service_id} if service_id else {"asr", "llm"}
    for sid in service_ids:
        SERVICE_WARMUP_STATUS.pop(sid, None)

    for key, task in list(SERVICE_WARMUP_TASKS.items()):
        if any(key.startswith(f"{sid}:") for sid in service_ids):
            task.cancel()
            SERVICE_WARMUP_TASKS.pop(key, None)

    for key in list(SERVICE_WARMUP_DONE):
        if any(key.startswith(f"{sid}:") for sid in service_ids):
            SERVICE_WARMUP_DONE.discard(key)


def attach_service_warmups(services: list[dict]) -> list[dict]:
    statuses = warmup_statuses()
    for service in services:
        warmup = statuses.get(service.get("id"))
        if not warmup:
            continue
        service["warmup"] = warmup
        if warmup.get("state") in {"queued", "warming", "retrying"} and service.get("state") == "ready":
            service["state"] = "warming"
    return services


def service_warmup_specs(settings: SessionSettings) -> dict:
    return {
        "asr": (
            service_warmup_key("asr", settings),
            lambda s=settings: warmup_asr(s),
        ),
        "llm": (
            service_warmup_key("llm", settings),
            lambda s=settings: warmup_llm(s),
        ),
    }


def queue_service_warmup_locked(service_id: str, key: str, warmup_factory) -> None:
    if key in SERVICE_WARMUP_DONE:
        set_warmup_status(service_id, key, "ready")
        return
    task = SERVICE_WARMUP_TASKS.get(key)
    if task and not task.done():
        return
    set_warmup_status(service_id, key, "queued")
    SERVICE_WARMUP_TASKS[key] = asyncio.create_task(run_service_warmup(service_id, key, warmup_factory))


async def schedule_service_warmup(service_id: str, settings: SessionSettings) -> None:
    settings_snapshot = SessionSettings(**asdict(settings))
    spec = service_warmup_specs(settings_snapshot).get(service_id)
    if not spec:
        return
    key, warmup_factory = spec
    async with SERVICE_WARMUP_LOCK:
        queue_service_warmup_locked(service_id, key, warmup_factory)


async def schedule_service_warmups(settings: SessionSettings) -> None:
    settings_snapshot = SessionSettings(**asdict(settings))
    async with SERVICE_WARMUP_LOCK:
        for service_id, (key, warmup_factory) in service_warmup_specs(settings_snapshot).items():
            queue_service_warmup_locked(service_id, key, warmup_factory)


async def run_service_warmup(service_id: str, key: str, warmup_factory) -> None:
    last_error: Exception | None = None
    try:
        for attempt in range(1, 13):
            try:
                set_warmup_status(service_id, key, "warming", attempt=attempt)
                await warmup_factory()
                SERVICE_WARMUP_DONE.add(key)
                set_warmup_status(service_id, key, "ready", attempt=attempt)
                return
            except Exception as exc:
                last_error = exc
                set_warmup_status(service_id, key, "retrying", attempt=attempt, error=str(exc))
                if attempt < 12:
                    await asyncio.sleep(5)
        if last_error:
            set_warmup_status(service_id, key, "failed", attempt=12, error=str(last_error))
            print(f"[warmup] {key} failed: {last_error}", flush=True)
    finally:
        SERVICE_WARMUP_TASKS.pop(key, None)


async def warmup_asr(settings: SessionSettings) -> None:
    silence = np.zeros(int(MIC_SAMPLE_RATE * 0.35), dtype=np.float32)
    wav = wav_bytes_from_float32(silence)
    await asyncio.wait_for(transcribe_audio(settings, wav), timeout=min(35.0, float(settings.asr_timeout)))


async def warmup_llm(settings: SessionSettings) -> None:
    payload = {
        "model": settings.llm_model,
        "messages": [
            {"role": "system", "content": "Reply with exactly one short token."},
            {"role": "user", "content": "warmup"},
        ],
        "stream": False,
        "temperature": 0.0,
        "max_tokens": 1,
    }
    async with httpx.AsyncClient(timeout=35.0) as client:
        resp = await client.post(settings.llm_url, json=payload, headers=llm_headers(settings))
    resp.raise_for_status()


async def reminder_loop(app: FastAPI) -> None:
    while True:
        try:
            for reminder in app.state.reminder_store.due():
                content = "\u63d0\u9192\uff1a" + str(reminder.get("content") or reminder.get("title") or "")
                try:
                    result = await deliver_proactive_text(
                        app,
                        title=reminder.get("title") or "鎻愰啋",
                        content=content,
                        channel=reminder.get("channel") or "web",
                        source="reminder",
                        platform_id=reminder.get("platform_id") or "",
                        sender_id=reminder.get("sender_id") or "",
                    )
                    app.state.reminder_store.mark_fired(
                        reminder["id"],
                        "done" if result.get("ok") else "failed",
                        result.get("error", ""),
                    )
                except Exception as exc:
                    app.state.reminder_store.mark_fired(reminder["id"], "failed", str(exc))
            await asyncio.sleep(15)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[reminder] loop error: {exc}", flush=True)
            await asyncio.sleep(15)


async def proactive_loop(app: FastAPI) -> None:
    while True:
        try:
            app.state.proactive_store.maybe_create_greetings()
            for event in app.state.proactive_store.due_pending_events():
                try:
                    result = await deliver_proactive_text(
                        app,
                        title=event.get("title") or "涓诲姩娑堟伅",
                        content=event.get("content") or "",
                        channel=event.get("channel") or "web",
                        source="proactive",
                        platform_id=event.get("platform_id") or "",
                        sender_id=event.get("sender_id") or "",
                    )
                    app.state.proactive_store.update_event(
                        event["id"],
                        {
                            "status": "done" if result.get("ok") else "failed",
                            "fired_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                            "conversation_id": result.get("conversation_id", ""),
                            "last_error": result.get("error", ""),
                        },
                    )
                except Exception as exc:
                    app.state.proactive_store.update_event(event["id"], {"status": "failed", "last_error": str(exc)})
            await asyncio.sleep(20)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[proactive] loop error: {exc}", flush=True)
            await asyncio.sleep(20)


def preferred_integration_id(app: FastAPI, explicit: str = "") -> str:
    if explicit:
        return explicit
    for integration in app.state.integration_manager.list_integrations().get("integrations", []):
        if integration.get("enabled") and integration.get("accounts"):
            return str(integration.get("id") or "")
    return ""


async def deliver_proactive_text(
    app: FastAPI,
    *,
    title: str,
    content: str,
    channel: str = "web",
    source: str = "proactive",
    platform_id: str = "",
    sender_id: str = "",
) -> dict:
    channel = str(channel or "web")
    content = str(content or "").strip()
    if not content:
        return {"ok": False, "error": "\u6d88\u606f\u5185\u5bb9\u4e3a\u7a7a\u3002"}

    result: dict[str, Any] = {"ok": False, "error": "", "conversation_id": ""}
    delivered = False
    errors = []

    if channel in {"web", "all"}:
        conversation = app.state.conversation_store.create(title or "\u4e3b\u52a8\u6d88\u606f")
        app.state.conversation_store.append_messages(
            conversation["id"],
            [
                {
                    "role": "assistant",
                    "content": content,
                    "source": source,
                    "display_name": app.state.settings.web_assistant_name or "鏋濊",
                }
            ],
        )
        delivered = True
        result["conversation_id"] = conversation["id"]

    if channel in {"weixin", "all"}:
        integration_id = preferred_integration_id(app, platform_id)
        if not integration_id:
            errors.append("\u6ca1\u6709\u53ef\u7528\u7684\u5fae\u4fe1\u63a5\u5165\u5b9e\u4f8b\u3002")
        else:
            try:
                sent = await app.state.integration_manager.send_weixin_text(integration_id, content, sender_id=sender_id)
                if sent.get("ok"):
                    delivered = True
                else:
                    errors.append(sent.get("error") or "\u5fae\u4fe1\u53d1\u9001\u5931\u8d25\u3002")
            except Exception as exc:
                errors.append(str(exc))

    if not delivered and not errors:
        errors.append("\u4e0d\u652f\u6301\u7684\u89e6\u8fbe\u901a\u9053\uff1a" + str(channel))
    result["ok"] = delivered and not errors
    result["error"] = "\uff1b".join(item for item in errors if item)
    return result


async def integration_watchdog_loop(app: FastAPI) -> None:
    while True:
        try:
            for integration in app.state.integration_manager.list_integrations().get("integrations", []):
                runtime = integration.get("runtime") or {}
                if not integration.get("enabled"):
                    continue
                if runtime.get("manual_stop"):
                    continue
                if not integration.get("accounts"):
                    continue
                if integration.get("status") in {"failed", "stopped", "logged_in"}:
                    branchwhisper_url = f"http://127.0.0.1:{int(getattr(app.state, 'server_port', 7860) or 7860)}"
                    await app.state.integration_manager.start_bridge(integration["id"], branchwhisper_url=branchwhisper_url)
                    app.state.integration_manager.append_log(integration["id"], "[watchdog] bridge restart requested")
            await asyncio.sleep(25)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[integration-watchdog] loop error: {exc}", flush=True)
            await asyncio.sleep(25)


def create_app(args) -> FastAPI:
    # The web server is a single orchestration endpoint. The ASR/LLM/TTS models
    # still live in their own services so they can be restarted and tuned
    # independently.
    app = FastAPI(title="鏋濊 BranchWhisper")
    app.state.server_host = args.host
    app.state.server_port = args.port
    app.state.settings = load_persisted_settings(SessionSettings.from_args(args), SETTINGS_CONFIG)
    enable_default_capabilities(app.state.settings)
    app.state.vad_store = VadModelStore(args.vad_device)
    service_config_path = Path(args.service_config) if args.service_config else SERVICE_PROFILES_CONFIG
    app.state.service_manager = ServiceManager(service_config_path, LOG_DIR)
    app.state.conversation_store = ConversationStore(CONVERSATION_DIR)
    app.state.memory_store = MemoryStore(MEMORY_DB)
    app.state.tool_providers = ToolProviderConfig(TOOL_PROVIDERS_CONFIG)
    app.state.tool_manager = ToolManager(TOOLS_CONFIG, app.state.tool_providers)
    app.state.bot_profiles = BotProfileStore(BOT_PROFILES_CONFIG, app.state.settings.system)
    app.state.avatar_store = AvatarStore(AVATAR_DIR)
    app.state.chat_image_store = ChatImageStore(CHAT_IMAGE_DIR)
    app.state.sticker_store = StickerStore(STICKER_DIR, STICKERS_CONFIG)
    app.state.sticker_policy = StickerPolicy()
    app.state.reminder_store = ReminderStore(REMINDERS_DB)
    app.state.proactive_store = ProactiveStore(PROACTIVE_CONFIG, PROACTIVE_DB)
    app.state.followup_policy = FollowupPolicy(app.state.proactive_store)
    app.state.integration_manager = IntegrationManager(INTEGRATIONS_CONFIG, LOG_DIR, INTEGRATION_MEDIA_DIR)
    app.state.external_dialog_engine = ExternalDialogEngine(
        app.state.integration_manager,
        app.state.conversation_store,
        app.state.memory_store,
        app.state.tool_manager,
        app.state.bot_profiles,
        INTEGRATION_MEDIA_DIR,
    )
    app.state.reminder_task = None
    app.state.proactive_task = None
    app.state.integration_watchdog_task = None
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.mount("/runtime/uploads", StaticFiles(directory=UPLOAD_DIR), name="runtime_uploads")

    @app.middleware("http")
    async def no_cache_static(request: Request, call_next):
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store, max-age=0"
            response.headers["Pragma"] = "no-cache"
        return response

    @app.on_event("startup")
    async def start_reminder_loop():
        app.state.reminder_task = asyncio.create_task(reminder_loop(app))
        app.state.proactive_task = asyncio.create_task(proactive_loop(app))
        app.state.integration_watchdog_task = asyncio.create_task(integration_watchdog_loop(app))

    @app.on_event("shutdown")
    async def stop_reminder_loop():
        for task in (app.state.reminder_task, app.state.proactive_task, app.state.integration_watchdog_task):
            if task and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    @app.get("/")
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/config")
    async def config():
        return public_settings(app.state.settings)

    @app.patch("/api/config")
    async def update_config(payload: dict | None = Body(default=None)):
        payload = dict(payload or {})
        update_llm_api_key(app.state.settings, payload)
        app.state.settings.update_from_dict(payload)
        save_persisted_settings(app.state.settings, SETTINGS_CONFIG)
        return public_settings(app.state.settings)

    @app.get("/api/config/tools")
    async def tool_provider_config(request: Request):
        require_local_service_control(request)
        return {"tools": app.state.tool_providers.public()}

    @app.patch("/api/config/tools")
    async def update_tool_provider_config(request: Request, payload: dict | None = Body(default=None)):
        require_local_service_control(request)
        return {"tools": app.state.tool_providers.update(payload or {})}

    @app.get("/api/bot-profiles")
    async def bot_profiles(request: Request):
        require_local_service_control(request)
        return app.state.bot_profiles.list_profiles()

    @app.post("/api/bot-profiles")
    async def create_bot_profile(request: Request, payload: dict | None = Body(default=None)):
        require_local_service_control(request)
        try:
            profile = app.state.bot_profiles.create(payload or {})
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"profile": profile, **app.state.bot_profiles.list_profiles()}

    @app.patch("/api/bot-profiles/{profile_id}")
    async def update_bot_profile(profile_id: str, request: Request, payload: dict | None = Body(default=None)):
        require_local_service_control(request)
        try:
            profile = app.state.bot_profiles.update(profile_id, payload or {})
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Profile not found") from exc
        return {"profile": profile, **app.state.bot_profiles.list_profiles()}

    @app.delete("/api/bot-profiles/{profile_id}")
    async def delete_bot_profile(profile_id: str, request: Request):
        require_local_service_control(request)
        return {"ok": app.state.bot_profiles.delete(profile_id), **app.state.bot_profiles.list_profiles()}

    @app.post("/api/assets/avatar")
    async def upload_avatar(request: Request, payload: dict | None = Body(default=None)):
        require_local_service_control(request)
        try:
            return {"asset": app.state.avatar_store.save_data_url(str((payload or {}).get("data_url") or ""))}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/assets/chat-image")
    async def upload_chat_image(request: Request, payload: dict | None = Body(default=None)):
        require_local_service_control(request)
        try:
            asset = app.state.chat_image_store.save_data_url(
                str((payload or {}).get("data_url") or ""),
                max_mb=float(getattr(app.state.settings, "vision_max_image_mb", 8.0)),
            )
            return {"asset": asset}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/stickers")
    async def list_stickers(request: Request):
        require_local_service_control(request)
        return {"stickers": app.state.sticker_store.list()}

    @app.post("/api/stickers")
    async def upload_sticker(request: Request, payload: dict | None = Body(default=None)):
        require_local_service_control(request)
        payload = payload or {}
        try:
            sticker = app.state.sticker_store.add_data_url(
                str(payload.get("data_url") or ""),
                tag=str(payload.get("tag") or "榛樿"),
                name=str(payload.get("name") or ""),
            )
            return {"sticker": sticker, "stickers": app.state.sticker_store.list()}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/api/stickers/{sticker_id}")
    async def delete_sticker(sticker_id: str, request: Request):
        require_local_service_control(request)
        return {"ok": app.state.sticker_store.delete(sticker_id), "stickers": app.state.sticker_store.list()}

    @app.get("/api/reminders")
    async def reminders(request: Request, status: str = ""):
        require_local_service_control(request)
        return {"reminders": app.state.reminder_store.list(status=status)}

    @app.post("/api/reminders")
    async def create_reminder(request: Request, payload: dict | None = Body(default=None)):
        require_local_service_control(request)
        try:
            reminder = app.state.reminder_store.create(payload or {})
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"reminder": reminder, "reminders": app.state.reminder_store.list()}

    @app.patch("/api/reminders/{reminder_id}")
    async def update_reminder(reminder_id: str, request: Request, payload: dict | None = Body(default=None)):
        require_local_service_control(request)
        try:
            reminder = app.state.reminder_store.update(reminder_id, payload or {})
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Reminder not found") from exc
        return {"reminder": reminder, "reminders": app.state.reminder_store.list()}

    @app.delete("/api/reminders/{reminder_id}")
    async def delete_reminder(reminder_id: str, request: Request):
        require_local_service_control(request)
        return {"ok": app.state.reminder_store.delete(reminder_id), "reminders": app.state.reminder_store.list()}

    @app.get("/api/proactive/config")
    async def proactive_config(request: Request):
        require_local_service_control(request)
        return {"config": app.state.proactive_store.public_config()}

    @app.patch("/api/proactive/config")
    async def update_proactive_config(request: Request, payload: dict | None = Body(default=None)):
        require_local_service_control(request)
        return {"config": app.state.proactive_store.update_config(payload or {})}

    @app.get("/api/proactive/events")
    async def proactive_events(request: Request, status: str = "", limit: int = 80):
        require_local_service_control(request)
        return {"events": app.state.proactive_store.list_events(status=status, limit=limit)}

    @app.post("/api/proactive/test")
    async def proactive_test(request: Request, payload: dict | None = Body(default=None)):
        require_local_service_control(request)
        payload = payload or {}
        config = app.state.proactive_store.load_config()
        channel = str(payload.get("channel") or app.state.proactive_store.default_channel(config) or "web")
        event = app.state.proactive_store.create_event(
            {
                "kind": "test",
                "title": payload.get("title") or "\u4e3b\u52a8\u6d88\u606f\u6d4b\u8bd5",
                "content": payload.get("content") or "\u8fd9\u662f\u4e00\u6761\u4e3b\u52a8\u6d88\u606f\u6d4b\u8bd5\u3002\u4fdd\u5b58\u540e\u4f1a\u51fa\u73b0\u5728\u5bf9\u8bdd\u5217\u8868\u91cc\u3002",
                "channel": channel,
                "status": "pending",
            }
        )
        result = await deliver_proactive_text(
            app,
            title=event["title"],
            content=event["content"],
            channel=event["channel"],
            source="proactive_test",
        )
        app.state.proactive_store.update_event(
            event["id"],
            {
                "status": "done" if result.get("ok") else "failed",
                "fired_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "conversation_id": result.get("conversation_id", ""),
                "last_error": result.get("error", ""),
            },
        )
        return {"event": app.state.proactive_store.get_event(event["id"]) or event, "result": result, "events": app.state.proactive_store.list_events()}

    @app.post("/api/proactive/events/{event_id}/dismiss")
    async def dismiss_proactive_event(event_id: str, request: Request):
        require_local_service_control(request)
        return {"ok": app.state.proactive_store.dismiss_event(event_id), "events": app.state.proactive_store.list_events()}

    @app.get("/api/memory")
    async def memory_items(limit: int = 200, query: str = "", layer: str = ""):
        return {
            "items": app.state.memory_store.list_memories(app.state.settings, limit=limit, query=query, layer=layer),
            "db_path": str(MEMORY_DB),
        }

    @app.post("/api/memory")
    async def create_memory_item(payload: dict | None = Body(default=None)):
        item = app.state.memory_store.create_memory(payload or {})
        return {"item": item}

    @app.patch("/api/memory/{memory_id}")
    async def update_memory_item(memory_id: str, payload: dict | None = Body(default=None)):
        item = app.state.memory_store.update_memory(memory_id, payload or {})
        return {"item": item}

    @app.delete("/api/memory/{memory_id}")
    async def delete_memory_item(memory_id: str):
        return {"ok": app.state.memory_store.delete_memory(memory_id)}

    @app.post("/api/memory/decay")
    async def decay_memory():
        return app.state.memory_store.apply_decay(app.state.settings)

    @app.get("/api/tools")
    async def tools_config():
        return app.state.tool_manager.get_config()

    @app.patch("/api/tools")
    async def update_tools_config(payload: dict | None = Body(default=None)):
        return app.state.tool_manager.update_config(payload or {})

    @app.post("/api/tools/test")
    async def test_tool(request: Request, payload: dict | None = Body(default=None)):
        require_local_service_control(request)
        payload = payload or {}
        tool_id = str(payload.get("tool") or payload.get("id") or "")
        arguments = payload.get("arguments") if isinstance(payload.get("arguments"), dict) else {}
        if not tool_id:
            raise HTTPException(status_code=400, detail="tool is required")
        started = time.perf_counter()
        result = await app.state.tool_manager.execute(
            tool_id,
            arguments,
            timeout=app.state.settings.tools_timeout,
            max_chars=app.state.settings.tools_max_result_chars,
        )
        return {"id": tool_id, "arguments": arguments, "elapsed_ms": int((time.perf_counter() - started) * 1000), "result": result}

    @app.post("/api/tools/resolve")
    async def resolve_tool(request: Request, payload: dict | None = Body(default=None)):
        require_local_service_control(request)
        text = str((payload or {}).get("text") or "")
        call = app.state.tool_manager.suggest_from_text(text)
        result = None
        if call:
            tool_id = str(call.get("id") or "")
            arguments = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
            try:
                result = await app.state.tool_manager.execute(
                    tool_id,
                    arguments,
                    timeout=app.state.settings.tools_timeout,
                    max_chars=app.state.settings.tools_max_result_chars,
                )
            except Exception as exc:
                result = {"ok": False, "tool": tool_id, "error": str(exc)}
        return {"tool_call": call, "result": result, "direct_answer": direct_answer_from_tool({"tool": call.get("id"), "result": result} if call else None)}

    @app.get("/api/health")
    async def health():
        settings = app.state.settings
        checks = await asyncio.gather(
            check_service("asr", health_url_from(settings.asr_url)),
            check_service("llm", health_url_from(settings.llm_url)),
            check_service("tts", health_url_from(settings.tts_url)),
            return_exceptions=True,
        )
        return {
            "vad": {"ok": True, "device": app.state.vad_store.device},
            "services": [item for item in checks if isinstance(item, dict)],
            "warmups": warmup_statuses(),
        }

    @app.get("/api/services")
    async def services():
        return {"services": attach_service_warmups(await app.state.service_manager.status_all())}

    @app.get("/api/system/resources")
    async def system_resources():
        return collect_system_resources()

    @app.post("/api/services/start-all")
    async def start_all_services(request: Request, payload: dict | None = Body(default=None)):
        require_local_service_control(request)
        overrides = (payload or {}).get("services") or {}
        services = await app.state.service_manager.start_all(overrides, allow_config_update=True)
        await schedule_service_warmups(app.state.settings)
        return {"services": attach_service_warmups(services)}

    @app.post("/api/services/stop-all")
    async def stop_all_services(request: Request):
        require_local_service_control(request)
        clear_warmup_status()
        return {"services": await app.state.service_manager.stop_all()}

    @app.post("/api/services/{service_id}/start")
    async def start_service(service_id: str, request: Request, payload: dict | None = Body(default=None)):
        require_local_service_control(request)
        service = await app.state.service_manager.start(service_id, payload or {}, allow_config_update=True)
        if service_id in {"asr", "llm"}:
            await schedule_service_warmup(service_id, app.state.settings)
        service = attach_service_warmups([service])[0]
        return {"service": service}

    @app.post("/api/services/{service_id}/stop")
    async def stop_service(service_id: str, request: Request):
        require_local_service_control(request)
        clear_warmup_status(service_id)
        service = await app.state.service_manager.stop(service_id)
        return {"service": service}

    @app.patch("/api/services/{service_id}")
    async def update_service(service_id: str, request: Request, payload: dict | None = Body(default=None)):
        require_local_service_control(request)
        service = app.state.service_manager.update_service(service_id, payload or {})
        return {"service": service}

    @app.get("/api/services/{service_id}/logs")
    async def service_logs(service_id: str, max_bytes: int = 24000):
        return {"id": service_id, "logs": app.state.service_manager.read_logs(service_id, max_bytes=max_bytes)}

    @app.delete("/api/services/logs")
    async def clear_all_service_logs(request: Request):
        require_local_service_control(request)
        return app.state.service_manager.clear_logs()

    @app.delete("/api/services/{service_id}/logs")
    async def clear_service_logs(service_id: str, request: Request):
        require_local_service_control(request)
        return app.state.service_manager.clear_logs(service_id)

    @app.get("/api/integrations")
    async def integrations(request: Request):
        require_local_service_control(request)
        return app.state.integration_manager.list_integrations()

    @app.get("/api/integrations/weixin/my-session")
    async def my_weixin_session(request: Request):
        require_local_service_control(request)
        integration_id = preferred_integration_id(app)
        return {
            "integration_id": integration_id,
            "session": app.state.integration_manager.my_weixin_session(integration_id) if integration_id else {},
        }

    @app.post("/api/integrations")
    async def create_integration(request: Request, payload: dict | None = Body(default=None)):
        require_local_service_control(request)
        try:
            item = app.state.integration_manager.create_integration(payload or {})
        except ValueError as exc:
            raise HTTPException(
                status_code=409,
                detail="\u63a5\u5165\u5b9e\u4f8b\u5df2\u5b58\u5728\uff0c\u8bf7\u7f16\u8f91\u5df2\u6709\u5b9e\u4f8b\u6216\u6362\u4e00\u4e2a\u5b9e\u4f8b\u540d\u3002",
            ) from exc
        return {"integration": item, **app.state.integration_manager.list_integrations()}

    @app.patch("/api/integrations/{integration_id}")
    async def update_integration(integration_id: str, request: Request, payload: dict | None = Body(default=None)):
        require_local_service_control(request)
        try:
            item = app.state.integration_manager.update_integration(integration_id, payload or {})
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Integration not found") from exc
        return {"integration": item, **app.state.integration_manager.list_integrations()}

    @app.delete("/api/integrations/{integration_id}")
    async def delete_integration(integration_id: str, request: Request):
        require_local_service_control(request)
        ok = app.state.integration_manager.delete_integration(integration_id)
        return {"ok": ok, **app.state.integration_manager.list_integrations()}

    @app.post("/api/integrations/{integration_id}/install")
    async def install_integration(integration_id: str, request: Request):
        require_local_service_control(request)
        try:
            result = await app.state.integration_manager.install_weixin_cli(integration_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Integration not found") from exc
        return {"result": result, **app.state.integration_manager.list_integrations()}

    @app.post("/api/integrations/{integration_id}/start")
    async def start_integration(integration_id: str, request: Request):
        require_local_service_control(request)
        try:
            branchwhisper_url = await resolve_branchwhisper_url(request)
            result = await app.state.integration_manager.start_bridge(
                integration_id,
                branchwhisper_url=branchwhisper_url,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Integration not found") from exc
        return {"result": result, **app.state.integration_manager.list_integrations()}

    @app.post("/api/integrations/{integration_id}/stop")
    async def stop_integration(integration_id: str, request: Request):
        require_local_service_control(request)
        process_result = app.state.integration_manager.stop_process(integration_id)
        return {"result": {"process": process_result}, **app.state.integration_manager.list_integrations()}

    @app.post("/api/integrations/{integration_id}/restart")
    async def restart_integration(integration_id: str, request: Request):
        require_local_service_control(request)
        app.state.integration_manager.stop_process(integration_id)
        try:
            branchwhisper_url = await resolve_branchwhisper_url(request)
            result = await app.state.integration_manager.start_bridge(
                integration_id,
                branchwhisper_url=branchwhisper_url,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Integration not found") from exc
        return {"result": result, **app.state.integration_manager.list_integrations()}

    @app.post("/api/integrations/{integration_id}/login")
    async def login_integration(integration_id: str, request: Request):
        require_local_service_control(request)
        try:
            result = await app.state.integration_manager.login(integration_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Integration not found") from exc
        return {"result": result, **app.state.integration_manager.list_integrations()}

    @app.post("/api/integrations/{integration_id}/login/qr")
    async def start_integration_qr_login(integration_id: str, request: Request, payload: dict | None = Body(default=None)):
        require_local_service_control(request)
        try:
            result = await app.state.integration_manager.request_weixin_login_qr(
                integration_id,
                force=bool((payload or {}).get("force", False)),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Integration not found") from exc
        return {"result": result, **app.state.integration_manager.list_integrations()}

    @app.post("/api/integrations/{integration_id}/login/poll")
    async def poll_integration_qr_login(integration_id: str, request: Request, payload: dict | None = Body(default=None)):
        require_local_service_control(request)
        try:
            result = await app.state.integration_manager.poll_weixin_login(
                integration_id,
                verify_code=str((payload or {}).get("verify_code") or ""),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Integration not found") from exc
        return {"result": result, **app.state.integration_manager.list_integrations()}

    @app.post("/api/integrations/{integration_id}/bridge/start")
    async def start_integration_bridge(integration_id: str, request: Request, payload: dict | None = Body(default=None)):
        require_local_service_control(request)
        payload = payload or {}
        branchwhisper_url = await resolve_branchwhisper_url(
            request,
            str(payload.get("branchwhisper_url") or payload.get("buding_url") or ""),
        )
        try:
            result = await app.state.integration_manager.start_bridge(
                integration_id,
                branchwhisper_url=branchwhisper_url,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Integration not found") from exc
        return {"result": result, **app.state.integration_manager.list_integrations()}

    @app.get("/api/integrations/{integration_id}/logs")
    async def integration_logs(integration_id: str, request: Request, max_bytes: int = 36000, scope: str = "all"):
        require_local_service_control(request)
        return {
            "id": integration_id,
            "scope": scope,
            "logs": app.state.integration_manager.read_logs_scoped(integration_id, max_bytes=max_bytes, scope=scope),
        }

    @app.delete("/api/integrations/{integration_id}/logs")
    async def clear_integration_logs(integration_id: str, request: Request):
        require_local_service_control(request)
        return app.state.integration_manager.clear_logs(integration_id)

    @app.post("/api/integrations/{integration_id}/timings/{trace_id}")
    async def update_integration_timing(integration_id: str, trace_id: str, request: Request, payload: dict | None = Body(default=None)):
        require_local_service_control(request)
        try:
            timing = app.state.integration_manager.update_message_timing(integration_id, trace_id, payload or {})
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"timing": timing}

    @app.post("/api/integrations/dialog")
    async def integration_dialog(request: Request, payload: dict | None = Body(default=None)):
        require_integration_dialog_access(request)
        try:
            return await app.state.external_dialog_engine.handle(payload or {}, app.state.settings)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Integration dialog failed: {exc}") from exc

    @app.get("/api/conversations")
    async def conversations(query: str = "", archived: str = "active"):
        return {"conversations": app.state.conversation_store.list(query=query, archived=archived)}

    @app.post("/api/conversations")
    async def create_conversation(payload: dict | None = Body(default=None)):
        conversation = app.state.conversation_store.create((payload or {}).get("title"))
        return {"conversation": conversation}

    @app.get("/api/conversations/{conversation_id}")
    async def conversation(conversation_id: str):
        loaded = app.state.conversation_store.load(conversation_id)
        if not loaded:
            return {"conversation": None}
        return {"conversation": loaded}

    @app.patch("/api/conversations/{conversation_id}")
    async def update_conversation(conversation_id: str, payload: dict | None = Body(default=None)):
        updated = app.state.conversation_store.update(conversation_id, payload or {})
        if not updated:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return {"conversation": updated, "conversations": app.state.conversation_store.list()}

    @app.get("/api/conversations/{conversation_id}/export.md")
    async def export_conversation_markdown(conversation_id: str):
        text = app.state.conversation_store.export_markdown(conversation_id)
        if not text:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return PlainTextResponse(
            text,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{conversation_id}.md"'},
        )

    @app.delete("/api/conversations/{conversation_id}")
    async def delete_conversation(conversation_id: str):
        deleted = app.state.conversation_store.delete(conversation_id)
        return {"ok": deleted, "conversations": app.state.conversation_store.list()}

    @app.websocket("/ws/dialog")
    async def dialog_socket(websocket: WebSocket):
        await websocket.accept()
        session = DialogSession(
            websocket,
            app.state.settings,
            app.state.vad_store,
            app.state.conversation_store,
            app.state.memory_store,
            app.state.tool_manager,
            app.state.followup_policy,
            app.state.chat_image_store,
            app.state.sticker_store,
            app.state.sticker_policy,
            websocket.query_params.get("conversation_id"),
        )
        await session.run()

    return app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    add_settings_args(parser)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument("--service-config", default="")
    return parser

def main() -> None:
    """Parse CLI args, create the FastAPI app, and run uvicorn."""
    import uvicorn

    parser = build_parser()
    args = parser.parse_args()
    app = create_app(args)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()


