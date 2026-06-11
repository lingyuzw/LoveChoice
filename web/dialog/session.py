from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import re
import time
import uuid
from dataclasses import asdict
from pathlib import Path

import httpx
import numpy as np
from fastapi import WebSocket, WebSocketDisconnect

from core.config import (
    SessionSettings,
    active_dialog_mode,
    active_history_turns,
    active_llm_model,
    active_llm_url,
    active_max_tokens,
    active_temperature,
    llm_headers,
    memory_mode,
    public_settings,
)
from data.conversations import ConversationStore
from engagement.proactive import FollowupPolicy
from media.assets import ChatImageStore, StickerStore
from media.sticker_policy import StickerPolicy
from service_runtime.audio_pipeline import (
    ReasoningStreamFilter,
    clean_for_tts,
    clean_reply_text,
    extract_chat_message_text,
    extract_finish_reason,
    extract_llm_delta,
    should_flush_tts,
    transcribe_audio,
    wav_bytes_from_float32,
)
from service_runtime.vad import MIC_SAMPLE_RATE, VadModelStore, VoiceVadSession
from tools.direct_answers import direct_answer_from_tool
from tools.runtime_brain import MemoryStore, ToolManager, parse_tool_call
from core.text_utils import split_reply_messages

END = object()
GLOBAL_TTS_LOCK = asyncio.Lock()

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

def compact_str(s: str) -> str:
    return "".join(s.split())[:200]


def attachment_text(attachments: list[dict]) -> str:
    parts = []
    for item in attachments or []:
        if item.get("type") == "image":
            parts.append(f"[图片] {item.get('summary') or item.get('url') or ''}".strip())
        elif item.get("type") == "sticker":
            parts.append(f"[表情包:{item.get('tag') or item.get('name') or '默认'}]")
    return " ".join(parts)


