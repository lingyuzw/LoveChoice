from __future__ import annotations

import re


def compact_text(text: str, limit: int = 600) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "..."


def format_reply_paragraphs(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", str(text or ""), flags=re.S | re.I)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def split_reply_messages(text: str, *, max_parts: int = 6, max_chars: int = 26) -> list[str]:
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
        if not re.search(r"[\u3002\uff01\uff1f!?~\uff5e\uff0c,\u3001\uff1b;]", line):
            spaced = split_spaced_chinese_clauses(line)
            if len(spaced) > 1:
                clauses.extend(spaced)
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


def split_spaced_chinese_clauses(text: str, *, min_part_len: int = 2) -> list[str]:
    parts = [part.strip() for part in re.split(r"\s+", text.strip()) if part.strip()]
    if len(parts) <= 1:
        return [text.strip()] if text.strip() else []
    chinese_like = sum(1 for part in parts if re.search(r"[\u4e00-\u9fff]", part))
    if chinese_like < max(2, int(len(parts) * 0.6)):
        return [text.strip()]
    merged: list[str] = []
    current = ""
    short_prefixes = {
        "\u6211",
        "\u4f60",
        "\u4ed6",
        "\u5979",
        "\u5b83",
        "\u8fd9",
        "\u90a3",
        "\u5c31",
        "\u4f46",
        "\u53ef",
        "\u554a",
        "\u54ce",
    }
    for part in parts:
        if len(part) < min_part_len or part in short_prefixes:
            current = join_reply_parts(current, part) if current else part
            continue
        if current:
            merged.append(current)
        current = part
    if current:
        merged.append(current)
    return merged if len(merged) > 1 else parts


def split_long_clause(text: str, *, soft_limit: int = 46) -> list[str]:
    text = text.strip()
    if len(text) <= soft_limit:
        return [text]
    spaced = split_spaced_chinese_clauses(text)
    if len(spaced) > 1:
        return spaced
    pieces = [part.strip() for part in re.split(r"(?<=[\uff0c,\u3001\uff1b;])", text) if part.strip()]
    if len(pieces) <= 1:
        return chunk_long_text(text, soft_limit * 2)
    result: list[str] = []
    for piece in pieces:
        sub_spaced = split_spaced_chinese_clauses(piece)
        if len(sub_spaced) > 1:
            result.extend(sub_spaced)
        else:
            result.append(piece)
    return result


def merge_reply_clauses(clauses: list[str], *, max_chars: int, max_parts: int = 6) -> list[str]:
    merged: list[str] = []
    current = ""
    min_chars = min(18, max_chars)
    for clause in clauses:
        clause = clause.strip()
        if not clause:
            continue
        candidate = join_reply_parts(current, clause) if current else clause
        if current and (len(candidate) > max_chars or is_chat_pause_clause(clause, current)):
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


def is_chat_pause_clause(next_clause: str, current: str) -> bool:
    next_clause = next_clause.strip()
    current = current.strip()
    standalone_current = {"\u544a\u8f9e", "\u7b97\u4e86", "\u884c\u5427", "\u61d2\u5f97"}
    if current in standalone_current:
        return True
    pause_starters = (
        "\u6253\u67b6",
        "\u544a\u8f9e",
        "\u602a\u6211",
        "\u5e03\u4e01",
        "\u6211\u53ea",
        "\u4f60\u627e",
        "\u5973\u7684",
        "\u7537\u7684",
    )
    if any(next_clause.startswith(prefix) for prefix in pause_starters):
        return True
    if len(current) < 8 and not next_clause.startswith(("\u6211\u53ea", "\u4f60\u627e")):
        return False
    if len(next_clause) <= 2:
        return False
    return len(current) >= 18 and re.search(r"^[\u6211\u4f60\u4ed6\u5979\u5b83\u8fd9\u90a3]", next_clause)


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
