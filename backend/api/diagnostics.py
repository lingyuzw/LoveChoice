from __future__ import annotations

import os
import sys
from pathlib import Path
from shutil import which

from fastapi import APIRouter, Request

from domain.paths import (
    FRONTEND_DIST_DIR,
    INTEGRATIONS_CONFIG,
    LOG_DIR,
    MEMORY_DB,
    PROACTIVE_CONFIG,
    PROACTIVE_DB,
    PROJECT_ROOT,
    REMINDERS_DB,
    RUNTIME_DIR,
    SERVICE_PROFILES_CONFIG,
    SETTINGS_CONFIG,
    STICKER_LIBRARY_INDEX,
    TOOL_PROVIDERS_CONFIG,
    TOOLS_CONFIG,
)


def path_status(path: Path, *, directory: bool = False) -> dict:
    exists = path.is_dir() if directory else path.exists()
    return {
        "path": str(path),
        "exists": exists,
        "kind": "directory" if directory else "file",
    }


def command_status(command: str) -> dict:
    resolved = which(command)
    return {"command": command, "available": bool(resolved), "path": resolved or ""}


def count_items(value) -> int:
    try:
        return len(value)
    except TypeError:
        return 0


def create_diagnostics_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/diagnostics/summary")
    async def diagnostics_summary(request: Request):
        service_manager = request.app.state.service_manager
        services = await service_manager.status_all()
        integrations = request.app.state.integration_manager.list_integrations()
        stickers = request.app.state.sticker_store.list()
        memories = request.app.state.memory_store.list_memories(request.app.state.settings, limit=1)
        reminders = request.app.state.reminder_store.list(status="")
        proactive_events = request.app.state.proactive_store.list_events(limit=1)

        files = {
            "runtime": path_status(RUNTIME_DIR, directory=True),
            "frontend_dist": path_status(FRONTEND_DIST_DIR, directory=True),
            "settings": path_status(SETTINGS_CONFIG),
            "service_profiles": path_status(SERVICE_PROFILES_CONFIG),
            "tools": path_status(TOOLS_CONFIG),
            "tool_providers": path_status(TOOL_PROVIDERS_CONFIG),
            "integrations": path_status(INTEGRATIONS_CONFIG),
            "stickers": path_status(STICKER_LIBRARY_INDEX),
            "memory_db": path_status(MEMORY_DB),
            "reminders_db": path_status(REMINDERS_DB),
            "proactive_config": path_status(PROACTIVE_CONFIG),
            "proactive_db": path_status(PROACTIVE_DB),
            "logs": path_status(LOG_DIR, directory=True),
        }
        commands = {name: command_status(name) for name in ["node", "npm", "ffmpeg", "openclaw"]}
        issues: list[str] = []
        if not files["frontend_dist"]["exists"]:
            issues.append("前端 dist 不存在，请先在 frontend 目录运行 npm run build。")
        if not files["runtime"]["exists"]:
            issues.append("runtime 目录不存在，后端启动时应自动创建。")
        if not commands["node"]["available"]:
            issues.append("未检测到 node，微信/OpenClaw 链路可能不可用。")
        if not commands["npm"]["available"]:
            issues.append("未检测到 npm，OpenClaw 依赖安装和维护可能不可用。")
        if not commands["ffmpeg"]["available"]:
            issues.append("未检测到 ffmpeg，语音转码和微信语音链路可能不可用。")
        if not integrations.get("environment", {}).get("openclaw", {}).get("ok") and not commands["openclaw"]["available"]:
            issues.append("未检测到 openclaw，微信接入启动会失败。")

        return {
            "ok": not issues,
            "project_root": str(PROJECT_ROOT),
            "python": {
                "executable": sys.executable,
                "version": sys.version.split()[0],
                "platform": sys.platform,
            },
            "process": {"pid": os.getpid(), "cwd": os.getcwd()},
            "files": files,
            "commands": commands,
            "counts": {
                "services": count_items(services),
                "integrations": count_items(integrations.get("integrations", [])),
                "stickers": count_items(stickers),
                "memories_sampled": count_items(memories),
                "reminders": count_items(reminders),
                "proactive_events_sampled": count_items(proactive_events),
            },
            "issues": issues,
        }

    return router