def format_reply_paragraphs(text: str) -> str:
    return clean_reply_text(text)


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
        followup_policy: FollowupPolicy | None,
        chat_image_store: ChatImageStore,
        sticker_store: StickerStore,
        sticker_policy: StickerPolicy,
        conversation_id: str | None,
    ):
        self.websocket = websocket
        self.settings = SessionSettings(**asdict(default_settings))
        self.vad_store = vad_store
        self.vad_session: VoiceVadSession | None = None
        self.conversation_store = conversation_store
        self.memory_store = memory_store
        self.tool_manager = tool_manager
        self.followup_policy = followup_policy
        self.chat_image_store = chat_image_store
        self.sticker_store = sticker_store
        self.sticker_policy = sticker_policy
        self.conversation = conversation_store.load(conversation_id) if conversation_id else None
        if not self.conversation:
            self.conversation = self.draft_conversation()
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
            self.conversation = self.draft_conversation()
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

        if msg_type == "message":
            text = str(data.get("text") or "").strip()
            attachments = data.get("attachments") if isinstance(data.get("attachments"), list) else []
            if text or attachments:
                await self.start_current_task(self.process_user_text(text or "请看看这张图片。", source="text", attachments=attachments))
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

    def run_background(self, coro, label: str) -> None:
        task = asyncio.create_task(coro)

        def _log_failure(done: asyncio.Task) -> None:
            with contextlib.suppress(asyncio.CancelledError):
                exc = done.exception()
                if exc:
                    self.trace_log(self.current_trace_id, f"{label}:background_error {exc}")

        task.add_done_callback(_log_failure)

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

    async def process_user_text(
        self,
        user_text: str,
        source: str,
        trace_id: str | None = None,
        attachments: list[dict] | None = None,
    ) -> None:
        if self.processing and source == "text":
            await self.send_event("busy")
            return

        trace_id = trace_id or self.begin_trace(source)
        self.current_trace_id = trace_id
        await self.send_event("trace", trace_id=trace_id, source=source)
        self.trace_log(trace_id, f"user source={source} text_len={len(user_text)}")

        old_processing = self.processing
        self.processing = True
        user_attachments = await self.prepare_user_attachments(attachments or [])
        request_user_text = self.compose_user_request_text(user_text, user_attachments)
        await self.send_event("user", text=user_text, source=source, attachments=user_attachments)
        self.persist_messages([{"role": "user", "content": user_text, "source": source, "attachments": user_attachments}], title_hint=user_text)
        await self.send_event("conversation_saved", conversation=self.conversation)

        try:
            repeat_text = extract_repeat_text(user_text)
            if repeat_text:
                # "跟着我说..." should be spoken exactly. Bypassing the LLM
                # avoids instruction drift and removes one latency source.
                self.trace_log(trace_id, f"repeat:direct text_len={len(repeat_text)}")
                repeat_text = format_reply_paragraphs(repeat_text)
                await self.send_event("assistant_start")
                await self.send_event("llm_delta", text=repeat_text)
                await self.stream_direct_tts(repeat_text)
                self.messages.append({"role": "user", "content": user_text})
                self.messages.append({"role": "assistant", "content": repeat_text})
                self.persist_assistant_reply(repeat_text)
                await self.send_event("conversation_saved", conversation=self.conversation)
                self.trim_history()
                return

            followup = self.followup_policy.maybe_question(user_text) if self.followup_policy else None
            if followup:
                question = format_reply_paragraphs(followup["question"])
                self.trace_log(trace_id, f"followup:{followup.get('id')}")
                await self.send_event("assistant_start")
                await self.send_event("llm_delta", text=question)
                self.messages.append({"role": "user", "content": user_text})
                self.messages.append({"role": "assistant", "content": question})
                self.persist_assistant_reply(question, source="followup")
                await self.send_event("conversation_saved", conversation=self.conversation)
                self.trim_history()
                return

            request_user_text = build_request_user_text(request_user_text, last_assistant_content(self.messages))
            tool_result = await self.maybe_execute_tool(user_text)
            if tool_result:
                direct_answer = direct_answer_from_tool(tool_result)
                if direct_answer:
                    direct_answer = format_reply_paragraphs(direct_answer)
                    self.trace_log(trace_id, f"tool:direct {tool_result.get('tool')}")
                    await self.send_event("assistant_start")
                    await self.send_event("tool", id=tool_result.get("tool"), arguments=tool_result.get("arguments") or {})
                    await self.send_event("llm_delta", text=direct_answer)
                    await self.stream_direct_tts(direct_answer)
                    self.messages.append({"role": "user", "content": request_user_text})
                    self.messages.append({"role": "assistant", "content": direct_answer})
                    assistant_attachments = self.choose_reply_sticker(user_text, direct_answer, source)
                    if assistant_attachments:
                        await self.send_event("assistant_attachment", attachments=assistant_attachments)
                    self.persist_assistant_reply(direct_answer, attachments=assistant_attachments)
                    await self.send_event("conversation_saved", conversation=self.conversation)
                    self.run_background(
                        self.remember_turn_safely(self.memory_observation_text(user_text, user_attachments), direct_answer),
                        "memory",
                    )
                    self.run_background(self.maybe_compact_conversation(self.conversation.get("id") or ""), "context_compaction")
                    self.trim_history()
                    return
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

            self.messages.append({"role": "user", "content": request_user_text})
            if full_answer:
                full_answer = format_reply_paragraphs(full_answer)
                self.messages.append({"role": "assistant", "content": full_answer})
                assistant_attachments = self.choose_reply_sticker(user_text, full_answer, source)
                if assistant_attachments:
                    await self.send_event("assistant_attachment", attachments=assistant_attachments)
                self.persist_assistant_reply(full_answer, attachments=assistant_attachments)
                await self.send_event("conversation_saved", conversation=self.conversation)
                self.run_background(
                    self.remember_turn_safely(self.memory_observation_text(user_text, user_attachments), full_answer),
                    "memory",
                )
                self.run_background(self.maybe_compact_conversation(self.conversation.get("id") or ""), "context_compaction")
            self.trim_history()
        except Exception as exc:
            self.trace_log(trace_id, f"dialog:error {exc}")
            await self.send_event("error", message=f"Dialog failed: {exc}")
        finally:
            self.processing = old_processing
            await self.send_event("turn_done")
            self.finish_trace(trace_id)

    async def prepare_user_attachments(self, attachments: list[dict]) -> list[dict]:
        prepared: list[dict] = []
        for attachment in attachments[:4]:
            if not isinstance(attachment, dict) or attachment.get("type") != "image":
                continue
            asset = self.chat_image_store.resolve(str(attachment.get("asset_id") or attachment.get("id") or ""))
            if not asset:
                continue
            summary = str(attachment.get("summary") or "").strip()
            if not summary:
                summary = await self.describe_image(asset)
            prepared.append(
                {
                    "type": "image",
                    "asset_id": asset["id"],
                    "url": asset["url"],
                    "mime": asset["mime"],
                    "summary": summary,
                }
            )
        return prepared

    async def describe_image(self, asset: dict) -> str:
        if not getattr(self.settings, "vision_enabled", True):
            return "图片理解未启用。"
        path = Path(asset.get("path") or "")
        if not path.exists():
            return "图片文件不存在，无法理解。"
        try:
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            data_url = f"data:{asset.get('mime') or 'image/png'};base64,{encoded}"
            payload = {
                "model": self.settings.vision_model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": "请用中文简洁描述这张图片的主体、场景、文字和可能的情绪。不要编造看不见的内容，80字以内。",
                            },
                            {"type": "image_url", "image_url": {"url": data_url}},
                        ],
                    }
                ],
                "stream": False,
                "temperature": 0.1,
                "max_tokens": 180,
            }
            async with httpx.AsyncClient(timeout=float(getattr(self.settings, "vision_timeout", 45.0))) as client:
                resp = await client.post(self.settings.vision_url, json=payload)
            resp.raise_for_status()
            summary = extract_chat_message_text(resp.json()).strip()
            return compact_text(summary, 260) or "图片已收到，但没有识别出明确内容。"
        except Exception as exc:
            return f"图片理解失败：{exc}"

    def compose_user_request_text(self, user_text: str, attachments: list[dict]) -> str:
        text = user_text.strip() or "请看看这张图片。"
        image_lines = []
        for index, item in enumerate(attachments, start=1):
            if item.get("type") == "image":
                image_lines.append(f"图片{index}摘要：{item.get('summary') or '未生成摘要'}")
        if image_lines:
            text += "\n\n用户随消息发送了图片。你只能基于图片摘要理解图片，不要假装看到了摘要以外的细节：\n" + "\n".join(image_lines)
        return text

    def choose_reply_sticker(self, user_text: str, reply_text: str, source: str) -> list[dict]:
        session_id = self.conversation.get("id") or source or "web"
        intent = self.sticker_policy.choose_intent(
            self.settings,
            session_id=session_id,
            user_text=user_text,
            reply_text=reply_text,
            source=source,
        )
        if not intent.get("send"):
            self.sticker_policy.mark_text_only(session_id)
            return []
        sticker = self.sticker_store.choose(str(intent.get("tag") or ""), avoid_id=str(intent.get("avoid_id") or ""), channel="web")
        if not sticker:
            self.sticker_policy.mark_text_only(session_id)
            return []
        self.sticker_policy.mark_sent(session_id, sticker["id"])
        self.sticker_store.mark_used(sticker["id"])
        return [
            {
                "type": "sticker",
                "asset_id": sticker["id"],
                "url": sticker["url"],
                "file": sticker.get("file") or "",
                "send_file": sticker.get("send_file") or "",
                "send_path": sticker.get("send_path") or "",
                "thumbnail": sticker.get("thumbnail") or sticker.get("url") or "",
                "mime": sticker.get("mime") or "image/png",
                "tag": sticker.get("tag") or "",
                "name": sticker.get("name") or "表情包",
            }
        ]

    def memory_observation_text(self, user_text: str, attachments: list[dict]) -> str:
        if not getattr(self.settings, "vision_memory_extract_enabled", False):
            return user_text
        image_summaries = [
            str(item.get("summary") or "").strip()
            for item in attachments or []
            if item.get("type") == "image" and item.get("summary")
        ]
        if not image_summaries:
            return user_text
        return user_text + "\n\n图片摘要（仅在通过记忆准入时才可记住）：\n" + "\n".join(image_summaries)

    async def maybe_compact_conversation(self, conversation_id: str = "") -> None:
        if not getattr(self.settings, "context_compaction_enabled", True):
            return
        conversation = self.conversation_store.load(conversation_id) if conversation_id else self.conversation
        if not conversation:
            return
        history = conversation.get("messages") or []
        keep_messages = max(4, int(getattr(self.settings, "context_keep_recent_turns", 10)) * 2)
        if len(history) <= max(36, keep_messages + 8):
            return
        compacted_until = int(conversation.get("compacted_until") or 0)
        cutoff = max(0, len(history) - keep_messages)
        if cutoff <= compacted_until:
            return
        estimated_chars = sum(len(str(item.get("content") or "")) for item in history)
        window_chars = int(getattr(self.settings, "context_window_tokens", 8192) * 2.2)
        threshold = window_chars * float(getattr(self.settings, "context_compaction_ratio", 0.7))
        if estimated_chars < threshold and len(history) < 60:
            return
        chunk = history[compacted_until:cutoff]
        if not chunk:
            return
        old_summary = str(conversation.get("context_summary") or "")
        transcript = "\n".join(
            f"{item.get('role')}: {compact_text(item.get('content') or attachment_text(item.get('attachments') or []), 220)}"
            for item in chunk
        )
        prompt = (
            "请更新一份中文会话摘要，用于长期聊天上下文压缩。"
            "保留用户偏好、重要事实、未完成事项、关系语气、正在讨论的项目和关键结论。"
            "删除寒暄、重复句、工具原始数据和无意义细节。"
            f"摘要不超过 {int(getattr(self.settings, 'context_summary_max_chars', 1200))} 字。\n\n"
            f"已有摘要：\n{old_summary or '无'}\n\n新增需要压缩的消息：\n{transcript}"
        )
        try:
            summary = await self.complete_llm_text(
                [{"role": "system", "content": "你是会话摘要器，只输出摘要正文。"}, {"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=700,
                timeout=20.0,
            )
        except Exception as exc:
            self.trace_log(self.current_trace_id, f"context_compaction failed: {exc}")
            return
        summary = compact_text(summary, int(getattr(self.settings, "context_summary_max_chars", 1200)))
        layers = list(self.conversation.get("context_summary_layers") or [])
        layers.insert(0, {"created_at": time.strftime("%Y-%m-%d %H:%M:%S"), "until": cutoff, "summary": summary})
        layers = layers[: max(1, int(getattr(self.settings, "context_summary_max_layers", 3)))]
        updated = self.conversation_store.update(
            conversation["id"],
            {"context_summary": summary, "context_summary_layers": layers, "compacted_until": cutoff},
        )
        if updated and self.conversation.get("id") == updated.get("id"):
            self.conversation = updated
            self.messages = self.build_llm_messages(self.conversation)

    async def remember_turn_safely(self, user_text: str, assistant_text: str) -> None:
        try:
            await self.memory_store.observe_turn(
                self.settings,
                user_text,
                assistant_text,
                self.extract_memories_with_llm,
                mode=memory_mode(self.settings),
            )
        except Exception as exc:
            print(f"[memory] turn update failed: {exc}", flush=True)

    def build_llm_messages(self, conversation: dict) -> list[dict[str, str]]:
        messages = [{"role": "system", "content": self.settings.system}]
        for item in conversation.get("messages") or []:
            role = item.get("role")
            content = item.get("content")
            attachments_note = attachment_text(item.get("attachments") or [])
            if role in {"user", "assistant"} and (content or attachments_note):
                full_content = content or ""
                if attachments_note:
                    full_content += "\n" + attachments_note
                messages.append({"role": role, "content": full_content.strip()})
        return messages

    def draft_conversation(self) -> dict:
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        return {
            "id": "",
            "title": "新的对话",
            "created_at": now,
            "updated_at": now,
            "archived": False,
            "favorite": False,
            "summary": "",
            "messages": [],
            "draft": True,
        }

    def persist_messages(self, messages: list[dict], title_hint: str | None = None) -> None:
        if not self.conversation.get("id"):
            self.conversation = self.conversation_store.create(title_hint)
        self.conversation = self.conversation_store.append_messages(
            self.conversation["id"],
            messages,
            title_hint=title_hint,
        )

    def persist_assistant_reply(self, text: str, *, attachments: list[dict] | None = None, source: str = "") -> None:
        parts = split_reply_messages(text)
        if not parts:
            return
        items = []
        for index, part in enumerate(parts):
            item = {"role": "assistant", "content": part}
            if source:
                item["source"] = source
            if attachments and index == len(parts) - 1:
                item["attachments"] = attachments
            items.append(item)
        self.persist_messages(items)

    def build_contextual_request_messages(self, user_text: str, request_user_text: str) -> list[dict[str, str]]:
        messages = list(self.messages)
        memory_context = self.memory_store.format_context(self.settings, user_text, mode=memory_mode(self.settings))

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
        context_summary = str(self.conversation.get("context_summary") or "").strip()
        if context_summary:
            old_content += "\n\n会话压缩摘要（较早聊天的浓缩记录，可能不完整，但比遗忘更可靠）：\n" + context_summary
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
            or re.search(r"(当前|现在|几点|几号|星期|最新|实时|热点|新闻|搜索|查一下|网上|天气|价格|汇率|网址|地图|地址|位置|附近|周边|路线|导航|怎么走|距离|在哪|在哪里|属于哪里|属于哪|哪个城市|哪个省|哪个区|哪个县|https?://)", user_text, flags=re.I)
        )
        if not tool_signal:
            return None

        call = heuristic_call
        if self.settings.tools_auto_call and not heuristic_call:
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
            "model": active_llm_model(self.settings),
            "messages": messages,
            "stream": False,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if getattr(self.settings, "thinking_enabled", False):
            payload["enable_thinking"] = True
        async with httpx.AsyncClient(timeout=timeout or self.settings.tools_timeout) as client:
            resp = await client.post(active_llm_url(self.settings), json=payload, headers=llm_headers(self.settings))
            if resp.status_code == 400 and payload.pop("enable_thinking", None):
                resp = await client.post(active_llm_url(self.settings), json=payload, headers=llm_headers(self.settings))
        resp.raise_for_status()
        data = resp.json()
        text = clean_reply_text(extract_chat_message_text(data))
        return text

    async def complete_llm_continuation(
        self,
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
            "model": active_llm_model(self.settings),
            "messages": retry_messages,
            "stream": False,
            "temperature": temperature,
            "max_tokens": min(220, max(80, active_max_tokens(self.settings))),
        }
        async with httpx.AsyncClient(timeout=timeout or self.settings.tools_timeout) as client:
            resp = await client.post(active_llm_url(self.settings), json=payload, headers=llm_headers(self.settings))
        resp.raise_for_status()
        return clean_reply_text(extract_chat_message_text(resp.json()))

    async def stream_llm(self, request_messages: list[dict[str, str]], text_queue: asyncio.Queue, allow_thinking: bool = True) -> str:
        # llama.cpp exposes an OpenAI-compatible SSE stream. We forward each
        # text delta to the page immediately, while buffering sentence-sized
        # pieces for TTS.
        payload = {
            "model": active_llm_model(self.settings),
            "messages": request_messages,
            "stream": True,
            "temperature": active_temperature(self.settings),
            "max_tokens": active_max_tokens(self.settings),
        }
        if active_dialog_mode(self.settings) == "local":
            payload.update(
                {
                    "top_p": 0.95,
                    "repeat_penalty": 1.18,
                    # llama.cpp DRY sampling：检测重复短语并惩罚，比单纯的 repeat_penalty 更精准
                    "dry_multiplier": 0.8,
                    "dry_base": 1.75,
                    "dry_allowed_length": 2,
                    "dry_penalty_last_n": -1,
                    "seed": int(time.time() * 1000) % 2147483647,
                }
            )
        if allow_thinking and getattr(self.settings, "thinking_enabled", False):
            payload["enable_thinking"] = True
        buffer = ""
        full_answer = ""
        reasoning_filter = ReasoningStreamFilter()
        first_chunk = True
        first_token = True
        started = time.perf_counter()
        finish_reason = ""

        async with httpx.AsyncClient(timeout=None) as client:
            stream_response = client.stream("POST", active_llm_url(self.settings), json=payload, headers=llm_headers(self.settings))
            async with stream_response as resp:
                if resp.status_code == 400 and payload.pop("enable_thinking", None):
                    await resp.aclose()
                    return await self.stream_llm(request_messages, text_queue, allow_thinking=False)
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

                    reason = extract_finish_reason(data)
                    if reason:
                        finish_reason = reason

                    text = reasoning_filter.feed(extract_llm_delta(data))
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

        tail_text = reasoning_filter.flush()
        if tail_text:
            full_answer += tail_text
            buffer += tail_text
            await self.send_event("llm_delta", text=tail_text)

        if finish_reason:
            self.trace_log(self.current_trace_id, f"llm:finish_reason {finish_reason}")
        if finish_reason == "length":
            try:
                continuation = await self.complete_llm_continuation(
                    request_messages,
                    clean_reply_text(full_answer),
                    temperature=active_temperature(self.settings),
                )
            except Exception as exc:
                continuation = ""
                self.trace_log(self.current_trace_id, f"llm:continuation_failed {exc}")
            if continuation:
                self.trace_log(self.current_trace_id, f"llm:continuation len={len(continuation)}")
                full_answer += continuation
                buffer += continuation
                await self.send_event("llm_delta", text=continuation)

        if buffer.strip():
            tts_text = clean_for_tts(buffer)
            if tts_text:
                await text_queue.put(tts_text)

        return clean_reply_text(full_answer)

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
        max_history_messages = max(2, active_history_turns(self.settings) * 2)
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



