from __future__ import annotations

import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path


class ReminderStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
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
                CREATE TABLE IF NOT EXISTS reminders (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    due_at TEXT NOT NULL,
                    timezone TEXT NOT NULL DEFAULT 'local',
                    repeat_rule TEXT NOT NULL DEFAULT '',
                    channel TEXT NOT NULL DEFAULT 'web',
                    platform_id TEXT NOT NULL DEFAULT '',
                    sender_id TEXT NOT NULL DEFAULT '',
                    conversation_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    fired_at TEXT NOT NULL DEFAULT ''
                )
                """
            )

    def list(self, status: str = "") -> list[dict]:
        query = "SELECT * FROM reminders"
        params: list[str] = []
        if status:
            query += " WHERE status=?"
            params.append(status)
        query += " ORDER BY due_at ASC"
        with self.session() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def create(self, payload: dict) -> dict:
        now = self._now()
        due_at = normalize_due_at(payload.get("due_at"))
        if not due_at:
            due_at = parse_due_at(str(payload.get("text") or payload.get("content") or ""))
        if not due_at:
            raise ValueError("due_at is required")
        item = {
            "id": uuid.uuid4().hex[:12],
            "title": str(payload.get("title") or payload.get("content") or payload.get("text") or "提醒")[:120],
            "content": str(payload.get("content") or payload.get("text") or payload.get("title") or "提醒")[:500],
            "due_at": due_at,
            "timezone": str(payload.get("timezone") or "local"),
            "repeat_rule": str(payload.get("repeat_rule") or ""),
            "channel": str(payload.get("channel") or "web"),
            "platform_id": str(payload.get("platform_id") or ""),
            "sender_id": str(payload.get("sender_id") or ""),
            "conversation_id": str(payload.get("conversation_id") or ""),
            "status": str(payload.get("status") or "pending"),
            "last_error": "",
            "created_at": now,
            "updated_at": now,
            "fired_at": "",
        }
        with self.session() as conn:
            conn.execute(
                """
                INSERT INTO reminders (
                    id,title,content,due_at,timezone,repeat_rule,channel,platform_id,sender_id,
                    conversation_id,status,last_error,created_at,updated_at,fired_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    item["id"],
                    item["title"],
                    item["content"],
                    item["due_at"],
                    item["timezone"],
                    item["repeat_rule"],
                    item["channel"],
                    item["platform_id"],
                    item["sender_id"],
                    item["conversation_id"],
                    item["status"],
                    item["last_error"],
                    item["created_at"],
                    item["updated_at"],
                    item["fired_at"],
                ],
            )
        return item

    def update(self, reminder_id: str, payload: dict) -> dict:
        existing = self.get(reminder_id)
        if not existing:
            raise KeyError(reminder_id)
        allowed = {"title", "content", "due_at", "timezone", "repeat_rule", "channel", "status", "last_error"}
        next_item = dict(existing)
        for key in allowed:
            if key in payload and payload[key] is not None:
                next_item[key] = normalize_due_at(payload[key]) if key == "due_at" else str(payload[key])
        next_item["updated_at"] = self._now()
        with self.session() as conn:
            conn.execute(
                """
                UPDATE reminders SET title=?,content=?,due_at=?,timezone=?,repeat_rule=?,channel=?,
                    status=?,last_error=?,updated_at=? WHERE id=?
                """,
                [
                    next_item["title"],
                    next_item["content"],
                    next_item["due_at"],
                    next_item["timezone"],
                    next_item["repeat_rule"],
                    next_item["channel"],
                    next_item["status"],
                    next_item["last_error"],
                    next_item["updated_at"],
                    reminder_id,
                ],
            )
        return self.get(reminder_id) or next_item

    def get(self, reminder_id: str) -> dict | None:
        with self.session() as conn:
            row = conn.execute("SELECT * FROM reminders WHERE id=?", [reminder_id]).fetchone()
        return dict(row) if row else None

    def delete(self, reminder_id: str) -> bool:
        with self.session() as conn:
            cur = conn.execute("DELETE FROM reminders WHERE id=?", [reminder_id])
            return cur.rowcount > 0

    def due(self) -> list[dict]:
        now = self._now()
        with self.session() as conn:
            rows = conn.execute(
                "SELECT * FROM reminders WHERE status='pending' AND due_at<=? ORDER BY due_at ASC",
                [now],
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_fired(self, reminder_id: str, status: str = "done", error: str = "") -> None:
        now = self._now()
        with self.session() as conn:
            conn.execute(
                "UPDATE reminders SET status=?, last_error=?, fired_at=?, updated_at=? WHERE id=?",
                [status, error, now, now, reminder_id],
            )

    def _now(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S")


def parse_due_at(text: str) -> str:
    now = datetime.now()
    text = str(text or "")
    day = now
    if "后天" in text:
        day = now + timedelta(days=2)
    elif "明天" in text:
        day = now + timedelta(days=1)
    match = re.search(r"(\d{1,2})[:：点时](?:\s*(\d{1,2})分?)?", text)
    if not match:
        return ""
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if "下午" in text or "晚上" in text:
        if hour < 12:
            hour += 12
    if "早上" in text and hour == 12:
        hour = 0
    due = day.replace(hour=max(0, min(23, hour)), minute=max(0, min(59, minute)), second=0, microsecond=0)
    if due < now and day.date() == now.date():
        due += timedelta(days=1)
    return due.strftime("%Y-%m-%d %H:%M:%S")


def normalize_due_at(value) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.replace("T", " ")
    if len(text) == 16 and re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$", text):
        return f"{text}:00"
    if re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", text):
        return text
    return text
