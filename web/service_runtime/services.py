from __future__ import annotations

import asyncio
import json
import os
import platform
import re
import signal
import socket
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx


DEFAULT_SERVICE_PROFILES = {
    "asr": {
        "label": "Qwen3-ASR vLLM",
        "description": "Speech recognition service, started by qwen-asr-serve.",
        "cwd": "/root/autodl-tmp/project",
        "command": (
            "env OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1 "
            "/root/miniconda3/bin/conda run --no-capture-output -n qwen3-asr qwen-asr-serve /root/autodl-tmp/project/Qwen3-ASR-1.7B "
            "--served-model-name qwen3-asr --gpu-memory-utilization 0.28 --max-model-len 4096 --max-num-seqs 1 "
            "--enforce-eager --host 0.0.0.0 --port 8001"
        ),
        "health_url": "http://127.0.0.1:8001/health",
        "startup_wait_sec": 0,
    },
    "llm": {
        "label": "llama.cpp Qwen3.5",
        "description": "OpenAI-compatible llama.cpp server for the chat model.",
        "cwd": "/root/autodl-tmp/project/llama.cpp",
        "command": (
            "./build-cuda/bin/llama-server -m ./Qwen3.5-9B.Q8_0.gguf --alias qwen3.5-9b "
            "--host 0.0.0.0 --port 8080 -ngl 99 -c 4096 --jinja --reasoning off"
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
            "--speaker hanser --load_vllm --fp16 --defer_load --host 0.0.0.0 --port 50000"
        ),
        "health_url": "http://127.0.0.1:50000/health",
        "startup_wait_sec": 0,
    },
}


class ServiceManager:
    """Manage ASR/LLM/TTS subprocesses and their runtime logs."""

    def __init__(self, config_path: Path | None, log_dir: Path):
        self.config_path = config_path
        self.log_dir = log_dir
        self.services = load_service_profiles(config_path)
        self.processes: dict[str, subprocess.Popen] = {}
        self.log_files: dict[str, Path] = {}
        self.started_at: dict[str, float] = {}
        self.log_dir.mkdir(parents=True, exist_ok=True)
        for sid in self.services:
            pid = self._read_service_pid(sid)
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
        if "startup_wait_sec" in patch and patch["startup_wait_sec"] is not None:
            try:
                service["startup_wait_sec"] = float(patch["startup_wait_sec"])
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
        tracked_pid = getattr(process, "pid", None) if process else None
        if process is not None:
            tracked_running = _safe_poll(process) is None
        if not tracked_running:
            tracked_pid = tracked_pid or self._read_service_pid(service_id)
            tracked_running = bool(tracked_pid and _is_pid_alive(tracked_pid))
        port_open = is_tcp_port_open(health_url)
        external_running = bool(health and health.get("ok")) or port_open
        running = tracked_running or external_running
        returncode = None if running or process is None else _safe_poll(process)
        runtime_state = service_runtime_state(
            running=running,
            tracked_running=tracked_running,
            health=health,
            port_open=port_open,
            returncode=returncode,
        )

        if (tracked_running or external_running) and process is None:
            tracked_pid = tracked_pid or self._read_service_pid(service_id)
            if tracked_pid and _is_pid_alive(tracked_pid) and service_id not in self.processes:
                proc = _create_virtual_process(tracked_pid)
                if proc:
                    self.processes[service_id] = proc
                    process = proc

        return {
            "id": service_id,
            **service,
            "running": running,
            "state": runtime_state,
            "error": service_runtime_error(health, returncode),
            "external": external_running and not tracked_running,
            "port_open": port_open,
            "pid": tracked_pid if tracked_running else None,
            "returncode": returncode,
            "started_at": self.started_at.get(service_id),
            "log_file": str(self.log_files.get(service_id, "")),
            "health": health,
        }

    async def start(self, service_id: str, overrides: dict | None = None, allow_config_update: bool = False) -> dict:
        if service_id not in self.services:
            raise KeyError(service_id)

        if overrides and allow_config_update:
            self.update_service(service_id, overrides)

        process = self.processes.get(service_id)
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
        command = tune_start_command(service_id, command)
        if not command:
            raise ValueError(f"{service_id} command is empty")

        cwd = service.get("cwd") or None
        if cwd and not Path(cwd).exists():
            raise FileNotFoundError(f"{service_id} cwd does not exist: {cwd}")

        self.log_dir.mkdir(parents=True, exist_ok=True)
        log_file = self.log_dir / f"{service_id}.log"
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
        self._write_service_pid(service_id, process.pid)
        return await self.status(service_id)

    async def stop(self, service_id: str) -> dict:
        if service_id not in self.services:
            raise KeyError(service_id)

        process = self.processes.get(service_id)
        pid = getattr(process, "pid", None) if process else self._read_service_pid(service_id)
        if process is None and pid and _is_pid_alive(pid):
            process = _create_virtual_process(pid)
            if process:
                self.processes[service_id] = process

        if process is not None and _safe_poll(process) is None:
            await asyncio.to_thread(self._terminate_process, process)
        elif pid and _is_pid_alive(pid):
            try:
                if platform.system() == "Windows":
                    os.kill(pid, signal.SIGTERM)
                else:
                    os.killpg(pid, signal.SIGTERM)
            except OSError:
                pass

        try:
            self._pid_file(service_id).unlink()
        except FileNotFoundError:
            pass
        self.processes.pop(service_id, None)
        self.started_at.pop(service_id, None)
        return await self.status(service_id)

    async def start_all(self, overrides: dict | None = None, allow_config_update: bool = False) -> list[dict]:
        results = []
        service_ids = list(self.services)
        for index, service_id in enumerate(service_ids):
            service_overrides = (overrides or {}).get(service_id)
            results.append(await self.start(service_id, service_overrides, allow_config_update=allow_config_update))
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

        log_file = self.log_files.get(service_id) or self.log_dir / f"{service_id}.log"
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
            log_file = self.log_files.get(sid) or self.log_dir / f"{sid}.log"
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_file.write_text("", encoding="utf-8")
            cleared.append({"id": sid, "log_file": str(log_file)})
        return {"cleared": cleared}

    def _terminate_process(self, process: subprocess.Popen) -> None:
        pid = process.pid
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                if platform.system() == "Windows":
                    os.kill(pid, sig)
                else:
                    os.killpg(pid, sig)
            except OSError:
                continue
            for _ in range(80):
                if not _is_pid_alive(pid):
                    if hasattr(process, "returncode"):
                        try:
                            process.returncode = process.returncode or -15
                        except AttributeError:
                            pass
                    return
                time.sleep(0.1)
        if hasattr(process, "returncode"):
            try:
                process.returncode = process.returncode or -15
            except AttributeError:
                pass

    def _pid_file(self, service_id: str) -> Path:
        return self.log_dir / f"{service_id}.pid"

    def _write_service_pid(self, service_id: str, pid: int) -> None:
        self._pid_file(service_id).write_text(str(pid), encoding="utf-8")

    def _read_service_pid(self, service_id: str) -> int | None:
        pf = self._pid_file(service_id)
        if pf.exists():
            try:
                return int(pf.read_text(encoding="utf-8").strip())
            except (ValueError, OSError):
                pass
        return None


