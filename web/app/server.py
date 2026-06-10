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
import numpy as np
from fastapi import Body, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from api.config import create_config_router
from api.profiles import create_profiles_router
from api.assets import create_assets_router
from api.engagement import create_engagement_router
from api.services import create_services_router
from api.dependencies import (
    local_branchwhisper_url,
    require_integration_dialog_access,
    require_local_service_control,
    unique_urls,
)
from domain.paths import (
    APP_DIR,
    AVATAR_DIR,
    BOT_PROFILES_CONFIG,
    CHAT_IMAGE_DIR,
    CONVERSATION_DIR,
    INTEGRATIONS_CONFIG,
    INTEGRATION_MEDIA_DIR,
    LOG_DIR,
    MEMORY_DB,
    PROACTIVE_CONFIG,
    PROACTIVE_DB,
    REMINDERS_DB,
    SERVICE_PROFILES_CONFIG,
    SETTINGS_CONFIG,
    STATIC_DIR,
    STICKER_DIR,
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
    app.include_router(create_config_router())
    app.include_router(create_profiles_router())
    app.include_router(create_assets_router())
    app.include_router(create_engagement_router(deliver_proactive_text))
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

    @app.get("/api/memory")
    async def memory_items(limit: int = 200, query: str = "", layer: str = "", mode: str = ""):
        return {
            "items": app.state.memory_store.list_memories(app.state.settings, limit=limit, query=query, layer=layer, mode=mode),
            "db_path": str(MEMORY_DB),
            "mode": mode or getattr(app.state.settings, "dialog_mode", "local"),
        }

    @app.post("/api/memory")
    async def create_memory_item(payload: dict | None = Body(default=None)):
        payload = payload or {}
        item = app.state.memory_store.create_memory(payload, mode=payload.get("mode") or getattr(app.state.settings, "dialog_mode", "local"))
        return {"item": item}

    @app.patch("/api/memory/{memory_id}")
    async def update_memory_item(memory_id: str, payload: dict | None = Body(default=None)):
        item = app.state.memory_store.update_memory(memory_id, payload or {})
        return {"item": item}

    @app.delete("/api/memory/{memory_id}")
    async def delete_memory_item(memory_id: str):
        return {"ok": app.state.memory_store.delete_memory(memory_id)}

    @app.post("/api/memory/decay")
    async def decay_memory(payload: dict | None = Body(default=None)):
        payload = payload or {}
        return app.state.memory_store.apply_decay(app.state.settings, mode=payload.get("mode"))

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
        if deleted:
            app.state.integration_manager.forget_conversation(conversation_id)
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


