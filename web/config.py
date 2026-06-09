from __future__ import annotations

import json
import os
import argparse
from dataclasses import asdict, dataclass
from pathlib import Path

MASKED_SECRET_CHARS = "*"
DEFAULT_TTS_VOLUME = 0.88
DEFAULT_TTS_FADE_MS = 5

DEFAULT_SYSTEM = (
    "你以“满穗”的人设和聊天风格与用户对话。她是 24 岁女生，福建人，二本毕业，"
    "身高 158cm，体重 50kg，看起来软软小小的，但心里很有主意，不是那种随便被拿捏的人。"
    "她开朗活泼，内心强大，独立、有主见；平时有点懒，不太愿意出门，"
    "但一提到好吃的就会突然来精神，梦想是吃遍天下美食。\n\n"
    "对话目标：像一个真实朋友在微信或语音里自然回话。不要像客服，不要像说明书，"
    "不要把人设资料一条条背出来。回复要轻松、自然、有生活感，可以可爱，可以嘴硬，"
    "可以犯懒，也可以偶尔小小发疯，但要真诚、有自己的判断。\n\n"
    "熟人判断 workflow：\n"
    "- 根据对方的语气、称呼、玩笑密度和上下文判断亲近程度。\n"
    "- 对熟人：可以活泼一点、皮一点，敢吐槽，偶尔撒娇、装傻、嘴硬，但不要油腻。\n"
    "- 对不熟的人：自然、礼貌、轻松，不端着，也不要一上来过分亲密。\n\n"
    "语言风格：\n"
    "- 始终中文回复，优先短句，适合语音 TTS。大多数回复控制在 5 到 30 个中文字；需要解释时才稍微长一点。\n"
    "- 回复长度要随机应变：有时很短，有时稍微展开，不要每次都同样长度。\n"
    "- 轻松口语化，可以调侃、反问、吐槽、犯懒、嘴硬，也可以偶尔用网络梗；但不要形成固定口癖。\n"
    "- 不刻意使用福建方言或地区口头禅。福建背景只作为人物经历存在，不靠方言硬凹。\n"
    "- 可以笑，可以闹一点，但不要固定用同一种笑声、开头或感叹词。\n"
    "- 可以偶尔口误、停顿或简化表达，让话更像真人即时回复。\n"
    "- 不确定时说“我想想”“这个我不太懂”“你等我捋一下”，不要装懂。\n\n"
    "互动规则：\n"
    "- 说话不要太客气，像熟人聊天。\n"
    "- 熟人让你做不合理的小事，可以懒懒地拒绝，也可以调侃回去。\n"
    "- 表达观点坦率直接，有自己的判断，不要一味顺着用户。\n"
    "- 不要轻易用 emoji；只有气氛明显起来、情绪起伏大时才偶尔用。\n"
    "- 句尾不要经常用语气词。不要固定用某个结尾，尤其不要总用“呢”“呀”“啦”“嘛”“~”。\n"
    "- 不要长篇说教、鸡汤、客服式总结。不要连续追问。\n"
    "- 不要输出 END。不要输出括号动作描写。不要编造当前现实行动、实时位置或真实经历。\n"
    "- 用户要求“跟着我说/重复/复读”时，准确重复用户给出的文本，不额外发挥。\n"
    "- 你能看到最近聊天记录，把它当作工作记忆；用户问刚才说了什么，要根据记录回答。\n\n"
    "风格样例，只学习味道，不要机械复读：\n"
    "Q：你今天出门了吗？A：没有，我和床绑定了。\n"
    "Q：你想吃什么？A：先来点辣的，我清醒一下。\n"
    "Q：你是不是又懒了？A：别乱说，我是节能模式。\n"
    "Q：陪我出去走走？A：可以，但你得拿吃的诱惑我。\n"
    "Q：你生气了？A：没有，就是暂时不想理人。\n"
    "Q：你这么小能打赢谁？A：我靠气势赢，懂不懂。\n"
    "Q：你怎么突然精神了？A：因为我听见吃饭两个字了。"
)


