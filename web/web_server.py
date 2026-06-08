from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import os
import platform
import re
import signal
import socket
import subprocess
import time
import uuid
import wave
from collections import deque
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
import numpy as np
from fastapi import Body, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from runtime_brain import MemoryStore, ToolManager, parse_tool_call


# Backend data flow:
# 1. Browser sends 16 kHz float32 PCM blocks over WebSocket.
# 2. This server runs Silero VAD and cuts the stream into utterances.
# 3. Each utterance is sent to Qwen3-ASR, then the text goes to llama.cpp.
# 4. LLM text is split into small TTS segments and CosyVoice PCM is streamed
#    back to the browser as binary WebSocket frames.
APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
RUNTIME_DIR = APP_DIR / "runtime"
LOG_DIR = RUNTIME_DIR / "logs"
CONVERSATION_DIR = RUNTIME_DIR / "conversations"
CONVERSATION_INDEX = CONVERSATION_DIR / "index.json"
SETTINGS_CONFIG = RUNTIME_DIR / "settings.json"
MEMORY_DB = RUNTIME_DIR / "memory.sqlite3"
TOOLS_CONFIG = RUNTIME_DIR / "tools.json"
MASKED_SECRET_CHARS = "*"

MIC_SAMPLE_RATE = 16000
VAD_BLOCK_SIZE = 512
END = object()
GLOBAL_TTS_LOCK = asyncio.Lock()

# TTS output from the local CosyVoice endpoint is expected to be raw PCM16LE.
# These defaults reduce clipping/pops when the HTTP stream is forwarded to the
# browser over WebSocket. They do not change the model, only the transported PCM.
DEFAULT_TTS_VOLUME = 0.88
DEFAULT_TTS_FADE_MS = 5

