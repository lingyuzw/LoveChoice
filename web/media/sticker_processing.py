from __future__ import annotations

import base64
import hashlib
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps


SUPPORTED_STICKER_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}


@dataclass(frozen=True)
class ProcessedStickerImage:
    source_hash: str
    original_path: Path
    processed_path: Path
    send_path: Path
    thumbnail_path: Path
    mime: str
    width: int
    height: int
    size: int


def parse_image_data_url(data_url: str) -> tuple[str, bytes]:
    match = re.match(r"^data:([^;,]+);base64,(.+)$", str(data_url or ""), flags=re.S)
    if not match:
        raise ValueError("需要 data URL 格式的图片")
    mime = match.group(1).strip().lower()
    if mime == "image/gif":
        raise ValueError("第一版暂不支持 GIF 表情包，请上传 PNG/JPG/WebP")
    if mime not in SUPPORTED_STICKER_MIME:
        raise ValueError("仅支持 png/jpg/jpeg/webp 表情包")
    try:
        raw = base64.b64decode(match.group(2), validate=True)
    except Exception as exc:
        raise ValueError("图片 base64 解析失败") from exc
    if not raw:
        raise ValueError("图片为空")
    return mime, raw


def safe_stem(value: str, fallback: str = "sticker") -> str:
    value = re.sub(r"\s+", "-", str(value or "").strip())
    value = re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fff]+", "", value)
    return value[:48] or fallback


def ensure_dirs(*paths: Path) -> None:
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)


def save_sticker_image(
    *,
    data_url: str,
    original_dir: Path,
    processed_dir: Path,
    send_dir: Path,
    thumbnail_dir: Path,
    name_hint: str = "",
    max_bytes: int = 8 * 1024 * 1024,
    send_max_edge: int = 640,
    thumb_max_edge: int = 220,
) -> ProcessedStickerImage:
    mime, raw = parse_image_data_url(data_url)
    if len(raw) > max_bytes:
        raise ValueError("表情包不能超过 8 MB")

    ensure_dirs(original_dir, processed_dir, send_dir, thumbnail_dir)
    source_hash = hashlib.sha256(raw).hexdigest()
    ext = SUPPORTED_STICKER_MIME[mime]
    stem = safe_stem(name_hint, fallback=f"sticker_{uuid.uuid4().hex[:8]}")
    unique = f"{source_hash[:12]}_{stem}"

    original_path = original_dir / f"{unique}{ext}"
    if not original_path.exists():
        original_path.write_bytes(raw)

    with Image.open(original_path) as image:
        image = ImageOps.exif_transpose(image)
        width, height = image.size
        normalized = image.convert("RGBA") if image.mode not in {"RGB", "RGBA"} else image.copy()

        processed_path = processed_dir / f"{unique}.webp"
        processed = normalized.copy()
        processed.thumbnail((send_max_edge, send_max_edge), Image.Resampling.LANCZOS)
        processed.save(processed_path, "WEBP", quality=92, method=6)

        send_path = send_dir / f"{unique}.png"
        send_image = normalized.copy()
        send_image.thumbnail((send_max_edge, send_max_edge), Image.Resampling.LANCZOS)
        send_image.save(send_path, "PNG", optimize=True)

        thumbnail_path = thumbnail_dir / f"{unique}.webp"
        thumb = normalized.copy()
        thumb.thumbnail((thumb_max_edge, thumb_max_edge), Image.Resampling.LANCZOS)
        thumb.save(thumbnail_path, "WEBP", quality=82, method=6)

    return ProcessedStickerImage(
        source_hash=source_hash,
        original_path=original_path,
        processed_path=processed_path,
        send_path=send_path,
        thumbnail_path=thumbnail_path,
        mime=mime,
        width=width,
        height=height,
        size=len(raw),
    )