@dataclass
class SessionSettings:
    asr_mode: str
    asr_url: str
    asr_model: str
    asr_timeout: float
    asr_max_tokens: int
    llm_url: str
    llm_model: str
    llm_api_key: str
    temperature: float
    max_tokens: int
    history_turns: int
    system: str
    ui_font_scale: float
    memory_enabled: bool
    memory_extract_enabled: bool
    memory_short_to_mid_days: int
    memory_short_to_mid_count: int
    memory_mid_to_long_days: int
    memory_mid_to_long_count: int
    memory_short_delete_days: int
    memory_mid_downgrade_days: int
    memory_long_downgrade_days: int
    memory_max_context_items: int
    tools_enabled: bool
    tools_auto_call: bool
    tools_timeout: float
    tools_max_result_chars: int
    tts_url: str
    tts_sample_rate: int
    tts_speed: float
    tts_seed: int
    tts_volume: float
    tts_fade_ms: int
    tts_enabled: bool
    vad_threshold: float
    vad_min_silence_ms: int
    vad_speech_pad_ms: int
    pre_speech_ms: int
    min_utterance_ms: int
    max_utterance_sec: float

    @classmethod
    def from_args(cls, args) -> "SessionSettings":
        return cls(
            asr_mode=args.asr_mode,
            asr_url=args.asr_url,
            asr_model=args.asr_model,
            asr_timeout=args.asr_timeout,
            asr_max_tokens=args.asr_max_tokens,
            llm_url=args.llm_url,
            llm_model=args.llm_model,
            llm_api_key=args.llm_api_key,
            temperature=args.temperature,
            max_tokens=args.max_tokens,
            history_turns=args.history_turns,
            system=args.system,
            ui_font_scale=args.ui_font_scale,
            memory_enabled=args.memory_enabled,
            memory_extract_enabled=args.memory_extract_enabled,
            memory_short_to_mid_days=args.memory_short_to_mid_days,
            memory_short_to_mid_count=args.memory_short_to_mid_count,
            memory_mid_to_long_days=args.memory_mid_to_long_days,
            memory_mid_to_long_count=args.memory_mid_to_long_count,
            memory_short_delete_days=args.memory_short_delete_days,
            memory_mid_downgrade_days=args.memory_mid_downgrade_days,
            memory_long_downgrade_days=args.memory_long_downgrade_days,
            memory_max_context_items=args.memory_max_context_items,
            tools_enabled=args.tools_enabled,
            tools_auto_call=args.tools_auto_call,
            tools_timeout=args.tools_timeout,
            tools_max_result_chars=args.tools_max_result_chars,
            tts_url=args.tts_url,
            tts_sample_rate=args.tts_sample_rate,
            tts_speed=args.tts_speed,
            tts_seed=args.tts_seed,
            tts_volume=args.tts_volume,
            tts_fade_ms=args.tts_fade_ms,
            tts_enabled=args.tts_enabled,
            vad_threshold=args.vad_threshold,
            vad_min_silence_ms=args.vad_min_silence_ms,
            vad_speech_pad_ms=args.vad_speech_pad_ms,
            pre_speech_ms=args.pre_speech_ms,
            min_utterance_ms=args.min_utterance_ms,
            max_utterance_sec=args.max_utterance_sec,
        )

    def update_from_dict(self, data: dict) -> None:
        allowed = set(asdict(self))
        for key, value in data.items():
            if key not in allowed or value in (None, ""):
                continue
            current = getattr(self, key)
            try:
                if isinstance(current, bool):
                    parsed = parse_bool_value(value)
                    if parsed is None:
                        continue
                    value = parsed
                elif isinstance(current, int):
                    value = int(value)
                elif isinstance(current, float):
                    value = float(value)
                if key == "ui_font_scale":
                    value = max(0.85, min(1.35, float(value)))
            except (TypeError, ValueError):
                continue
            setattr(self, key, value)


def parse_bool_value(value) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "y", "on", "enabled"}:
            return True
        if normalized in {"false", "0", "no", "n", "off", "disabled"}:
            return False
    return None


def load_persisted_settings(settings: SessionSettings, path: Path) -> SessionSettings:
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                settings.update_from_dict(data)
        except Exception:
            pass
    return settings


def save_persisted_settings(settings: SessionSettings, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(settings), ensure_ascii=False, indent=2), encoding="utf-8")


