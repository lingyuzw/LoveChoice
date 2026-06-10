from __future__ import annotations

import base64
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any


IMAGE_MIME_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


def now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def safe_tag(value: str) -> str:
    value = re.sub(r"\s+", "-", str(value or "").strip())
    value = re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fff]+", "", value)
    return value[:32] or "默认"


def parse_data_url(data_url: str) -> tuple[str, bytes]:
    match = re.match(r"^data:([^;,]+);base64,(.+)$", str(data_url or ""), flags=re.S)
    if not match:
        raise ValueError("需要 data URL 格式的图片")
    mime = match.group(1).strip().lower()
    if mime not in IMAGE_MIME_EXT:
        raise ValueError("仅支持 png/jpeg/webp/gif 图片")
    try:
        raw = base64.b64decode(match.group(2), validate=True)
    except Exception as exc:
        raise ValueError("图片 base64 解析失败") from exc
    if not raw:
        raise ValueError("图片为空")
    return mime, raw


class ChatImageStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def save_data_url(self, data_url: str, max_mb: float = 8.0) -> dict:
        mime, raw = parse_data_url(data_url)
        max_bytes = int(max(1.0, float(max_mb or 8.0)) * 1024 * 1024)
        if len(raw) > max_bytes:
            raise ValueError(f"图片不能超过 {max_mb:g} MB")
        asset_id = f"img_{uuid.uuid4().hex[:16]}"
        ext = IMAGE_MIME_EXT[mime]
        path = self.root / f"{asset_id}{ext}"
        path.write_bytes(raw)
        return {
            "id": asset_id,
            "type": "image",
            "mime": mime,
            "size": len(raw),
            "url": f"/runtime/uploads/chat_images/{path.name}",
            "path": str(path),
            "created_at": now_text(),
        }

    def resolve(self, asset_id: str) -> dict | None:
        safe_id = re.sub(r"[^0-9A-Za-z_\-]", "", str(asset_id or ""))
        if not safe_id:
            return None
        for ext in IMAGE_MIME_EXT.values():
            path = self.root / f"{safe_id}{ext}"
            if path.exists():
                return {
                    "id": safe_id,
                    "type": "image",
                    "mime": next((m for m, e in IMAGE_MIME_EXT.items() if e == ext), "image/png"),
                    "size": path.stat().st_size,
                    "url": f"/runtime/uploads/chat_images/{path.name}",
                    "path": str(path),
                }
        return None


class StickerStore:
    def __init__(self, root: Path, index_path: Path):
        self.root = root
        self.index_path = index_path
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.index_path.exists():
            self.save([])

    def list(self) -> list[dict]:
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                return []
        except Exception:
            return []
        return [self.normalize(item) for item in data if isinstance(item, dict)]

    def save(self, items: list[dict]) -> None:
        self.index_path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")

    def add_data_url(self, data_url: str, tag: str = "默认", name: str = "") -> dict:
        mime, raw = parse_data_url(data_url)
        if len(raw) > 8 * 1024 * 1024:
            raise ValueError("表情包不能超过 8 MB")
        sticker_id = f"stk_{uuid.uuid4().hex[:16]}"
        ext = IMAGE_MIME_EXT[mime]
        path = self.root / f"{sticker_id}{ext}"
        path.write_bytes(raw)
        item = {
            "id": sticker_id,
            "name": str(name or safe_tag(tag)).strip()[:48],
            "tag": safe_tag(tag),
            "mime": mime,
            "url": f"/runtime/uploads/stickers/{path.name}",
            "path": str(path),
            "enabled": True,
            "created_at": now_text(),
            "last_used_at": "",
            "use_count": 0,
        }
        items = self.list()
        items.insert(0, item)
        self.save(items)
        return item

    def delete(self, sticker_id: str) -> bool:
        items = self.list()
        target = next((item for item in items if item["id"] == sticker_id), None)
        if not target:
            return False
        try:
            Path(target.get("path") or "").unlink(missing_ok=True)
        except OSError:
            pass
        self.save([item for item in items if item["id"] != sticker_id])
        return True

    def mark_used(self, sticker_id: str) -> dict | None:
        items = self.list()
        found = None
        for item in items:
            if item["id"] == sticker_id:
                item["last_used_at"] = now_text()
                item["use_count"] = int(item.get("use_count") or 0) + 1
                found = item
                break
        if found:
            self.save(items)
        return found

    def choose(self, tag: str = "", avoid_id: str = "") -> dict | None:
        items = [item for item in self.list() if item.get("enabled", True)]
        if tag:
            tagged = [item for item in items if item.get("tag") == tag and item.get("id") != avoid_id]
            if tagged:
                items = tagged
            else:
                items = [item for item in items if item.get("id") != avoid_id]
        else:
            items = [item for item in items if item.get("id") != avoid_id]
        if not items:
            return None
        items.sort(key=lambda item: (int(item.get("use_count") or 0), item.get("last_used_at") or ""))
        return items[0]

    def normalize(self, item: dict[str, Any]) -> dict:
        return {
            "id": str(item.get("id") or ""),
            "name": str(item.get("name") or item.get("tag") or "表情包"),
            "tag": safe_tag(str(item.get("tag") or "默认")),
            "mime": str(item.get("mime") or "image/png"),
            "url": str(item.get("url") or ""),
            "path": str(item.get("path") or ""),
            "enabled": item.get("enabled") is not False,
            "created_at": str(item.get("created_at") or ""),
            "last_used_at": str(item.get("last_used_at") or ""),
            "use_count": int(item.get("use_count") or 0),
        }
