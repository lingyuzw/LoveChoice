from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any


STICKER_ACTIVITY = {
    "off": {"probability": 0.0, "cooldown": 999999, "daily_limit": 0, "max_streak": 0},
    "low": {"probability": 0.18, "cooldown": 480, "daily_limit": 8, "max_streak": 1},
    "standard": {"probability": 0.36, "cooldown": 240, "daily_limit": 20, "max_streak": 1},
    "active": {"probability": 0.62, "cooldown": 90, "daily_limit": 60, "max_streak": 2},
    "very_active": {"probability": 0.82, "cooldown": 45, "daily_limit": 120, "max_streak": 2},
}

STICKER_TAG_RULES = [
    ("早安", r"早安|早上好|起床"),
    ("晚安", r"晚安|睡觉|困了|睡不着"),
    ("吃饭", r"吃饭|好吃|饿|火锅|奶茶|辣"),
    ("安慰", r"累|难受|不开心|委屈|烦|压力|崩溃|抱抱"),
    ("开心", r"开心|哈哈|笑死|好耶|太棒|夸|喜欢"),
    ("无语", r"无语|离谱|服了|绷不住|尴尬"),
    ("疑惑", r"什么|为啥|为什么|怎么会|真的假的"),
    ("鼓励", r"加油|努力|考试|面试|工作|项目|训练"),
    ("撒娇", r"想你|陪我|理我|哄我"),
]

SERIOUS_TEXT_RE = re.compile(r"(报错|失败|异常|HTTP|Traceback|代码|接口|配置|安装|日志|测试失败|无法连接)", re.I)
TOOL_TEXT_RE = re.compile(r"(天气|新闻|搜索|股价|汇率|地图|路线|网址|网页|查询结果|API)", re.I)


@dataclass
class StickerRuntimeState:
    last_sent_at: float = 0.0
    last_sticker_id: str = ""
    streak: int = 0
    sent_today: int = 0
    day: str = ""


class StickerPolicy:
    def __init__(self) -> None:
        self.sessions: dict[str, StickerRuntimeState] = {}

    def choose_intent(self, settings: Any, *, session_id: str, user_text: str, reply_text: str, source: str = "web") -> dict:
        if not getattr(settings, "stickers_enabled", True):
            return {"send": False, "reason": "disabled"}
        activity = str(getattr(settings, "sticker_activity", "active") or "active")
        if activity == "custom":
            config = {
                "probability": float(getattr(settings, "sticker_custom_probability", 0.65) or 0.65),
                "cooldown": int(getattr(settings, "sticker_cooldown_sec", 90) or 90),
                "daily_limit": int(getattr(settings, "sticker_daily_limit", 60) or 60),
                "max_streak": int(getattr(settings, "sticker_max_streak", 2) or 2),
            }
        else:
            config = STICKER_ACTIVITY.get(activity, STICKER_ACTIVITY["active"])
        if config["probability"] <= 0:
            return {"send": False, "reason": "off"}

        text = f"{user_text}\n{reply_text}"
        if SERIOUS_TEXT_RE.search(text) or TOOL_TEXT_RE.search(user_text):
            return {"send": False, "reason": "serious_or_tool"}

        now = time.time()
        day = time.strftime("%Y-%m-%d")
        state = self.sessions.setdefault(session_id or "default", StickerRuntimeState(day=day))
        if state.day != day:
            state.day = day
            state.sent_today = 0
            state.streak = 0
        if state.sent_today >= config["daily_limit"]:
            return {"send": False, "reason": "daily_limit"}
        if now - state.last_sent_at < config["cooldown"]:
            return {"send": False, "reason": "cooldown"}
        if state.streak >= config["max_streak"]:
            return {"send": False, "reason": "streak"}

        tag = self.infer_tag(user_text, reply_text)
        score = self.score(text, tag)
        if score < config["probability"]:
            return {"send": False, "reason": "probability", "tag": tag, "score": score}
        return {"send": True, "tag": tag, "reason": "matched", "avoid_id": state.last_sticker_id}

    def mark_sent(self, session_id: str, sticker_id: str) -> None:
        state = self.sessions.setdefault(session_id or "default", StickerRuntimeState(day=time.strftime("%Y-%m-%d")))
        state.last_sent_at = time.time()
        state.last_sticker_id = sticker_id
        state.streak += 1
        state.sent_today += 1

    def mark_text_only(self, session_id: str) -> None:
        state = self.sessions.setdefault(session_id or "default", StickerRuntimeState(day=time.strftime("%Y-%m-%d")))
        state.streak = 0

    def infer_tag(self, user_text: str, reply_text: str) -> str:
        text = f"{user_text}\n{reply_text}"
        for tag, pattern in STICKER_TAG_RULES:
            if re.search(pattern, text, re.I):
                return tag
        return "开心"

    def score(self, text: str, tag: str) -> float:
        value = 0.28
        if tag:
            value += 0.22
        if re.search(r"[!！~～]|哈哈|笑死|好耶|抱抱|呜|哎呀", text):
            value += 0.25
        if len(text) <= 80:
            value += 0.12
        return min(1.0, value)
