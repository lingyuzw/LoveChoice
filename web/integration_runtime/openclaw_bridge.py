from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import random
import signal
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from core.text_utils import split_reply_messages
from weixin_media import WeixinImageSendError, WeixinVoiceSendError, download_weixin_media, send_weixin_image, send_weixin_voice


DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
DEFAULT_CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
DEFAULT_POLL_TIMEOUT_MS = 35_000
OPENCLAW_WEIXIN_VERSION = "2.4.4"
ILINK_APP_ID = "bot"
MESSAGE_TYPE_USER = 1
MESSAGE_TYPE_BOT = 2
MESSAGE_STATE_FINISH = 2
ITEM_TEXT = 1
ITEM_IMAGE = 2
ITEM_VOICE = 3
SESSION_EXPIRED = -14


STOP = False
IMAGE_FOLLOWUP_WAIT_SEC = 3.0


def build_client_version(version: str) -> int:
    parts = []
    for part in str(version or "").split(".")[:3]:
        try:
            parts.append(int(part))
        except ValueError:
            parts.append(0)
    while len(parts) < 3:
        parts.append(0)
    major, minor, patch = parts
    return ((major & 0xFF) << 16) | ((minor & 0xFF) << 8) | (patch & 0xFF)


def log(text: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {text}", flush=True)


def handle_signal(signum, _frame) -> None:
    global STOP
    STOP = True
    log(f"received signal {signum}, stopping after current poll")


def resolve_state_dir(profile: str, explicit: str = "") -> Path:
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get("OPENCLAW_STATE_DIR") or os.environ.get("CLAWDBOT_STATE_DIR")
    if env:
        return Path(env).expanduser()
    home = Path.home()
    if profile and profile not in {"default", "main"}:
        return home / f".openclaw-{profile}"
    return home / ".openclaw"


def weixin_state_dir(state_dir: Path) -> Path:
    return state_dir / "openclaw-weixin"


def accounts_dir(state_dir: Path) -> Path:
    return weixin_state_dir(state_dir) / "accounts"


def load_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def derive_raw_account_id(account_id: str) -> str | None:
    if account_id.endswith("-im-bot"):
        return f"{account_id[:-7]}@im.bot"
    if account_id.endswith("-im-wechat"):
        return f"{account_id[:-10]}@im.wechat"
    return None


def list_account_ids(state_dir: Path) -> list[str]:
    index = weixin_state_dir(state_dir) / "accounts.json"
    data = load_json(index, [])
    if isinstance(data, list):
        return [str(item) for item in data if str(item).strip()]
    return []


def load_account(state_dir: Path, account_id: str) -> dict:
    candidates = [accounts_dir(state_dir) / f"{account_id}.json"]
    raw = derive_raw_account_id(account_id)
    if raw:
        candidates.append(accounts_dir(state_dir) / f"{raw}.json")
    candidates.append(state_dir / "credentials" / "openclaw-weixin" / "credentials.json")
    for path in candidates:
        data = load_json(path, None)
        if isinstance(data, dict) and data.get("token"):
            return {
                "account_id": account_id,
                "token": str(data.get("token") or ""),
                "base_url": str(data.get("baseUrl") or data.get("base_url") or DEFAULT_BASE_URL),
                "cdn_base_url": str(data.get("cdnBaseUrl") or data.get("cdn_base_url") or DEFAULT_CDN_BASE_URL),
                "user_id": str(data.get("userId") or data.get("user_id") or ""),
                "path": str(path),
            }
    return {
        "account_id": account_id,
        "token": "",
        "base_url": DEFAULT_BASE_URL,
        "cdn_base_url": DEFAULT_CDN_BASE_URL,
        "user_id": "",
        "path": "",
    }


def sync_path(state_dir: Path, account_id: str) -> Path:
    return accounts_dir(state_dir) / f"{account_id}.sync.json"


def load_sync_buf(state_dir: Path, account_id: str) -> str:
    paths = [sync_path(state_dir, account_id)]
    raw = derive_raw_account_id(account_id)
    if raw:
        paths.append(accounts_dir(state_dir) / f"{raw}.sync.json")
    paths.append(state_dir / "agents" / "default" / "sessions" / ".openclaw-weixin-sync" / "default.json")
    for path in paths:
        data = load_json(path, {})
        if isinstance(data, dict) and isinstance(data.get("get_updates_buf"), str):
            return data["get_updates_buf"]
    return ""


def save_sync_buf(state_dir: Path, account_id: str, value: str) -> None:
    if value:
        save_json(sync_path(state_dir, account_id), {"get_updates_buf": value})


def context_token_path(state_dir: Path, account_id: str) -> Path:
    return accounts_dir(state_dir) / f"{account_id}.context-tokens.json"


def load_context_tokens(state_dir: Path, account_id: str) -> dict[str, str]:
    data = load_json(context_token_path(state_dir, account_id), {})
    if isinstance(data, dict):
        return {str(k): str(v) for k, v in data.items() if k and v}
    return {}


def save_context_tokens(state_dir: Path, account_id: str, tokens: dict[str, str]) -> None:
    save_json(context_token_path(state_dir, account_id), tokens)


def body_from_items(items: list[dict] | None) -> str:
    if not items:
        return ""
    for item in items:
        if item.get("type") == ITEM_TEXT:
            text = ((item.get("text_item") or {}).get("text") or "").strip()
            if not text:
                continue
            ref = item.get("ref_msg") or {}
            ref_item = ref.get("message_item") if isinstance(ref, dict) else None
            if not ref_item:
                return text
            ref_parts = []
            if ref.get("title"):
                ref_parts.append(str(ref["title"]))
            ref_body = body_from_items([ref_item])
            if ref_body:
                ref_parts.append(ref_body)
            return f"[引用: {' | '.join(ref_parts)}]\n{text}" if ref_parts else text
        if item.get("type") == ITEM_VOICE:
            text = ((item.get("voice_item") or {}).get("text") or "").strip()
            if text:
                return text
        if item.get("type") == ITEM_IMAGE:
            return "[图片]"
        if str(item.get("type")) not in {str(ITEM_TEXT), str(ITEM_VOICE)}:
            return "[用户发送了一条当前版本暂不能直接解析的微信媒体消息，可能是图片、表情包或文件。请自然说明你现在还看不到这张图，让用户在 Web 端上传图片，或稍后等微信图片解析接入。]"
    return ""


def image_media_candidates_from_item(item: dict) -> list[dict]:
    image_item = item.get("image_item") if isinstance(item.get("image_item"), dict) else {}
    candidates = []
    for key in ("media", "mid_media", "mid_size", "thumb_media", "thumbnail_media", "thumb"):
        media = image_item.get(key)
        if isinstance(media, dict):
            candidates.append({"kind": key, "media": media})
    return candidates


def extract_image_items(items: list[dict] | None) -> list[dict]:
    result = []
    for item in items or []:
        if item.get("type") != ITEM_IMAGE:
            continue
        candidates = image_media_candidates_from_item(item)
        if candidates:
            result.append({"item": item, "candidates": candidates})
    return result


def download_inbound_images(state_dir: Path, account: dict, msg: dict) -> list[dict]:
    images = []
    for index, entry in enumerate(extract_image_items(msg.get("item_list"))[:4]):
        item_id = str(msg.get("message_id") or msg.get("client_id") or uuid.uuid4().hex[:12])
        output = state_dir / "branchwhisper-inbound-media" / f"{account['account_id']}-{item_id}-{index}.jpg"
        info = {"ok": False, "path": str(output), "mime": "image/jpeg", "index": index, "error": "", "attempts": []}
        for candidate in entry.get("candidates") or []:
            media = candidate.get("media") or {}
            query = str(media.get("encrypt_query_param") or media.get("encrypted_query_param") or "").strip()
            aes_key = str(media.get("aes_key") or media.get("aeskey") or "").strip()
            attempt = {"kind": candidate.get("kind") or "media", "has_query": bool(query), "has_aes_key": bool(aes_key)}
            if not query:
                attempt["error"] = "missing encrypt_query_param"
                info["attempts"].append(attempt)
                continue
            try:
                if aes_key:
                    downloaded = download_weixin_media(
                        encrypt_query_param=query,
                        aes_key=aes_key,
                        output_file=str(output),
                        cdn_base_url=str(account.get("cdn_base_url") or DEFAULT_CDN_BASE_URL),
                    )
                else:
                    downloaded = download_weixin_media(
                        encrypt_query_param=query,
                        aes_key="",
                        output_file=str(output),
                        cdn_base_url=str(account.get("cdn_base_url") or DEFAULT_CDN_BASE_URL),
                    )
                attempt["ok"] = True
                info.update({"ok": True, "download": downloaded, "media_kind": candidate.get("kind") or "media", "error": ""})
                info["attempts"].append(attempt)
                break
            except Exception as exc:
                attempt["error"] = str(exc)
                info["error"] = str(exc)
                info["attempts"].append(attempt)
        if not info.get("ok") and not info.get("error"):
            info["error"] = "incoming image media has no downloadable candidate"
        images.append(info)
    return images


def build_base_info() -> dict:
    return {"channel_version": "branchwhisper-bridge", "bot_agent": "BranchWhisper/1.0 (openclaw-weixin)"}


def build_headers(token: str = "") -> dict:
    uin = base64.b64encode(str(random.getrandbits(32)).encode("utf-8")).decode("ascii")
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": uin,
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(build_client_version(OPENCLAW_WEIXIN_VERSION)),
    }
    if token:
        headers["Authorization"] = f"Bearer {token.strip()}"
    return headers


