from __future__ import annotations

import re


def compact_text(text: str, limit: int = 600) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "..."


def format_reply_paragraphs(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", str(text or ""), flags=re.S | re.I)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def split_reply_messages(text: str, *, max_parts: int = 6, max_chars: int = 60) -> list[str]:
    text = format_reply_paragraphs(str(text or "")).strip()
    if not text:
        return []
    clauses = natural_reply_clauses(text)
    return merge_reply_clauses(clauses, max_chars=max_chars, max_parts=max_parts)


def natural_reply_clauses(text: str) -> list[str]:
    clauses: list[str] = []
    for line in re.split(r"\r?\n+", text):
        line = line.strip()
        if not line:
            continue
        start = 0
        for match in re.finditer(r"[\u3002\uff01\uff1f!?~\uff5e]+", line):
            end = match.end()
            part = line[start:end].strip()
            if part:
                clauses.append(part)
            start = end
        rest = line[start:].strip()
        if rest:
            clauses.extend(split_long_clause(rest))
    return clauses or [text]


def split_long_clause(text: str, *, soft_limit: int = 46) -> list[str]:
    text = text.strip()
    if len(text) <= soft_limit:
        return [text]
    pieces = [part.strip() for part in re.split(r"(?<=[\uff0c,\u3001\uff1b;])", text) if part.strip()]
    if len(pieces) <= 1:
        return chunk_long_text(text, soft_limit * 2)
    return pieces


def merge_reply_clauses(clauses: list[str], *, max_chars: int, max_parts: int = 6) -> list[str]:
    merged: list[str] = []
    current = ""
    min_chars = min(18, max_chars)
    for clause in clauses:
        clause = clause.strip()
        if not clause:
            continue
        candidate = join_reply_parts(current, clause) if current else clause
        if current and len(candidate) > max_chars and len(current) >= min_chars:
            merged.append(current)
            current = clause
        else:
            current = candidate
    if current:
        merged.append(current)
    bounded: list[str] = []
    for item in merged:
        if len(item) > max_chars * 2:
            bounded.extend(chunk_long_text(item, max_chars * 2))
        else:
            bounded.append(item)
    return [item for item in bounded if item]


def chunk_long_text(text: str, limit: int) -> list[str]:
    text = text.strip()
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + limit)
        if end < len(text):
            cut = max(
                text.rfind("\u3002", start, end),
                text.rfind("\uff01", start, end),
                text.rfind("\uff1f", start, end),
                text.rfind("\uff0c", start, end),
                text.rfind(",", start, end),
                text.rfind("\u3001", start, end),
                text.rfind("\uff1b", start, end),
                text.rfind(";", start, end),
                text.rfind(" ", start, end),
            )
            if cut > start + max(12, int(limit * 0.45)):
                end = cut + 1
        chunks.append(text[start:end].strip())
        start = end
    return [item for item in chunks if item]


def trim_reply_part(text: str, limit: int) -> str:
    return text.strip()


def join_reply_parts(left: str, right: str) -> str:
    left = left.strip()
    right = right.strip()
    if not left:
        return right
    if not right:
        return left
    if re.search(r"[\u4e00-\u9fff\u3002\uff01\uff1f~\uff5e\uff0c\u3001\uff1b\uff1a]$", left) and re.search(
        r"^[\u4e00-\u9fff\u201c\u2018\uff08\u300a]",
        right,
    ):
        return left + right
    return f"{left} {right}".strip()


def is_story_request(text: str) -> bool:
    return any(keyword in text for keyword in ("\u6545\u4e8b", "\u7761\u524d", "\u7ae5\u8bdd"))


def extract_repeat_text(text: str) -> str | None:
    prefixes = (
        "\u8ddf\u7740\u6211\u8bf4",
        "\u8ddf\u6211\u8bf4",
        "\u8ddf\u6211\u5ff5",
        "\u7167\u7740\u6211\u8bf4",
        "\u590d\u8bfb",
        "\u91cd\u590d",
        "\u8bf7\u4f60\u91cd\u590d",
        "\u8bf7\u4f60\u8ddf\u7740\u6211\u8bf4",
        "\u4f60\u8ddf\u7740\u6211\u8bf4",
    )
    for prefix in prefixes:
        index = str(text or "").find(prefix)
        if index == -1:
            continue
        value = str(text or "")[index + len(prefix) :].strip().lstrip("\u3000 \t\r\n\uff0c,:\uff1a")
        return value or None
    return None
