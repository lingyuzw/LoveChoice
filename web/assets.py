from __future__ import annotations

import base64
import re
import uuid
from pathlib import Path


ALLOWED_AVATAR_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
MAX_AVATAR_BYTES = 2 * 1024 * 1024


class AvatarStore:
    def __init__(self, root: Path, public_prefix: str = "/runtime/uploads/avatars"):
        self.root = root
        self.public_prefix = public_prefix.rstrip("/")
        self.root.mkdir(parents=True, exist_ok=True)

    def save_data_url(self, data_url: str) -> dict:
        match = re.match(r"^data:(image/[a-zA-Z0-9.+-]+);base64,(.+)$", str(data_url or ""), flags=re.S)
        if not match:
            raise ValueError("avatar must be a base64 image data URL")
        mime = match.group(1).lower()
        suffix = ALLOWED_AVATAR_TYPES.get(mime)
        if not suffix:
            raise ValueError("unsupported avatar image type")
        try:
            raw = base64.b64decode(match.group(2), validate=True)
        except Exception as exc:
            raise ValueError("invalid avatar base64 data") from exc
        if not raw:
            raise ValueError("avatar is empty")
        if len(raw) > MAX_AVATAR_BYTES:
            raise ValueError("avatar is too large; limit is 2 MB")
        name = f"{uuid.uuid4().hex}{suffix}"
        path = self.root / name
        path.write_bytes(raw)
        return {
            "url": f"{self.public_prefix}/{name}",
            "filename": name,
            "mime": mime,
            "size": len(raw),
        }