def load_service_profiles(config_path: Path | None) -> dict:
    profiles = json.loads(json.dumps(DEFAULT_SERVICE_PROFILES))
    if config_path and config_path.exists():
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        for service_id, service_patch in (data.get("services") or {}).items():
            if service_id in profiles and isinstance(service_patch, dict):
                profiles[service_id].update(service_patch)
    return profiles


def service_runtime_state(
    *,
    running: bool,
    tracked_running: bool,
    health: dict | None,
    port_open: bool,
    returncode: int | None,
) -> str:
    payload = normalized_health_payload(health)
    model_status = str(payload.get("status") or "").lower()
    ready = payload.get("ready")

    if model_status in {"loading", "warming", "starting"}:
        return "warming" if model_status == "warming" else "starting"
    if ready is False:
        return "starting"
    if model_status == "error":
        return "failed"
    if health and health.get("ok"):
        return "ready"
    if health and health.get("ok") is False and not port_open:
        return "failed"
    if running or tracked_running or port_open:
        return "starting"
    if returncode is not None:
        return "failed"
    return "stopped"


def service_runtime_error(health: dict | None, returncode: int | None) -> str:
    payload = normalized_health_payload(health)
    error = payload.get("error") or (health or {}).get("error")
    if error:
        return str(error)
    if health and health.get("ok") is False and health.get("status"):
        return f"HTTP {health.get('status')}"
    if returncode is not None:
        return f"exit {returncode}"
    return ""


def normalized_health_payload(health: dict | None) -> dict:
    payload = (health or {}).get("payload") or {}
    if not isinstance(payload, dict):
        return {}
    detail = payload.get("detail")
    if isinstance(detail, dict):
        merged = dict(payload)
        merged.update(detail)
        return merged
    return payload


def tune_start_command(service_id: str, command: str) -> str:
    if service_id != "asr" or "--gpu-memory-utilization" not in command:
        return command
    safe_util = safe_gpu_memory_utilization(default=0.28)
    return re.sub(r"--gpu-memory-utilization\s+\S+", f"--gpu-memory-utilization {safe_util:.2f}", command)


def safe_gpu_memory_utilization(default: float = 0.28) -> float:
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.free,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=1.5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return default

    if result.returncode != 0:
        return default

    for line in result.stdout.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 2:
            continue
        try:
            free_mb = float(parts[0])
            total_mb = float(parts[1])
        except ValueError:
            continue
        if total_mb <= 0:
            continue
        # vLLM refuses to start when requested total memory is higher than
        # currently free memory. Keep a small buffer for LLM/TTS coexistence.
        utilization = max(0.18, min(default, (free_mb / total_mb) - 0.03))
        return round(utilization, 2)
    return default


async def check_service(name: str, url: str) -> dict:
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=1.5) as client:
            resp = await client.get(url)
        payload = {}
        try:
            payload = resp.json()
        except ValueError:
            payload = {}
        return {
            "name": name,
            "ok": resp.status_code < 500,
            "status": resp.status_code,
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "url": url,
            "payload": payload if isinstance(payload, dict) else {},
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


def _is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _safe_poll(proc) -> int | None:
    try:
        return proc.poll()
    except AttributeError:
        return None


def _create_virtual_process(pid: int) -> subprocess.Popen | None:
    try:
        proc = subprocess.Popen.__new__(subprocess.Popen)
        proc.pid = pid
        proc.returncode = None
        if platform.system() == "Windows":
            return None
        return proc
    except Exception:
        return None
