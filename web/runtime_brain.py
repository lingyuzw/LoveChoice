from __future__ import annotations

import html as html_lib
import json
import re
import sqlite3
import time
import uuid
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

import httpx


SECONDS_PER_DAY = 86400
MEMORY_LAYERS = {"short", "mid", "long"}


def now_ts() -> float:
    return time.time()


def ts_to_text(value: float | int | None) -> str:
    if not value:
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(value)))


def days_since(timestamp: float | int | None, now: float | None = None) -> float:
    if not timestamp:
        return 999999.0
    return max(0.0, ((now or now_ts()) - float(timestamp)) / SECONDS_PER_DAY)


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def compact_text(text: str, limit: int = 280) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def normalize_key(text: str) -> str:
    text = re.sub(r"\s+", "", str(text or "").lower())
    return text[:160]


class MemoryStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def session(self):
        conn = self.connect()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init_db(self) -> None:
        with self.session() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_items (
                    id TEXT PRIMARY KEY,
                    key TEXT NOT NULL,
                    key_norm TEXT NOT NULL UNIQUE,
                    value TEXT NOT NULL,
                    layer TEXT NOT NULL DEFAULT 'short',
                    count INTEGER NOT NULL DEFAULT 0,
                    confidence REAL NOT NULL DEFAULT 0.4,
                    importance REAL NOT NULL DEFAULT 0.4,
                    first_seen_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL,
                    last_changed_at REAL NOT NULL,
                    pinned INTEGER NOT NULL DEFAULT 0,
                    source TEXT NOT NULL DEFAULT 'chat'
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_events (
                    id TEXT PRIMARY KEY,
                    item_id TEXT NOT NULL,
                    seen_at REAL NOT NULL,
                    source TEXT NOT NULL DEFAULT 'chat',
                    excerpt TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(item_id) REFERENCES memory_items(id) ON DELETE CASCADE
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_layer ON memory_items(layer)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_last_seen ON memory_items(last_seen_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_events_item_seen ON memory_events(item_id, seen_at)")
            # 记忆类型: semantic_fact / episodic_event
            try:
                conn.execute("ALTER TABLE memory_items ADD COLUMN memory_type TEXT NOT NULL DEFAULT 'semantic_fact'")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE memory_items ADD COLUMN time_text TEXT NOT NULL DEFAULT ''")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE memory_items ADD COLUMN event_date TEXT NOT NULL DEFAULT ''")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE memory_items ADD COLUMN time_of_day TEXT NOT NULL DEFAULT ''")
            except sqlite3.OperationalError:
                pass
            # embedding 列，用于向量语义检索（余弦相似度）
            try:
                conn.execute("ALTER TABLE memory_items ADD COLUMN embedding BLOB")
            except sqlite3.OperationalError:
                pass
    async def _get_embedding(self, text: str, llm_url: str = "http://127.0.0.1:8080/v1/embeddings") -> list[float] | None:
        """调用 llama.cpp /v1/embeddings 获取文本的向量表示。"""
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                resp = await client.post(
                    llm_url,
                    json={"input": text, "model": "text-embedding"},
                )
            resp.raise_for_status()
            data = resp.json()
            embedding = (data.get("data") or [{}])[0].get("embedding")
            return embedding
        except Exception:
            return None
    def list_memories(self, settings: Any, limit: int = 200, query: str = "", layer: str = "") -> list[dict]:
        self.apply_decay(settings)
        clauses = []
        params: list[Any] = []
        if layer in MEMORY_LAYERS:
            clauses.append("layer = ?")
            params.append(layer)
        if query:
            clauses.append("(key LIKE ? OR value LIKE ?)")
            like = f"%{query}%"
            params.extend([like, like])
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        sql = f"""
            SELECT * FROM memory_items
            {where}
            ORDER BY pinned DESC,
                     CASE layer WHEN 'long' THEN 3 WHEN 'mid' THEN 2 ELSE 1 END DESC,
                     last_seen_at DESC
            LIMIT ?
        """
        params.append(max(1, min(500, int(limit))))
        with self.session() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self.row_to_dict(row) for row in rows]

    def row_to_dict(self, row: sqlite3.Row) -> dict:
        data = dict(row)
        data["pinned"] = bool(data.get("pinned"))
        data["first_seen_text"] = ts_to_text(data.get("first_seen_at"))
        data["last_seen_text"] = ts_to_text(data.get("last_seen_at"))
        data["last_changed_text"] = ts_to_text(data.get("last_changed_at"))
        # 新字段 safe defaults
        data.setdefault("memory_type", "semantic_fact")
        data.setdefault("time_text", "")
        data.setdefault("event_date", "")
        data.setdefault("time_of_day", "")
        return data

    def create_memory(self, payload: dict, source: str = "manual") -> dict:
        key = compact_text(payload.get("key") or payload.get("value") or "", 120)
        value = compact_text(payload.get("value") or key, 1000)
        if not key or not value:
            raise ValueError("memory key/value is empty")
        layer = payload.get("layer") if payload.get("layer") in MEMORY_LAYERS else "mid"
        item = {
            "key": key,
            "value": value,
            "layer": layer,
            "confidence": float(payload.get("confidence", 0.85)),
            "importance": float(payload.get("importance", 0.75)),
            "pinned": bool(payload.get("pinned", False)),
        }
        return self.upsert_memory(item, source=source, excerpt=value)

    def update_memory(self, memory_id: str, payload: dict) -> dict | None:
        allowed = {"key", "value", "layer", "confidence", "importance", "pinned"}
        sets = []
        params: list[Any] = []
        for key, value in payload.items():
            if key not in allowed:
                continue
            if key == "layer" and value not in MEMORY_LAYERS:
                continue
            if key == "pinned":
                value = 1 if value else 0
            if key in {"confidence", "importance"}:
                value = clamp(float(value), 0.0, 1.0)
            if key == "key":
                sets.append("key_norm = ?")
                params.append(normalize_key(value))
            sets.append(f"{key} = ?")
            params.append(value)
        if not sets:
            return self.get_memory(memory_id)
        sets.append("last_changed_at = ?")
        params.append(now_ts())
        params.append(memory_id)
        with self.session() as conn:
            conn.execute(f"UPDATE memory_items SET {', '.join(sets)} WHERE id = ?", params)
        return self.get_memory(memory_id)

    def get_memory(self, memory_id: str) -> dict | None:
        with self.session() as conn:
            row = conn.execute("SELECT * FROM memory_items WHERE id = ?", (memory_id,)).fetchone()
        return self.row_to_dict(row) if row else None

    def delete_memory(self, memory_id: str) -> bool:
        with self.session() as conn:
            cur = conn.execute("DELETE FROM memory_items WHERE id = ?", (memory_id,))
        return cur.rowcount > 0

    async def observe_turn(self, settings: Any, user_text: str, assistant_text: str = "", llm_extract_fn=None) -> list[dict]:
        if not getattr(settings, "memory_enabled", True) or not getattr(settings, "memory_extract_enabled", True):
            return []
        # Memory extraction must never block or break the dialogue turn.
        candidates = []
        if llm_extract_fn:
            try:
                candidates = await extract_memory_candidates_llm(user_text, llm_extract_fn, assistant_text=assistant_text)
            except Exception as exc:
                print(f"[memory] LLM extraction failed, falling back to rules: {exc}", flush=True)
        if not candidates:
            try:
                candidates = extract_memory_candidates(user_text)
            except Exception as exc:
                print(f"[memory] rule extraction failed: {exc}", flush=True)
                candidates = []
        saved = []
        for candidate in candidates:
            try:
                mem = self.upsert_memory(candidate, source=candidate.get("source", "chat"), excerpt=user_text)
            except Exception as exc:
                print(f"[memory] skipped invalid candidate: {exc}; candidate={candidate!r}", flush=True)
                continue
            # TODO(4E): 异步获取 embedding 并写入 memory_items.embedding 列
            #   embedding = await self._get_embedding(candidate["value"])
            #   if embedding:
            #       with self.session() as conn:
            #           conn.execute("UPDATE memory_items SET embedding = ? WHERE id = ?",
            #                        (sqlite3.Binary(struct.pack(f'{len(embedding)}f', *embedding)), mem["id"]))
            saved.append(mem)
        try:
            self.apply_decay(settings)
        except Exception as exc:
            print(f"[memory] decay failed: {exc}", flush=True)
        return saved

    def upsert_memory(self, item: dict, source: str = "chat", excerpt: str = "") -> dict:
        now = now_ts()
        key = compact_text(item.get("key") or item.get("value") or "", 120)
        value = compact_text(item.get("value") or key, 1000)
        if not key or not value:
            raise ValueError("memory key/value is empty")
        key_norm = normalize_key(key)
        layer = item.get("layer") if item.get("layer") in MEMORY_LAYERS else "short"
        confidence_gain = 0.3 if source == "manual" else float(item.get("confidence_gain", 0.12))
        importance = clamp(float(item.get("importance", 0.45)), 0.0, 1.0)
        memory_type = item.get("memory_type", "semantic_fact")
        time_text = item.get("time_text", "")
        event_date = item.get("event_date", "")
        time_of_day = item.get("time_of_day", "")

        # episodic_event: force key_norm to include time so different-days events stay separate
        if memory_type == "episodic_event" and time_text:
            key_norm = normalize_key(key) + ":" + normalize_key(time_text)

        with self.session() as conn:
            confidence = clamp(float(item.get("confidence", 0.45)), 0.0, 1.0)
            if source == "manual":
                layer = item.get("layer") if item.get("layer") in MEMORY_LAYERS else "mid"
                confidence = max(confidence, 0.85)

            insert_columns = (
                "id",
                "key",
                "key_norm",
                "value",
                "layer",
                "count",
                "confidence",
                "importance",
                "first_seen_at",
                "last_seen_at",
                "last_changed_at",
                "pinned",
                "source",
                "memory_type",
                "time_text",
                "event_date",
                "time_of_day",
            )
            insert_values = (
                str(uuid.uuid4()),
                key,
                key_norm,
                value,
                layer,
                1,
                confidence,
                importance,
                now,
                now,
                now,
                1 if item.get("pinned") else 0,
                source,
                memory_type,
                time_text,
                event_date,
                time_of_day,
            )
            placeholders = ", ".join("?" for _ in insert_values)
            conn.execute(
                f"INSERT INTO memory_items ({', '.join(insert_columns)}) VALUES ({placeholders})"
                " ON CONFLICT(key_norm) DO UPDATE SET"
                f" value = excluded.value,"
                f" layer = CASE"
                f"  WHEN excluded.layer = 'long' OR memory_items.pinned THEN 'long'"
                f"  WHEN excluded.layer = 'mid' AND memory_items.layer = 'short' THEN 'mid'"
                f"  ELSE memory_items.layer END,"
                f" count = memory_items.count + 1,"
                f" confidence = MIN(1.0, memory_items.confidence + {confidence_gain!r}),"
                f" importance = MAX(memory_items.importance, {importance!r}),"
                f" last_seen_at = excluded.last_seen_at,"
                f" last_changed_at = excluded.last_changed_at,"
                f" pinned = MAX(memory_items.pinned, {1 if item.get('pinned') else 0!r})",
                insert_values,
            )

            row = conn.execute("SELECT id FROM memory_items WHERE key_norm = ?", (key_norm,)).fetchone()
            memory_id = row["id"] if row else ""

            conn.execute(
                "INSERT INTO memory_events (id, item_id, seen_at, source, excerpt) VALUES (?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), memory_id, now, source, compact_text(excerpt, 500)),
            )
            row = conn.execute("SELECT * FROM memory_items WHERE id = ?", (memory_id,)).fetchone()

        return self.row_to_dict(row)
    def apply_decay(self, settings: Any) -> dict:
        now = now_ts()
        short_delete_days = float(getattr(settings, "memory_short_delete_days", 180))
        mid_downgrade_days = float(getattr(settings, "memory_mid_downgrade_days", 180))
        long_downgrade_days = float(getattr(settings, "memory_long_downgrade_days", 365))
        short_to_mid_days = float(getattr(settings, "memory_short_to_mid_days", 60))
        short_to_mid_count = int(getattr(settings, "memory_short_to_mid_count", 3))
        mid_to_long_days = float(getattr(settings, "memory_mid_to_long_days", 180))
        mid_to_long_count = int(getattr(settings, "memory_mid_to_long_count", 5))

        promoted = downgraded = deleted = 0
        with self.session() as conn:
            rows = conn.execute("SELECT * FROM memory_items").fetchall()
            for row in rows:
                memory_id = row["id"]
                layer = row["layer"]
                if row["pinned"]:
                    if layer != "long":
                        conn.execute("UPDATE memory_items SET layer = 'long', last_changed_at = ? WHERE id = ?", (now, memory_id))
                        promoted += 1
                    continue

                age = days_since(row["last_seen_at"], now)
                if layer == "short" and age > short_delete_days:
                    conn.execute("DELETE FROM memory_items WHERE id = ?", (memory_id,))
                    deleted += 1
                    continue
                if layer == "mid" and age > mid_downgrade_days:
                    conn.execute("UPDATE memory_items SET layer = 'short', last_changed_at = ? WHERE id = ?", (now, memory_id))
                    downgraded += 1
                    continue
                if layer == "long" and age > long_downgrade_days:
                    conn.execute("UPDATE memory_items SET layer = 'mid', last_changed_at = ? WHERE id = ?", (now, memory_id))
                    downgraded += 1
                    continue

                if layer == "short":
                    count = self.count_events_since(conn, memory_id, now - short_to_mid_days * SECONDS_PER_DAY)
                    if count >= short_to_mid_count:
                        conn.execute("UPDATE memory_items SET layer = 'mid', last_changed_at = ? WHERE id = ?", (now, memory_id))
                        promoted += 1
                elif layer == "mid":
                    count = self.count_events_since(conn, memory_id, now - mid_to_long_days * SECONDS_PER_DAY)
                    if count >= mid_to_long_count:
                        conn.execute("UPDATE memory_items SET layer = 'long', last_changed_at = ? WHERE id = ?", (now, memory_id))
                        promoted += 1
        return {"promoted": promoted, "downgraded": downgraded, "deleted": deleted}

    def count_events_since(self, conn: sqlite3.Connection, memory_id: str, since: float) -> int:
        row = conn.execute(
            "SELECT COUNT(*) AS count FROM memory_events WHERE item_id = ? AND seen_at >= ?",
            (memory_id, since),
        ).fetchone()
        return int(row["count"] if row else 0)

    def relevant_memories(self, settings: Any, query: str, limit: int | None = None) -> list[dict]:
        if not getattr(settings, "memory_enabled", True):
            return []
        self.apply_decay(settings)
        limit = limit or int(getattr(settings, "memory_max_context_items", 12))
        with self.session() as conn:
            rows = conn.execute("SELECT * FROM memory_items ORDER BY last_seen_at DESC LIMIT 300").fetchall()

        query_norm = normalize_key(query)
        query_chars = set(query_norm)
        scored = []
        for row in rows:
            data = self.row_to_dict(row)
            text = normalize_key(data["key"] + data["value"])
            layer_weight = {"long": 3.0, "mid": 2.0, "short": 1.0}.get(data["layer"], 1.0)
            score = layer_weight + float(data["importance"]) + float(data["confidence"])
            if query_norm and (query_norm in text or text in query_norm):
                score += 4.0
            elif query_chars:
                overlap = len(query_chars.intersection(set(text))) / max(1, min(len(query_chars), 24))
                score += min(2.0, overlap * 2.0)
            score += max(0.0, 1.0 - days_since(data["last_seen_at"]) / 365.0)
            if data.get("pinned"):
                score += 2.0
            scored.append((score, data))

        scored.sort(key=lambda item: item[0], reverse=True)
        return [data for _score, data in scored[: max(1, min(30, limit))]]

    def format_context(self, settings: Any, query: str) -> str:
        memories = self.relevant_memories(settings, query)
        if not memories:
            return ""
        lines = [
            "可参考的用户记忆。记忆分为两类：",
            "- 长期事实（语义偏好/身份/习惯），每次对话可用",
            "- 具体事件（某个时间点发生的事），只在时间匹配时使用",
            "不要在用户没有问的情况下主动复述记忆。时间信息可以帮助你判断事件是否相关。",
            "",
        ]
        for item in memories:
            mtype = item.get("memory_type", "semantic_fact")
            if mtype == "episodic_event":
                t = item.get("time_text", "") or ""
                d = item.get("event_date", "") or ""
                tod = item.get("time_of_day", "") or ""
                extra = f" [时间: {t}]" if t else ""
                if d:
                    extra += f" [日期: {d}]"
                lines.append(
                    f"- [事件{extra}] {item['value']}（{item['count']} 次）"
                )
            else:
                label = {"short": "短期", "mid": "中期", "long": "长期"}.get(item["layer"], item["layer"])
                lines.append(
                    f"- [{label}偏好] {item['value']}（{item['count']} 次）"
                )
        return "\n".join(lines)


def _resolve_event_date(time_text: str) -> tuple[str, str]:
    """返回 (time_text, event_date_iso) 从相对时间表达。"""
    from datetime import date, timedelta
    today = date.today()
    t_clean = time_text.strip()
    mapping = {
        "今天晚上": 0, "今天中午": 0, "今天下午": 0, "今天上午": 0, "今天早上": 0, "今天": 0, "今早": 0, "今晚": 0,
        "昨天晚上": 1, "昨天下午": 1, "昨天上午": 1, "昨天早上": 1, "昨天中午": 1, "昨晚": 1, "昨天": 1,
        "前天晚上": 2, "前天下午": 2, "前天上午": 2, "前天": 2,
        "刚才": 0, "刚刚": 0,
    }
    for key, offset in mapping.items():
        if t_clean.startswith(key):
            d = today - timedelta(days=offset)
            return (t_clean, d.isoformat())
    m = re.match(r"(\d{1,2})月(\d{1,2})[号日]", t_clean)
    if m:
        try:
            mo, da = int(m.group(1)), int(m.group(2))
            d = date(today.year, mo, da)
            if d > today:
                d = date(today.year - 1, mo, da)
            return (t_clean, d.isoformat())
        except ValueError:
            pass
    return (t_clean, today.isoformat())

def _guess_time_of_day(time_text: str, full_sentence: str = "") -> str:
    hints = {
        "早": "morning", "上午": "morning", "晨": "morning",
        "中午": "noon", "午": "noon",
        "下午": "afternoon",
        "傍晚": "evening", "黄昏": "evening", "晚上": "evening",
        "夜": "night", "宵": "night", "深夜": "night", "凌晨": "night",
    }
    txt = time_text + full_sentence
    for kw, val in hints.items():
        if kw in txt:
            return val
    return "unknown"


EXTRACT_MEMORY_PROMPT = (
    "从用户消息中提取可记忆的事实性信息。仅提取明确表述的偏好、身份、状态或事件。\n"
    "输出 JSON 数组，每项包含：\n"
    "- memory_type: \"semantic_fact\" 或 \"episodic_event\"\n"
    "- key: 简短的记忆键名（中文）\n"
    "- value: 记忆内容（中文）\n"
    "- importance: 重要性 0.0-1.0（偏好/身份 0.7+，闲聊状态 0.4-0.6，随手提及 0.2-0.4）\n"
    "- time_text: 时间表达式（仅 episodic_event 需要，否则空字符串）\n"
    "- event_date: ISO 日期（仅 episodic_event 需要，否则空字符串）\n"
    "- time_of_day: morning/noon/afternoon/evening/night/unknown\n\n"
    "如果没有可提取的信息，输出空数组 []。\n\n"
    "用户消息："
)


async def extract_memory_candidates_llm(user_text: str, extract_fn, assistant_text: str = "") -> list[dict]:
    """用 LLM 从用户消息中提取记忆候选。extract_fn(text) -> str 是异步函数。"""
    candidates: list[dict] = []
    try:
        prompt = (
            EXTRACT_MEMORY_PROMPT
            + user_text
            + "\n\n助手回复（只作为理解上下文，不要把助手自己的话记成用户事实）："
            + compact_text(assistant_text, 360)
        )
        result_text = await extract_fn(prompt)
        result_text = re.sub(r"^```(?:json)?|```$", "", result_text.strip(), flags=re.I | re.M).strip()
        match = re.search(r"\[.*\]", result_text, flags=re.S)
        if match:
            data = json.loads(match.group(0))
            if isinstance(data, list):
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    if not item.get("value") or not item.get("key"):
                        continue
                    candidates.append({
                        "key": compact_text(item["key"], 120),
                        "value": compact_text(item["value"], 300),
                        "layer": "short",
                        "confidence": 0.55,
                        "importance": clamp(float(item.get("importance", 0.45)), 0.0, 1.0),
                        "source": "chat",
                        "memory_type": item.get("memory_type", "semantic_fact"),
                        "time_text": item.get("time_text", ""),
                        "event_date": item.get("event_date", ""),
                        "time_of_day": item.get("time_of_day", "unknown"),
                        "confidence_gain": 0.12,
                    })
    except Exception:
        pass
    return candidates


def extract_memory_candidates(text: str) -> list[dict]:
    """提取记忆候选。优先尝试 LLM 驱动提取，失败时回退到正则规则。

    事件记忆保留时间语境，不同时间的事件创建不同记忆。
    """
    text = compact_text(text, 500)
    if not text or is_low_value_memory_text(text):
        return []
    # 显式指令走专用流程（无需 LLM）
    explicit = re.search(r"(?:帮我记住|你记一下|记住)[:：,，\s]*(.+)", text)
    if explicit:
        value = compact_text(explicit.group(1), 300)
        if value:
            return [{
                "key": f"用户明确要求记住：{value[:50]}",
                "value": value,
                "layer": "mid",
                "confidence": 0.9,
                "confidence_gain": 0.3,
                "importance": 0.85,
                "source": "explicit",
                "memory_type": "semantic_fact",
            }]
        return []
    # 注意：LLM 驱动提取在 observe_turn 中异步调用；
    # 此处同步方法保留 fallback 逻辑
    return _extract_memory_fallback(text)


def _extract_memory_fallback(text: str) -> list[dict]:
    """基于正则规则的回退提取，在 LLM 不可用时使用。"""
    text = compact_text(text, 500)
    if not text or is_low_value_memory_text(text):
        return []

    candidates: list[dict] = []

    # ── 时间词提取 ──
    time_words = [
        "昨天晚上", "今天中午", "今天下午", "今天上午", "今天早上", "今天晚上",
        "昨天下午", "昨天上午", "昨天早上", "昨天中午", "前天晚上", "前天下午", "前天上午",
        "今晚", "今天", "今早", "昨天", "昨晚", "前天", "刚才", "刚刚",
    ]
    time_re = re.compile(r"(" + "|".join(time_words) + r")")

    found_time = None
    tm = time_re.search(text)
    if tm and text.startswith("我"):
        found_time = tm.group(1)
        # 提取时间词后的内容作为事件描述
        after_time = text[tm.end():]
        after_time = re.sub(r'^(?:了|过|，|,|。|\.|\s)+', '', after_time).strip()
        if after_time and len(after_time) >= 2 and not is_low_value_memory_text(after_time):
            resolved_time, resolved_date = _resolve_event_date(found_time)
            tod = _guess_time_of_day(found_time, text)
            # 事件类型
            if any(kw in after_time for kw in ["吃", "喝", "点", "叫"]):
                evt_type = "meal"
            elif any(kw in after_time for kw in ["启动", "关了", "重启", "调试", "部署", "训练", "配置", "修", "改"]):
                evt_type = "operation"
            else:
                evt_type = "activity"
            candidates.append({
                "key": f"episodic:{evt_type}:{resolved_date}:{after_time[:30]}",
                "value": f"用户{found_time}{after_time}",
                "layer": "short",
                "confidence": 0.5,
                "importance": 0.55,
                "source": "chat",
                "memory_type": "episodic_event",
                "time_text": resolved_time,
                "event_date": resolved_date,
                "time_of_day": tod,
            })
            return candidates  # 事件提取成功即返回

    # ── 语义事实 (semantic_fact) ──
    fact_patterns = [
        (r"(?:我叫|我的名字是|我是)([一-龥A-Za-z0-9_\- ]{1,24})", "用户身份", "用户名字或身份是{}", 0.82),
        (r"我(?:很|特别|超|最)?喜欢([^。！？!?，,]{1,40})", "用户偏好", "用户喜欢{}", 0.65),
        (r"我(?:不喜欢|讨厌)([^。！？!?，,]{1,40})", "用户偏好", "用户不喜欢{}", 0.65),
        (r"我(?:住在|现居|在)([^。！？!?，,]{1,40})", "用户信息", "用户提到在{}", 0.58),
        (r"我(?:最近|现在|正在|准备|打算)([^。！？!?]{2,80})", "用户状态", "用户最近/当前{}", 0.55),
        (r"我[^。！？!?,\n]{0,6}(?:觉得|感觉|认为)([^。！？!?，,\n]{1,50})", "用户想法", "用户觉得{}", 0.52),
    ]
    for pattern, fact_type, value_tpl, importance in fact_patterns:
        for match in re.finditer(pattern, text):
            value = compact_text(match.group(1), 100).strip(" ，,。.!！?")
            if value and not is_low_value_memory_text(value):
                candidates.append({
                    "key": f"{fact_type}:{value[:40]}",
                    "value": value_tpl.format(value),
                    "layer": "short",
                    "confidence": 0.45,
                    "importance": importance,
                    "source": "chat",
                    "memory_type": "semantic_fact",
                })

    # ── 通用 fallback ──
    if not candidates and "我" in text and not re.search(r"[?？]", text) and 8 <= len(text) <= 90:
        candidates.append({
            "key": f"用户陈述:{text[:50]}",
            "value": text,
            "layer": "short",
            "confidence": 0.35,
            "importance": 0.35,
            "source": "chat",
            "memory_type": "semantic_fact",
        })

    # ── 去重 ──
    deduped = []
    seen = set()
    for item in candidates:
        norm = normalize_key(item["key"])
        if norm not in seen:
            seen.add(norm)
            deduped.append(item)
    return deduped[:5]

def is_low_value_memory_text(text: str) -> bool:
    value = re.sub(r"\s+", "", text)
    if len(value) < 3:
        return True
    if re.fullmatch(r"[0-9a-zA-Z，,。.！!？?~～哈啊嗯额呃哦]+", value):
        return True
    low_value = {
        "你好",
        "哈喽",
        "hello",
        "谢谢",
        "好的",
        "可以",
        "嗯嗯",
        "不用啦",
    }
    return value.lower() in low_value


class LinkTextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[dict] = []
        self._active_href = ""
        self._active_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key: value or "" for key, value in attrs}
        classes = attrs_dict.get("class", "")
        if tag == "a" and ("result__a" in classes or "result-link" in classes):
            self._active_href = attrs_dict.get("href", "")
            self._active_text = []

    def handle_data(self, data: str) -> None:
        if self._active_href:
            self._active_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._active_href:
            title = compact_text(html_lib.unescape(" ".join(self._active_text)), 160)
            href = normalize_result_url(self._active_href)
            if title and href:
                self.links.append({"title": title, "url": href})
            self._active_href = ""
            self._active_text = []


class TextExtractParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title = ""
        self.text: list[str] = []
        self._in_title = False
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
        if tag in {"script", "style", "noscript", "svg"}:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag in {"script", "style", "noscript", "svg"} and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        data = compact_text(html_lib.unescape(data), 400)
        if not data:
            return
        if self._in_title:
            self.title += data
        elif self._skip_depth == 0:
            self.text.append(data)


def normalize_result_url(url: str) -> str:
    url = html_lib.unescape(url)
    parsed = urlparse(url)
    if "duckduckgo.com" in parsed.netloc and parsed.query:
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        if target:
            return unquote(target)
    if parsed.scheme in {"http", "https"}:
        return url
    return ""


class ToolManager:
    BUILTIN_TOOLS = [
        {
            "id": "web_search",
            "name": "网页搜索",
            "description": "搜索互联网上的公开网页，适合查最新信息、资料、教程和泛查询。",
            "args": {"query": "搜索关键词", "limit": "返回条数，默认 5"},
        },
        {
            "id": "hot_news",
            "name": "热点新闻",
            "description": "查询当前新闻、热点和近期事件。",
            "args": {"topic": "可选主题", "region": "地区，默认 CN", "limit": "返回条数，默认 6"},
        },
        {
            "id": "url_fetch",
            "name": "读取网页",
            "description": "读取用户给出的 URL 并提取标题和正文摘要。",
            "args": {"url": "http 或 https URL"},
        },
        {
            "id": "weather",
            "name": "天气",
            "description": "查询城市当前天气和简要预报。",
            "args": {"location": "城市或地区"},
        },
        {
            "id": "finance",
            "name": "财经价格",
            "description": "查询股票、汇率、币价、商品价格等公开财经信息。",
            "args": {"query": "标的或问题"},
        },
    ]

    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.config_path.exists():
            self.save_config(self.default_config())

    def default_config(self) -> dict:
        return {
            "builtins": {tool["id"]: {"enabled": True} for tool in self.BUILTIN_TOOLS},
            "custom_tools": [],
        }

    def load_config(self) -> dict:
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
        except Exception:
            data = self.default_config()
        base = self.default_config()
        base["builtins"].update(data.get("builtins") or {})
        base["custom_tools"] = [tool for tool in data.get("custom_tools") or [] if isinstance(tool, dict)]
        return base

    def save_config(self, data: dict) -> dict:
        builtins = data.get("builtins") or {}
        custom_tools = data.get("custom_tools") or []
        payload = {
            "builtins": builtins,
            "custom_tools": [self.normalize_custom_tool(tool) for tool in custom_tools if isinstance(tool, dict)],
        }
        self.config_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return self.get_config()

    def update_config(self, patch: dict) -> dict:
        data = self.load_config()
        if isinstance(patch.get("builtins"), dict):
            data["builtins"].update(patch["builtins"])
        if isinstance(patch.get("custom_tools"), list):
            data["custom_tools"] = patch["custom_tools"]
        return self.save_config(data)

    def get_config(self) -> dict:
        data = self.load_config()
        return {
            "builtins": self.builtin_specs(include_disabled=True, config=data),
            "custom_tools": data["custom_tools"],
        }

    def normalize_custom_tool(self, tool: dict) -> dict:
        tool_id = re.sub(r"[^a-zA-Z0-9_\-]", "_", str(tool.get("id") or tool.get("name") or "")).strip("_")
        if not tool_id:
            tool_id = f"custom_{uuid.uuid4().hex[:8]}"
        return {
            "id": tool_id,
            "name": str(tool.get("name") or tool_id),
            "enabled": bool(tool.get("enabled", True)),
            "description": str(tool.get("description") or ""),
            "method": str(tool.get("method") or "GET").upper(),
            "url": str(tool.get("url") or ""),
            "headers": tool.get("headers") if isinstance(tool.get("headers"), dict) else {},
            "query": tool.get("query") if isinstance(tool.get("query"), dict) else {},
            "body": tool.get("body") if isinstance(tool.get("body"), (dict, list, str)) else {},
        }

    def builtin_specs(self, include_disabled: bool = False, config: dict | None = None) -> list[dict]:
        config = config or self.load_config()
        specs = []
        for spec in self.BUILTIN_TOOLS:
            enabled = bool((config.get("builtins") or {}).get(spec["id"], {}).get("enabled", True))
            if enabled or include_disabled:
                specs.append({**spec, "enabled": enabled, "builtin": True})
        return specs

    def enabled_specs(self) -> list[dict]:
        config = self.load_config()
        specs = self.builtin_specs(config=config)
        for tool in config["custom_tools"]:
            if tool.get("enabled", True):
                specs.append(
                    {
                        "id": tool["id"],
                        "name": tool.get("name") or tool["id"],
                        "description": tool.get("description") or "",
                        "args": "按该 API 配置中的 URL/query/body 模板提供参数",
                        "builtin": False,
                    }
                )
        return specs

    def tool_exists(self, tool_id: str) -> bool:
        return any(tool["id"] == tool_id for tool in self.enabled_specs())

    def planner_tool_text(self) -> str:
        return json.dumps(self.enabled_specs(), ensure_ascii=False, indent=2)

    def suggest_from_text(self, text: str) -> dict | None:
        lowered = text.lower()
        url_match = re.search(r"https?://[^\s，。！？]+", text)
        if url_match and self.tool_exists("url_fetch"):
            return {"id": "url_fetch", "arguments": {"url": url_match.group(0)}}
        if re.search(r"(热点|新闻|最近发生|时事|头条)", text) and self.tool_exists("hot_news"):
            topic = re.sub(r"(今天|现在|当前|最新|热点|新闻|帮我|查一下|看看|是什么|有哪些)", "", text).strip(" ，。？?")
            return {"id": "hot_news", "arguments": {"topic": topic[:40], "limit": 6}}
        if re.search(r"(天气|下雨|气温|温度)", text) and self.tool_exists("weather"):
            location = re.sub(r"(天气|下雨|气温|温度|今天|现在|查一下|怎么样|如何)", "", text).strip(" ，。？?")
            return {"id": "weather", "arguments": {"location": location or "北京"}}
        if re.search(r"(股票|股价|汇率|币价|价格|金价|美股|基金|btc|eth|usd|cny)", lowered) and self.tool_exists("finance"):
            return {"id": "finance", "arguments": {"query": text}}
        if re.search(r"(搜索|查一下|帮我查|网上|资料|最新)", text) and self.tool_exists("web_search"):
            return {"id": "web_search", "arguments": {"query": text, "limit": 5}}
        return None

    async def execute(self, tool_id: str, arguments: dict | None, timeout: float = 12, max_chars: int = 4000) -> dict:
        args = arguments or {}
        config = self.load_config()
        if tool_id == "web_search":
            result = await self.web_search(str(args.get("query") or ""), int(args.get("limit") or 5), timeout)
        elif tool_id == "hot_news":
            result = await self.hot_news(str(args.get("topic") or ""), str(args.get("region") or "CN"), int(args.get("limit") or 6), timeout)
        elif tool_id == "url_fetch":
            result = await self.url_fetch(str(args.get("url") or ""), timeout)
        elif tool_id == "weather":
            result = await self.weather(str(args.get("location") or "北京"), timeout)
        elif tool_id == "finance":
            result = await self.web_search(str(args.get("query") or ""), int(args.get("limit") or 5), timeout)
            result["kind"] = "finance_search"
        else:
            custom = next((tool for tool in config["custom_tools"] if tool.get("id") == tool_id and tool.get("enabled", True)), None)
            if not custom:
                raise KeyError(f"Unknown or disabled tool: {tool_id}")
            result = await self.custom_api(custom, args, timeout)

        return truncate_result(result, max_chars)

    async def web_search(self, query: str, limit: int = 5, timeout: float = 12) -> dict:
        if not query.strip():
            return {"ok": False, "error": "query is empty", "results": []}
        limit = max(1, min(10, limit))
        url = "https://duckduckgo.com/html/"
        headers = {"User-Agent": "Mozilla/5.0 lovechoice-tool/1.0"}
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=headers) as client:
            resp = await client.get(url, params={"q": query})
        resp.raise_for_status()
        parser = LinkTextParser()
        parser.feed(resp.text)
        return {"ok": True, "tool": "web_search", "query": query, "results": parser.links[:limit]}

    async def hot_news(self, topic: str = "", region: str = "CN", limit: int = 6, timeout: float = 12) -> dict:
        limit = max(1, min(12, limit))
        region = (region or "CN").upper()
        if topic.strip():
            url = f"https://news.google.com/rss/search?q={quote(topic)}&hl=zh-CN&gl={region}&ceid={region}:zh-Hans"
        else:
            url = f"https://news.google.com/rss?hl=zh-CN&gl={region}&ceid={region}:zh-Hans"
        headers = {"User-Agent": "Mozilla/5.0 lovechoice-tool/1.0"}
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=headers) as client:
            resp = await client.get(url)
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
        items = []
        for item in root.findall(".//item")[:limit]:
            items.append(
                {
                    "title": compact_text(item.findtext("title") or "", 180),
                    "url": item.findtext("link") or "",
                    "published": item.findtext("pubDate") or "",
                    "source": item.findtext("source") or "",
                }
            )
        return {"ok": True, "tool": "hot_news", "topic": topic, "region": region, "results": items}

    async def url_fetch(self, url: str, timeout: float = 12) -> dict:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return {"ok": False, "error": "Only http/https URLs are supported"}
        headers = {"User-Agent": "Mozilla/5.0 lovechoice-tool/1.0"}
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=headers) as client:
            resp = await client.get(url)
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "")
        if "html" not in content_type:
            text = compact_text(resp.text, 2500)
            return {"ok": True, "tool": "url_fetch", "url": str(resp.url), "content_type": content_type, "text": text}
        parser = TextExtractParser()
        parser.feed(resp.text)
        text = compact_text(" ".join(parser.text), 2500)
        return {"ok": True, "tool": "url_fetch", "url": str(resp.url), "title": compact_text(parser.title, 180), "text": text}

    async def weather(self, location: str, timeout: float = 12) -> dict:
        location = location.strip() or "北京"
        url = f"https://wttr.in/{quote(location)}"
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(url, params={"format": "j1", "lang": "zh"})
        resp.raise_for_status()
        data = resp.json()
        current = (data.get("current_condition") or [{}])[0]
        area = (data.get("nearest_area") or [{}])[0]
        return {
            "ok": True,
            "tool": "weather",
            "location": location,
            "area": area.get("areaName", [{}])[0].get("value", location) if isinstance(area.get("areaName"), list) else location,
            "current": {
                "temp_c": current.get("temp_C"),
                "feels_like_c": current.get("FeelsLikeC"),
                "humidity": current.get("humidity"),
                "weather": (current.get("lang_zh") or current.get("weatherDesc") or [{}])[0].get("value", ""),
                "wind_kmph": current.get("windspeedKmph"),
            },
        }

    async def custom_api(self, tool: dict, args: dict, timeout: float = 12) -> dict:
        method = str(tool.get("method") or "GET").upper()
        url = render_template(str(tool.get("url") or ""), args)
        if not url:
            return {"ok": False, "error": "custom api url is empty"}
        headers = render_structure(tool.get("headers") or {}, args)
        query = render_structure(tool.get("query") or {}, args)
        body = render_structure(tool.get("body") or {}, args)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.request(method, url, headers=headers, params=query, json=body if body not in ({}, "") else None)
        content_type = resp.headers.get("content-type", "")
        try:
            payload: Any = resp.json()
        except Exception:
            payload = compact_text(resp.text, 3000)
        return {
            "ok": resp.status_code < 400,
            "tool": tool.get("id"),
            "status": resp.status_code,
            "content_type": content_type,
            "result": payload,
        }


