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

    def observe_turn(self, settings: Any, user_text: str, assistant_text: str = "") -> list[dict]:
        if not getattr(settings, "memory_enabled", True) or not getattr(settings, "memory_extract_enabled", True):
            return []
        candidates = extract_memory_candidates(user_text)
        saved = []
        for candidate in candidates:
            saved.append(self.upsert_memory(candidate, source=candidate.get("source", "chat"), excerpt=user_text))
        self.apply_decay(settings)
        return saved

    def upsert_memory(self, item: dict, source: str = "chat", excerpt: str = "") -> dict:
        now = now_ts()
        key = compact_text(item["key"], 120)
        value = compact_text(item.get("value") or key, 1000)
        key_norm = normalize_key(key)
        layer = item.get("layer") if item.get("layer") in MEMORY_LAYERS else "short"
        confidence_gain = 0.3 if source == "manual" else float(item.get("confidence_gain", 0.12))
        importance = clamp(float(item.get("importance", 0.45)), 0.0, 1.0)

        with self.session() as conn:
            row = conn.execute("SELECT * FROM memory_items WHERE key_norm = ?", (key_norm,)).fetchone()
            if row:
                memory_id = row["id"]
                confidence = clamp(float(row["confidence"]) + confidence_gain, 0.0, 1.0)
                next_layer = row["layer"]
                if layer == "long" or row["pinned"]:
                    next_layer = "long"
                elif layer == "mid" and row["layer"] == "short":
                    next_layer = "mid"
                conn.execute(
                    """
                    UPDATE memory_items
                    SET value = ?, layer = ?, count = count + 1, confidence = ?,
                        importance = MAX(importance, ?), last_seen_at = ?, last_changed_at = ?,
                        pinned = MAX(pinned, ?)
                    WHERE id = ?
                    """,
                    (value, next_layer, confidence, importance, now, now, 1 if item.get("pinned") else 0, memory_id),
                )
            else:
                memory_id = str(uuid.uuid4())
                confidence = clamp(float(item.get("confidence", 0.45)), 0.0, 1.0)
                if source == "manual":
                    layer = item.get("layer") if item.get("layer") in MEMORY_LAYERS else "mid"
                    confidence = max(confidence, 0.85)
                conn.execute(
                    """
                    INSERT INTO memory_items (
                        id, key, key_norm, value, layer, count, confidence, importance,
                        first_seen_at, last_seen_at, last_changed_at, pinned, source
                    )
                    VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        memory_id,
                        key,
                        key_norm,
                        value,
                        layer,
                        confidence,
                        importance,
                        now,
                        now,
                        now,
                        1 if item.get("pinned") else 0,
                        source,
                    ),
                )

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
        lines = ["可参考的用户记忆。只在相关时自然使用，不要直接背给用户："]
        for item in memories:
            label = {"short": "短期", "mid": "中期", "long": "长期"}.get(item["layer"], item["layer"])
            lines.append(
                f"- [{label}] {item['value']}（记录 {item['count']} 次，最近：{item['last_seen_text']}）"
            )
        return "\n".join(lines)


def extract_memory_candidates(text: str) -> list[dict]:
    text = compact_text(text, 500)
    if not text or is_low_value_memory_text(text):
        return []

    candidates: list[dict] = []
    explicit = re.search(r"(?:帮我记住|你记一下|记住)[:：,，\s]*(.+)", text)
    if explicit:
        value = compact_text(explicit.group(1), 300)
        if value:
            candidates.append(
                {
                    "key": f"用户明确要求记住：{value[:50]}",
                    "value": value,
                    "layer": "mid",
                    "confidence": 0.9,
                    "confidence_gain": 0.3,
                    "importance": 0.85,
                    "source": "explicit",
                }
            )
        return candidates

    patterns = [
        (r"(?:我叫|我的名字是|我是)([\u4e00-\u9fa5A-Za-z0-9_\- ]{1,24})", "用户身份：{}", "用户名字或身份是{}", 0.82),
        (r"我(?:很|特别|超|最)?喜欢([^。！？!?，,]{1,40})", "用户喜欢{}", "用户喜欢{}", 0.65),
        (r"我(?:不喜欢|讨厌)([^。！？!?，,]{1,40})", "用户不喜欢{}", "用户不喜欢{}", 0.65),
        (r"我(?:住在|现居|在)([^。！？!?，,]{1,40})(?:生活|住|工作)?", "用户地点：{}", "用户提到自己在{}", 0.58),
        (r"我(?:最近|现在|正在|准备|打算)([^。！？!?]{2,80})", "用户近期状态：{}", "用户最近/当前{}", 0.55),
    ]
    for pattern, key_tpl, value_tpl, importance in patterns:
        for match in re.finditer(pattern, text):
            value = compact_text(match.group(1), 100).strip(" ，,。.!！?")
            if value and not is_low_value_memory_text(value):
                candidates.append(
                    {
                        "key": key_tpl.format(value[:50]),
                        "value": value_tpl.format(value),
                        "layer": "short",
                        "confidence": 0.45,
                        "importance": importance,
                        "source": "chat",
                    }
                )

    if not candidates and "我" in text and not re.search(r"[?？]", text) and 8 <= len(text) <= 90:
        candidates.append(
            {
                "key": f"用户提到：{text[:50]}",
                "value": text,
                "layer": "short",
                "confidence": 0.35,
                "importance": 0.35,
                "source": "chat",
            }
        )

    deduped: list[dict] = []
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