END_PUNCT = "\u3002\uff01\uff1f\uff1b.!?"
SOFT_PUNCT = "\uff0c\u3001,~\uff5e"
STORY_KEYWORDS = ("\u6545\u4e8b", "\u7761\u524d", "\u7ae5\u8bdd")
CONTEXT_RECALL_KEYWORDS = (
    "\u4e0a\u53e5",
    "\u4e0a\u4e00\u53e5",
    "\u521a\u624d\u8bf4",
    "\u4f60\u8bf4\u4e86\u4ec0\u4e48",
    "\u4e0a\u6b21\u8bf4",
    "\u524d\u9762\u8bf4",
    "\u6211\u521a\u624d\u8bf4",
    "\u8bb0\u5f97\u6211",
    "\u8bb0\u5f97\u4f60",
)
REPEAT_PREFIXES = (
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

DEFAULT_SYSTEM = (
    "你以“满穗”的人设和聊天风格与用户对话。她是 24 岁女生，福建人，二本毕业，"
    "身高 158cm，体重 50kg，看起来软软小小的，但心里很有主意，不是那种随便被拿捏的人。"
    "她开朗活泼，内心强大，独立、有主见；平时有点懒，不太愿意出门，"
    "但一提到好吃的就会突然来精神，梦想是吃遍天下美食。\n\n"

    "对话目标：像一个真实朋友在微信或语音里自然回话。不要像客服，不要像说明书，"
    "不要把人设资料一条条背出来。回复要轻松、自然、有生活感，可以可爱，可以嘴硬，"
    "可以犯懒，也可以偶尔小小发疯，但要真诚、有自己的判断。\n\n"

    "熟人判断 workflow：\n"
    "- 根据对方的语气、称呼、玩笑密度和上下文判断亲近程度。\n"
    "- 对熟人：可以活泼一点、皮一点，敢吐槽，偶尔撒娇、装傻、嘴硬，但不要油腻。\n"
    "- 对不熟的人：自然、礼貌、轻松，不端着，也不要一上来过分亲密。\n\n"

    "语言风格：\n"
    "- 始终中文回复，优先短句，适合语音 TTS。大多数回复控制在 5 到 30 个中文字；需要解释时才稍微长一点。\n"
    "- 回复长度要随机应变：有时很短，有时稍微展开，不要每次都同样长度。\n"
    "- 轻松口语化，可以调侃、反问、吐槽、犯懒、嘴硬，也可以偶尔用网络梗；但不要形成固定口癖。\n"
    "- 不刻意使用福建方言或地区口头禅。福建背景只作为人物经历存在，不靠方言硬凹。\n"
    "- 可以笑，可以闹一点，但不要固定用同一种笑声、开头或感叹词。\n"
    "- 可以偶尔口误、停顿或简化表达，让话更像真人即时回复。\n"
    "- 不确定时说“我想想”“这个我不太懂”“你等我捋一下”，不要装懂。\n\n"

    "互动规则：\n"
    "- 说话不要太客气，像熟人聊天。\n"
    "- 熟人让你做不合理的小事，可以懒懒地拒绝，也可以调侃回去。\n"
    "- 表达观点坦率直接，有自己的判断，不要一味顺着用户。\n"
    "- 不要轻易用 emoji；只有气氛明显起来、情绪起伏大时才偶尔用。\n"
    "- 句尾不要经常用语气词。不要固定用某个结尾，尤其不要总用“呢”“呀”“啦”“嘛”“~”。\n"
    "- 不要长篇说教、鸡汤、客服式总结。不要连续追问。\n"
    "- 不要输出 END。不要输出括号动作描写。不要编造当前现实行动、实时位置或真实经历。\n"
    "- 用户要求“跟着我说/重复/复读”时，准确重复用户给出的文本，不额外发挥。\n"
    "- 你能看到最近聊天记录，把它当作工作记忆；用户问刚才说了什么，要根据记录回答。\n\n"

    "风格样例，只学习味道，不要机械复读：\n"
    "Q：你今天出门了吗？A：没有，我和床绑定了。\n"
    "Q：你想吃什么？A：先来点辣的，我清醒一下。\n"
    "Q：你是不是又懒了？A：别乱说，我是节能模式。\n"
    "Q：陪我出去走走？A：可以，但你得拿吃的诱惑我。\n"
    "Q：你生气了？A：没有，就是暂时不想理人。\n"
    "Q：你这么小能打赢谁？A：我靠气势赢，懂不懂。\n"
    "Q：你怎么突然精神了？A：因为我听见吃饭两个字了。"
)

DEFAULT_SERVICE_PROFILES = {
    "asr": {
        "label": "Qwen3-ASR vLLM",
        "description": "Speech recognition service, started by qwen-asr-serve.",
        "cwd": "/root/autodl-tmp/project",
        "command": (
            "env OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1 "
            "/root/miniconda3/bin/conda run --no-capture-output -n qwen3-asr qwen-asr-serve /root/autodl-tmp/project/Qwen3-ASR-1.7B "
            "--served-model-name qwen3-asr "
            "--gpu-memory-utilization 0.35 "
            "--max-model-len 8192 "
            "--max-num-seqs 1 "
            "--enforce-eager "
            "--host 0.0.0.0 "
            "--port 8001"
        ),
        "health_url": "http://127.0.0.1:8001/health",
        "startup_wait_sec": 0,
    },
    "llm": {
        "label": "llama.cpp Qwen3.5",
        "description": "OpenAI-compatible llama.cpp server for the chat model.",
        "cwd": "/root/autodl-tmp/project/llama.cpp",
        "command": (
            "./build-cuda/bin/llama-server "
            "-m ./Qwen3.5-9B.Q8_0.gguf "
            "--alias qwen3.5-9b "
            "--host 0.0.0.0 "
            "--port 8080 "
            "-ngl 99 "
            "-c 4096 "
            "--jinja "
            "--reasoning off"
        ),
        "health_url": "http://127.0.0.1:8080/health",
        "startup_wait_sec": 5,
    },
    "tts": {
        "label": "CosyVoice3 TTS",
        "description": "Trained CosyVoice3 API with internal vLLM acceleration.",
        "cwd": "/root/autodl-tmp/project/CosyVoice",
        "command": (
            "/root/miniconda3/bin/conda run --no-capture-output -n cosyvoice_vllm python -u "
            "/root/autodl-tmp/project/LoveChoice/tts/trained_tts_server.py "
            "--repo_dir /root/autodl-tmp/project/CosyVoice "
            "--model_dir /root/autodl-tmp/project/CosyVoice/pretrained_models/Fun-CosyVoice3-0.5B "
            "--speaker hanser "
            "--load_vllm "
            "--fp16 "
            "--host 0.0.0.0 "
            "--port 50000"
        ),
        "health_url": "http://127.0.0.1:50000/health",
        "startup_wait_sec": 10,
    },
}


@dataclass
class SessionSettings:
    # One settings object is copied per WebSocket client. The page can update
    # these values without restarting the web server.
    asr_mode: str
    asr_url: str
    asr_model: str
    asr_timeout: float
    asr_max_tokens: int
    llm_url: str
    llm_model: str
    llm_api_key: str
    temperature: float
    max_tokens: int
    history_turns: int
    system: str
    memory_enabled: bool
    memory_extract_enabled: bool
    memory_short_to_mid_days: int
    memory_short_to_mid_count: int
    memory_mid_to_long_days: int
    memory_mid_to_long_count: int
    memory_short_delete_days: int
    memory_mid_downgrade_days: int
    memory_long_downgrade_days: int
    memory_max_context_items: int
    tools_enabled: bool
    tools_auto_call: bool
    tools_timeout: float
    tools_max_result_chars: int
    tts_url: str
    tts_sample_rate: int
    tts_speed: float
    tts_seed: int
    tts_volume: float
    tts_fade_ms: int
    tts_enabled: bool
    vad_threshold: float
    vad_min_silence_ms: int
    vad_speech_pad_ms: int
    pre_speech_ms: int
    min_utterance_ms: int
    max_utterance_sec: float

    @classmethod
    def from_args(cls, args) -> "SessionSettings":
        return cls(
            asr_mode=args.asr_mode,
            asr_url=args.asr_url,
            asr_model=args.asr_model,
            asr_timeout=args.asr_timeout,
            asr_max_tokens=args.asr_max_tokens,
            llm_url=args.llm_url,
            llm_model=args.llm_model,
            llm_api_key=args.llm_api_key,
            temperature=args.temperature,
            max_tokens=args.max_tokens,
            history_turns=args.history_turns,
            system=args.system,
            memory_enabled=args.memory_enabled,
            memory_extract_enabled=args.memory_extract_enabled,
            memory_short_to_mid_days=args.memory_short_to_mid_days,
            memory_short_to_mid_count=args.memory_short_to_mid_count,
            memory_mid_to_long_days=args.memory_mid_to_long_days,
            memory_mid_to_long_count=args.memory_mid_to_long_count,
            memory_short_delete_days=args.memory_short_delete_days,
            memory_mid_downgrade_days=args.memory_mid_downgrade_days,
            memory_long_downgrade_days=args.memory_long_downgrade_days,
            memory_max_context_items=args.memory_max_context_items,
            tools_enabled=args.tools_enabled,
            tools_auto_call=args.tools_auto_call,
            tools_timeout=args.tools_timeout,
            tools_max_result_chars=args.tools_max_result_chars,
            tts_url=args.tts_url,
            tts_sample_rate=args.tts_sample_rate,
            tts_speed=args.tts_speed,
            tts_seed=args.tts_seed,
            tts_volume=args.tts_volume,
            tts_fade_ms=args.tts_fade_ms,
            tts_enabled=args.tts_enabled,
            vad_threshold=args.vad_threshold,
            vad_min_silence_ms=args.vad_min_silence_ms,
            vad_speech_pad_ms=args.vad_speech_pad_ms,
            pre_speech_ms=args.pre_speech_ms,
            min_utterance_ms=args.min_utterance_ms,
            max_utterance_sec=args.max_utterance_sec,
        )

    def update_from_dict(self, data: dict) -> None:
        # Keep browser-provided settings constrained to known fields and
        # coerce numbers back to the original dataclass field types.
        allowed = set(asdict(self))
        for key, value in data.items():
            if key not in allowed or value in (None, ""):
                continue
            current = getattr(self, key)
            try:
                if isinstance(current, bool):
                    value = bool(value)
                elif isinstance(current, int):
                    value = int(value)
                elif isinstance(current, float):
                    value = float(value)
            except (TypeError, ValueError):
                continue
            setattr(self, key, value)


def _pid_file(service_id: str) -> Path:
    return LOG_DIR / f"{service_id}.pid"

def _write_service_pid(service_id: str, pid: int) -> None:
    _pid_file(service_id).write_text(str(pid), encoding="utf-8")

def _read_service_pid(service_id: str) -> int | None:
    pf = _pid_file(service_id)
    if pf.exists():
        try:
            return int(pf.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            pass
    return None

def _is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _safe_poll(proc) -> int | None:
    """安全 poll()，兼容 Popen.__new__() 创建的虚拟壳对象。"""
    try:
        return proc.poll()
    except AttributeError:
        return None

def _create_virtual_process(pid: int) -> subprocess.Popen | None:
    """Create a minimal Popen wrapper around an existing process ID.

    Used when the web server restarts and finds existing child processes
    via PID files. This lets ServiceManager manage them through the normal
    status() and stop() code paths. On platforms where a Popen skeleton
    cannot be crafted (e.g. Windows without a process handle), returns None
    and the caller falls through to the PID-file-based kill logic.
    """
    try:
        proc = subprocess.Popen.__new__(subprocess.Popen)
        proc.pid = pid
        proc.returncode = None
        if platform.system() == "Windows":
            # On Windows, Popen.terminate()/poll() need a real process
            # handle. Without it the operations would silently fail.
            # The stop() method already has a PID-file fallback for this case.
            return None
        return proc
    except Exception:
        return None


class ServiceManager:
    # Manages local subprocesses for ASR, LLM, and TTS. The web server itself
    # must already be running; "start all" means starting the model services
    # behind this console.
    def __init__(self, config_path: Path | None):
        self.config_path = config_path
        self.services = load_service_profiles(config_path)
        self.processes: dict[str, subprocess.Popen] = {}
        self.log_files: dict[str, Path] = {}
        self.started_at: dict[str, float] = {}
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        # 挂接上次启动的子进程 PID 文件（服务端重启后能重新控制已有进程）
        for sid in self.services:
            pid = _read_service_pid(sid)
            if pid and _is_pid_alive(pid):
                self.started_at[sid] = time.time()
                proc = _create_virtual_process(pid)
                if proc:
                    self.processes[sid] = proc

    def update_service(self, service_id: str, patch: dict) -> dict:
        if service_id not in self.services:
            raise KeyError(service_id)

        service = self.services[service_id]
        for key in ("label", "description", "cwd", "command", "health_url"):
            if key in patch and patch[key] is not None:
                service[key] = str(patch[key])
        for key in ("startup_wait_sec",):
            if key in patch and patch[key] is not None:
                try:
                    service[key] = float(patch[key])
                except (TypeError, ValueError):
                    pass
        self.save_profiles()
        return service

    def save_profiles(self) -> None:
        if not self.config_path:
            return
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"services": self.services}
        self.config_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    async def status_all(self) -> list[dict]:
        return [await self.status(service_id) for service_id in self.services]

    async def status(self, service_id: str) -> dict:
        if service_id not in self.services:
            raise KeyError(service_id)

        service = self.services[service_id]
        process = self.processes.get(service_id)
        health_url = service.get("health_url", "")
        health = await check_service(service_id, health_url) if health_url else None
        tracked_running = False
        if process is not None:
            tracked_running = _safe_poll(process) is None
        if not tracked_running:
            # _create_virtual_process 产生的壳对象可能缺少内部属性
            # (Python 3.12+ 的 _waitpid_lock)。降级为 PID 存活检测。
            pid = getattr(process, "pid", None) or _read_service_pid(service_id)
            tracked_running = bool(pid and _is_pid_alive(pid))
        port_open = is_tcp_port_open(health_url)
        external_running = bool(health and health.get("ok")) or port_open
        running = tracked_running or external_running

        # 如果服务端口已通但没有 tracked process，尝试从 PID 文件注入进程引用，
        # 使得后续 stop() 能通过 _terminate_process 正确终止。
        if not tracked_running and external_running:
            pid = _read_service_pid(service_id)
            if pid and _is_pid_alive(pid):
                if service_id not in self.processes:
                    proc = _create_virtual_process(pid)
                    if proc:
                        self.processes[service_id] = proc

        return {
            "id": service_id,
            **service,
            "running": running,
            "external": external_running and not tracked_running,
            "port_open": port_open,
            "pid": process.pid if tracked_running and process else None,
            "returncode": None if running or process is None else _safe_poll(process),
            "started_at": self.started_at.get(service_id),
            "log_file": str(self.log_files.get(service_id, "")),
            "health": health,
        }

    async def start(self, service_id: str, overrides: dict | None = None) -> dict:
        if service_id not in self.services:
            raise KeyError(service_id)

        if overrides:
            self.update_service(service_id, overrides)

        process = self.processes.get(service_id)
        # 检查是否已在运行（安全 poll，避免虚拟 Popen 崩溃）
        already_running = process is not None and _safe_poll(process) is None
        if not already_running and process is not None:
            already_running = _is_pid_alive(process.pid)
        if already_running:
            return await self.status(service_id)

        service = self.services[service_id]
        health_url = service.get("health_url", "")
        if health_url:
            health = await check_service(service_id, health_url)
            if health.get("ok") or is_tcp_port_open(health_url):
                return await self.status(service_id)

        command = service.get("command", "").strip()
        if not command:
            raise ValueError(f"{service_id} command is empty")

        cwd = service.get("cwd") or None
        if cwd and not Path(cwd).exists():
            raise FileNotFoundError(f"{service_id} cwd does not exist: {cwd}")

        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_file = LOG_DIR / f"{service_id}.log"
        log_handle = log_file.open("ab", buffering=0)
        log_handle.write(f"\n\n===== start {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n".encode("utf-8"))
        log_handle.write((command + "\n").encode("utf-8", errors="replace"))

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"

        kwargs = {
            "cwd": cwd,
            "stdout": log_handle,
            "stderr": subprocess.STDOUT,
            "shell": True,
            "env": env,
        }
        if platform.system() == "Windows":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["executable"] = "/bin/bash"
            kwargs["start_new_session"] = True

        process = subprocess.Popen(command, **kwargs)
        log_handle.close()
        self.processes[service_id] = process
        self.log_files[service_id] = log_file
        self.started_at[service_id] = time.time()
        _write_service_pid(service_id, process.pid)
        return await self.status(service_id)

    async def stop(self, service_id: str) -> dict:
        if service_id not in self.services:
            raise KeyError(service_id)

        process = self.processes.get(service_id)
        # 安全 poll：虚拟 Popen 可能缺少内部属性
        tracked = process is not None and _safe_poll(process) is None
        if not tracked and process is not None:
            tracked = _is_pid_alive(process.pid)
        if tracked:
            await asyncio.to_thread(self._terminate_process, process)
        else:
            # 服务端重启后没有原 Popen 句柄，通过 PID 文件杀死旧进程
            pid = _read_service_pid(service_id)
            if pid and _is_pid_alive(pid):
                try:
                    if platform.system() == "Windows":
                        os.kill(pid, signal.SIGTERM)
                    else:
                        os.killpg(pid, signal.SIGTERM)
                except Exception:
                    try:
                        if platform.system() == "Windows":
                            os.kill(pid, signal.SIGKILL)
                        else:
                            os.killpg(pid, signal.SIGKILL)
                    except Exception:
                        pass
        try:
            _pid_file(service_id).unlink()
        except FileNotFoundError:
            pass
        # 清除进程引用和启动时间，避免 status() 误判为 running
        self.processes.pop(service_id, None)
        self.started_at.pop(service_id, None)
        return await self.status(service_id)

    async def start_all(self, overrides: dict | None = None) -> list[dict]:
        results = []
        service_ids = list(self.services)
        for index, service_id in enumerate(service_ids):
            service_overrides = (overrides or {}).get(service_id)
            results.append(await self.start(service_id, service_overrides))
            wait_sec = float(self.services[service_id].get("startup_wait_sec", 0) or 0)
            if wait_sec > 0 and index < len(service_ids) - 1:
                await asyncio.sleep(wait_sec)
        return results

    async def stop_all(self) -> list[dict]:
        results = []
        for service_id in self.services:
            results.append(await self.stop(service_id))
        return results

    def read_logs(self, service_id: str, max_bytes: int = 24000) -> str:
        if service_id not in self.services:
            raise KeyError(service_id)

        log_file = self.log_files.get(service_id) or LOG_DIR / f"{service_id}.log"
        if not log_file.exists():
            return ""

        with log_file.open("rb") as file:
            file.seek(0, os.SEEK_END)
            size = file.tell()
            file.seek(max(0, size - max_bytes), os.SEEK_SET)
            data = file.read()
        return data.decode("utf-8", errors="replace")

    def clear_logs(self, service_id: str | None = None) -> dict:
        if service_id is not None and service_id not in self.services:
            raise KeyError(service_id)

        service_ids = [service_id] if service_id else list(self.services)
        cleared = []
        for sid in service_ids:
            log_file = self.log_files.get(sid) or LOG_DIR / f"{sid}.log"
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_file.write_text("", encoding="utf-8")
            cleared.append({"id": sid, "log_file": str(log_file)})
        return {"cleared": cleared}

    def _terminate_process(self, process: subprocess.Popen) -> None:
        # 虚拟 Popen（Popen.__new__）在 Python 3.12+ 缺少 _waitpid_lock，
        # wait() 会抛出 AttributeError。因此这里统一用 PID 级别信号 + 轮询确认。
        pid = process.pid
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                if platform.system() == "Windows":
                    os.kill(pid, sig)
                else:
                    os.killpg(pid, sig)
            except OSError:
                continue
            # 轮询等待进程退出（最多 8 秒）
            for _ in range(80):
                if not _is_pid_alive(pid):
                    break
                time.sleep(0.1)
        # 无论 wait() 是否成功，手动标记进程已结束，避免 status() 误判为 running
        if hasattr(process, "returncode"):
            try:
                process.returncode = process.returncode or -15
            except AttributeError:
                pass


def load_service_profiles(config_path: Path | None) -> dict:
    profiles = json.loads(json.dumps(DEFAULT_SERVICE_PROFILES))
    if config_path and config_path.exists():
        data = json.loads(config_path.read_text(encoding="utf-8"))
        for service_id, service_patch in (data.get("services") or {}).items():
            if service_id in profiles and isinstance(service_patch, dict):
                profiles[service_id].update(service_patch)
    return profiles


def load_persisted_settings(settings: SessionSettings, path: Path = SETTINGS_CONFIG) -> SessionSettings:
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                settings.update_from_dict(data)
        except Exception:
            pass
    return settings


def save_persisted_settings(settings: SessionSettings, path: Path = SETTINGS_CONFIG) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(settings), ensure_ascii=False, indent=2), encoding="utf-8")