def render_template(value: str, args: dict) -> str:
    def replace(match: re.Match) -> str:
        key = match.group(1)
        return str(args.get(key, match.group(0)))

    return re.sub(r"\{([a-zA-Z0-9_\-]+)\}", replace, value)


def render_structure(value: Any, args: dict) -> Any:
    if isinstance(value, str):
        rendered = render_template(value, args)
        try:
            return json.loads(rendered)
        except Exception:
            return rendered
    if isinstance(value, list):
        return [render_structure(item, args) for item in value]
    if isinstance(value, dict):
        return {key: render_structure(item, args) for key, item in value.items()}
    return value


def truncate_result(result: dict, max_chars: int) -> dict:
    text = json.dumps(result, ensure_ascii=False)
    if len(text) <= max_chars:
        return result
    return {
        "ok": result.get("ok", True),
        "truncated": True,
        "text": text[: max(200, max_chars - 1)] + "…",
    }


def parse_tool_call(text: str) -> dict | None:
    text = (text or "").strip()
    if not text:
        return None
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.I | re.M).strip()
    candidates = [text]
    match = re.search(r"\{.*\}", text, flags=re.S)
    if match:
        candidates.append(match.group(0))
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except Exception:
            continue
        call = data.get("tool_call") if isinstance(data, dict) else None
        if not call:
            return None
        tool_id = call.get("id") or call.get("name")
        if not tool_id:
            return None
        arguments = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
        return {"id": str(tool_id), "arguments": arguments}
    return None
