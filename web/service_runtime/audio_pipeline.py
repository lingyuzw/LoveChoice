from __future__ import annotations

import base64
import io
import json
import re
import wave
from typing import Any

import httpx
import numpy as np

MIC_SAMPLE_RATE = 16000
END_PUNCT = "\u3002\uff01\uff1f\uff1b.!?"
SOFT_PUNCT = "\uff0c\u3001,~\uff5e"


def wav_bytes_from_float32(audio: np.ndarray) -> bytes:
    audio = np.asarray(audio, dtype=np.float32).reshape(-1)
    audio = np.clip(audio, -1.0, 1.0)
    pcm = (audio * 32767.0).astype("<i2")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(MIC_SAMPLE_RATE)
        wav.writeframes(pcm.tobytes())
    return buffer.getvalue()


async def transcribe_audio(settings: Any, audio_bytes: bytes) -> str:
    if settings.asr_mode == "chat":
        return await transcribe_via_chat(settings, audio_bytes)
    return await transcribe_via_transcriptions(settings, audio_bytes)


async def transcribe_via_transcriptions(settings: Any, audio_bytes: bytes) -> str:
    files = {"file": ("speech.wav", audio_bytes, "audio/wav")}
    data = {"model": settings.asr_model}
    async with httpx.AsyncClient(timeout=settings.asr_timeout) as client:
        resp = await client.post(settings.asr_url, data=data, files=files)
    resp.raise_for_status()
    return parse_asr_text(extract_asr_response_text(resp.json()))


async def transcribe_via_chat(settings: Any, audio_bytes: bytes) -> str:
    audio_b64 = base64.b64encode(audio_bytes).decode("ascii")
    payload = {
        "model": settings.asr_model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "audio_url",
                        "audio_url": {"url": f"data:audio/wav;base64,{audio_b64}"},
                    }
                ],
            }
        ],
        "temperature": 0,
        "max_tokens": settings.asr_max_tokens,
    }
    async with httpx.AsyncClient(timeout=settings.asr_timeout) as client:
        resp = await client.post(settings.asr_url, json=payload)
    resp.raise_for_status()
    return parse_asr_text(extract_asr_response_text(resp.json()))


def extract_asr_response_text(data: dict) -> str:
    if isinstance(data.get("text"), str):
        return data["text"]

    choices = data.get("choices") or []
    if choices:
        message = choices[0].get("message") or {}
        if isinstance(message.get("content"), str):
            return message["content"]

    return json.dumps(data, ensure_ascii=False)


def parse_asr_text(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""

    try:
        from qwen_asr import parse_asr_output

        _language, text = parse_asr_output(raw)
        return text.strip()
    except Exception:
        pass

    text = re.sub(r"<\|[^|]+?\|>", "", raw)
    text = re.sub(r"\[[0-9:.]+\s*-\s*[0-9:.]+\]", "", text)
    return text.strip()


def extract_llm_delta(data: dict) -> str:
    choice = (data.get("choices") or [{}])[0]
    delta = choice.get("delta") or {}
    text = delta.get("content")
    if text is None:
        message = choice.get("message") or {}
        text = message.get("content")
    return text or ""


def extract_chat_message_text(data: dict) -> str:
    choices = data.get("choices") or []
    if choices:
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            return content
        text = choices[0].get("text")
        if isinstance(text, str):
            return text
    if isinstance(data.get("content"), str):
        return data["content"]
    return json.dumps(data, ensure_ascii=False)


def should_flush_tts(text: str, first_chunk: bool) -> bool:
    stripped = text.strip()
    if not stripped:
        return False

    if first_chunk:
        if len(stripped) >= 3 and stripped.endswith(tuple(END_PUNCT)):
            return True
        if len(stripped) >= 8 and stripped.endswith(tuple(SOFT_PUNCT)):
            return True
        return len(stripped) >= 14

    if len(stripped) < 32:
        return False
    if len(stripped) >= 42 and stripped.endswith(tuple(END_PUNCT)):
        return True
    if len(stripped) >= 60 and stripped.endswith(tuple(SOFT_PUNCT)):
        return True
    return len(stripped) >= 90


def clean_for_tts(text: str) -> str:
    text = str(text or "").strip()
    if not text:
        return ""

    for marker in ("<|endofprompt|>", "<|im_start|>assistant", "assistant:", "Assistant:"):
        if marker in text:
            text = text.split(marker)[-1]

    text = re.sub(r"<\|.*?\|>", "", text)

    prompt_fragments = (
        "You are a helpful assistant<|endofprompt|>",
        "You are a helpful assistant",
        "You are a helpful",
        "A conversation between User and Assistant",
    )
    for fragment in prompt_fragments:
        text = text.replace(fragment, "")

    text = re.sub(r"(^|\n)\s*(system|user|assistant)\s*[:：]\s*", "\\1", text, flags=re.I)
    text = re.sub(r"\s*END\s*$", "", text, flags=re.IGNORECASE)
    text = text.replace("<s>", "").replace("</s>", "")
    text = re.sub(r"\s+", " ", text).strip()

    if re.fullmatch(r"[A-Za-z0-9\s,.'\"!?:;_\-<>|/]+", text or ""):
        if re.search(r"(helpful|assistant|system|prompt|user|conversation)", text, flags=re.I):
            return ""

    return text