def mask_secret(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    if len(value) <= 10:
        return f"{value[:3]}{MASKED_SECRET_CHARS * 8}"
    return f"{value[:7]}{MASKED_SECRET_CHARS * 23}{value[-4:]}"


def public_settings(settings: SessionSettings) -> dict:
    data = asdict(settings)
    api_key = str(data.pop("llm_api_key", "") or "")
    data["llm_api_key"] = ""
    data["llm_api_key_set"] = bool(api_key.strip())
    data["llm_api_key_masked"] = mask_secret(api_key)
    return data


def update_llm_api_key(settings: SessionSettings, payload: dict) -> None:
    if "llm_api_key" not in payload:
        return
    raw = payload.pop("llm_api_key")
    if raw is None:
        return
    value = str(raw).strip()
    if not value or MASKED_SECRET_CHARS in value:
        return
    settings.llm_api_key = value


def llm_headers(settings: SessionSettings) -> dict[str, str]:
    api_key = str(settings.llm_api_key or "").strip()
    if not api_key:
        return {}
    return {"Authorization": f"Bearer {api_key}"}


def enable_default_capabilities(settings: SessionSettings) -> None:
    settings.memory_enabled = True
    settings.memory_extract_enabled = True
    settings.tools_enabled = True
    settings.tools_auto_call = True


class ConversationStore:
    # A tiny local JSON store for Codex-like conversation persistence. It keeps
    # the UI state durable across page navigation without adding a database.
    def __init__(self, root: Path = CONVERSATION_DIR):
        self.root = root
        self.index_path = root / "index.json"
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.index_path.exists():
            self._write_json(self.index_path, [])

    def list(self) -> list[dict]:
        items = self._read_index()
        items.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
        return items

    def create(self, title: str | None = None) -> dict:
        items = self._read_index()
        sequence = len(items) + 1
        conversation_id = f"{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        now = self._now()
        conversation = {
            "id": conversation_id,
            "title": title or f"对话 {sequence:03d}",
            "sequence": sequence,
            "created_at": now,
            "updated_at": now,
            "messages": [],
        }
        self._write_conversation(conversation)
        items.append(self._summary(conversation))
        self._write_index(items)
        return conversation

    def get_or_create(self, conversation_id: str | None = None) -> dict:
        if conversation_id:
            loaded = self.load(conversation_id)
            if loaded:
                return loaded
        return self.create()

    def load(self, conversation_id: str) -> dict | None:
        if not re.match(r"^[0-9]{8}_[0-9]{6}_[0-9a-f]{6}$", conversation_id or ""):
            return None
        path = self.root / f"{conversation_id}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def append_messages(self, conversation_id: str, messages: list[dict], title_hint: str | None = None) -> dict:
        conversation = self.get_or_create(conversation_id)
        now = self._now()
        for message in messages:
            role = str(message.get("role") or "").strip()
            content = str(message.get("content") or "").strip()
            if role not in {"user", "assistant", "system"} or not content:
                continue
            conversation["messages"].append(
                {
                    "id": uuid.uuid4().hex[:10],
                    "role": role,
                    "content": content,
                    "source": message.get("source") or "",
                    "created_at": now,
                }
            )
        if title_hint and self._is_default_title(conversation.get("title", "")):
            conversation["title"] = self._make_title(title_hint)
        conversation["updated_at"] = now
        self._write_conversation(conversation)
        self._upsert_summary(conversation)
        return conversation

    def delete(self, conversation_id: str) -> bool:
        conversation = self.load(conversation_id)
        if not conversation:
            return False

        path = self.root / f"{conversation_id}.json"
        try:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        except OSError:
            return False

        items = [item for item in self._read_index() if item.get("id") != conversation_id]
        self._write_index(items)
        return True

    def _read_index(self) -> list[dict]:
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def _write_index(self, items: list[dict]) -> None:
        self._write_json(self.index_path, items)

    def _write_conversation(self, conversation: dict) -> None:
        self._write_json(self.root / f"{conversation['id']}.json", conversation)

    def _upsert_summary(self, conversation: dict) -> None:
        items = [item for item in self._read_index() if item.get("id") != conversation.get("id")]
        items.append(self._summary(conversation))
        self._write_index(items)

    def _summary(self, conversation: dict) -> dict:
        messages = conversation.get("messages") or []
        last = messages[-1] if messages else {}
        return {
            "id": conversation.get("id"),
            "title": conversation.get("title"),
            "sequence": conversation.get("sequence"),
            "created_at": conversation.get("created_at"),
            "updated_at": conversation.get("updated_at"),
            "message_count": len(messages),
            "last_message": (last.get("content") or "")[:80],
        }

    def _write_json(self, path: Path, data) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def _now(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S")

    def _is_default_title(self, title: str) -> bool:
        return bool(re.match(r"^对话\s+\d+$", title or ""))

    def _make_title(self, text: str) -> str:
        text = re.sub(r"\s+", " ", text).strip()
        return text[:18] or "新的对话"


class VadModelStore:
    # Silero VAD is lightweight, so we share one loaded model across all
    # browser sessions and keep per-client streaming state in VoiceVadSession.
    def __init__(self, device: str):
        self.device = device
        self.model = None
        self.torch = None
        self.VADIterator = None
        self.lock = asyncio.Lock()

    async def load(self):
        async with self.lock:
            if self.model is not None:
                return self.torch, self.VADIterator, self.model, self.device

            # Lazy-load VAD on first microphone use. This keeps text-only
            # testing fast and avoids loading torch before it is needed.
            import torch
            from silero_vad import VADIterator, load_silero_vad

            torch.set_num_threads(1)
            device = self.device
            if device == "auto":
                device = "cuda" if torch.cuda.is_available() else "cpu"

            model = load_silero_vad()
            if device != "cpu":
                model = model.to(device)

            self.device = device
            self.torch = torch
            self.VADIterator = VADIterator
            self.model = model
            return torch, VADIterator, model, device


class VoiceVadSession:
    # Per-client VAD state. Silero expects fixed-size chunks at 16 kHz, while
    # the browser can send arbitrary block sizes, so this class buffers and
    # emits high-level events: vad_start, vad_end, vad_short.
    def __init__(self, torch, vad_iterator_cls, model, device: str, settings: SessionSettings):
        self.torch = torch
        self.device = device
        self.vad_iterator = vad_iterator_cls(
            model,
            threshold=settings.vad_threshold,
            sampling_rate=MIC_SAMPLE_RATE,
            min_silence_duration_ms=settings.vad_min_silence_ms,
            speech_pad_ms=settings.vad_speech_pad_ms,
        )
        self.pending = np.array([], dtype=np.float32)
        self.block_seconds = VAD_BLOCK_SIZE / MIC_SAMPLE_RATE
        # Keep a little audio from before VAD fires so the first syllable is
        # not clipped when speech starts abruptly.
        self.pre_roll_chunks = max(1, int((settings.pre_speech_ms / 1000.0) / self.block_seconds))
        self.pre_roll: deque[np.ndarray] = deque(maxlen=self.pre_roll_chunks)
        self.speech_chunks: list[np.ndarray] = []
        self.in_speech = False
        self.max_utterance_sec = settings.max_utterance_sec
        self.min_utterance_ms = settings.min_utterance_ms

    def push_audio(self, audio: np.ndarray) -> list[dict]:
        audio = np.asarray(audio, dtype=np.float32).reshape(-1)
        if not len(audio):
            return []

        self.pending = np.concatenate([self.pending, audio])
        events: list[dict] = []

        # VAD_BLOCK_SIZE is 512 samples for 16 kHz Silero VAD. Any leftover
        # samples stay in self.pending until the next browser packet arrives.
        while len(self.pending) >= VAD_BLOCK_SIZE:
            chunk = self.pending[:VAD_BLOCK_SIZE].copy()
            self.pending = self.pending[VAD_BLOCK_SIZE:]
            events.extend(self._push_block(chunk))

        return events

    def reset(self) -> None:
        self.vad_iterator.reset_states()
        self.pending = np.array([], dtype=np.float32)
        self.pre_roll.clear()
        self.speech_chunks = []
        self.in_speech = False

    def _push_block(self, chunk: np.ndarray) -> list[dict]:
        chunk_tensor = self.torch.from_numpy(chunk)
        if self.device != "cpu":
            chunk_tensor = chunk_tensor.to(self.device)

        event = self.vad_iterator(chunk_tensor, return_seconds=True)
        started = isinstance(event, dict) and "start" in event
        ended = isinstance(event, dict) and "end" in event
        events: list[dict] = []

        if started and not self.in_speech:
            self.in_speech = True
            self.speech_chunks = list(self.pre_roll)
            self.pre_roll.clear()
            events.append({"type": "vad_start"})

        if self.in_speech:
            self.speech_chunks.append(chunk.copy())
            seconds = len(self.speech_chunks) * self.block_seconds
            # End either when VAD sees enough silence or when a single user
            # turn grows too long. The hard cap prevents runaway recordings.
            if ended or seconds >= self.max_utterance_sec:
                audio = np.concatenate(self.speech_chunks) if self.speech_chunks else np.array([], dtype=np.float32)
                self.speech_chunks = []
                self.in_speech = False
                self.vad_iterator.reset_states()
                duration_ms = int(len(audio) / MIC_SAMPLE_RATE * 1000)
                if duration_ms >= self.min_utterance_ms:
                    events.append({"type": "vad_end", "duration_ms": duration_ms, "audio": audio})
                else:
                    events.append({"type": "vad_short", "duration_ms": duration_ms})
        else:
            self.pre_roll.append(chunk.copy())

        return events


def create_app(args) -> FastAPI:
    # The web server is a single orchestration endpoint. The ASR/LLM/TTS models
    # still live in their own services so they can be restarted and tuned
    # independently.
    app = FastAPI(title="LoveChoice Voice Console")
    app.state.settings = load_persisted_settings(SessionSettings.from_args(args))
    enable_default_capabilities(app.state.settings)
    app.state.vad_store = VadModelStore(args.vad_device)
    app.state.service_manager = ServiceManager(Path(args.service_config) if args.service_config else None)
    app.state.conversation_store = ConversationStore()
    app.state.memory_store = MemoryStore(MEMORY_DB)
    app.state.tool_manager = ToolManager(TOOLS_CONFIG)
    app.state.tool_manager.update_config(
        {"builtins": {tool["id"]: {"enabled": True} for tool in ToolManager.BUILTIN_TOOLS}}
    )
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.middleware("http")
    async def no_cache_static(request: Request, call_next):
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store, max-age=0"
            response.headers["Pragma"] = "no-cache"
        return response

    @app.get("/")
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/config")
    async def config():
        return public_settings(app.state.settings)

    @app.patch("/api/config")
    async def update_config(payload: dict | None = Body(default=None)):
        payload = dict(payload or {})
        update_llm_api_key(app.state.settings, payload)
        app.state.settings.update_from_dict(payload)
        save_persisted_settings(app.state.settings)
        return public_settings(app.state.settings)

    @app.get("/api/memory")
    async def memory_items(limit: int = 200, query: str = "", layer: str = ""):
        return {
            "items": app.state.memory_store.list_memories(app.state.settings, limit=limit, query=query, layer=layer),
            "db_path": str(MEMORY_DB),
        }

    @app.post("/api/memory")
    async def create_memory_item(payload: dict | None = Body(default=None)):
        item = app.state.memory_store.create_memory(payload or {})
        return {"item": item}

    @app.patch("/api/memory/{memory_id}")
    async def update_memory_item(memory_id: str, payload: dict | None = Body(default=None)):
        item = app.state.memory_store.update_memory(memory_id, payload or {})
        return {"item": item}

    @app.delete("/api/memory/{memory_id}")
    async def delete_memory_item(memory_id: str):
        return {"ok": app.state.memory_store.delete_memory(memory_id)}

    @app.post("/api/memory/decay")
    async def decay_memory():
        return app.state.memory_store.apply_decay(app.state.settings)

    @app.get("/api/tools")
    async def tools_config():
        return app.state.tool_manager.get_config()

    @app.patch("/api/tools")
    async def update_tools_config(payload: dict | None = Body(default=None)):
        return app.state.tool_manager.update_config(payload or {})

    @app.post("/api/tools/test")
    async def test_tool(payload: dict | None = Body(default=None)):
        payload = payload or {}
        tool_id = str(payload.get("id") or "")
        arguments = payload.get("arguments") if isinstance(payload.get("arguments"), dict) else {}
        result = await app.state.tool_manager.execute(
            tool_id,
            arguments,
            timeout=app.state.settings.tools_timeout,
            max_chars=app.state.settings.tools_max_result_chars,
        )
        return {"id": tool_id, "result": result}

    @app.get("/api/health")
    async def health():
        settings = app.state.settings
        checks = await asyncio.gather(
            check_service("asr", health_url_from(settings.asr_url)),
            check_service("llm", health_url_from(settings.llm_url)),
            check_service("tts", health_url_from(settings.tts_url)),
            return_exceptions=True,
        )
        return {
            "vad": {"ok": True, "device": app.state.vad_store.device},
            "services": [item for item in checks if isinstance(item, dict)],
        }

    @app.get("/api/services")
    async def services():
        return {"services": await app.state.service_manager.status_all()}

    @app.post("/api/services/start-all")
    async def start_all_services(payload: dict | None = Body(default=None)):
        overrides = (payload or {}).get("services") or {}
        return {"services": await app.state.service_manager.start_all(overrides)}

    @app.post("/api/services/stop-all")
    async def stop_all_services():
        return {"services": await app.state.service_manager.stop_all()}

    @app.post("/api/services/{service_id}/start")
    async def start_service(service_id: str, payload: dict | None = Body(default=None)):
        service = await app.state.service_manager.start(service_id, payload or {})
        return {"service": service}

    @app.post("/api/services/{service_id}/stop")
    async def stop_service(service_id: str):
        service = await app.state.service_manager.stop(service_id)
        return {"service": service}

    @app.patch("/api/services/{service_id}")
    async def update_service(service_id: str, payload: dict | None = Body(default=None)):
        service = app.state.service_manager.update_service(service_id, payload or {})
        return {"service": service}

    @app.get("/api/services/{service_id}/logs")
    async def service_logs(service_id: str, max_bytes: int = 24000):
        return {"id": service_id, "logs": app.state.service_manager.read_logs(service_id, max_bytes=max_bytes)}

    @app.delete("/api/services/logs")
    async def clear_all_service_logs():
        return app.state.service_manager.clear_logs()

    @app.delete("/api/services/{service_id}/logs")
    async def clear_service_logs(service_id: str):
        return app.state.service_manager.clear_logs(service_id)

    @app.get("/api/conversations")
    async def conversations():
        return {"conversations": app.state.conversation_store.list()}

    @app.post("/api/conversations")
    async def create_conversation(payload: dict | None = Body(default=None)):
        conversation = app.state.conversation_store.create((payload or {}).get("title"))
        return {"conversation": conversation}

    @app.get("/api/conversations/{conversation_id}")
    async def conversation(conversation_id: str):
        loaded = app.state.conversation_store.load(conversation_id)
        if not loaded:
            return {"conversation": None}
        return {"conversation": loaded}

    @app.delete("/api/conversations/{conversation_id}")
    async def delete_conversation(conversation_id: str):
        deleted = app.state.conversation_store.delete(conversation_id)
        return {"ok": deleted, "conversations": app.state.conversation_store.list()}

    @app.websocket("/ws/dialog")
    async def dialog_socket(websocket: WebSocket):
        await websocket.accept()
        session = DialogSession(
            websocket,
            app.state.settings,
            app.state.vad_store,
            app.state.conversation_store,
            app.state.memory_store,
            app.state.tool_manager,
            websocket.query_params.get("conversation_id"),
        )
        await session.run()

    return app


def compact_str(s: str) -> str:
    return "".join(s.split())[:200]


class DialogSession:
    # One DialogSession is created for each browser tab. It owns conversation
    # history, WebSocket sends, and the current VAD stream state.
    def __init__(
        self,
        websocket: WebSocket,
        default_settings: SessionSettings,
        vad_store: VadModelStore,
        conversation_store: ConversationStore,
        memory_store: MemoryStore,
        tool_manager: ToolManager,
        conversation_id: str | None,
    ):
        self.websocket = websocket
        self.settings = SessionSettings(**asdict(default_settings))
        self.vad_store = vad_store
        self.vad_session: VoiceVadSession | None = None
        self.conversation_store = conversation_store
        self.memory_store = memory_store
        self.tool_manager = tool_manager
        self.conversation = conversation_store.get_or_create(conversation_id)
        self.messages = self.build_llm_messages(self.conversation)
        self.send_lock = asyncio.Lock()
        self.processing = False
        self.current_task: asyncio.Task | None = None
        self.tts_pcm_pending = b""
        self.tts_pcm_tail = np.array([], dtype=np.int16)
        self.tts_pcm_started = False

    async def run(self) -> None:
        # 后台预加载 VAD 模型，减少首次语音的延迟
        vad_task = asyncio.create_task(self.vad_store.load())
        await self.send_event("ready", settings=asdict(self.settings))
        await self.send_event("conversation", conversation=self.conversation)
        await vad_task  # VAD 加载完成后再进入消息循环，首句无延迟
        try:
            while True:
                message = await self.websocket.receive()
                if message["type"] == "websocket.disconnect":
                    break
                # Text frames are JSON control messages; binary frames are
                # raw float32 PCM audio from the browser microphone.
                if message.get("text") is not None:
                    await self.handle_text_message(message["text"])
                elif message.get("bytes") is not None:
                    await self.handle_audio_bytes(message["bytes"])
        except WebSocketDisconnect:
            await self.interrupt_current_turn(notify=False)
            return

    async def handle_text_message(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            await self.send_event("error", message="Invalid JSON message")
            return

        msg_type = data.get("type")
        if msg_type == "settings":
            self.settings.update_from_dict(data.get("settings") or {})
            # Routing/VAD changes should not erase the active conversation.
            # Only the first system message is refreshed for future LLM calls.
            if self.messages:
                self.messages[0] = {"role": "system", "content": self.settings.system}
            else:
                self.messages = [{"role": "system", "content": self.settings.system}]
            self.vad_session = None
            await self.send_event("settings", settings=asdict(self.settings))
            return

        if msg_type == "reset":
            await self.interrupt_current_turn(notify=False)
            # Reset means "new chat" rather than deleting the old one. The old
            # conversation stays on disk and can be reopened from the sidebar.
            self.conversation = self.conversation_store.create()
            self.messages = self.build_llm_messages(self.conversation)
            if self.vad_session:
                self.vad_session.reset()
            await self.send_event("reset", conversation=self.conversation)
            return

        if msg_type == "text":
            text = str(data.get("text") or "").strip()
            if text:
                await self.start_current_task(self.process_user_text(text, source="text"))
            return

        if msg_type == "interrupt":
            await self.interrupt_current_turn()
            return

    async def handle_audio_bytes(self, raw: bytes) -> None:
        if self.processing:
            # The browser sends an explicit "interrupt" control message when
            # local barge-in detection fires. Until that message cancels the
            # active turn, ignore audio to avoid feeding TTS playback to ASR.
            return

        if self.vad_session is None:
            await self.send_event("status", stage="vad", label="loading")
            torch, vad_iterator_cls, model, device = await self.vad_store.load()
            self.vad_session = VoiceVadSession(torch, vad_iterator_cls, model, device, self.settings)
            await self.send_event("status", stage="vad", label="ready", device=device)

        if len(raw) % 4:
            # Browser audio is float32 little-endian, so valid packets are
            # always multiples of 4 bytes.
            return

        audio = np.frombuffer(raw, dtype="<f4").astype(np.float32, copy=False)
        for event in self.vad_session.push_audio(audio):
            event_type = event.get("type")
            if event_type == "vad_start":
                await self.send_event("vad_start")
            elif event_type == "vad_short":
                await self.send_event("vad_short", duration_ms=event["duration_ms"])
            elif event_type == "vad_end":
                await self.send_event("vad_end", duration_ms=event["duration_ms"])
                await self.start_current_task(self.process_utterance(event["audio"]))

    async def start_current_task(self, coro) -> None:
        if self.current_task and not self.current_task.done():
            await self.send_event("busy")
            return
        self.current_task = asyncio.create_task(coro)

        def _clear_task(task: asyncio.Task) -> None:
            if self.current_task is task:
                self.current_task = None

        self.current_task.add_done_callback(_clear_task)

    async def interrupt_current_turn(self, notify: bool = True) -> None:
        task = self.current_task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        self.current_task = None
        self.processing = False
        self.reset_tts_pcm_state()
        if self.vad_session:
            self.vad_session.reset()
        if notify:
            await self.send_event("interrupted")

    async def process_utterance(self, audio: np.ndarray) -> None:
        # VAD has decided that one user turn is complete. Convert it to WAV
        # because Qwen3-ASR service endpoints expect file-like audio input.
        self.processing = True
        dialog_started = False
        try:
            wav_bytes = wav_bytes_from_float32(audio)
            start = time.perf_counter()
            await self.send_event("status", stage="asr", label="running")
            user_text = await transcribe_audio(self.settings, wav_bytes)
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            await self.send_event("metric", name="asr_ms", value=elapsed_ms)
            await self.send_event("asr", text=user_text)
            if user_text:
                dialog_started = True
                await self.process_user_text(user_text, source="voice")
        except Exception as exc:
            await self.send_event("error", message=f"ASR failed: {exc}")
        finally:
            self.processing = False
            if not dialog_started:
                await self.send_event("turn_done")

    async def process_user_text(self, user_text: str, source: str) -> None:
        if self.processing and source == "text":
            await self.send_event("busy")
            return

        old_processing = self.processing
        self.processing = True
        await self.send_event("user", text=user_text, source=source)
        self.persist_messages([{"role": "user", "content": user_text, "source": source}], title_hint=user_text)
        await self.send_event("conversation_saved", conversation=self.conversation)

        try:
            repeat_text = extract_repeat_text(user_text)
            if repeat_text:
                # "跟着我说..." should be spoken exactly. Bypassing the LLM
                # avoids instruction drift and removes one latency source.
                await self.send_event("assistant_start")
                await self.send_event("llm_delta", text=repeat_text)
                await self.stream_direct_tts(repeat_text)
                self.messages.append({"role": "user", "content": user_text})
                self.messages.append({"role": "assistant", "content": repeat_text})
                self.persist_messages([{"role": "assistant", "content": repeat_text}])
                await self.send_event("conversation_saved", conversation=self.conversation)
                self.trim_history()
                return

            request_user_text = build_request_user_text(user_text, last_assistant_content(self.messages))
            tool_result = await self.maybe_execute_tool(user_text)
            if tool_result:
                request_user_text += (
                    "\n\n联网/API 工具结果如下。请基于结果回答用户；如果结果不足或失败，要自然说明不确定，不要编造：\n"
                    + json.dumps(tool_result, ensure_ascii=False)
                )
            request_messages = self.build_contextual_request_messages(user_text, request_user_text)
            text_queue: asyncio.Queue = asyncio.Queue()
            # TTS runs in parallel with the streaming LLM response. The LLM
            # producer places completed text segments in this queue.
            tts_task = asyncio.create_task(self.tts_queue_worker(text_queue))

            await self.send_event("assistant_start")
            full_answer = await self.stream_llm(request_messages, text_queue)
            await text_queue.put(END)
            await tts_task

            self.messages.append({"role": "user", "content": user_text})
            if full_answer:
                self.messages.append({"role": "assistant", "content": full_answer})
                self.persist_messages([{"role": "assistant", "content": full_answer}])
                await self.send_event("conversation_saved", conversation=self.conversation)
                await self.memory_store.observe_turn(self.settings, user_text, full_answer)
            self.trim_history()
        except Exception as exc:
            await self.send_event("error", message=f"Dialog failed: {exc}")
        finally:
            self.processing = old_processing
            await self.send_event("turn_done")

    def build_llm_messages(self, conversation: dict) -> list[dict[str, str]]:
        messages = [{"role": "system", "content": self.settings.system}]
        for item in conversation.get("messages") or []:
            role = item.get("role")
            content = item.get("content")
            if role in {"user", "assistant"} and content:
                messages.append({"role": role, "content": content})
        return messages

    def persist_messages(self, messages: list[dict], title_hint: str | None = None) -> None:
        self.conversation = self.conversation_store.append_messages(
            self.conversation["id"],
            messages,
            title_hint=title_hint,
        )

    def build_contextual_request_messages(self, user_text: str, request_user_text: str) -> list[dict[str, str]]:
        messages = list(self.messages)
        memory_context = self.memory_store.format_context(self.settings, user_text)

        # 注入当前时间
        from datetime import datetime
        now_str = datetime.now().strftime("%Y年%m月%d日 %A %H:%M")
        time_note = f"\n\n当前时间：{now_str}。你要自然地感知这个时间（比如晚上就聊晚上的话题，早上就聊早上的），但不要生硬地报时。"
        # 重复检测
        recent_user_msgs = [m.get("content","") for m in messages[-6:] if m.get("role") == "user"]
        if len(recent_user_msgs) >= 2 and recent_user_msgs[-1]:
            last = compact_str(recent_user_msgs[-1])
            prev = compact_str(recent_user_msgs[-2])
            if last and prev and last == prev:
                time_note += "\n注意：用户刚才问了和上一轮完全一样的问题。你应该稍微不耐烦或用不同方式回答，而不是原句重复。"

        old_content = messages[0].get("content", "")
        if memory_context:
            old_content += "\n\n" + memory_context
        old_content += time_note
        messages[0] = {**messages[0], "content": old_content}

        messages.append({"role": "user", "content": request_user_text})
        return messages

    async def maybe_execute_tool(self, user_text: str) -> dict | None:
        if not self.settings.tools_enabled:
            return None

        specs = self.tool_manager.enabled_specs()
        if not specs:
            return None

        heuristic_call = self.tool_manager.suggest_from_text(user_text)
        custom_enabled = any(not spec.get("builtin") for spec in specs)
        tool_signal = bool(
            heuristic_call
            or custom_enabled
            or re.search(r"(当前|现在|最新|实时|热点|新闻|搜索|查一下|网上|天气|价格|汇率|网址|https?://)", user_text, flags=re.I)
        )
        if not tool_signal:
            return None

        call = heuristic_call
        if self.settings.tools_auto_call:
            planned = await self.plan_tool_call(user_text)
            if planned:
                call = planned

        if not call:
            return None

        tool_id = call.get("id") or ""
        arguments = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
        if not self.tool_manager.tool_exists(tool_id):
            return None

        await self.send_event("tool", id=tool_id, arguments=arguments)
        try:
            result = await self.tool_manager.execute(
                tool_id,
                arguments,
                timeout=self.settings.tools_timeout,
                max_chars=self.settings.tools_max_result_chars,
            )
            return {"tool": tool_id, "arguments": arguments, "result": result}
        except Exception as exc:
            return {"tool": tool_id, "arguments": arguments, "result": {"ok": False, "error": str(exc)}}

    async def plan_tool_call(self, user_text: str) -> dict | None:
        planner_system = (
            "你是工具路由器，只输出 JSON，不输出解释。"
            "当用户需要当前、实时、联网、热点新闻、天气、财经价格、URL 读取或某个自定义 API 时，选择一个工具。"
            "普通闲聊、稳定常识、情绪陪伴和不需要联网的问题，输出 {\"tool_call\": null}。"
            "输出格式必须是 {\"tool_call\":{\"id\":\"工具id\",\"arguments\":{...}}} 或 {\"tool_call\":null}。\n\n"
            "可用工具：\n"
            f"{self.tool_manager.planner_tool_text()}"
        )
        messages = [
            {"role": "system", "content": planner_system},
            {"role": "user", "content": user_text},
        ]
        try:
            text = await self.complete_llm_text(messages, temperature=0.0, max_tokens=260)
        except Exception:
            return None
        return parse_tool_call(text)

    async def complete_llm_text(self, messages: list[dict[str, str]], temperature: float = 0.0, max_tokens: int = 260) -> str:
        payload = {
            "model": self.settings.llm_model,
            "messages": messages,
            "stream": False,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        async with httpx.AsyncClient(timeout=self.settings.tools_timeout) as client:
            resp = await client.post(self.settings.llm_url, json=payload, headers=llm_headers(self.settings))
        resp.raise_for_status()
        return extract_chat_message_text(resp.json())

    async def stream_llm(self, request_messages: list[dict[str, str]], text_queue: asyncio.Queue) -> str:
        # llama.cpp exposes an OpenAI-compatible SSE stream. We forward each
        # text delta to the page immediately, while buffering sentence-sized
        # pieces for TTS.
        payload = {
            "model": self.settings.llm_model,
            "messages": request_messages,
            "stream": True,
            "temperature": self.settings.temperature,
            "max_tokens": self.settings.max_tokens,
            "top_p": 0.95,
            "repeat_penalty": 1.18,
            "frequency_penalty": 0.4,
            "presence_penalty": 0.3,
        }
        buffer = ""
        full_answer = ""
        first_chunk = True
        first_token = True
        started = time.perf_counter()

        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("POST", self.settings.llm_url, json=payload, headers=llm_headers(self.settings)) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    if line.startswith("data: "):
                        line = line[6:]
                    if line.strip() == "[DONE]":
                        break

                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    text = extract_llm_delta(data)
                    if not text:
                        continue

                    if first_token:
                        await self.send_event("metric", name="llm_first_token_ms", value=int((time.perf_counter() - started) * 1000))
                        first_token = False

                    full_answer += text
                    buffer += text
                    await self.send_event("llm_delta", text=text)
                    if should_flush_tts(buffer, first_chunk):
                        # First segment is intentionally shorter for lower
                        # first-audio latency; later segments are longer to
                        # reduce TTS prosody jumps. Clean before enqueueing so
                        # prompt echoes like <|endofprompt|> never reach TTS.
                        tts_text = clean_for_tts(buffer)
                        if tts_text:
                            await text_queue.put(tts_text)
                        buffer = ""
                        first_chunk = False

        if buffer.strip():
            tts_text = clean_for_tts(buffer)
            if tts_text:
                await text_queue.put(tts_text)

        return clean_for_tts(full_answer) or full_answer.strip()

    async def tts_queue_worker(self, text_queue: asyncio.Queue) -> None:
        # TTS requests are sequential so audio order is deterministic. The
        # audio itself still streams back chunk by chunk inside each request.
        # If tts_enabled is false, drain the queue silently without calling TTS.
        while True:
            text = await text_queue.get()
            if text is END:
                return
            if not self.settings.tts_enabled:
                continue
            text = clean_for_tts(str(text))
            if text:
                await self.stream_direct_tts(text)

    async def stream_direct_tts(self, text: str) -> None:
        if not self.settings.tts_enabled:
            return
        # CosyVoice server returns raw PCM16 mono. Text events and binary audio
        # frames share the same WebSocket, so the frontend can update text and
        # schedule audio playback without polling.
        #
        # Raw PCM16 must stay 2-byte aligned. Network/HTTP chunks are transport
        # chunks, not audio frame boundaries; odd bytes or abrupt segment edges
        # can sound like pops/clicks in the browser.
        text = clean_for_tts(text)
        if not text:
            return

        # CosyVoice/vLLM paths can be unstable under concurrent requests, so all
        # browser sessions share one TTS lock. This prevents overlapping /tts
        # calls that can produce garbled "啊啊啊" audio or NoneType failures.
        async with GLOBAL_TTS_LOCK:
            await self.send_event("tts_segment", text=text)
            payload = {
                "text": text,
                "stream": True,
                "speed": self.settings.tts_speed,
                "seed": self.settings.tts_seed,
            }
            started = time.perf_counter()
            first_audio = True
            self.reset_tts_pcm_state()

            try:
                async with httpx.AsyncClient(timeout=None) as client:
                    async with client.stream("POST", self.settings.tts_url, json=payload) as resp:
                        resp.raise_for_status()
                        async for chunk in resp.aiter_bytes():
                            if not chunk:
                                continue
                            if first_audio:
                                await self.send_event("metric", name="tts_first_audio_ms", value=int((time.perf_counter() - started) * 1000))
                                await self.send_event("audio_format", sample_rate=self.settings.tts_sample_rate, channels=1, format="pcm_s16le")
                                first_audio = False

                            safe_chunk = self.process_tts_pcm_chunk(chunk)
                            if safe_chunk:
                                await self.send_audio(safe_chunk)

                    tail = self.finish_tts_pcm_stream()
                    if tail:
                        await self.send_audio(tail)

            except httpx.ConnectError as exc:
                await self.send_event(
                    "error",
                    message=(
                        f"CosyVoice3 TTS 服务连接失败：{self.settings.tts_url}。"
                        "请去“服务”页面启动 CosyVoice3 TTS，并查看 tts 日志。"
                        f"原始错误：{exc}"
                    ),
                )
            except httpx.HTTPStatusError as exc:
                await self.send_event(
                    "error",
                    message=f"CosyVoice3 TTS 返回 HTTP {exc.response.status_code}：请查看 tts 日志。",
                )
            except httpx.HTTPError as exc:
                await self.send_event("error", message=f"CosyVoice3 TTS 请求失败：{exc}")
            finally:
                self.reset_tts_pcm_state()

    def reset_tts_pcm_state(self) -> None:
        self.tts_pcm_pending = b""
        self.tts_pcm_tail = np.array([], dtype=np.int16)
        self.tts_pcm_started = False

    def process_tts_pcm_chunk(self, chunk: bytes) -> bytes:
        data = self.tts_pcm_pending + chunk
        if len(data) < 2:
            self.tts_pcm_pending = data
            return b""

        if len(data) % 2:
            self.tts_pcm_pending = data[-1:]
            data = data[:-1]
        else:
            self.tts_pcm_pending = b""

        samples = np.frombuffer(data, dtype="<i2").astype(np.float32)
        if samples.size == 0:
            return b""

        volume = float(np.clip(self.settings.tts_volume, 0.05, 1.5))
        samples *= volume

        fade_samples = self.tts_fade_samples()
        if not self.tts_pcm_started:
            fade_len = min(fade_samples, samples.size)
            if fade_len > 1:
                samples[:fade_len] *= np.linspace(0.0, 1.0, fade_len, dtype=np.float32)
            self.tts_pcm_started = True

        samples = np.clip(samples, -32768, 32767).astype(np.int16)

        if fade_samples <= 0:
            return samples.astype("<i2", copy=False).tobytes()

        if self.tts_pcm_tail.size:
            samples = np.concatenate([self.tts_pcm_tail, samples])

        if samples.size <= fade_samples:
            self.tts_pcm_tail = samples
            return b""

        send_samples = samples[:-fade_samples]
        self.tts_pcm_tail = samples[-fade_samples:]
        return send_samples.astype("<i2", copy=False).tobytes()

    def finish_tts_pcm_stream(self) -> bytes:
        if self.tts_pcm_pending:
            self.tts_pcm_pending = b""

        tail = self.tts_pcm_tail.astype(np.float32)
        self.tts_pcm_tail = np.array([], dtype=np.int16)
        if tail.size == 0:
            return b""

        fade_len = min(self.tts_fade_samples(), tail.size)
        if fade_len > 1:
            tail[-fade_len:] *= np.linspace(1.0, 0.0, fade_len, dtype=np.float32)

        tail = np.clip(tail, -32768, 32767).astype(np.int16)
        return tail.astype("<i2", copy=False).tobytes()

    def tts_fade_samples(self) -> int:
        fade_ms = max(0, int(self.settings.tts_fade_ms))
        return int(self.settings.tts_sample_rate * fade_ms / 1000)
    async def send_event(self, event_type: str, **payload) -> None:
        payload["type"] = event_type
        async with self.send_lock:
            # Serialize WebSocket sends: FastAPI/Starlette does not allow
            # concurrent send_text/send_bytes calls on the same socket.
            await self.websocket.send_text(json.dumps(payload, ensure_ascii=False))

    async def send_audio(self, chunk: bytes) -> None:
        async with self.send_lock:
            await self.websocket.send_bytes(chunk)

    def trim_history(self) -> None:
        max_history_messages = max(2, self.settings.history_turns * 2)
        if len(self.messages) > 1 + max_history_messages:
            del self.messages[1 : len(self.messages) - max_history_messages]


async def check_service(name: str, url: str) -> dict:
    # Health checks are intentionally shallow. Some OpenAI-compatible servers
    # may not implement /health, but this still catches obvious port mistakes.
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=1.5) as client:
            resp = await client.get(url)
        return {
            "name": name,
            "ok": resp.status_code < 500,
            "status": resp.status_code,
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "url": url,
        }
    except Exception as exc:
        return {"name": name, "ok": False, "error": str(exc), "url": url}


def health_url_from(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "/health", "", ""))


def is_tcp_port_open(url: str) -> bool:
    if not url:
        return False
    parts = urlsplit(url)
    host = parts.hostname
    port = parts.port
    if not host or not port:
        return False
    try:
        with socket.create_connection((host, port), timeout=0.35):
            return True
    except OSError:
        return False


def wav_bytes_from_float32(audio: np.ndarray) -> bytes:
    audio = np.asarray(audio, dtype=np.float32).reshape(-1)
    audio = np.clip(audio, -1.0, 1.0)
    pcm = (audio * 32767.0).astype("<i2")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(MIC_SAMPLE_RATE)
        wav.writeframes(pcm.tobytes())
    return buffer.getvalue()


async def transcribe_audio(settings: SessionSettings, audio_bytes: bytes) -> str:
    # qwen-asr-serve can be used in two modes depending on the installed
    # version/backend: an audio transcription endpoint or chat completions with
    # an audio_url payload.
    if settings.asr_mode == "chat":
        return await transcribe_via_chat(settings, audio_bytes)
    return await transcribe_via_transcriptions(settings, audio_bytes)


async def transcribe_via_transcriptions(settings: SessionSettings, audio_bytes: bytes) -> str:
    files = {"file": ("speech.wav", audio_bytes, "audio/wav")}
    data = {"model": settings.asr_model}
    async with httpx.AsyncClient(timeout=settings.asr_timeout) as client:
        resp = await client.post(settings.asr_url, data=data, files=files)
    resp.raise_for_status()
    return parse_asr_text(extract_asr_response_text(resp.json()))


async def transcribe_via_chat(settings: SessionSettings, audio_bytes: bytes) -> str:
    audio_b64 = base64.b64encode(audio_bytes).decode("ascii")
    payload = {
        "model": settings.asr_model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "audio_url",
                        "audio_url": {"url": f"data:audio/wav;base64,{audio_b64}"},
                    }
                ],
            }
        ],
        "temperature": 0,
        "max_tokens": settings.asr_max_tokens,
    }
    async with httpx.AsyncClient(timeout=settings.asr_timeout) as client:
        resp = await client.post(settings.asr_url, json=payload)
    resp.raise_for_status()
    return parse_asr_text(extract_asr_response_text(resp.json()))


