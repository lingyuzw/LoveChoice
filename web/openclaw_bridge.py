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


DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
DEFAULT_POLL_TIMEOUT_MS = 35_000
OPENCLAW_WEIXIN_VERSION = "2.4.4"
ILINK_APP_ID = "bot"
MESSAGE_TYPE_USER = 1
MESSAGE_TYPE_BOT = 2
MESSAGE_STATE_FINISH = 2
ITEM_TEXT = 1
ITEM_VOICE = 3
SESSION_EXPIRED = -14


STOP = False


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
                "user_id": str(data.get("userId") or data.get("user_id") or ""),
                "path": str(path),
            }
    return {"account_id": account_id, "token": "", "base_url": DEFAULT_BASE_URL, "user_id": "", "path": ""}


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
    return ""


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


def call_branchwhisper(branchwhisper_url: str, integration_id: str, account_id: str, msg: dict, text: str) -> dict:
    from_user_id = msg.get("from_user_id") or ""
    session_id = msg.get("session_id") or msg.get("group_id") or from_user_id or "default"
    payload = {
        "platform_id": integration_id,
        "session_id": str(session_id),
        "sender_id": str(from_user_id),
        "text": text,
        "metadata": {
            "account_id": account_id,
            "channel": "openclaw-weixin",
            "message_id": msg.get("message_id"),
            "client_id": msg.get("client_id"),
            "context_token": msg.get("context_token") or "",
            "create_time_ms": msg.get("create_time_ms"),
            "display_name": msg.get("display_name") or msg.get("nickname") or msg.get("nick_name") or "",
            "avatar_url": msg.get("avatar_url") or msg.get("head_img_url") or msg.get("portrait") or "",
        },
    }
    with httpx.Client(timeout=120) as client:
        resp = client.post(f"{branchwhisper_url.rstrip('/')}/api/integrations/dialog", json=payload)
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


def process_message(
    client: httpx.Client,
    state_dir: Path,
    branchwhisper_url: str,
    integration_id: str,
    account: dict,
    msg: dict,
    seen: set[str],
) -> None:
    if msg.get("message_type") == MESSAGE_TYPE_BOT:
        return
    text = body_from_items(msg.get("item_list"))
    if not text:
        log(f"skip non-text message account={account['account_id']} from={msg.get('from_user_id')}")
        return
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
    result = call_branchwhisper(branchwhisper_url, integration_id, account["account_id"], msg, text)
    branch_ms = int((time.perf_counter() - branch_started) * 1000)
    reply = str(result.get("reply_text") or "").strip()
    trace_id = str(result.get("trace_id") or "")
    if reply:
        send_started = time.perf_counter()
        try:
            message_id = send_text(client, account, from_user_id, reply, context_token=context_token)
            send_ms = int((time.perf_counter() - send_started) * 1000)
            total_ms = int((time.perf_counter() - branch_started) * 1000)
            report_branchwhisper_timing(
                branchwhisper_url,
                integration_id,
                trace_id,
                {"dialog_ms": branch_ms, "send_ms": send_ms, "bridge_ms": total_ms, "send_status": "sent"},
            )
            log(
                f"sent text account={account['account_id']} to={from_user_id} client_id={message_id} "
                f"branch_ms={branch_ms} send_ms={send_ms} timings={result.get('timings') or {}}"
            )
        except Exception as exc:
            send_ms = int((time.perf_counter() - send_started) * 1000)
            report_branchwhisper_timing(
                branchwhisper_url,
                integration_id,
                trace_id,
                {"dialog_ms": branch_ms, "send_ms": send_ms, "send_status": "failed", "send_error": str(exc)[:240]},
            )
            raise
    if result.get("send_voice") and result.get("voice_file"):
        log(
            "voice reply generated but not sent as media yet: "
            f"{result.get('voice_file')} (requires Weixin CDN upload support)"
        )


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
    log(f"bridge starting integration={args.integration_id} profile={args.profile} state_dir={state_dir}")
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
    with httpx.Client() as client:
        for account in accounts:
            notify_start(client, account)
        try:
            while not STOP:
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
                                )
                            except Exception as exc:
                                log(
                                    f"message processing error account={account_id} "
                                    f"message_id={msg.get('message_id')} err={exc}"
                                )
                if args.once:
                    break
        finally:
            for account in accounts:
                notify_stop(client, account)
    log("bridge stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
