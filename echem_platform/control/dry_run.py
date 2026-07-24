from __future__ import annotations

from typing import Any

from .macro_compiler import compile_protocol
from .models import CONTROL_COMPILER_VERSION, PROTOCOL_SCHEMA_VERSION, CompiledMacro


DRY_RUN_STAGE = "web_dry_run"
SUPPORTED_TECHNIQUES = (
    {"id": "cv", "label": "CV", "description": "循环伏安"},
    {"id": "ocp", "label": "OCP", "description": "开路电位"},
    {"id": "lsv", "label": "LSV / Tafel", "description": "线性扫描"},
    {"id": "eis", "label": "EIS", "description": "交流阻抗"},
)
REQUEST_KEYS = {"protocol", "output_folder", "allowed_run_root"}


def default_dry_run_draft() -> dict[str, Any]:
    """Return a public format demonstration, never an experimental recommendation."""
    return {
        "id": "draft-main",
        "allowed_run_root": "D:/EchemPlatform/Runs",
        "output_folder": "D:/EchemPlatform/Runs/RUN-DRY-001",
        "protocol": {
            "schema_version": PROTOCOL_SCHEMA_VERSION,
            "name": "网页 Dry-run 格式演示",
            "sample_id": "DEMO-001",
            "notes": "字段格式演示；所有参数必须经实验负责人复核后才能进入未来的实机阶段。",
            "execution_mode": "single_macro",
            "steps": [
                {
                    "id": "step-01",
                    "name": "CV 格式演示",
                    "technique": "cv",
                    "enabled": True,
                    "save_basename": "DEMO_CV",
                    "params": {
                        "initial_v": 0.05,
                        "high_v": 0.05,
                        "low_v": -0.05,
                        "direction": "n",
                        "scan_rate_v_s": 0.01,
                        "segments": 2,
                        "sample_interval_v": 0.001,
                        "quiet_time_s": 0,
                        "auto_sensitivity": True,
                    },
                }
            ],
        },
    }


def dry_run_capabilities() -> dict[str, Any]:
    """Describe the offline-only web surface without exposing a launch action."""
    return {
        "stage": DRY_RUN_STAGE,
        "schema_version": PROTOCOL_SCHEMA_VERSION,
        "compiler_version": CONTROL_COMPILER_VERSION,
        "supported_techniques": list(SUPPORTED_TECHNIQUES),
        "execution_modes": ["single_macro"],
        "max_steps": 100,
        "draft_persistence": True,
        "macro_preview": True,
        "instrument_control_enabled": False,
        "instrument_started": False,
        "launch_available": False,
        "serial_access": False,
        "network_control": False,
        "default_draft": default_dry_run_draft(),
    }


def _validate_request_shape(payload: Any) -> tuple[dict[str, Any], str, str]:
    if not isinstance(payload, dict):
        raise ValueError("Dry-run 请求必须是 JSON 对象。")
    unknown = sorted(set(payload) - REQUEST_KEYS)
    if unknown:
        raise ValueError(f"Dry-run 请求包含未知字段：{', '.join(unknown)}")
    protocol = payload.get("protocol")
    if not isinstance(protocol, dict):
        raise ValueError("protocol 必须是 JSON 对象。")
    output_folder = payload.get("output_folder")
    allowed_run_root = payload.get("allowed_run_root")
    if not isinstance(output_folder, str) or not output_folder.strip():
        raise ValueError("output_folder 必须是非空字符串。")
    if not isinstance(allowed_run_root, str) or not allowed_run_root.strip():
        raise ValueError("allowed_run_root 必须是非空字符串。")
    return protocol, output_folder, allowed_run_root


def _step_preview(compiled: CompiledMacro) -> list[dict[str, Any]]:
    previews: list[dict[str, Any]] = []
    for index, step in enumerate(compiled.normalized_protocol.steps, start=1):
        previews.append(
            {
                "index": index,
                "id": step.id,
                "name": step.name,
                "technique": step.technique,
                "enabled": step.enabled,
                "save_basename": step.save_basename,
                "expected_seconds": step.expected_seconds,
                "warnings": list(step.warnings),
                "params": dict(step.params),
            }
        )
    return previews


def _safety_checks(compiled: CompiledMacro) -> list[dict[str, str]]:
    active = compiled.normalized_protocol.active_steps
    checks = [
        {
            "id": "offline_only",
            "label": "当前仅生成离线预览，不存在软件或仪器启动入口。",
            "status": "passed",
        },
        {
            "id": "output_path",
            "label": "输出目录位于允许的 Windows 运行根目录之下。",
            "status": "passed",
        },
        {
            "id": "output_names",
            "label": "启用工步的保存文件名唯一且符合 ASCII 白名单。",
            "status": "passed",
        },
        {
            "id": "macro_static_check",
            "label": "生成的宏已通过文件头、命令和工步顺序静态复核。",
            "status": "passed",
        },
        {
            "id": "experiment_review",
            "label": "电位基准、范围、扫速、量程和时间仍需实验负责人复核。",
            "status": "manual",
        },
    ]
    if any(step.technique == "eis" for step in active):
        checks.append(
            {
                "id": "eis_points_per_decade",
                "label": "EIS Points/Decade 尚不能由当前宏字段确认，需在设备参数页人工复核。",
                "status": "manual",
            }
        )
    return checks


def build_dry_run(payload: Any, *, include_macro_preview: bool) -> dict[str, Any]:
    """Validate and compile entirely in memory, returning a browser-safe preview."""
    protocol, output_folder, allowed_run_root = _validate_request_shape(payload)
    compiled = compile_protocol(
        protocol,
        output_folder=output_folder,
        allowed_run_root=allowed_run_root,
    )
    active_steps = compiled.normalized_protocol.active_steps
    known_expected_seconds = sum(
        step.expected_seconds or 0.0 for step in active_steps
    )
    expected_seconds_complete = all(
        step.expected_seconds is not None for step in active_steps
    )
    output_files = [
        {
            "step_id": step.id,
            "basename": step.save_basename,
            "binary": f"{step.save_basename}.bin",
            "text": f"{step.save_basename}.txt",
        }
        for step in active_steps
    ]
    response: dict[str, Any] = {
        "status": "valid",
        "stage": DRY_RUN_STAGE,
        "mode": "compile_preview" if include_macro_preview else "validation",
        "compiler_version": compiled.compiler_version,
        "schema_version": compiled.normalized_protocol.schema_version,
        "protocol_sha256": compiled.protocol_sha256,
        "macro_sha256": compiled.macro_sha256,
        "macro_header_hex": compiled.payload[:5].hex(" ").upper(),
        "macro_bytes": len(compiled.payload),
        "output_folder": compiled.output_folder,
        "active_step_count": len(active_steps),
        "known_expected_seconds": known_expected_seconds,
        "expected_seconds_complete": expected_seconds_complete,
        "warnings": list(compiled.normalized_protocol.warnings),
        "steps": _step_preview(compiled),
        "output_files": output_files,
        "safety_checks": _safety_checks(compiled),
        "normalized_protocol": compiled.normalized_protocol.to_dict(),
        "instrument_control_enabled": False,
        "instrument_started": False,
        "launch_available": False,
        "serial_access": False,
        "network_control": False,
        "macro_written": False,
    }
    if include_macro_preview:
        response["macro_preview"] = compiled.body
    return response