def extract_asr_response_text(data: dict) -> str:
    if isinstance(data.get("text"), str):
        return data["text"]

    choices = data.get("choices") or []
    if choices:
        message = choices[0].get("message") or {}
        if isinstance(message.get("content"), str):
            return message["content"]

    return json.dumps(data, ensure_ascii=False)


def parse_asr_text(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""

    try:
        # When qwen_asr is installed, use its parser because it knows the
        # model's output wrapper format better than a generic regex.
        from qwen_asr import parse_asr_output

        _language, text = parse_asr_output(raw)
        return text.strip()
    except Exception:
        pass

    text = re.sub(r"<\|[^|]+?\|>", "", raw)
    text = re.sub(r"\[[0-9:.]+\s*-\s*[0-9:.]+\]", "", text)
    return text.strip()


def extract_llm_delta(data: dict) -> str:
    choice = (data.get("choices") or [{}])[0]
    delta = choice.get("delta") or {}
    text = delta.get("content")
    if text is None:
        message = choice.get("message") or {}
        text = message.get("content")
    return text or ""


def extract_chat_message_text(data: dict) -> str:
    choices = data.get("choices") or []
    if choices:
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            return content
        text = choices[0].get("text")
        if isinstance(text, str):
            return text
    if isinstance(data.get("content"), str):
        return data["content"]
    return json.dumps(data, ensure_ascii=False)


def is_story_request(text: str) -> bool:
    return any(keyword in text for keyword in STORY_KEYWORDS)


def build_request_user_text(text: str, previous_assistant: str | None = None) -> str:
    context_note = ""
    if previous_assistant and is_context_recall_request(text):
        context_note = (
            "\n\nContext note for this turn only: your immediately previous assistant reply was: "
            f"{previous_assistant}"
        )

    if not is_story_request(text):
        return text + context_note

    # Story constraints are added only for this request and are not stored in
    # long-term history, so normal chat does not inherit bedtime-story rules.
    return (
        text
        + "\n\nTask: The user is asking for a bedtime/story. "
        "Directly tell a warm, coherent Chinese story for voice reading. "
        "Start with one short Chinese sentence under 8 Chinese characters. "
        "Then continue the story in 100 to 180 Chinese characters. "
        "Do not start with a permission phrase such as 'sure' or 'of course'. "
        "Do not give sleep advice, do not ask the user a question, "
        "do not evaluate your own story, and do not output END."
        + context_note
    )


def extract_repeat_text(text: str) -> str | None:
    for prefix in REPEAT_PREFIXES:
        index = text.find(prefix)
        if index == -1:
            continue
        value = text[index + len(prefix) :].strip()
        value = value.lstrip("\u3000 \t\r\n\uff0c,:\uff1a")
        return value or None
    return None


def is_context_recall_request(text: str) -> bool:
    return any(keyword in text for keyword in CONTEXT_RECALL_KEYWORDS)


def last_assistant_content(messages: list[dict[str, str]]) -> str | None:
    for message in reversed(messages):
        if message.get("role") == "assistant" and message.get("content"):
            return message["content"]
    return None


def should_flush_tts(text: str, first_chunk: bool) -> bool:
    # This is the latency/prosody trade-off knob. Smaller chunks start sooner,
    # but every TTS request can slightly change rhythm; later chunks are larger.
    stripped = text.strip()
    if not stripped:
        return False

    if first_chunk:
        if len(stripped) >= 3 and stripped.endswith(tuple(END_PUNCT)):
            return True
        if len(stripped) >= 8 and stripped.endswith(tuple(SOFT_PUNCT)):
            return True
        return len(stripped) >= 14

    if len(stripped) < 32:
        return False
    if len(stripped) >= 42 and stripped.endswith(tuple(END_PUNCT)):
        return True
    if len(stripped) >= 60 and stripped.endswith(tuple(SOFT_PUNCT)):
        return True
    return len(stripped) >= 90


def clean_for_tts(text: str) -> str:
    """Normalize assistant text before sending it to CosyVoice.

    llama.cpp / chat-template misconfiguration can sometimes echo prompt text,
    for example: "You are a helpful assistant<|endofprompt|>你好". CosyVoice
    should only receive natural speakable text, not system prompts or special
    tokens. This function is deliberately conservative: it removes known prompt
    wrappers and drops pure prompt fragments, while keeping normal Chinese text.
    """
    text = str(text or "").strip()
    if not text:
        return ""

    # Keep only the real assistant answer after common prompt separators.
    for marker in ("<|endofprompt|>", "<|im_start|>assistant", "assistant:", "Assistant:"):
        if marker in text:
            text = text.split(marker)[-1]

    # Remove common chat-template / special tokens.
    text = re.sub(r"<\|.*?\|>", "", text)

    # Remove accidental prompt echoes that should never be spoken.
    prompt_fragments = (
        "You are a helpful assistant<|endofprompt|>",
        "You are a helpful assistant",
        "You are a helpful",
        "A conversation between User and Assistant",
    )
    for fragment in prompt_fragments:
        text = text.replace(fragment, "")

    # Remove role labels at the beginning or on their own lines.
    text = re.sub(r"(^|\n)\s*(system|user|assistant)\s*[:：]\s*", "\\1", text, flags=re.I)

    # Remove explicit stop words and leftover wrappers.
    text = re.sub(r"\s*END\s*$", "", text, flags=re.IGNORECASE)
    text = text.replace("<s>", "").replace("</s>", "")

    # Collapse whitespace to make TTS rhythm more stable.
    text = re.sub(r"\s+", " ", text).strip()

    # If only an English prompt fragment is left, do not synthesize it.
    if re.fullmatch(r"[A-Za-z0-9\s,.'\"!?:;_\-<>|/]+", text or ""):
        if re.search(r"(helpful|assistant|system|prompt|user|conversation)", text, flags=re.I):
            return ""

    return text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--asr-mode", choices=["transcription", "chat"], default="transcription")
    parser.add_argument("--asr-url", default="http://127.0.0.1:8001/v1/audio/transcriptions")
    parser.add_argument("--asr-model", default="qwen3-asr")
    parser.add_argument("--asr-timeout", type=float, default=120)
    parser.add_argument("--asr-max-tokens", type=int, default=256)

    parser.add_argument("--llm-url", default="http://127.0.0.1:8080/v1/chat/completions")
    parser.add_argument("--llm-model", default="qwen3.5-9b")
    parser.add_argument("--llm-api-key", default=os.environ.get("BUDING_LLM_API_KEY", ""))
    parser.add_argument("--temperature", type=float, default=0.35)
    parser.add_argument("--max-tokens", type=int, default=220)
    parser.add_argument("--history-turns", type=int, default=8)
    parser.add_argument("--system", default=DEFAULT_SYSTEM)

    parser.add_argument("--memory-enabled", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--memory-extract-enabled", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--memory-short-to-mid-days", type=int, default=60)
    parser.add_argument("--memory-short-to-mid-count", type=int, default=3)
    parser.add_argument("--memory-mid-to-long-days", type=int, default=180)
    parser.add_argument("--memory-mid-to-long-count", type=int, default=5)
    parser.add_argument("--memory-short-delete-days", type=int, default=180)
    parser.add_argument("--memory-mid-downgrade-days", type=int, default=180)
    parser.add_argument("--memory-long-downgrade-days", type=int, default=365)
    parser.add_argument("--memory-max-context-items", type=int, default=12)

    parser.add_argument("--tools-enabled", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--tools-auto-call", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--tools-timeout", type=float, default=12.0)
    parser.add_argument("--tools-max-result-chars", type=int, default=4000)

    parser.add_argument("--tts-url", default="http://127.0.0.1:50000/tts")
    parser.add_argument("--tts-sample-rate", type=int, default=24000)
    parser.add_argument("--tts-speed", type=float, default=1.08)
    parser.add_argument("--tts-seed", type=int, default=42)
    parser.add_argument("--tts-volume", type=float, default=DEFAULT_TTS_VOLUME)
    parser.add_argument("--tts-fade-ms", type=int, default=DEFAULT_TTS_FADE_MS)
    parser.add_argument("--tts-enabled", action=argparse.BooleanOptionalAction, default=True)

    parser.add_argument("--vad-device", choices=["cpu", "cuda", "auto"], default="cpu")
    parser.add_argument("--vad-threshold", type=float, default=0.50)
    parser.add_argument("--vad-min-silence-ms", type=int, default=350)
    parser.add_argument("--vad-speech-pad-ms", type=int, default=120)
    parser.add_argument("--pre-speech-ms", type=int, default=250)
    parser.add_argument("--min-utterance-ms", type=int, default=250)
    parser.add_argument("--max-utterance-sec", type=float, default=15.0)

    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument("--service-config", default="")
    return parser


def main() -> None:
    """程序入口：解析参数，创建 FastAPI app，用 uvicorn 启动。"""
    import uvicorn

    parser = build_parser()
    args = parser.parse_args()
    app = create_app(args)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
