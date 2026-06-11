from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any

import httpx

from service_runtime.audio_pipeline import extract_chat_message_text, strip_reasoning_text


STICKER_EMOTIONS = [
    "laugh",
    "smug",
    "angry",
    "sad",
    "comfort",
    "confused",
    "shock",
    "sleepy",
    "cute",
    "bye",
    "silent",
    "agree",
    "reject",
]


def default_sticker_analysis() -> dict:
    return {
        "emotion": "laugh",
        "intensity": 3,
        "tags": [],
        "scene": [],
        "avoid": [],
        "caption": "",
        "ocr_text": "",
        "description": "",
        "confidence": 0.0,
    }


def normalize_analysis(data: dict[str, Any]) -> dict:
    result = default_sticker_analysis()
    emotion = str(data.get("emotion") or "").strip().lower()
    result["emotion"] = emotion if emotion in STICKER_EMOTIONS else "laugh"
    try:
        result["intensity"] = max(1, min(5, int(float(data.get("intensity", 3)))))
    except Exception:
        result["intensity"] = 3
    for key in ("tags", "scene", "avoid"):
        value = data.get(key)
        if isinstance(value, list):
            result[key] = [str(item).strip()[:32] for item in value if str(item).strip()][:8]
        elif isinstance(value, str) and value.strip():
            result[key] = [item.strip()[:32] for item in re.split(r"[,，/、\s]+", value) if item.strip()][:8]
    for key in ("caption", "ocr_text", "description"):
        result[key] = str(data.get(key) or "").strip()[:300]
    try:
        result["confidence"] = max(0.0, min(1.0, float(data.get("confidence", 0.0))))
    except Exception:
        result["confidence"] = 0.0
    return result


def extract_json_object(text: str) -> dict:
    text = strip_reasoning_text(str(text or "")).strip()
    if not text:
        raise ValueError("Vision 返回为空")
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, flags=re.S)
    if not match:
        raise ValueError("Vision 未返回 JSON 对象")
    parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("Vision JSON 不是对象")
    return parsed


class StickerVisionAnalyzer:
    def __init__(self, settings) -> None:
        self.settings = settings

    async def analyze(self, image_path: Path, mime: str = "image/png") -> dict:
        if not getattr(self.settings, "vision_enabled", True):
            raise RuntimeError("图片理解未启用")
        encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
        data_url = f"data:{mime or 'image/png'};base64,{encoded}"
        payload = {
            "model": self.settings.vision_model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "你是表情包入库分析器。请只输出 JSON 对象，不要 Markdown。"
                                "字段必须包含 emotion, intensity, tags, scene, avoid, caption, ocr_text, description, confidence。"
                                "emotion 只能从以下值选择：laugh, smug, angry, sad, comfort, confused, shock, sleepy, cute, bye, silent, agree, reject。"
                                "intensity 为 1-5；tags/scene/avoid 为中文短词数组。"
                                "caption 用一句中文说明这个表情适合什么时候发。"
                                "ocr_text 填图片里能看清的文字，没有则空字符串。"
                            ),
                        },
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
            "stream": False,
            "temperature": 0.1,
            "max_tokens": 420,
        }
        async with httpx.AsyncClient(timeout=float(getattr(self.settings, "vision_timeout", 45.0))) as client:
            resp = await client.post(self.settings.vision_url, json=payload)
        resp.raise_for_status()
        text = extract_chat_message_text(resp.json())
        return normalize_analysis(extract_json_object(text))
