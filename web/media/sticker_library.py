from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from typing import Any

from media.assets import normalize_channel, normalize_channels, safe_tag
from media.sticker_processing import ProcessedStickerImage, save_sticker_image
from media.sticker_vision import default_sticker_analysis, normalize_analysis


APPROVED_STATUS = "approved"
PENDING_STATUS = "pending"
FAILED_STATUS = "failed"
DISABLED_STATUS = "disabled"
REVIEW_STATUSES = {APPROVED_STATUS, PENDING_STATUS, FAILED_STATUS, DISABLED_STATUS}


def now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def public_runtime_path(path: Path) -> str:
    parts = path.as_posix().split("/runtime/", 1)
    return f"/runtime/{parts[1]}" if len(parts) == 2 else str(path)


def emotion_prefix(value: str) -> str:
    value = re.sub(r"[^0-9a-zA-Z_\-]", "", str(value or "").lower())
    return value or "sticker"


class StickerLibrary:
    def __init__(
        self,
        *,
        index_path: Path,
        original_dir: Path,
        processed_dir: Path,
        send_dir: Path,
        thumbnail_dir: Path,
    ) -> None:
        self.index_path = index_path
        self.original_dir = original_dir
        self.processed_dir = processed_dir
        self.send_dir = send_dir
        self.thumbnail_dir = thumbnail_dir
        for path in (index_path.parent, original_dir, processed_dir, send_dir, thumbnail_dir):
            path.mkdir(parents=True, exist_ok=True)
        if not index_path.exists():
            self.save([])

    def list(self, *, status: str = "", emotion: str = "", query: str = "") -> list[dict]:
        items = self.load()
        status = str(status or "").strip()
        emotion = str(emotion or "").strip()
        query = str(query or "").strip().lower()
        if status:
            items = [item for item in items if item.get("review_status") == status]
        if emotion:
            items = [item for item in items if item.get("emotion") == emotion]
        if query:
            items = [item for item in items if query in self.search_blob(item)]
        return items

    def load(self) -> list[dict]:
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                return []
        except Exception:
            return []
        return [self.normalize(item) for item in data if isinstance(item, dict)]

    def save(self, items: list[dict]) -> None:
        self.index_path.write_text(json.dumps([self.normalize(item) for item in items], ensure_ascii=False, indent=2), encoding="utf-8")

    def normalize(self, item: dict[str, Any]) -> dict:
        status = str(item.get("review_status") or (APPROVED_STATUS if item.get("legacy_approved") else PENDING_STATUS))
        if status not in REVIEW_STATUSES:
            status = PENDING_STATUS
        enabled = item.get("enabled") is not False and status == APPROVED_STATUS
        tags = self.normalize_text_list(item.get("tags") or ([item.get("tag")] if item.get("tag") else []))
        scene = self.normalize_text_list(item.get("scene"))
        avoid = self.normalize_text_list(item.get("avoid"))
        return {
            "id": str(item.get("id") or f"stk_{uuid.uuid4().hex[:12]}"),
            "name": str(item.get("name") or item.get("id") or "表情包")[:80],
            "tag": safe_tag(str(item.get("tag") or (tags[0] if tags else item.get("emotion") or "默认"))),
            "emotion": emotion_prefix(str(item.get("emotion") or item.get("tag") or "laugh")),
            "intensity": max(1, min(5, int(item.get("intensity") or 3))),
            "tags": tags,
            "scene": scene,
            "avoid": avoid,
            "caption": str(item.get("caption") or "")[:360],
            "ocr_text": str(item.get("ocr_text") or "")[:240],
            "description": str(item.get("description") or "")[:360],
            "confidence": float(item.get("confidence") or 0.0),
            "source_hash": str(item.get("source_hash") or ""),
            "mime": str(item.get("mime") or "image/png"),
            "file": str(item.get("file") or item.get("url") or ""),
            "path": str(item.get("path") or ""),
            "send_file": str(item.get("send_file") or item.get("path") or ""),
            "send_path": str(item.get("send_path") or item.get("path") or ""),
            "thumbnail": str(item.get("thumbnail") or item.get("url") or ""),
            "thumbnail_path": str(item.get("thumbnail_path") or ""),
            "original_file": str(item.get("original_file") or ""),
            "original_path": str(item.get("original_path") or ""),
            "url": str(item.get("url") or item.get("thumbnail") or item.get("file") or ""),
            "review_status": status,
            "enabled": enabled,
            "channels": normalize_channels(item.get("channels") or "all"),
            "error": str(item.get("error") or "")[:500],
            "created_at": str(item.get("created_at") or now_text()),
            "updated_at": str(item.get("updated_at") or ""),
            "last_used_at": str(item.get("last_used_at") or ""),
            "use_count": int(item.get("use_count") or 0),
        }

    def normalize_text_list(self, value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(item).strip()[:40] for item in value if str(item).strip()][:10]
        if isinstance(value, str) and value.strip():
            return [item.strip()[:40] for item in re.split(r"[,，/、\s]+", value) if item.strip()][:10]
        return []

    def search_blob(self, item: dict) -> str:
        return " ".join(
            [
                str(item.get("name") or ""),
                str(item.get("tag") or ""),
                str(item.get("emotion") or ""),
                str(item.get("caption") or ""),
                str(item.get("ocr_text") or ""),
                " ".join(item.get("tags") or []),
                " ".join(item.get("scene") or []),
            ]
        ).lower()

    def find_by_hash(self, source_hash: str) -> dict | None:
        source_hash = str(source_hash or "")
        if not source_hash:
            return None
        return next((item for item in self.load() if item.get("source_hash") == source_hash), None)

    def create_pending(
        self,
        *,
        processed: ProcessedStickerImage,
        analysis: dict | None = None,
        channels: str | list[str] = "all",
        name: str = "",
        error: str = "",
    ) -> dict:
        analysis = normalize_analysis(analysis or default_sticker_analysis())
        sticker_id = self.next_id(analysis["emotion"])
        item = {
            "id": sticker_id,
            "name": name or sticker_id,
            "tag": analysis["tags"][0] if analysis["tags"] else analysis["emotion"],
            "emotion": analysis["emotion"],
            "intensity": analysis["intensity"],
            "tags": analysis["tags"],
            "scene": analysis["scene"],
            "avoid": analysis["avoid"],
            "caption": analysis["caption"],
            "ocr_text": analysis["ocr_text"],
            "description": analysis["description"],
            "confidence": analysis["confidence"],
            "source_hash": processed.source_hash,
            "mime": processed.mime,
            "file": public_runtime_path(processed.processed_path),
            "path": str(processed.processed_path),
            "send_file": public_runtime_path(processed.send_path),
            "send_path": str(processed.send_path),
            "thumbnail": public_runtime_path(processed.thumbnail_path),
            "thumbnail_path": str(processed.thumbnail_path),
            "original_file": public_runtime_path(processed.original_path),
            "original_path": str(processed.original_path),
            "url": public_runtime_path(processed.thumbnail_path),
            "review_status": FAILED_STATUS if error else PENDING_STATUS,
            "enabled": False,
            "channels": normalize_channels(channels),
            "error": error,
            "created_at": now_text(),
            "updated_at": now_text(),
        }
        items = self.load()
        items.insert(0, item)
        self.save(items)
        return self.normalize(item)

    def next_id(self, emotion: str) -> str:
        prefix = emotion_prefix(emotion)
        used = {item.get("id") for item in self.load()}
        for index in range(1, 10000):
            candidate = f"{prefix}_{index:03d}"
            if candidate not in used:
                return candidate
        return f"{prefix}_{uuid.uuid4().hex[:8]}"

    def add_upload(self, *, data_url: str, name: str = "", channels: str | list[str] = "all", analysis: dict | None = None, error: str = "") -> dict:
        processed = save_sticker_image(
            data_url=data_url,
            original_dir=self.original_dir,
            processed_dir=self.processed_dir,
            send_dir=self.send_dir,
            thumbnail_dir=self.thumbnail_dir,
            name_hint=name,
        )
        duplicate = self.find_by_hash(processed.source_hash)
        if duplicate:
            return {**duplicate, "duplicate": True}
        return self.create_pending(processed=processed, analysis=analysis, channels=channels, name=name, error=error)

    def update(self, sticker_id: str, patch: dict) -> dict:
        items = self.load()
        for index, item in enumerate(items):
            if item.get("id") != sticker_id:
                continue
            updated = {**item, **self.sanitize_patch(patch), "updated_at": now_text()}
            items[index] = self.normalize(updated)
            self.save(items)
            return items[index]
        raise KeyError(sticker_id)

    def sanitize_patch(self, patch: dict) -> dict:
        allowed = {
            "name",
            "tag",
            "emotion",
            "intensity",
            "tags",
            "scene",
            "avoid",
            "caption",
            "ocr_text",
            "description",
            "confidence",
            "review_status",
            "enabled",
            "channels",
            "error",
        }
        return {key: value for key, value in (patch or {}).items() if key in allowed}

    def approve(self, sticker_id: str) -> dict:
        return self.update(sticker_id, {"review_status": APPROVED_STATUS, "enabled": True, "error": ""})

    def delete(self, sticker_id: str) -> bool:
        items = self.load()
        target = next((item for item in items if item.get("id") == sticker_id), None)
        if not target:
            return False
        for key in ("path", "send_path", "thumbnail_path"):
            try:
                Path(target.get(key) or "").unlink(missing_ok=True)
            except OSError:
                pass
        self.save([item for item in items if item.get("id") != sticker_id])
        return True

    def choose(self, tag: str = "", avoid_id: str = "", channel: str = "web") -> dict | None:
        channel = normalize_channel(channel)
        items = [
            item
            for item in self.load()
            if item.get("enabled") and item.get("review_status") == APPROVED_STATUS and channel in item.get("channels", [])
        ]
        if tag:
            tagged = [item for item in items if (item.get("tag") == tag or tag in item.get("tags", [])) and item.get("id") != avoid_id]
            items = tagged or [item for item in items if item.get("id") != avoid_id]
        else:
            items = [item for item in items if item.get("id") != avoid_id]
        if not items:
            return None
        items.sort(key=lambda item: (int(item.get("use_count") or 0), item.get("last_used_at") or ""))
        return items[0]

    def mark_used(self, sticker_id: str) -> dict | None:
        items = self.load()
        found = None
        for item in items:
            if item.get("id") == sticker_id:
                item["last_used_at"] = now_text()
                item["use_count"] = int(item.get("use_count") or 0) + 1
                found = item
                break
        if found:
            self.save(items)
        return found

    def mark_used_many(self, sticker_ids: list[str]) -> list[dict]:
        changed = []
        for sticker_id in sticker_ids:
            item = self.mark_used(str(sticker_id or ""))
            if item:
                changed.append(item)
        return changed
