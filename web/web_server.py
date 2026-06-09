from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import contextlib
import time
import uuid
from dataclasses import asdict
from pathlib import Path

import httpx
import numpy as np
from fastapi import Body, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from audio_pipeline import (
    clean_for_tts,
    extract_chat_message_text,
    extract_llm_delta,
    should_flush_tts,
    transcribe_audio,
    wav_bytes_from_float32,
)
from config import (
    SessionSettings,
    add_settings_args,
    enable_default_capabilities,
    llm_headers,
    load_persisted_settings,
    public_settings,
    save_persisted_settings,
    update_llm_api_key,
)
from conversations import ConversationStore
from runtime_brain import MemoryStore, ToolManager, parse_tool_call
from services import ServiceManager, check_service, health_url_from
from system_resources import collect_system_resources
from vad import MIC_SAMPLE_RATE, VadModelStore, VoiceVadSession


# Backend data flow:
# 1. Browser sends 16 kHz float32 PCM blocks over WebSocket.
# 2. This server runs Silero VAD and cuts the stream into utterances.
# 3. Each utterance is sent to Qwen3-ASR, then the text goes to llama.cpp.
# 4. LLM text is split into small TTS segments and CosyVoice PCM is streamed
#    back to the browser as binary WebSocket frames.
APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
RUNTIME_DIR = APP_DIR / "runtime"
LOG_DIR = RUNTIME_DIR / "logs"
CONVERSATION_DIR = RUNTIME_DIR / "conversations"
SETTINGS_CONFIG = RUNTIME_DIR / "settings.json"
MEMORY_DB = RUNTIME_DIR / "memory.sqlite3"
TOOLS_CONFIG = RUNTIME_DIR / "tools.json"

END = object()
GLOBAL_TTS_LOCK = asyncio.Lock()
SERVICE_WARMUP_LOCK = asyncio.Lock()
SERVICE_WARMUP_TASKS: dict[str, asyncio.Task] = {}
SERVICE_WARMUP_DONE: set[str] = set()
SERVICE_WARMUP_STATUS: dict[str, dict] = {}
LOCALHOST_NAMES = {"127.0.0.1", "::1", "localhost"}

# TTS output from the local CosyVoice endpoint is expected to be raw PCM16LE.
# These defaults reduce clipping/pops when the HTTP stream is forwarded to the
# browser over WebSocket. They do not change the model, only the transported PCM.
STORY_KEYWORDS = ("\u6545\u4e8b", "\u7761\u524d", "\u7ae5\u8bdd")
CONTEXT_RECALL_KEYWORDS = (
    "\u4e0a\u53e5",
    "\u4e0a\u4e00\u53e5",
    "\u521a\u624d\u8bf4",
    "\u4f60\u8bf4\u4e86\u4ec0\u4e48",
    "\u4e0a\u6b21\u8bf4",
    "\u524d\u9762\u8bf4",
    "\u6211\u521a\u624d\u8bf4",
    "\u8bb0\u5f97\u6211",
    "\u8bb0\u5f97\u4f60",
)
REPEAT_PREFIXES = (
    "\u8ddf\u7740\u6211\u8bf4",
    "\u8ddf\u6211\u8bf4",
    "\u8ddf\u6211\u5ff5",
    "\u7167\u7740\u6211\u8bf4",
    "\u590d\u8bfb",
    "\u91cd\u590d",
    "\u8bf7\u4f60\u91cd\u590d",
    "\u8bf7\u4f60\u8ddf\u7740\u6211\u8bf4",
    "\u4f60\u8ddf\u7740\u6211\u8bf4",
)



def is_local_request(request: Request) -> bool:
    host = request.client.host if request.client else ""
    return host in LOCALHOST_NAMES


def require_local_service_control(request: Request) -> None:
    if is_local_request(request) or os.environ.get("BUDING_ALLOW_REMOTE_SERVICE_CONTROL") == "1":
        return
    raise HTTPException(
        status_code=403,
        detail="Service control is restricted to localhost. Set BUDING_ALLOW_REMOTE_SERVICE_CONTROL=1 to override.",
    )


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


