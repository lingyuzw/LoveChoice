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

    def list(self, query: str = "", archived: str = "active") -> list[dict]:
        items = [self._hydrate_summary(item) for item in self._read_index()]
        if archived == "active":
            items = [item for item in items if not item.get("archived")]
        elif archived == "archived":
            items = [item for item in items if item.get("archived")]
        if query:
            q = query.lower()
            items = [
                item
                for item in items
                if q in str(item.get("title") or "").lower()
                or q in str(item.get("last_message") or "").lower()
                or q in str(item.get("summary") or "").lower()
            ]
        items.sort(key=lambda item: (not item.get("favorite"), item.get("updated_at", "")), reverse=False)
        items.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
        items.sort(key=lambda item: bool(item.get("favorite")), reverse=True)
        return items

    def _hydrate_summary(self, item: dict) -> dict:
        if item.get("platform_id") or item.get("source"):
            return item
        conversation_id = str(item.get("id") or "")
        if not conversation_id:
            return item
        conversation = self.load(conversation_id)
        if not conversation:
            return item
        return {**item, **self._summary(conversation)}

    def create(self, title: str | None = None, metadata: dict | None = None) -> dict:
        items = self._read_index()
        sequence = len(items) + 1
        conversation_id = f"{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        now = self._now()
        metadata = metadata if isinstance(metadata, dict) else {}
        conversation = {
            "id": conversation_id,
            "title": title or f"对话 {sequence:03d}",
            "sequence": sequence,
            "created_at": now,
            "updated_at": now,
            "archived": False,
            "favorite": False,
            "summary": "",
            "context_summary": "",
            "context_summary_layers": [],
            "compacted_until": 0,
            "source": str(metadata.get("source") or ""),
            "platform_id": str(metadata.get("platform_id") or ""),
            "sender_id": str(metadata.get("sender_id") or ""),
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
                return self._normalize_conversation(loaded)
        return self.create()

    def load(self, conversation_id: str) -> dict | None:
        if not re.match(r"^[0-9]{8}_[0-9]{6}_[0-9a-f]{6}$", conversation_id or ""):
            return None
        path = self.root / f"{conversation_id}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        return self._normalize_conversation(data) if isinstance(data, dict) else None

    def append_messages(self, conversation_id: str, messages: list[dict], title_hint: str | None = None) -> dict:
        conversation = self.get_or_create(conversation_id)
        now = self._now()
        for message in messages:
            role = str(message.get("role") or "").strip()
            content = str(message.get("content") or "").strip()
            attachments = self._normalize_attachments(message.get("attachments") or [])
            if role not in {"user", "assistant", "system"} or (not content and not attachments):
                continue
            item = {
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
            if attachments:
                item["attachments"] = attachments
            conversation["messages"].append(item)
        if title_hint and self._is_default_title(conversation.get("title", "")):
            conversation["title"] = self._make_title(title_hint)
        conversation["updated_at"] = now
        self._write_conversation(conversation)
        self._upsert_summary(conversation)
        return conversation

    def update(self, conversation_id: str, payload: dict) -> dict | None:
        conversation = self.load(conversation_id)
        if not conversation:
            return None
        for key in ("title", "summary", "source", "platform_id", "sender_id"):
            if key in payload and payload[key] is not None:
                conversation[key] = str(payload[key]).strip()[:240]
        if "context_summary" in payload and payload["context_summary"] is not None:
            conversation["context_summary"] = str(payload["context_summary"]).strip()[:4000]
        if "context_summary_layers" in payload and isinstance(payload["context_summary_layers"], list):
            conversation["context_summary_layers"] = payload["context_summary_layers"][:3]
        if "compacted_until" in payload:
            try:
                conversation["compacted_until"] = max(0, int(payload["compacted_until"]))
            except (TypeError, ValueError):
                pass
        for key in ("archived", "favorite"):
            if key in payload:
                conversation[key] = bool(payload[key])
        conversation["updated_at"] = self._now()
        self._write_conversation(conversation)
        self._upsert_summary(conversation)
        return conversation

    def delete(self, conversation_id: str) -> bool:
        conversation = self.load(conversation_id)
        if not conversation:
            return False
        path = self.root / f"{conversation_id}.json"
        try:
            path.unlink(missing_ok=True)
        except OSError:
            return False
        items = [item for item in self._read_index() if item.get("id") != conversation_id]
        self._write_index(items)
        return True

    def export_markdown(self, conversation_id: str) -> str:
        conversation = self.load(conversation_id)
        if not conversation:
            return ""
        lines = [f"# {conversation.get('title') or conversation_id}", ""]
        if conversation.get("summary"):
            lines.extend([f"> {conversation.get('summary')}", ""])
        for message in conversation.get("messages") or []:
            role = message.get("role") or "message"
            created = message.get("created_at") or ""
            content = message.get("content") or ""
            lines.extend([f"## {role} {created}".strip(), "", content, ""])
        return "\n".join(lines).strip() + "\n"

    def _normalize_conversation(self, conversation: dict) -> dict:
        conversation.setdefault("archived", False)
        conversation.setdefault("favorite", False)
        conversation.setdefault("summary", "")
        conversation.setdefault("context_summary", "")
        conversation.setdefault("context_summary_layers", [])
        conversation.setdefault("compacted_until", 0)
        conversation.setdefault("source", "")
        conversation.setdefault("platform_id", "")
        conversation.setdefault("sender_id", "")
        conversation.setdefault("messages", [])
        for item in conversation["messages"]:
            if isinstance(item, dict):
                item["attachments"] = self._normalize_attachments(item.get("attachments") or [])
        return conversation

    def _read_index(self) -> list[dict]:
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                return []
            return [self._normalize_summary(item) for item in data if isinstance(item, dict)]
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
        conversation = self._normalize_conversation(conversation)
        messages = conversation.get("messages") or []
        last = messages[-1] if messages else {}
        source_message = next(
            (
                item
                for item in reversed(messages)
                if item.get("platform_id") or item.get("sender_id") or item.get("source")
            ),
            {},
        )
        return self._normalize_summary(
            {
                "id": conversation.get("id"),
                "title": conversation.get("title"),
                "sequence": conversation.get("sequence"),
                "created_at": conversation.get("created_at"),
                "updated_at": conversation.get("updated_at"),
                "archived": bool(conversation.get("archived")),
                "favorite": bool(conversation.get("favorite")),
                "summary": conversation.get("summary") or self._auto_summary(messages),
                "message_count": len(messages),
                "last_message": self._message_preview(last),
                "source": conversation.get("source") or source_message.get("source") or "",
                "platform_id": conversation.get("platform_id") or source_message.get("platform_id") or "",
                "sender_id": conversation.get("sender_id") or source_message.get("sender_id") or "",
            }
        )

    def _normalize_summary(self, item: dict) -> dict:
        item.setdefault("archived", False)
        item.setdefault("favorite", False)
        item.setdefault("summary", "")
        item.setdefault("message_count", 0)
        item.setdefault("last_message", "")
        item.setdefault("source", "")
        item.setdefault("platform_id", "")
        item.setdefault("sender_id", "")
        return item

    def _write_json(self, path: Path, data) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def _now(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S")

    def _is_default_title(self, title: str) -> bool:
        return bool(re.match(r"^(对话|瀵硅瘽)\s+\d+$", title or ""))

    def _make_title(self, text: str) -> str:
        text = re.sub(r"\s+", " ", text).strip()
        return text[:18] or "新的对话"

    def _auto_summary(self, messages: list[dict]) -> str:
        useful = [
            str(item.get("content") or "").strip()
            for item in messages
            if item.get("role") in {"user", "assistant"} and item.get("content")
        ]
        if not useful:
            return ""
        text = re.sub(r"\s+", " ", useful[-1]).strip()
        return text[:96]

    def _normalize_attachments(self, attachments) -> list[dict]:
        if not isinstance(attachments, list):
            return []
        normalized = []
        for attachment in attachments:
            if not isinstance(attachment, dict):
                continue
            atype = str(attachment.get("type") or "").strip()
            if atype not in {"image", "sticker"}:
                continue
            item = {
                "type": atype,
                "asset_id": str(attachment.get("asset_id") or attachment.get("id") or ""),
                "url": str(attachment.get("url") or ""),
                "mime": str(attachment.get("mime") or ""),
                "tag": str(attachment.get("tag") or ""),
                "name": str(attachment.get("name") or ""),
                "summary": str(attachment.get("summary") or ""),
            }
            if item["asset_id"] or item["url"]:
                normalized.append(item)
        return normalized[:6]

    def _message_preview(self, message: dict) -> str:
        content = str((message or {}).get("content") or "").strip()
        if content:
            return content[:80]
        attachments = (message or {}).get("attachments") or []
        if attachments:
            atype = attachments[0].get("type")
            return "[图片]" if atype == "image" else "[表情包]"
        return ""
