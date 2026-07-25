from __future__ import annotations

import json
import os
import re
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
    "control_stage",
    "chi_executable",
    "chi_executable_sha256",
    "chi_working_directory",
    "control_root",
    "run_root",
    "stage_c_ocp_profile_sha256",
    "arm_token_ttl_seconds",
    "completion_grace_seconds",
}

SHA256_PATTERN = re.compile(r"[0-9a-fA-F]{64}")
CONTROL_STAGE_OFF = "off"
CONTROL_STAGE_OCP_60S = "ocp_60s"
CONTROL_STAGES = {CONTROL_STAGE_OFF, CONTROL_STAGE_OCP_60S}


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
    base_raw = _read_object(path)
    if base_raw.get("instrument_control_enabled", False) is not False:
        raise ValueError("公开基础配置禁止启用仪器控制；只能在本机忽略的覆盖文件中启用。")
    raw = dict(base_raw)
    sources = [path]
    local_raw: dict[str, Any] = {}

    if include_local:
        override = (local_path or local_override_path(path)).resolve()
        if override.exists():
            local_raw = _read_object(override)
            raw.update(local_raw)
            sources.append(override)

    port = int(raw.get("port", 8787))
    if not 1 <= port <= 65535:
        raise ValueError("端口必须在 1 到 65535 之间。")
    instrument_control_enabled = raw.get("instrument_control_enabled", False)
    if not isinstance(instrument_control_enabled, bool):
        raise ValueError("instrument_control_enabled 必须是布尔值。")
    control_stage = str(raw.get("control_stage", CONTROL_STAGE_OFF)).strip().lower()
    if control_stage not in CONTROL_STAGES:
        raise ValueError("control_stage 只允许 off 或 ocp_60s。")
    if instrument_control_enabled:
        if not local_raw or local_raw.get("instrument_control_enabled") is not True:
            raise ValueError("仪器控制只能由未跟踪的本机覆盖配置显式启用。")
        if control_stage != CONTROL_STAGE_OCP_60S:
            raise ValueError("阶段 C 启用仪器控制时 control_stage 必须为 ocp_60s。")

    control_paths = {
        key: str(raw.get(key, "")).strip()
        for key in (
            "chi_executable",
            "chi_working_directory",
            "control_root",
            "run_root",
        )
    }
    executable_sha256 = str(raw.get("chi_executable_sha256", "")).strip().lower()
    profile_sha256 = str(raw.get("stage_c_ocp_profile_sha256", "")).strip().lower()
    if executable_sha256 and not SHA256_PATTERN.fullmatch(executable_sha256):
        raise ValueError("chi_executable_sha256 必须是 64 位十六进制 SHA-256。")
    if profile_sha256 and not SHA256_PATTERN.fullmatch(profile_sha256):
        raise ValueError("stage_c_ocp_profile_sha256 必须是 64 位十六进制 SHA-256。")
    if instrument_control_enabled:
        missing = sorted(key for key, value in control_paths.items() if not value)
        if not executable_sha256:
            missing.append("chi_executable_sha256")
        if not profile_sha256:
            missing.append("stage_c_ocp_profile_sha256")
        if missing:
            raise ValueError(
                "阶段 C 启用仪器控制缺少本机配置：" + ", ".join(missing)
            )

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
        "instrument_control_enabled": instrument_control_enabled,
        "control_stage": control_stage,
        **control_paths,
        "chi_executable_sha256": executable_sha256,
        "stage_c_ocp_profile_sha256": profile_sha256,
        "arm_token_ttl_seconds": min(
            600,
            max(60, int(raw.get("arm_token_ttl_seconds", 300))),
        ),
        "completion_grace_seconds": min(
            120,
            max(5, int(raw.get("completion_grace_seconds", 20))),
        ),
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
