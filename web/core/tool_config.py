from __future__ import annotations

import json
from pathlib import Path
from typing import Any


GAODE_BASE_URL = "https://restapi.amap.com/v3"
DEFAULT_TOOL_PROVIDER_VERSION = 2


DEFAULT_TOOL_PROVIDER_CONFIG: dict[str, Any] = {
    "enabled": True,
    "auto_call": True,
    "timeout": 12.0,
    "max_result_chars": 4000,
    "_provider_defaults_version": DEFAULT_TOOL_PROVIDER_VERSION,
    "url_fetch": {
        "enabled": True,
        "user_agent": "Mozilla/5.0 BranchWhisper/1.0",
        "max_chars": 2500,
    },
    "weather": {
        "enabled": True,
        "provider": "gaode",
        "base_url": GAODE_BASE_URL,
        "api_key": "",
        "default_location": "北京",
    },
    "search": {
        "enabled": True,
        "provider": "gaode",
        "base_url": GAODE_BASE_URL,
        "api_key": "",
        "limit": 5,
    },
    "news": {
        "enabled": True,
        "provider": "google_rss",
        "base_url": "https://news.google.com/rss",
        "api_key": "",
        "region": "CN",
        "limit": 6,
    },
    "finance": {
        "enabled": True,
        "provider": "search",
        "base_url": "",
        "api_key": "",
    },
    "map": {
        "enabled": True,
        "provider": "gaode",
        "base_url": GAODE_BASE_URL,
        "api_key": "",
    },
    "reminder": {
        "enabled": True,
        "web_enabled": True,
        "weixin_enabled": True,
        "webhook_url": "",
    },
}


SECRET_KEYS = {"api_key", "token", "secret", "webhook_url"}


def deep_merge(base: dict, patch: dict) -> dict:
    result = dict(base)
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def mask_secret(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    if len(value) <= 8:
        return value[:2] + "*" * 6
    return value[:4] + "*" * 12 + value[-4:]


class ToolProviderConfig:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.save(DEFAULT_TOOL_PROVIDER_CONFIG)

    def load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        data = self._migrate_defaults(self._drop_runtime_fields(data))
        return deep_merge(DEFAULT_TOOL_PROVIDER_CONFIG, data)

    def public(self) -> dict:
        return self._mask(self.load())

    def update(self, patch: dict) -> dict:
        current = self.load()
        merged = deep_merge(current, self._strip_masked_secrets(patch or {}, current))
        self.save(merged)
        return self.public()

    def save(self, data: dict) -> None:
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _mask(self, value: Any) -> Any:
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                if key in SECRET_KEYS:
                    result[key] = ""
                    result[f"{key}_set"] = bool(str(item or "").strip())
                    result[f"{key}_masked"] = mask_secret(str(item or ""))
                else:
                    result[key] = self._mask(item)
            return result
        if isinstance(value, list):
            return [self._mask(item) for item in value]
        return value

    def _strip_masked_secrets(self, patch: Any, current: Any) -> Any:
        if not isinstance(patch, dict):
            return patch
        result = {}
        for key, value in patch.items():
            if key.endswith("_set") or key.endswith("_masked"):
                continue
            if isinstance(value, dict):
                result[key] = self._strip_masked_secrets(value, (current or {}).get(key, {}) if isinstance(current, dict) else {})
                continue
            if key in SECRET_KEYS and (value is None or value == "" or "*" in str(value)):
                if isinstance(current, dict) and key in current:
                    continue
            result[key] = value
        return result

    def _drop_runtime_fields(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: self._drop_runtime_fields(item)
                for key, item in value.items()
                if not (str(key).endswith("_set") or str(key).endswith("_masked"))
            }
        if isinstance(value, list):
            return [self._drop_runtime_fields(item) for item in value]
        return value

    def _migrate_defaults(self, data: dict) -> dict:
        result = dict(data or {})
        try:
            version = int(result.get("_provider_defaults_version") or 0)
        except (TypeError, ValueError):
            version = 0
        if version >= DEFAULT_TOOL_PROVIDER_VERSION:
            return result

        for key in ("weather", "search", "map"):
            provider = dict(result.get(key) or {})
            provider["enabled"] = True
            provider["provider"] = "gaode"
            provider["base_url"] = GAODE_BASE_URL
            result[key] = provider
        result["_provider_defaults_version"] = DEFAULT_TOOL_PROVIDER_VERSION
        result["enabled"] = True
        result["auto_call"] = True
        return result
