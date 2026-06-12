from __future__ import annotations

import argparse
import asyncio
import os
import contextlib
import time
from dataclasses import asdict
from pathlib import Path
import sys

import httpx
import numpy as np
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from api.config import create_config_router
from api.diagnostics import create_diagnostics_router
from api.profiles import create_profiles_router
from api.assets import create_assets_router
from api.engagement import create_engagement_router
from api.services import create_services_router
from api.memory import create_memory_router
from api.tools import create_tools_router
from api.conversations import create_conversations_router
from api.integrations import create_integrations_router
from api.dependencies import (
    local_branchwhisper_url,
    unique_urls,
)
from domain.paths import (
    APP_DIR,
    AVATAR_DIR,
    BOT_PROFILES_CONFIG,
    CHAT_IMAGE_DIR,
    CONVERSATION_DIR,
    FRONTEND_DIST_DIR,
    INTEGRATIONS_CONFIG,
    INTEGRATION_MEDIA_DIR,
    LOG_DIR,
    MEMORY_DB,
    PROACTIVE_CONFIG,
    PROACTIVE_DB,
    REMINDERS_DB,
    SERVICE_PROFILES_CONFIG,
    SETTINGS_CONFIG,
    STICKER_DIR,
    STICKER_LIBRARY_DIR,
    STICKER_LIBRARY_INDEX,
    STICKER_ORIGINALS_DIR,
    STICKER_PROCESSED_DIR,
    STICKER_SEND_DIR,
    STICKER_THUMBNAIL_DIR,
    STICKERS_CONFIG,
    TOOL_PROVIDERS_CONFIG,
    TOOLS_CONFIG,
    UPLOAD_DIR,
)
from media.avatars import AvatarStore
from core.config import (
    SessionSettings,
    add_settings_args,
    active_llm_model,
    active_llm_url,
    enable_default_capabilities,
    llm_headers,
    load_persisted_settings,
)
from data.conversations import ConversationStore
from dialog.session import DialogSession
from integration_runtime.manager import ExternalDialogEngine, IntegrationManager
from media.assets import ChatImageStore, StickerStore
from media.sticker_library import StickerLibrary
from data.profiles import BotProfileStore
from engagement.proactive import FollowupPolicy, ProactiveStore
from engagement.reminders import ReminderStore
from tools.runtime_brain import MemoryStore, ToolManager
from service_runtime.services import ServiceManager
from service_runtime.audio_pipeline import transcribe_audio, wav_bytes_from_float32
from media.sticker_policy import StickerPolicy
from core.tool_config import ToolProviderConfig
from service_runtime.vad import MIC_SAMPLE_RATE, VadModelStore


# Backend data flow:
# 1. Browser sends 16 kHz float32 PCM blocks over WebSocket.
# 2. This server runs Silero VAD and cuts the stream into utterances.
# 3. Each utterance is sent to Qwen3-ASR, then the text goes to llama.cpp.
# 4. LLM text is split into small TTS segments and CosyVoice PCM is streamed
#    back to the browser as binary WebSocket frames.
SERVICE_WARMUP_LOCK = asyncio.Lock()
SERVICE_WARMUP_TASKS: dict[str, asyncio.Task] = {}
SERVICE_WARMUP_DONE: set[str] = set()
SERVICE_WARMUP_STATUS: dict[str, dict] = {}
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
        return f"llm:{active_llm_url(settings)}:{active_llm_model(settings)}"
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
        "model": active_llm_model(settings),
        "messages": [
            {"role": "system", "content": "Reply with exactly one short token."},
            {"role": "user", "content": "warmup"},
        ],
        "stream": False,
        "temperature": 0.0,
        "max_tokens": 1,
    }
    async with httpx.AsyncClient(timeout=35.0) as client:
        resp = await client.post(active_llm_url(settings), json=payload, headers=llm_headers(settings))
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
    app = FastAPI(title="BranchWhisper")
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
    app.state.sticker_library = StickerLibrary(
        index_path=STICKER_LIBRARY_INDEX,
        original_dir=STICKER_ORIGINALS_DIR,
        processed_dir=STICKER_PROCESSED_DIR,
        send_dir=STICKER_SEND_DIR,
        thumbnail_dir=STICKER_THUMBNAIL_DIR,
    )
    app.state.sticker_store = StickerStore(STICKER_DIR, STICKERS_CONFIG, library=app.state.sticker_library)
    app.state.sticker_policy = StickerPolicy()
    app.state.reminder_store = ReminderStore(REMINDERS_DB)
    app.state.proactive_store = ProactiveStore(PROACTIVE_CONFIG, PROACTIVE_DB)
    app.state.followup_policy = FollowupPolicy(app.state.proactive_store)
    app.state.integration_manager = IntegrationManager(INTEGRATIONS_CONFIG, LOG_DIR, INTEGRATION_MEDIA_DIR)
    app.state.integration_manager.sticker_store = app.state.sticker_store
    app.state.integration_manager.sticker_policy = app.state.sticker_policy
    app.state.external_dialog_engine = ExternalDialogEngine(
        app.state.integration_manager,
        app.state.conversation_store,
        app.state.memory_store,
        app.state.tool_manager,
        app.state.bot_profiles,
        INTEGRATION_MEDIA_DIR,
        app.state.sticker_store,
        app.state.sticker_policy,
    )
    app.state.reminder_task = None
    app.state.proactive_task = None
    app.state.integration_watchdog_task = None
    if FRONTEND_DIST_DIR.exists():
        app.mount("/app/assets", StaticFiles(directory=FRONTEND_DIST_DIR / "assets"), name="vue_assets")
    app.mount("/runtime/uploads", StaticFiles(directory=UPLOAD_DIR), name="runtime_uploads")
    app.mount("/runtime/stickers", StaticFiles(directory=STICKER_LIBRARY_DIR), name="runtime_stickers")
    app.include_router(create_config_router())
    app.include_router(create_diagnostics_router())
    app.include_router(create_profiles_router())
    app.include_router(create_assets_router())
    app.include_router(create_engagement_router(deliver_proactive_text))
    app.include_router(create_memory_router())
    app.include_router(create_tools_router())
    app.include_router(create_conversations_router())
    app.include_router(create_integrations_router(resolve_branchwhisper_url, preferred_integration_id))
    app.include_router(
        create_services_router(
            attach_service_warmups,
            warmup_statuses,
            schedule_service_warmup,
            schedule_service_warmups,
            clear_warmup_status,
        )
    )

    @app.middleware("http")
    async def no_cache_vue_shell(request: Request, call_next):
        response = await call_next(request)
        if request.url.path in {"/", "/app", "/app/"} or (request.url.path.startswith("/app/") and not request.url.path.startswith("/app/assets/")):
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
        index_path = FRONTEND_DIST_DIR / "index.html"
        if index_path.exists():
            return RedirectResponse(url="/app/")
        raise HTTPException(status_code=503, detail="Frontend is not built. Run `cd frontend && npm run build`.")

    @app.get("/app")
    @app.get("/app/{path:path}")
    async def vue_app(path: str = ""):
        index_path = FRONTEND_DIST_DIR / "index.html"
        if index_path.exists():
            return FileResponse(index_path, headers={"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"})
        raise HTTPException(status_code=503, detail="Frontend is not built. Run `cd frontend && npm run build`.")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
            '<rect width="64" height="64" rx="12" fill="#d8aa50"/>'
            '<text x="32" y="39" text-anchor="middle" font-family="Arial,sans-serif" font-size="22" font-weight="800" fill="#1b1409">BW</text>'
            "</svg>"
        )
        return Response(content=svg, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})

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