def mask_secret(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    if len(value) <= 10:
        return f"{value[:3]}{MASKED_SECRET_CHARS * 8}"
    return f"{value[:7]}{MASKED_SECRET_CHARS * 23}{value[-4:]}"


def public_settings(settings: SessionSettings) -> dict:
    data = asdict(settings)
    api_key = str(data.pop("llm_api_key", "") or "")
    data["llm_api_key"] = ""
    data["llm_api_key_set"] = bool(api_key.strip())
    data["llm_api_key_masked"] = mask_secret(api_key)
    return data


def update_llm_api_key(settings: SessionSettings, payload: dict) -> None:
    if "llm_api_key" not in payload:
        return
    raw = payload.pop("llm_api_key")
    if raw is None:
        return
    value = str(raw).strip()
    if not value or MASKED_SECRET_CHARS in value:
        return
    settings.llm_api_key = value


def llm_headers(settings: SessionSettings) -> dict[str, str]:
    api_key = str(settings.llm_api_key or "").strip()
    if not api_key:
        return {}
    return {"Authorization": f"Bearer {api_key}"}


def enable_default_capabilities(settings: SessionSettings) -> None:
    """Compatibility hook kept for older launch scripts.

    Defaults are already supplied by command-line arguments and persisted
    settings. This function must not overwrite saved user choices on restart.
    """
    return None


def add_settings_args(parser) -> None:
    parser.add_argument("--asr-mode", choices=["transcription", "chat"], default="transcription")
    parser.add_argument("--asr-url", default="http://127.0.0.1:8001/v1/audio/transcriptions")
    parser.add_argument("--asr-model", default="qwen3-asr")
    parser.add_argument("--asr-timeout", type=float, default=120)
    parser.add_argument("--asr-max-tokens", type=int, default=256)

    parser.add_argument("--llm-url", default="http://127.0.0.1:8080/v1/chat/completions")
    parser.add_argument("--llm-model", default="qwen3.5-9b")
    parser.add_argument(
        "--llm-api-key",
        default=os.environ.get("BRANCHWHISPER_LLM_API_KEY", os.environ.get("BUDING_LLM_API_KEY", "")),
    )
    parser.add_argument("--temperature", type=float, default=0.35)
    parser.add_argument("--max-tokens", type=int, default=220)
    parser.add_argument("--history-turns", type=int, default=8)
    parser.add_argument("--system", default=DEFAULT_SYSTEM)
    parser.add_argument("--ui-font-scale", type=float, default=1.0)

    parser.add_argument("--memory-enabled", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--memory-extract-enabled", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--memory-short-to-mid-days", type=int, default=60)
    parser.add_argument("--memory-short-to-mid-count", type=int, default=3)
    parser.add_argument("--memory-mid-to-long-days", type=int, default=180)
    parser.add_argument("--memory-mid-to-long-count", type=int, default=5)
    parser.add_argument("--memory-short-delete-days", type=int, default=180)
    parser.add_argument("--memory-mid-downgrade-days", type=int, default=180)
    parser.add_argument("--memory-long-downgrade-days", type=int, default=365)
    parser.add_argument("--memory-max-context-items", type=int, default=12)

    parser.add_argument("--tools-enabled", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--tools-auto-call", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--tools-timeout", type=float, default=12.0)
    parser.add_argument("--tools-max-result-chars", type=int, default=4000)

    parser.add_argument("--tts-url", default="http://127.0.0.1:50000/tts")
    parser.add_argument("--tts-sample-rate", type=int, default=24000)
    parser.add_argument("--tts-speed", type=float, default=1.08)
    parser.add_argument("--tts-seed", type=int, default=42)
    parser.add_argument("--tts-volume", type=float, default=DEFAULT_TTS_VOLUME)
    parser.add_argument("--tts-fade-ms", type=int, default=DEFAULT_TTS_FADE_MS)
    parser.add_argument("--tts-enabled", action=argparse.BooleanOptionalAction, default=True)

    parser.add_argument("--vad-device", choices=["cpu", "cuda", "auto"], default="cpu")
    parser.add_argument("--vad-threshold", type=float, default=0.50)
    parser.add_argument("--vad-min-silence-ms", type=int, default=350)
    parser.add_argument("--vad-speech-pad-ms", type=int, default=120)
    parser.add_argument("--pre-speech-ms", type=int, default=250)
    parser.add_argument("--min-utterance-ms", type=int, default=250)
    parser.add_argument("--max-utterance-sec", type=float, default=15.0)
