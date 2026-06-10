from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


SCRIPT_PATH = Path(__file__).with_name("weixin_voice_sender.mjs")


class WeixinVoiceSendError(RuntimeError):
    pass


def send_weixin_voice(
    *,
    base_url: str,
    token: str,
    to_user_id: str,
    voice_file: str,
    text: str = "",
    context_token: str = "",
    cdn_base_url: str = "",
    timeout: float = 90.0,
) -> dict:
    command = [
        "node",
        str(SCRIPT_PATH),
        "--base-url",
        base_url,
        "--token",
        token,
        "--to",
        to_user_id,
        "--voice-file",
        voice_file,
    ]
    if context_token:
        command.extend(["--context-token", context_token])
    if cdn_base_url:
        command.extend(["--cdn-base-url", cdn_base_url])
    if text:
        command.extend(["--text", text])
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise WeixinVoiceSendError("node is not available in PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise WeixinVoiceSendError("voice sender timed out") from exc

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    try:
        payload = json.loads(stdout) if stdout else {}
    except json.JSONDecodeError as exc:
        detail = stderr or stdout or f"exit {proc.returncode}"
        raise WeixinVoiceSendError(f"voice sender returned invalid JSON: {detail[:240]}") from exc

    if proc.returncode != 0 or not payload.get("ok"):
        detail = str(payload.get("error") or stderr or f"exit {proc.returncode}")
        stage = str(payload.get("stage") or "unknown")
        raise WeixinVoiceSendError(f"{stage}: {detail[:220]}")
    return payload


def self_test_weixin_voice_sender() -> dict:
    proc = subprocess.run(
        ["node", str(SCRIPT_PATH), "--self-test"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    try:
        payload = json.loads((proc.stdout or "").strip() or "{}")
    except json.JSONDecodeError:
        payload = {"ok": False, "error": (proc.stderr or proc.stdout or "").strip()}
    payload.setdefault("exit_code", proc.returncode)
    payload.setdefault("python", sys.executable)
    return payload
