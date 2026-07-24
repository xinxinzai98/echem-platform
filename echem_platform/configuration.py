from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


CONFIG_KEYS = {
    "bind",
    "port",
    "scan_interval_seconds",
    "stable_age_seconds",
    "max_file_bytes",
    "max_points_per_curve",
    "watch_roots",
    "extensions",
    "instrument_control_enabled",
}


def local_override_path(path: Path) -> Path:
    """Return the untracked local override path for a public base config."""
    return path.with_name(f"{path.stem}.local{path.suffix}")


def _read_object(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError as exc:
        raise ValueError(f"配置文件不存在：{path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"配置文件不是有效 JSON：{path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"配置文件顶层必须是 JSON 对象：{path}")
    unknown = sorted(set(payload) - CONFIG_KEYS)
    if unknown:
        raise ValueError(f"配置文件包含未知字段：{', '.join(unknown)}")
    return payload


def _list_value(raw: dict[str, Any], key: str) -> list[Any]:
    value = raw.get(key, [])
    if not isinstance(value, list):
        raise ValueError(f"配置字段 {key} 必须是数组。")
    return value


def load_config(
    path: Path,
    *,
    include_local: bool = True,
    local_path: Path | None = None,
) -> dict[str, Any]:
    """Load a tracked base config and optionally overlay an untracked local config."""
    path = path.resolve()
    raw = _read_object(path)
    sources = [path]

    if include_local:
        override = (local_path or local_override_path(path)).resolve()
        if override.exists():
            raw.update(_read_object(override))
            sources.append(override)

    port = int(raw.get("port", 8787))
    if not 1 <= port <= 65535:
        raise ValueError("端口必须在 1 到 65535 之间。")
    instrument_control_enabled = raw.get("instrument_control_enabled", False)
    if not isinstance(instrument_control_enabled, bool):
        raise ValueError("instrument_control_enabled 必须是布尔值。")
    if instrument_control_enabled:
        raise ValueError("V0.3 阶段 A 只支持离线编译，禁止启用仪器控制。")

    config = {
        "bind": str(raw.get("bind", "127.0.0.1")),
        "port": port,
        "scan_interval_seconds": max(3, int(raw.get("scan_interval_seconds", 15))),
        "stable_age_seconds": max(0, int(raw.get("stable_age_seconds", 2))),
        "max_file_bytes": max(1024, int(raw.get("max_file_bytes", 50 * 1024 * 1024))),
        "max_points_per_curve": max(100, int(raw.get("max_points_per_curve", 2000))),
        "watch_roots": [str(item) for item in _list_value(raw, "watch_roots")],
        "extensions": [
            str(item).lower() if str(item).startswith(".") else "." + str(item).lower()
            for item in _list_value(raw, "extensions")
        ],
        "config_sources": [str(source) for source in sources],
        "local_override_active": len(sources) > 1,
        "instrument_control_enabled": False,
    }
    if config["bind"] not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("安全策略只允许监听本机回环地址。")
    return config


def resolve_watch_roots(config: dict[str, Any], base: Path) -> list[Path]:
    roots: list[Path] = []
    for raw in config["watch_roots"]:
        candidate = Path(os.path.expandvars(os.path.expanduser(str(raw))))
        if not candidate.is_absolute():
            candidate = base / candidate
        roots.append(candidate.resolve())
    return roots