def endpoint(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def get_updates(client: httpx.Client, account: dict, sync_buf: str, timeout_ms: int) -> dict:
    payload = {"get_updates_buf": sync_buf or "", "base_info": build_base_info()}
    resp = client.post(
        endpoint(account["base_url"], "ilink/bot/getupdates"),
        json=payload,
        headers=build_headers(account["token"]),
        timeout=(timeout_ms / 1000.0) + 10,
    )
    resp.raise_for_status()
    return resp.json()


def notify_start(client: httpx.Client, account: dict) -> None:
    try:
        client.post(
            endpoint(account["base_url"], "ilink/bot/msg/notifystart"),
            json={"base_info": build_base_info()},
            headers=build_headers(account["token"]),
            timeout=10,
        )
    except Exception as exc:
        log(f"notify_start failed account={account['account_id']} err={exc}")


def notify_stop(client: httpx.Client, account: dict) -> None:
    try:
        client.post(
            endpoint(account["base_url"], "ilink/bot/msg/notifystop"),
            json={"base_info": build_base_info()},
            headers=build_headers(account["token"]),
            timeout=10,
        )
    except Exception as exc:
        log(f"notify_stop failed account={account['account_id']} err={exc}")


def send_text(client: httpx.Client, account: dict, to_user_id: str, text: str, context_token: str = "") -> str:
    client_id = f"branchwhisper-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
    payload = {
        "msg": {
            "from_user_id": "",
            "to_user_id": to_user_id,
            "client_id": client_id,
            "message_type": MESSAGE_TYPE_BOT,
            "message_state": MESSAGE_STATE_FINISH,
            "item_list": [{"type": ITEM_TEXT, "text_item": {"text": text}}],
            **({"context_token": context_token} if context_token else {}),
        },
        "base_info": build_base_info(),
    }
    resp = client.post(
        endpoint(account["base_url"], "ilink/bot/sendmessage"),
        json=payload,
        headers=build_headers(account["token"]),
        timeout=20,
    )
    resp.raise_for_status()
    return client_id


def call_branchwhisper(branchwhisper_url: str, integration_id: str, account_id: str, msg: dict, text: str, images: list[dict] | None = None) -> dict:
    from_user_id = msg.get("from_user_id") or ""
    session_id = msg.get("session_id") or msg.get("group_id") or from_user_id or "default"
    payload = {
        "platform_id": integration_id,
        "session_id": str(session_id),
        "sender_id": str(from_user_id),
        "text": text,
        "images": images or [],
        "metadata": {
            "account_id": account_id,
            "channel": "openclaw-weixin",
            "message_id": msg.get("message_id"),
            "client_id": msg.get("client_id"),
            "context_token": msg.get("context_token") or "",
            "create_time_ms": msg.get("create_time_ms"),
        },
    }
    url = f"{branchwhisper_url.rstrip('/')}/api/integrations/dialog"
    with httpx.Client(timeout=120) as client:
        try:
            resp = client.post(url, json=payload)
        except httpx.ConnectError as exc:
            raise RuntimeError(f"BranchWhisper dialog endpoint refused connection: {url}") from exc
        except httpx.TimeoutException as exc:
            raise RuntimeError(f"BranchWhisper dialog endpoint timed out: {url}") from exc
    resp.raise_for_status()
    return resp.json()


def report_branchwhisper_timing(branchwhisper_url: str, integration_id: str, trace_id: str, patch: dict) -> None:
    if not trace_id:
        return
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(
                f"{branchwhisper_url.rstrip('/')}/api/integrations/{integration_id}/timings/{trace_id}",
                json=patch,
            )
            resp.raise_for_status()
    except Exception as exc:
        log(f"timing report failed trace={trace_id}: {exc}")


def voice_fallback_text(result: dict, voice_error: str = "") -> str:
    if not result.get("voice_requested"):
        return ""
    error = str(voice_error or result.get("voice_error") or "").strip()
    if error:
        return f"语音暂时发不出来：{error[:120]}。我先把文字版发给你。"
    return ""


def send_voice_reply(
    *,
    branchwhisper_url: str,
    integration_id: str,
    trace_id: str,
    account: dict,
    to_user_id: str,
    context_token: str,
    result: dict,
) -> bool:
    if not result.get("send_voice") or not result.get("voice_file"):
        return False
    started = time.perf_counter()
    try:
        sent = send_weixin_voice(
            base_url=account["base_url"],
            token=account["token"],
            to_user_id=to_user_id,
            voice_file=str(result.get("voice_file") or ""),
            text=str(result.get("reply_text") or "")[:240],
            context_token=context_token,
            cdn_base_url=str(account.get("cdn_base_url") or DEFAULT_CDN_BASE_URL),
        )
        voice_send_ms = int((time.perf_counter() - started) * 1000)
        report_branchwhisper_timing(
            branchwhisper_url,
            integration_id,
            trace_id,
            {
                "voice_send_ms": voice_send_ms,
                "voice_send_status": "accepted",
                "voice_message_id": str(sent.get("message_id") or ""),
                "voice_stage": str(sent.get("stage") or "accepted"),
                "voice_format": str(sent.get("transcode_format") or ""),
                "voice_diagnostic": json.dumps(
                    {
                        "encode_type": sent.get("encode_type"),
                        "sample_rate": sent.get("sample_rate"),
                        "gain_db": sent.get("gain_db"),
                        "playtime_ms": sent.get("playtime_ms"),
                        "source_audio": sent.get("source_audio"),
                        "transcode_audio": sent.get("transcode_audio"),
                        "upload_ms": sent.get("upload_ms"),
                        "upload_method": sent.get("upload_method"),
                        "upload_url_kind": sent.get("upload_url_kind"),
                        "send_ms": sent.get("send_ms"),
                    },
                    ensure_ascii=False,
                )[:240],
            },
        )
        log(
            f"voice api accepted account={account['account_id']} to={to_user_id} "
            f"message_id={sent.get('message_id') or ''} voice_send_ms={voice_send_ms} "
            f"playtime_ms={sent.get('playtime_ms') or 0} format={sent.get('transcode_format') or ''} "
            f"encode_type={sent.get('encode_type') or ''} client_delivery=unconfirmed"
        )
        return True
    except (WeixinVoiceSendError, Exception) as exc:
        voice_send_ms = int((time.perf_counter() - started) * 1000)
        error = str(exc)
        report_branchwhisper_timing(
            branchwhisper_url,
            integration_id,
            trace_id,
            {
                "voice_send_ms": voice_send_ms,
                "voice_send_status": "failed",
                "voice_error": error[:240],
                "voice_stage": error.split(":", 1)[0][:80] if ":" in error else "unknown",
            },
        )
        result["voice_error"] = error
        log(f"voice send failed account={account['account_id']} to={to_user_id} err={error[:240]}")
        return False


def send_sticker_replies(
    *,
    branchwhisper_url: str,
    integration_id: str,
    trace_id: str,
    account: dict,
    to_user_id: str,
    context_token: str,
    result: dict,
) -> dict:
    attachments = result.get("attachments") if isinstance(result.get("attachments"), list) else []
    stickers = [item for item in attachments if isinstance(item, dict) and item.get("type") == "sticker"]
    if not stickers:
        return {"count": 0, "errors": []}
    sent = 0
    errors: list[str] = []
    sent_ids: list[str] = []
    started = time.perf_counter()
    for sticker in stickers[:2]:
        image_file = str(sticker.get("path") or "").strip()
        if not image_file:
            errors.append("sticker missing local path")
            continue
        try:
            image_sent = send_weixin_image(
                base_url=account["base_url"],
                token=account["token"],
                to_user_id=to_user_id,
                image_file=image_file,
                context_token=context_token,
                cdn_base_url=str(account.get("cdn_base_url") or DEFAULT_CDN_BASE_URL),
            )
            sent += 1
            sticker_id = str(sticker.get("asset_id") or sticker.get("id") or "").strip()
            if sticker_id:
                sent_ids.append(sticker_id)
            log(
                f"sent sticker account={account['account_id']} to={to_user_id} "
                f"message_id={image_sent.get('message_id') or ''} sticker={sticker.get('asset_id') or sticker.get('id') or ''} "
                f"upload_ms={image_sent.get('upload_ms') or 0} send_ms={image_sent.get('send_ms') or 0}"
            )
        except (WeixinImageSendError, Exception) as exc:
            error = str(exc)
            errors.append(error[:180])
            log(
                f"sticker send failed account={account['account_id']} to={to_user_id} "
                f"sticker={sticker.get('asset_id') or sticker.get('id') or ''} err={error[:240]}"
            )
    report_branchwhisper_timing(
        branchwhisper_url,
        integration_id,
        trace_id,
        {
            "sticker_send_ms": int((time.perf_counter() - started) * 1000),
            "sticker_send_status": "sent" if sent else ("failed" if errors else "skipped"),
            "sticker_count": sent,
            "sticker_sent_ids": ",".join(sent_ids),
            "sticker_error": "; ".join(errors)[:240],
        },
    )
    return {"count": sent, "errors": errors}


def message_fingerprint(account_id: str, msg: dict, text: str) -> str:
    material = "|".join(
        [
            account_id,
            str(msg.get("message_id") or ""),
            str(msg.get("client_id") or ""),
            str(msg.get("from_user_id") or ""),
            str(msg.get("create_time_ms") or ""),
            text,
        ]
    )
    return hashlib.sha256(material.encode("utf-8", errors="ignore")).hexdigest()


def dispatch_message(
    client: httpx.Client,
    state_dir: Path,
    branchwhisper_url: str,
    integration_id: str,
    account: dict,
    msg: dict,
    text: str,
    images: list[dict] | None,
    seen: set[str],
) -> None:
    images = images or []
    if images:
        ok_images = sum(1 for item in images if item.get("ok"))
        errors = "; ".join(str(item.get("error") or "") for item in images if item.get("error"))
        log(f"inbound images account={account['account_id']} ok={ok_images}/{len(images)} errors={errors[:180]}")
    fingerprint = message_fingerprint(account["account_id"], msg, text)
    if fingerprint in seen:
        return
    seen.add(fingerprint)

    from_user_id = str(msg.get("from_user_id") or "")
    context_token = str(msg.get("context_token") or "")
    tokens = load_context_tokens(state_dir, account["account_id"])
    if from_user_id and context_token:
        tokens[from_user_id] = context_token
        save_context_tokens(state_dir, account["account_id"], tokens)
    else:
        context_token = tokens.get(from_user_id, "")

    log(f"inbound account={account['account_id']} from={from_user_id} text={text[:120]}")
    branch_started = time.perf_counter()
    result = call_branchwhisper(branchwhisper_url, integration_id, account["account_id"], msg, text, images=images)
    handle_branchwhisper_result(client, branchwhisper_url, integration_id, account, from_user_id, context_token, result, branch_started)


def handle_branchwhisper_result(
    client: httpx.Client,
    branchwhisper_url: str,
    integration_id: str,
    account: dict,
    from_user_id: str,
    context_token: str,
    result: dict,
    branch_started: float,
) -> None:
    branch_ms = int((time.perf_counter() - branch_started) * 1000)
    reply = str(result.get("reply_text") or "").strip()
    reply_parts = [str(part).strip() for part in (result.get("reply_parts") or []) if str(part).strip()]
    if not reply_parts and reply:
        reply_parts = split_reply_messages(reply)
    trace_id = str(result.get("trace_id") or "")
    if reply_parts:
        send_started = time.perf_counter()
        try:
            message_ids: list[str] = []
            for index, part in enumerate(reply_parts):
                message_ids.append(send_text(client, account, from_user_id, part, context_token=context_token))
                if index < len(reply_parts) - 1:
                    time.sleep(0.18)
            send_ms = int((time.perf_counter() - send_started) * 1000)
            timings = result.get("timings") if isinstance(result.get("timings"), dict) else {}
            report_branchwhisper_timing(
                branchwhisper_url,
                integration_id,
                trace_id,
                {"send_ms": send_ms, "branch_ms": branch_ms, "text_parts": len(reply_parts)},
            )
            log(
                f"sent text account={account['account_id']} to={from_user_id} parts={len(reply_parts)} "
                f"client_ids={','.join(message_ids)} branch_ms={branch_ms} send_ms={send_ms} timings={timings}"
            )
        except Exception as exc:
            log(f"send text failed account={account['account_id']} to={from_user_id} err={exc}")
    voice_sent = send_voice_reply(
        branchwhisper_url=branchwhisper_url,
        integration_id=integration_id,
        trace_id=trace_id,
        account=account,
        to_user_id=from_user_id,
        context_token=context_token,
        result=result,
    )
    voice_notice = "" if voice_sent else voice_fallback_text(result)
    if voice_notice:
        try:
            notice_id = send_text(client, account, from_user_id, voice_notice, context_token=context_token)
            log(f"sent voice fallback notice account={account['account_id']} to={from_user_id} client_id={notice_id}")
        except Exception as exc:
            log(f"voice fallback notice failed account={account['account_id']} to={from_user_id} err={exc}")
    if result.get("send_voice") and result.get("voice_file") and not voice_sent:
        log(
            "voice reply generated but media send failed: "
            f"{result.get('voice_file')} error={result.get('voice_error') or '-'}"
        )
    send_sticker_replies(
        branchwhisper_url=branchwhisper_url,
        integration_id=integration_id,
        trace_id=trace_id,
        account=account,
        to_user_id=from_user_id,
        context_token=context_token,
        result=result,
    )


def process_message(
    client: httpx.Client,
    state_dir: Path,
    branchwhisper_url: str,
    integration_id: str,
    account: dict,
    msg: dict,
    seen: set[str],
    pending_images: dict[str, dict] | None = None,
) -> None:
    if msg.get("message_type") == MESSAGE_TYPE_BOT:
        return
    account["state_dir"] = str(state_dir)
    text = body_from_items(msg.get("item_list"))
    images = download_inbound_images(state_dir, account, msg)
    if not text:
        if any(item.get("ok") for item in images):
            text = "[图片]"
        else:
            log(f"skip non-text message account={account['account_id']} from={msg.get('from_user_id')}")
            return
    from_user_id = str(msg.get("from_user_id") or "")
    pending_key = f"{account['account_id']}:{from_user_id}"
    if pending_images is not None and images and text == "[图片]":
        pending_images[pending_key] = {"created_at": time.monotonic(), "account": account, "msg": msg, "text": text, "images": images}
        log(f"delayed image-only message account={account['account_id']} from={from_user_id} wait_sec={IMAGE_FOLLOWUP_WAIT_SEC:g}")
        return
    if pending_images is not None and text and text != "[图片]" and pending_key in pending_images:
        pending = pending_images.pop(pending_key)
        images = [*(pending.get("images") or []), *images]
        text = f"{pending.get('text') or '[图片]'}\n{text}".strip()
        log(f"merged image follow-up account={account['account_id']} from={from_user_id} text={text[:120]}")
    dispatch_message(client, state_dir, branchwhisper_url, integration_id, account, msg, text, images, seen)


def flush_pending_images(
    client: httpx.Client,
    state_dir: Path,
    branchwhisper_url: str,
    integration_id: str,
    pending_images: dict[str, dict],
    seen: set[str],
) -> None:
    now = time.monotonic()
    expired = [key for key, item in pending_images.items() if now - float(item.get("created_at") or now) >= IMAGE_FOLLOWUP_WAIT_SEC]
    for key in expired:
        item = pending_images.pop(key, None)
        if not item:
            continue
        account = item["account"]
        log(f"flushing delayed image-only message account={account['account_id']} key={key}")
        dispatch_message(client, state_dir, branchwhisper_url, integration_id, account, item["msg"], item.get("text") or "[图片]", item.get("images") or [], seen)

def choose_accounts(state_dir: Path, account_id: str = "") -> list[dict]:
    account_ids = [account_id] if account_id else list_account_ids(state_dir)
    accounts = [load_account(state_dir, item) for item in account_ids]
    return [item for item in accounts if item.get("token")]


def main() -> int:
    parser = argparse.ArgumentParser(description="BranchWhisper OpenClaw Weixin bridge")
    parser.add_argument("--integration-id", required=True)
    parser.add_argument("--profile", default="branchwhisper")
    parser.add_argument("--state-dir", default="")
    parser.add_argument("--account-id", default="")
    parser.add_argument("--branchwhisper-url", "--buding-url", dest="branchwhisper_url", default="http://127.0.0.1:7860")
    parser.add_argument("--poll-timeout-ms", type=int, default=DEFAULT_POLL_TIMEOUT_MS)
    parser.add_argument("--retry-delay-sec", type=float, default=3.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--test-text", default="")
    args = parser.parse_args()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    state_dir = resolve_state_dir(args.profile, args.state_dir)
    log(
        f"bridge starting integration={args.integration_id} profile={args.profile} "
        f"state_dir={state_dir} branchwhisper_url={args.branchwhisper_url.rstrip('/')}"
    )
    accounts = choose_accounts(state_dir, args.account_id)
    if not accounts:
        log(
            "no logged-in openclaw-weixin account found. "
            "Run: openclaw --profile <profile> channels login --channel openclaw-weixin"
        )
        return 2
    log(f"loaded accounts: {', '.join(item['account_id'] for item in accounts)}")

    if args.test_text:
        fake = {
            "from_user_id": "bridge_test@im.wechat",
            "session_id": "bridge_test",
            "context_token": "",
            "message_id": int(time.time()),
            "item_list": [{"type": ITEM_TEXT, "text_item": {"text": args.test_text}}],
        }
        result = call_branchwhisper(args.branchwhisper_url, args.integration_id, accounts[0]["account_id"], fake, args.test_text)
        log(json.dumps(result, ensure_ascii=False))
        if args.once:
            return 0

    sync_bufs = {account["account_id"]: load_sync_buf(state_dir, account["account_id"]) for account in accounts}
    seen: set[str] = set()
    pending_images: dict[str, dict] = {}
    with httpx.Client() as client:
        for account in accounts:
            notify_start(client, account)
        try:
            while not STOP:
                flush_pending_images(client, state_dir, args.branchwhisper_url, args.integration_id, pending_images, seen)
                for account in accounts:
                    if STOP:
                        break
                    account_id = account["account_id"]
                    try:
                        resp = get_updates(client, account, sync_bufs.get(account_id, ""), args.poll_timeout_ms)
                    except httpx.TimeoutException:
                        continue
                    except Exception as exc:
                        log(f"getUpdates error account={account_id}: {exc}")
                        time.sleep(max(0.5, args.retry_delay_sec))
                        continue

                    if resp.get("longpolling_timeout_ms"):
                        args.poll_timeout_ms = int(resp["longpolling_timeout_ms"])
                    is_error = (resp.get("ret") not in (None, 0)) or (resp.get("errcode") not in (None, 0))
                    if is_error:
                        log(
                            f"getUpdates failed account={account_id} ret={resp.get('ret')} "
                            f"errcode={resp.get('errcode')} errmsg={resp.get('errmsg')}"
                        )
                        if resp.get("ret") == SESSION_EXPIRED or resp.get("errcode") == SESSION_EXPIRED:
                            time.sleep(60)
                        else:
                            time.sleep(max(0.5, args.retry_delay_sec))
                        continue

                    new_buf = resp.get("get_updates_buf")
                    if isinstance(new_buf, str) and new_buf:
                        sync_bufs[account_id] = new_buf
                        save_sync_buf(state_dir, account_id, new_buf)
                    for msg in resp.get("msgs") or []:
                        if isinstance(msg, dict):
                            try:
                                process_message(
                                    client,
                                    state_dir,
                                    args.branchwhisper_url,
                                    args.integration_id,
                                    account,
                                    msg,
                                    seen,
                                    pending_images,
                                )
                            except Exception as exc:
                                log(
                                    f"message processing error account={account_id} "
                                    f"message_id={msg.get('message_id')} err={exc}"
                                )
                    flush_pending_images(client, state_dir, args.branchwhisper_url, args.integration_id, pending_images, seen)
                if args.once:
                    break
        finally:
            flush_pending_images(client, state_dir, args.branchwhisper_url, args.integration_id, pending_images, seen)
            for account in accounts:
                notify_stop(client, account)
    log("bridge stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
