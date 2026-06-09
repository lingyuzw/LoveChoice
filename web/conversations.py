from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path


class ConversationStore:
    """Small JSON-backed conversation store for the browser console."""

    def __init__(self, root: Path):
        self.root = root
        self.index_path = root / "index.json"
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.index_path.exists():
            self._write_json(self.index_path, [])

    def list(self) -> list[dict]:
        items = self._read_index()
        items.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
        return items

    def create(self, title: str | None = None) -> dict:
        items = self._read_index()
        sequence = len(items) + 1
        conversation_id = f"{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        now = self._now()
        conversation = {
            "id": conversation_id,
            "title": title or f"对话 {sequence:03d}",
            "sequence": sequence,
            "created_at": now,
            "updated_at": now,
            "messages": [],
        }
        self._write_conversation(conversation)
        items.append(self._summary(conversation))
        self._write_index(items)
        return conversation

    def get_or_create(self, conversation_id: str | None = None) -> dict:
        if conversation_id:
            loaded = self.load(conversation_id)
            if loaded:
                return loaded
        return self.create()

    def load(self, conversation_id: str) -> dict | None:
        if not re.match(r"^[0-9]{8}_[0-9]{6}_[0-9a-f]{6}$", conversation_id or ""):
            return None
        path = self.root / f"{conversation_id}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def append_messages(self, conversation_id: str, messages: list[dict], title_hint: str | None = None) -> dict:
        conversation = self.get_or_create(conversation_id)
        now = self._now()
        for message in messages:
            role = str(message.get("role") or "").strip()
            content = str(message.get("content") or "").strip()
            if role not in {"user", "assistant", "system"} or not content:
                continue
            conversation["messages"].append(
                {
                    "id": uuid.uuid4().hex[:10],
                    "role": role,
                    "content": content,
                    "source": message.get("source") or "",
                    "display_name": message.get("display_name") or "",
                    "avatar_url": message.get("avatar_url") or "",
                    "platform_id": message.get("platform_id") or "",
                    "sender_id": message.get("sender_id") or "",
                    "created_at": now,
                }
            )
        if title_hint and self._is_default_title(conversation.get("title", "")):
            conversation["title"] = self._make_title(title_hint)
        conversation["updated_at"] = now
        self._write_conversation(conversation)
        self._upsert_summary(conversation)
        return conversation

    def delete(self, conversation_id: str) -> bool:
        conversation = self.load(conversation_id)
        if not conversation:
            return False

        path = self.root / f"{conversation_id}.json"
        try:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        except OSError:
            return False

        items = [item for item in self._read_index() if item.get("id") != conversation_id]
        self._write_index(items)
        return True

    def _read_index(self) -> list[dict]:
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def _write_index(self, items: list[dict]) -> None:
        self._write_json(self.index_path, items)

    def _write_conversation(self, conversation: dict) -> None:
        self._write_json(self.root / f"{conversation['id']}.json", conversation)

    def _upsert_summary(self, conversation: dict) -> None:
        items = [item for item in self._read_index() if item.get("id") != conversation.get("id")]
        items.append(self._summary(conversation))
        self._write_index(items)

    def _summary(self, conversation: dict) -> dict:
        messages = conversation.get("messages") or []
        last = messages[-1] if messages else {}
        return {
            "id": conversation.get("id"),
            "title": conversation.get("title"),
            "sequence": conversation.get("sequence"),
            "created_at": conversation.get("created_at"),
            "updated_at": conversation.get("updated_at"),
            "message_count": len(messages),
            "last_message": (last.get("content") or "")[:80],
        }

    def _write_json(self, path: Path, data) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def _now(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S")

    def _is_default_title(self, title: str) -> bool:
        return bool(re.match(r"^对话\s+\d+$", title or ""))

    def _make_title(self, text: str) -> str:
        text = re.sub(r"\s+", " ", text).strip()
        return text[:18] or "新的对话"
