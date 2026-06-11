from __future__ import annotations

import re
from dataclasses import dataclass


STICKER_DIRECTIVE_RE = re.compile(
    r"[\[【]\s*(?:表情包|表情|sticker)\s*[:：]\s*([^\]】\r\n]{1,48})\s*[\]】]",
    flags=re.I,
)
DIRECTIVE_LABELS = ("表情包", "表情", "sticker")


@dataclass
class StickerDirectiveResult:
    text: str
    tags: list[str]


def extract_sticker_directives(text: str, *, strip: bool = True) -> StickerDirectiveResult:
    tags: list[str] = []

    def replace(match: re.Match[str]) -> str:
        tag = clean_sticker_tag(match.group(1))
        if tag:
            tags.append(tag)
        return ""

    cleaned = STICKER_DIRECTIVE_RE.sub(replace, str(text or ""))
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n[ \t]+", "\n", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return StickerDirectiveResult(cleaned.strip() if strip else cleaned, unique_tags(tags))


def clean_sticker_tag(value: str) -> str:
    value = re.sub(r"\s+", "", str(value or "").strip())
    value = value.strip("[]【】()（）,:：，。.!！？?、;；")
    return value[:32]


def unique_tags(tags: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for tag in tags:
        key = tag.lower()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(tag)
    return result


class StickerDirectiveStreamFilter:
    """Hide streamed sticker directives and collect their requested tags."""

    def __init__(self) -> None:
        self.pending = ""
        self.tags: list[str] = []

    def feed(self, text: str) -> str:
        self.pending += str(text or "")
        return self._drain(complete_only=True)

    def flush(self) -> str:
        return self._drain(complete_only=False)

    def consume_tags(self) -> list[str]:
        tags = self.tags
        self.tags = []
        return tags

    def _drain(self, *, complete_only: bool) -> str:
        if not self.pending:
            return ""
        if complete_only:
            split_at = self._unclosed_directive_start(self.pending)
            chunk = self.pending[:split_at]
            self.pending = self.pending[split_at:]
        else:
            chunk = self.pending
            self.pending = ""
        result = extract_sticker_directives(chunk, strip=False)
        self.tags.extend(result.tags)
        return result.text

    @staticmethod
    def _unclosed_directive_start(text: str) -> int:
        start = max(text.rfind("["), text.rfind("【"))
        if start < 0:
            return len(text)
        fragment = text[start:]
        if "]" in fragment or "】" in fragment:
            return len(text)
        if len(fragment) > 64:
            return len(text)
        body = fragment[1:].lstrip()
        if not body:
            return start
        label = re.split(r"[:：]", body, maxsplit=1)[0].strip().lower()
        if any(item.startswith(label) or label.startswith(item) for item in DIRECTIVE_LABELS):
            return start
        return len(text)
