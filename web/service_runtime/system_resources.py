from __future__ import annotations

import os
import platform
import re
import subprocess
import time
from pathlib import Path


def collect_system_resources() -> dict:
    return {
        "platform": platform.platform(),
        "cpu": read_cpu(),
        "memory": read_memory(),
        "gpus": read_gpus(),
    }


def read_cpu() -> dict:
    psutil_cpu = read_cpu_with_psutil()
    if psutil_cpu:
        return psutil_cpu

    load = None
    try:
        load = os.getloadavg()
    except (AttributeError, OSError):
        pass

    percent = read_linux_cpu_percent()
    return {
        "percent": percent,
        "load_1m": load[0] if load else None,
        "load_5m": load[1] if load else None,
        "load_15m": load[2] if load else None,
        "cores": os.cpu_count() or 0,
    }


def read_cpu_with_psutil() -> dict | None:
    try:
        import psutil
    except Exception:
        return None

    load = None
    try:
        load = os.getloadavg()
    except (AttributeError, OSError):
        pass
    return {
        "percent": psutil.cpu_percent(interval=0.05),
        "load_1m": load[0] if load else None,
        "load_5m": load[1] if load else None,
        "load_15m": load[2] if load else None,
        "cores": psutil.cpu_count() or os.cpu_count() or 0,
    }


def read_linux_cpu_percent() -> float | None:
    first = read_proc_stat_cpu()
    if not first:
        return None
    time.sleep(0.08)
    second = read_proc_stat_cpu()
    if not second:
        return None

    idle_delta = second["idle"] - first["idle"]
    total_delta = second["total"] - first["total"]
    if total_delta <= 0:
        return None
    return round(max(0.0, min(100.0, (1.0 - idle_delta / total_delta) * 100.0)), 1)


def read_proc_stat_cpu() -> dict | None:
    path = Path("/proc/stat")
    if not path.exists():
        return None
    try:
        parts = path.read_text(encoding="utf-8").splitlines()[0].split()[1:]
        values = [int(value) for value in parts]
    except Exception:
        return None
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return {"idle": idle, "total": sum(values)}


def read_memory() -> dict:
    psutil_memory = read_memory_with_psutil()
    if psutil_memory:
        return psutil_memory

    info = read_linux_meminfo()
    total = info.get("MemTotal", 0)
    available = info.get("MemAvailable", 0)
    used = max(0, total - available)
    percent = round(used / total * 100.0, 1) if total else None
    return {"total_bytes": total * 1024, "used_bytes": used * 1024, "available_bytes": available * 1024, "percent": percent}


def read_memory_with_psutil() -> dict | None:
    try:
        import psutil
    except Exception:
        return None
    mem = psutil.virtual_memory()
    return {
        "total_bytes": int(mem.total),
        "used_bytes": int(mem.used),
        "available_bytes": int(mem.available),
        "percent": float(mem.percent),
    }


def read_linux_meminfo() -> dict[str, int]:
    path = Path("/proc/meminfo")
    if not path.exists():
        return {}
    info: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^(\w+):\s+(\d+)", line)
        if match:
            info[match.group(1)] = int(match.group(2))
    return info


def read_gpus() -> list[dict]:
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=1.5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []

    if result.returncode != 0:
        return []

    gpus = []
    for index, line in enumerate(result.stdout.splitlines()):
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 5:
            continue
        name, util, used, total, temp = parts[:5]
        used_mb = parse_float(used)
        total_mb = parse_float(total)
        gpus.append(
            {
                "index": index,
                "name": name,
                "util_percent": parse_float(util),
                "memory_used_mb": used_mb,
                "memory_total_mb": total_mb,
                "memory_percent": round(used_mb / total_mb * 100.0, 1) if used_mb is not None and total_mb else None,
                "temperature_c": parse_float(temp),
            }
        )
    return gpus


def parse_float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