async def schedule_service_warmups(settings: SessionSettings) -> None:
    settings_snapshot = SessionSettings(**asdict(settings))
    warmups = [
        (
            "asr",
            service_warmup_key("asr", settings_snapshot),
            lambda s=settings_snapshot: warmup_asr(s),
        ),
        (
            "llm",
            service_warmup_key("llm", settings_snapshot),
            lambda s=settings_snapshot: warmup_llm(s),
        ),
    ]
    async with SERVICE_WARMUP_LOCK:
        for service_id, key, warmup_factory in warmups:
            if key in SERVICE_WARMUP_DONE:
                set_warmup_status(service_id, key, "ready")
                continue
            task = SERVICE_WARMUP_TASKS.get(key)
            if task and not task.done():
                continue
            set_warmup_status(service_id, key, "queued")
            SERVICE_WARMUP_TASKS[key] = asyncio.create_task(run_service_warmup(service_id, key, warmup_factory))


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


def create_app(args) -> FastAPI:
    # The web server is a single orchestration endpoint. The ASR/LLM/TTS models
    # still live in their own services so they can be restarted and tuned
    # independently.
    app = FastAPI(title="LoveChoice Voice Console")
    app.state.settings = load_persisted_settings(SessionSettings.from_args(args), SETTINGS_CONFIG)
    enable_default_capabilities(app.state.settings)
    app.state.vad_store = VadModelStore(args.vad_device)
    app.state.service_manager = ServiceManager(Path(args.service_config) if args.service_config else None, LOG_DIR)
    app.state.conversation_store = ConversationStore(CONVERSATION_DIR)
    app.state.memory_store = MemoryStore(MEMORY_DB)
    app.state.tool_manager = ToolManager(TOOLS_CONFIG)
    app.state.tool_manager.update_config(
        {"builtins": {tool["id"]: {"enabled": True} for tool in ToolManager.BUILTIN_TOOLS}}
    )
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.middleware("http")
    async def no_cache_static(request: Request, call_next):
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store, max-age=0"
            response.headers["Pragma"] = "no-cache"
        return response

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
    async def test_tool(payload: dict | None = Body(default=None)):
        payload = payload or {}
        tool_id = str(payload.get("id") or "")
        arguments = payload.get("arguments") if isinstance(payload.get("arguments"), dict) else {}
        result = await app.state.tool_manager.execute(
            tool_id,
            arguments,
            timeout=app.state.settings.tools_timeout,
            max_chars=app.state.settings.tools_max_result_chars,
        )
        return {"id": tool_id, "result": result}

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
        services = await app.state.service_manager.start_all(overrides)
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
        service = await app.state.service_manager.start(service_id)
        if service_id in {"asr", "llm"}:
            await schedule_service_warmups(app.state.settings)
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

    @app.get("/api/conversations")
    async def conversations():
        return {"conversations": app.state.conversation_store.list()}

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
            websocket.query_params.get("conversation_id"),
        )
        await session.run()

    return app


def compact_str(s: str) -> str:
    return "".join(s.split())[:200]


class DialogSession:
    # One DialogSession is created for each browser tab. It owns conversation
    # history, WebSocket sends, and the current VAD stream state.
    def __init__(
        self,
        websocket: WebSocket,
        default_settings: SessionSettings,
        vad_store: VadModelStore,
        conversation_store: ConversationStore,
        memory_store: MemoryStore,
        tool_manager: ToolManager,
        conversation_id: str | None,
    ):
        self.websocket = websocket
        self.settings = SessionSettings(**asdict(default_settings))
        self.vad_store = vad_store
        self.vad_session: VoiceVadSession | None = None
        self.conversation_store = conversation_store
        self.memory_store = memory_store
        self.tool_manager = tool_manager
        self.conversation = conversation_store.get_or_create(conversation_id)
        self.messages = self.build_llm_messages(self.conversation)
        self.vad_load_task: asyncio.Task | None = None
        self.send_lock = asyncio.Lock()
        self.processing = False
        self.current_task: asyncio.Task | None = None
        self.tts_pcm_pending = b""
        self.tts_pcm_tail = np.array([], dtype=np.int16)
        self.tts_pcm_started = False
        self.current_trace_id = ""

    async def run(self) -> None:
        # Preload VAD in the background. Text-only chat should keep working
        # even when the voice stack dependencies are not installed locally.
        self.vad_load_task = asyncio.create_task(self.vad_store.load())
        self.vad_load_task.add_done_callback(self.consume_background_task_exception)
        await schedule_service_warmups(self.settings)
        await self.send_event("ready", settings=public_settings(self.settings))
        await self.send_event("conversation", conversation=self.conversation)
        try:
            while True:
                message = await self.websocket.receive()
                if message["type"] == "websocket.disconnect":
                    break
                # Text frames are JSON control messages; binary frames are
                # raw float32 PCM audio from the browser microphone.
                if message.get("text") is not None:
                    await self.handle_text_message(message["text"])
                elif message.get("bytes") is not None:
                    await self.handle_audio_bytes(message["bytes"])
        except WebSocketDisconnect:
            await self.interrupt_current_turn(notify=False)
            return
        finally:
            if self.vad_load_task and not self.vad_load_task.done():
                self.vad_load_task.cancel()

    @staticmethod
    def consume_background_task_exception(task: asyncio.Task) -> None:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            task.result()

    def begin_trace(self, source: str) -> str:
        trace_id = f"{int(time.time() * 1000):x}-{uuid.uuid4().hex[:6]}"
        self.current_trace_id = trace_id
        self.trace_log(trace_id, f"start source={source} conversation={self.conversation.get('id')}")
        return trace_id

    def trace_log(self, trace_id: str, message: str) -> None:
        if not trace_id:
            return
        safe_message = " ".join(str(message).split())
        print(f"[dialog:{trace_id}] {safe_message}", flush=True)

    def finish_trace(self, trace_id: str, status: str = "done") -> None:
        if not trace_id:
            return
        self.trace_log(trace_id, f"finish status={status}")
        if self.current_trace_id == trace_id:
            self.current_trace_id = ""

    async def handle_text_message(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            await self.send_event("error", message="Invalid JSON message")
            return

        msg_type = data.get("type")
        if msg_type == "settings":
            self.settings.update_from_dict(data.get("settings") or {})
            # Routing/VAD changes should not erase the active conversation.
            # Only the first system message is refreshed for future LLM calls.
            if self.messages:
                self.messages[0] = {"role": "system", "content": self.settings.system}
            else:
                self.messages = [{"role": "system", "content": self.settings.system}]
            self.vad_session = None
            await self.send_event("settings", settings=public_settings(self.settings))
            return

        if msg_type == "reset":
            await self.interrupt_current_turn(notify=False)
            # Reset means "new chat" rather than deleting the old one. The old
            # conversation stays on disk and can be reopened from the sidebar.
            self.conversation = self.conversation_store.create()
            self.messages = self.build_llm_messages(self.conversation)
            if self.vad_session:
                self.vad_session.reset()
            await self.send_event("reset", conversation=self.conversation)
            return

        if msg_type == "text":
            text = str(data.get("text") or "").strip()
            if text:
                await self.start_current_task(self.process_user_text(text, source="text"))
            return

        if msg_type == "interrupt":
            await self.interrupt_current_turn()
            return

    async def handle_audio_bytes(self, raw: bytes) -> None:
        if self.processing:
            # The browser sends an explicit "interrupt" control message when
            # local barge-in detection fires. Until that message cancels the
            # active turn, ignore audio to avoid feeding TTS playback to ASR.
            return

        if self.vad_session is None:
            await self.send_event("status", stage="vad", label="loading")
            try:
                vad_task = self.vad_load_task or asyncio.create_task(self.vad_store.load())
                self.vad_load_task = vad_task
                torch, vad_iterator_cls, model, device = await vad_task
            except Exception as exc:
                await self.send_event("error", message=f"VAD failed to load: {exc}")
                return
            self.vad_session = VoiceVadSession(torch, vad_iterator_cls, model, device, self.settings)
            await self.send_event("status", stage="vad", label="ready", device=device)

        if len(raw) % 4:
            # Browser audio is float32 little-endian, so valid packets are
            # always multiples of 4 bytes.
            return

        audio = np.frombuffer(raw, dtype="<f4").astype(np.float32, copy=False)
        for event in self.vad_session.push_audio(audio):
            event_type = event.get("type")
            if event_type == "vad_start":
                await self.send_event("vad_start")
            elif event_type == "vad_short":
                await self.send_event("vad_short", duration_ms=event["duration_ms"])
            elif event_type == "vad_end":
                await self.send_event("vad_end", duration_ms=event["duration_ms"])
                await self.start_current_task(self.process_utterance(event["audio"]))

    async def start_current_task(self, coro) -> None:
        if self.current_task and not self.current_task.done():
            await self.send_event("busy")
            return
        self.current_task = asyncio.create_task(coro)

        def _clear_task(task: asyncio.Task) -> None:
            if self.current_task is task:
                self.current_task = None

        self.current_task.add_done_callback(_clear_task)

    async def interrupt_current_turn(self, notify: bool = True) -> None:
        task = self.current_task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        self.current_task = None
        self.processing = False
        self.reset_tts_pcm_state()
        if self.vad_session:
            self.vad_session.reset()
        if notify:
            await self.send_event("interrupted")

    async def process_utterance(self, audio: np.ndarray) -> None:
        # VAD has decided that one user turn is complete. Convert it to WAV
        # because Qwen3-ASR service endpoints expect file-like audio input.
        self.processing = True
        dialog_started = False
        trace_id = self.begin_trace("voice")
        await self.send_event("trace", trace_id=trace_id, source="voice")
        try:
            wav_bytes = wav_bytes_from_float32(audio)
            start = time.perf_counter()
            await self.send_event("status", stage="asr", label="running")
            self.trace_log(trace_id, f"asr:start samples={audio.size} wav_bytes={len(wav_bytes)}")
            user_text = await transcribe_audio(self.settings, wav_bytes)
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            self.trace_log(trace_id, f"asr:done ms={elapsed_ms} text_len={len(user_text)}")
            await self.send_event("metric", name="asr_ms", value=elapsed_ms)
            await self.send_event("asr", text=user_text)
            if user_text:
                dialog_started = True
                await self.process_user_text(user_text, source="voice", trace_id=trace_id)
        except Exception as exc:
            self.trace_log(trace_id, f"asr:error {exc}")
            await self.send_event("error", message=f"ASR failed: {exc}")
        finally:
            self.processing = False
            if not dialog_started:
                await self.send_event("turn_done")
                self.finish_trace(trace_id, "asr_empty_or_failed")

    async def process_user_text(self, user_text: str, source: str, trace_id: str | None = None) -> None:
        if self.processing and source == "text":
            await self.send_event("busy")
            return

        trace_id = trace_id or self.begin_trace(source)
        self.current_trace_id = trace_id
        await self.send_event("trace", trace_id=trace_id, source=source)
        self.trace_log(trace_id, f"user source={source} text_len={len(user_text)}")

        old_processing = self.processing
        self.processing = True
        await self.send_event("user", text=user_text, source=source)
        self.persist_messages([{"role": "user", "content": user_text, "source": source}], title_hint=user_text)
        await self.send_event("conversation_saved", conversation=self.conversation)

        try:
            repeat_text = extract_repeat_text(user_text)
            if repeat_text:
                # "跟着我说..." should be spoken exactly. Bypassing the LLM
                # avoids instruction drift and removes one latency source.
                self.trace_log(trace_id, f"repeat:direct text_len={len(repeat_text)}")
                await self.send_event("assistant_start")
                await self.send_event("llm_delta", text=repeat_text)
                await self.stream_direct_tts(repeat_text)
                self.messages.append({"role": "user", "content": user_text})
                self.messages.append({"role": "assistant", "content": repeat_text})
                self.persist_messages([{"role": "assistant", "content": repeat_text}])
                await self.send_event("conversation_saved", conversation=self.conversation)
                self.trim_history()
                return

            request_user_text = build_request_user_text(user_text, last_assistant_content(self.messages))
            tool_result = await self.maybe_execute_tool(user_text)
            if tool_result:
                request_user_text += (
                    "\n\n联网/API 工具结果如下。请基于结果回答用户；如果结果不足或失败，要自然说明不确定，不要编造：\n"
                    + json.dumps(tool_result, ensure_ascii=False)
                )
            request_messages = self.build_contextual_request_messages(user_text, request_user_text)
            text_queue: asyncio.Queue = asyncio.Queue()
            # TTS runs in parallel with the streaming LLM response. The LLM
            # producer places completed text segments in this queue.
            tts_task = asyncio.create_task(self.tts_queue_worker(text_queue))

            await self.send_event("assistant_start")
            full_answer = ""
            try:
                self.trace_log(trace_id, "llm:start")
                full_answer = await self.stream_llm(request_messages, text_queue)
                self.trace_log(trace_id, f"llm:done answer_len={len(full_answer)}")
            finally:
                await text_queue.put(END)
                try:
                    await asyncio.wait_for(tts_task, timeout=30)
                except asyncio.TimeoutError:
                    tts_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await tts_task

            self.messages.append({"role": "user", "content": user_text})
            if full_answer:
                self.messages.append({"role": "assistant", "content": full_answer})
                self.persist_messages([{"role": "assistant", "content": full_answer}])
                await self.send_event("conversation_saved", conversation=self.conversation)
                await self.remember_turn_safely(user_text, full_answer)
            self.trim_history()
        except Exception as exc:
            self.trace_log(trace_id, f"dialog:error {exc}")
            await self.send_event("error", message=f"Dialog failed: {exc}")
        finally:
            self.processing = old_processing
            await self.send_event("turn_done")
            self.finish_trace(trace_id)

    async def remember_turn_safely(self, user_text: str, assistant_text: str) -> None:
        try:
            await self.memory_store.observe_turn(self.settings, user_text, assistant_text, self.extract_memories_with_llm)
        except Exception as exc:
            print(f"[memory] turn update failed: {exc}", flush=True)

    def build_llm_messages(self, conversation: dict) -> list[dict[str, str]]:
        messages = [{"role": "system", "content": self.settings.system}]
        for item in conversation.get("messages") or []:
            role = item.get("role")
            content = item.get("content")
            if role in {"user", "assistant"} and content:
                messages.append({"role": role, "content": content})
        return messages

    def persist_messages(self, messages: list[dict], title_hint: str | None = None) -> None:
        self.conversation = self.conversation_store.append_messages(
            self.conversation["id"],
            messages,
            title_hint=title_hint,
        )

    def build_contextual_request_messages(self, user_text: str, request_user_text: str) -> list[dict[str, str]]:
        messages = list(self.messages)
        memory_context = self.memory_store.format_context(self.settings, user_text)

        # 注入当前时间
        from datetime import datetime
        now_str = datetime.now().strftime("%Y年%m月%d日 %A %H:%M")
        time_note = f"\n\n当前时间：{now_str}。你要自然地感知这个时间（比如晚上就聊晚上的话题，早上就聊早上的），但不要生硬地报时。"
        # 重复检测
        recent_user_msgs = [m.get("content","") for m in messages[-6:] if m.get("role") == "user"]
        if len(recent_user_msgs) >= 2 and recent_user_msgs[-1]:
            last = compact_str(recent_user_msgs[-1])
            prev = compact_str(recent_user_msgs[-2])
            if last and prev and last == prev:
                time_note += "\n注意：用户刚才问了和上一轮完全一样的问题。你应该稍微不耐烦或用不同方式回答，而不是原句重复。"

        recent_assistant = [compact_str(m.get("content", "")) for m in messages[-8:] if m.get("role") == "assistant"]
        recent_assistant = [text for text in recent_assistant if text]
        if recent_assistant:
            time_note += (
                "\n\n最近你已经说过这些回复片段，请避免原句复用、固定开头和重复解释；"
                "除非用户明确要求复读，否则要换一种自然说法：\n"
                + "\n".join(f"- {text[:80]}" for text in recent_assistant[-3:])
            )

        old_content = messages[0].get("content", "")
        if memory_context:
            old_content += "\n\n" + memory_context
        old_content += time_note
        messages[0] = {**messages[0], "content": old_content}

        messages.append({"role": "user", "content": request_user_text})
        return messages

    async def extract_memories_with_llm(self, prompt: str) -> str:
        messages = [
            {
                "role": "system",
                "content": "你是记忆抽取器。只输出 JSON 数组，不输出解释、Markdown 或多余文字。",
            },
            {"role": "user", "content": prompt},
        ]
        return await self.complete_llm_text(messages, temperature=0.0, max_tokens=420, timeout=10.0)

    async def maybe_execute_tool(self, user_text: str) -> dict | None:
        if not self.settings.tools_enabled:
            return None

        specs = self.tool_manager.enabled_specs()
        if not specs:
            return None

        heuristic_call = self.tool_manager.suggest_from_text(user_text)
        custom_enabled = any(not spec.get("builtin") for spec in specs)
        tool_signal = bool(
            heuristic_call
            or custom_enabled
            or re.search(r"(当前|现在|最新|实时|热点|新闻|搜索|查一下|网上|天气|价格|汇率|网址|https?://)", user_text, flags=re.I)
        )
        if not tool_signal:
            return None

        call = heuristic_call
        if self.settings.tools_auto_call:
            planned = await self.plan_tool_call(user_text)
            if planned:
                call = planned

        if not call:
            return None

        tool_id = call.get("id") or ""
        arguments = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
        if not self.tool_manager.tool_exists(tool_id):
            return None

        await self.send_event("tool", id=tool_id, arguments=arguments)
        try:
            result = await self.tool_manager.execute(
                tool_id,
                arguments,
                timeout=self.settings.tools_timeout,
                max_chars=self.settings.tools_max_result_chars,
            )
            return {"tool": tool_id, "arguments": arguments, "result": result}
        except Exception as exc:
            return {"tool": tool_id, "arguments": arguments, "result": {"ok": False, "error": str(exc)}}

    async def plan_tool_call(self, user_text: str) -> dict | None:
        planner_system = (
            "你是工具路由器，只输出 JSON，不输出解释。"
            "当用户需要当前、实时、联网、热点新闻、天气、财经价格、URL 读取或某个自定义 API 时，选择一个工具。"
            "普通闲聊、稳定常识、情绪陪伴和不需要联网的问题，输出 {\"tool_call\": null}。"
            "输出格式必须是 {\"tool_call\":{\"id\":\"工具id\",\"arguments\":{...}}} 或 {\"tool_call\":null}。\n\n"
            "可用工具：\n"
            f"{self.tool_manager.planner_tool_text()}"
        )
        messages = [
            {"role": "system", "content": planner_system},
            {"role": "user", "content": user_text},
        ]
        try:
            text = await self.complete_llm_text(messages, temperature=0.0, max_tokens=260)
        except Exception:
            return None
        return parse_tool_call(text)

    async def complete_llm_text(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.0,
        max_tokens: int = 260,
        timeout: float | None = None,
    ) -> str:
        payload = {
            "model": self.settings.llm_model,
            "messages": messages,
            "stream": False,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        async with httpx.AsyncClient(timeout=timeout or self.settings.tools_timeout) as client:
            resp = await client.post(self.settings.llm_url, json=payload, headers=llm_headers(self.settings))
        resp.raise_for_status()
        return extract_chat_message_text(resp.json())

    async def stream_llm(self, request_messages: list[dict[str, str]], text_queue: asyncio.Queue) -> str:
        # llama.cpp exposes an OpenAI-compatible SSE stream. We forward each
        # text delta to the page immediately, while buffering sentence-sized
        # pieces for TTS.
        payload = {
            "model": self.settings.llm_model,
            "messages": request_messages,
            "stream": True,
            "temperature": self.settings.temperature,
            "max_tokens": self.settings.max_tokens,
            "top_p": 0.95,
            "repeat_penalty": 1.18,
            # llama.cpp DRY sampling：检测重复短语并惩罚，比单纯的 repeat_penalty 更精准
            "dry_multiplier": 0.8,
            "dry_base": 1.75,
            "dry_allowed_length": 2,
            "dry_penalty_last_n": -1,
            "seed": int(time.time() * 1000) % 2147483647,
        }
        buffer = ""
        full_answer = ""
        first_chunk = True
        first_token = True
        started = time.perf_counter()

        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("POST", self.settings.llm_url, json=payload, headers=llm_headers(self.settings)) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    if line.startswith("data: "):
                        line = line[6:]
                    if line.strip() == "[DONE]":
                        break

                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    text = extract_llm_delta(data)
                    if not text:
                        continue

                    if first_token:
                        elapsed_ms = int((time.perf_counter() - started) * 1000)
                        self.trace_log(self.current_trace_id, f"llm:first_token ms={elapsed_ms}")
                        await self.send_event("metric", name="llm_first_token_ms", value=elapsed_ms)
                        first_token = False

                    full_answer += text
                    buffer += text
                    await self.send_event("llm_delta", text=text)
                    if should_flush_tts(buffer, first_chunk):
                        # First segment is intentionally shorter for lower
                        # first-audio latency; later segments are longer to
                        # reduce TTS prosody jumps. Clean before enqueueing so
                        # prompt echoes like <|endofprompt|> never reach TTS.
                        tts_text = clean_for_tts(buffer)
                        if tts_text:
                            await text_queue.put(tts_text)
                        buffer = ""
                        first_chunk = False

        if buffer.strip():
            tts_text = clean_for_tts(buffer)
            if tts_text:
                await text_queue.put(tts_text)

        return clean_for_tts(full_answer) or full_answer.strip()

    async def tts_queue_worker(self, text_queue: asyncio.Queue) -> None:
        # TTS requests are sequential so audio order is deterministic. The
        # audio itself still streams back chunk by chunk inside each request.
        # If tts_enabled is false, drain the queue silently without calling TTS.
        while True:
            text = await text_queue.get()
            if text is END:
                return
            if not self.settings.tts_enabled:
                continue
            text = clean_for_tts(str(text))
            if text:
                await self.stream_direct_tts(text)

    async def stream_direct_tts(self, text: str) -> None:
        if not self.settings.tts_enabled:
            return
        # CosyVoice server returns raw PCM16 mono. Text events and binary audio
        # frames share the same WebSocket, so the frontend can update text and
        # schedule audio playback without polling.
        #
        # Raw PCM16 must stay 2-byte aligned. Network/HTTP chunks are transport
        # chunks, not audio frame boundaries; odd bytes or abrupt segment edges
        # can sound like pops/clicks in the browser.
        text = clean_for_tts(text)
        if not text:
            return

        # CosyVoice/vLLM paths can be unstable under concurrent requests, so all
        # browser sessions share one TTS lock. This prevents overlapping /tts
        # calls that can produce garbled "啊啊啊" audio or NoneType failures.
        async with GLOBAL_TTS_LOCK:
            await self.send_event("tts_segment", text=text)
            self.trace_log(self.current_trace_id, f"tts:start text_len={len(text)}")
            payload = {
                "text": text,
                "stream": True,
                "speed": self.settings.tts_speed,
                "seed": self.settings.tts_seed,
            }
            started = time.perf_counter()
            first_audio = True
            self.reset_tts_pcm_state()

            try:
                async with httpx.AsyncClient(timeout=None) as client:
                    async with client.stream("POST", self.settings.tts_url, json=payload) as resp:
                        resp.raise_for_status()
                        async for chunk in resp.aiter_bytes():
                            if not chunk:
                                continue
                            if first_audio:
                                elapsed_ms = int((time.perf_counter() - started) * 1000)
                                self.trace_log(self.current_trace_id, f"tts:first_audio ms={elapsed_ms}")
                                await self.send_event("metric", name="tts_first_audio_ms", value=elapsed_ms)
                                await self.send_event("audio_format", sample_rate=self.settings.tts_sample_rate, channels=1, format="pcm_s16le")
                                first_audio = False

                            safe_chunk = self.process_tts_pcm_chunk(chunk)
                            if safe_chunk:
                                await self.send_audio(safe_chunk)

                    tail = self.finish_tts_pcm_stream()
                    if tail:
                        await self.send_audio(tail)

            except httpx.ConnectError as exc:
                self.trace_log(self.current_trace_id, f"tts:connect_error {exc}")
                await self.send_event(
                    "error",
                    message=(
                        f"CosyVoice3 TTS 服务连接失败：{self.settings.tts_url}。"
                        "请去“服务”页面启动 CosyVoice3 TTS，并查看 tts 日志。"
                        f"原始错误：{exc}"
                    ),
                )
            except httpx.HTTPStatusError as exc:
                self.trace_log(self.current_trace_id, f"tts:http_error status={exc.response.status_code}")
                detail = ""
                with contextlib.suppress(Exception):
                    detail_data = exc.response.json()
                    detail = str(detail_data.get("detail") or detail_data.get("status") or "")
                if exc.response.status_code == 503 and "loading" in detail.lower():
                    await self.send_event("status", stage="tts", label="loading")
                    return
                await self.send_event(
                    "error",
                    message=f"CosyVoice3 TTS 返回 HTTP {exc.response.status_code}：请查看 tts 日志。",
                )
            except httpx.HTTPError as exc:
                self.trace_log(self.current_trace_id, f"tts:http_error {exc}")
                await self.send_event("error", message=f"CosyVoice3 TTS 请求失败：{exc}")
            finally:
                self.trace_log(self.current_trace_id, "tts:finish")
                self.reset_tts_pcm_state()

    def reset_tts_pcm_state(self) -> None:
        self.tts_pcm_pending = b""
        self.tts_pcm_tail = np.array([], dtype=np.int16)
        self.tts_pcm_started = False

    def process_tts_pcm_chunk(self, chunk: bytes) -> bytes:
        data = self.tts_pcm_pending + chunk
        if len(data) < 2:
            self.tts_pcm_pending = data
            return b""

        if len(data) % 2:
            self.tts_pcm_pending = data[-1:]
            data = data[:-1]
        else:
            self.tts_pcm_pending = b""

        samples = np.frombuffer(data, dtype="<i2").astype(np.float32)
        if samples.size == 0:
            return b""

        volume = float(np.clip(self.settings.tts_volume, 0.05, 1.5))
        samples *= volume

        fade_samples = self.tts_fade_samples()
        if not self.tts_pcm_started:
            fade_len = min(fade_samples, samples.size)
            if fade_len > 1:
                samples[:fade_len] *= np.linspace(0.0, 1.0, fade_len, dtype=np.float32)
            self.tts_pcm_started = True

        samples = np.clip(samples, -32768, 32767).astype(np.int16)

        if fade_samples <= 0:
            return samples.astype("<i2", copy=False).tobytes()

        if self.tts_pcm_tail.size:
            samples = np.concatenate([self.tts_pcm_tail, samples])

        if samples.size <= fade_samples:
            self.tts_pcm_tail = samples
            return b""

        send_samples = samples[:-fade_samples]
        self.tts_pcm_tail = samples[-fade_samples:]
        return send_samples.astype("<i2", copy=False).tobytes()

    def finish_tts_pcm_stream(self) -> bytes:
        if self.tts_pcm_pending:
            self.tts_pcm_pending = b""

        tail = self.tts_pcm_tail.astype(np.float32)
        self.tts_pcm_tail = np.array([], dtype=np.int16)
        if tail.size == 0:
            return b""

        fade_len = min(self.tts_fade_samples(), tail.size)
        if fade_len > 1:
            tail[-fade_len:] *= np.linspace(1.0, 0.0, fade_len, dtype=np.float32)

        tail = np.clip(tail, -32768, 32767).astype(np.int16)
        return tail.astype("<i2", copy=False).tobytes()

    def tts_fade_samples(self) -> int:
        fade_ms = max(0, int(self.settings.tts_fade_ms))
        return int(self.settings.tts_sample_rate * fade_ms / 1000)

    async def send_event(self, event_type: str, **payload) -> None:
        if self.current_trace_id and "trace_id" not in payload:
            payload["trace_id"] = self.current_trace_id
        payload["type"] = event_type
        async with self.send_lock:
            # Serialize WebSocket sends: FastAPI/Starlette does not allow
            # concurrent send_text/send_bytes calls on the same socket.
            await self.websocket.send_text(json.dumps(payload, ensure_ascii=False))

    async def send_audio(self, chunk: bytes) -> None:
        async with self.send_lock:
            await self.websocket.send_bytes(chunk)

    def trim_history(self) -> None:
        max_history_messages = max(2, self.settings.history_turns * 2)
        if len(self.messages) > 1 + max_history_messages:
            del self.messages[1 : len(self.messages) - max_history_messages]


def is_story_request(text: str) -> bool:
    return any(keyword in text for keyword in STORY_KEYWORDS)


def build_request_user_text(text: str, previous_assistant: str | None = None) -> str:
    context_note = ""
    if previous_assistant and is_context_recall_request(text):
        context_note = (
            "\n\nContext note for this turn only: your immediately previous assistant reply was: "
            f"{previous_assistant}"
        )

    if not is_story_request(text):
        return text + context_note

    # Story constraints are added only for this request and are not stored in
    # long-term history, so normal chat does not inherit bedtime-story rules.
    return (
        text
        + "\n\nTask: The user is asking for a bedtime/story. "
        "Directly tell a warm, coherent Chinese story for voice reading. "
        "Start with one short Chinese sentence under 8 Chinese characters. "
        "Then continue the story in 100 to 180 Chinese characters. "
        "Do not start with a permission phrase such as 'sure' or 'of course'. "
        "Do not give sleep advice, do not ask the user a question, "
        "do not evaluate your own story, and do not output END."
        + context_note
    )


def extract_repeat_text(text: str) -> str | None:
    for prefix in REPEAT_PREFIXES:
        index = text.find(prefix)
        if index == -1:
            continue
        value = text[index + len(prefix) :].strip()
        value = value.lstrip("\u3000 \t\r\n\uff0c,:\uff1a")
        return value or None
    return None


def is_context_recall_request(text: str) -> bool:
    return any(keyword in text for keyword in CONTEXT_RECALL_KEYWORDS)


def last_assistant_content(messages: list[dict[str, str]]) -> str | None:
    for message in reversed(messages):
        if message.get("role") == "assistant" and message.get("content"):
            return message["content"]
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    add_settings_args(parser)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument("--service-config", default="")
    return parser

def main() -> None:
    """程序入口：解析参数，创建 FastAPI app，用 uvicorn 启动。"""
    import uvicorn

    parser = build_parser()
    args = parser.parse_args()
    app = create_app(args)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()

