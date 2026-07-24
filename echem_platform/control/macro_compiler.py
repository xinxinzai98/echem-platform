from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import PureWindowsPath
from typing import Any

from .models import (
    CONTROL_COMPILER_VERSION,
    CompiledMacro,
    MacroCommand,
    MacroInspection,
    MacroValidationError,
    NormalizedProtocol,
    NormalizedStep,
)
from .validation import SAVE_BASENAME_PATTERN, normalize_protocol


CHI_MACRO_HEADER = bytes((0x43, 0x02, 0x00, 0x00, 0x0A))
MAX_MACRO_BYTES = 1024 * 1024
NO_VALUE_COMMANDS = {
    "celloff",
    "initeon",
    "fullcycleon",
    "abortov",
    "autosens",
    "eio",
    "impft",
    "impautosens",
    "run",
    "beep",
}
VALUE_COMMANDS = {
    "folder",
    "tech",
    "ei",
    "eh",
    "el",
    "ef",
    "pn",
    "v",
    "cl",
    "si",
    "qt",
    "sens",
    "st",
    "fh",
    "fl",
    "amp",
    "save",
    "tsave",
    "forcequit",
}
FORBIDDEN_COMMANDS = {
    "fileoverride",
    "cellon",
    "abortigt",
    "abortilt",
    "delay",
    "for",
    "next",
    "header",
    "end",
}
NUMERIC_COMMANDS = {
    "ei",
    "eh",
    "el",
    "ef",
    "v",
    "si",
    "qt",
    "sens",
    "st",
    "fh",
    "fl",
    "amp",
}
NUMERIC_RANGES = {
    "ei": (-10.0, 10.0, True),
    "eh": (-10.0, 10.0, True),
    "el": (-10.0, 10.0, True),
    "ef": (-10.0, 10.0, True),
    "v": (0.0, 1000.0, False),
    "si": (0.0, 31_536_000.0, False),
    "qt": (0.0, 31_536_000.0, True),
    "sens": (1e-12, 1000.0, True),
    "st": (0.0, 31_536_000.0, False),
    "fh": (0.0, 1e9, False),
    "fl": (0.0, 1e9, False),
    "amp": (0.0, 10.0, False),
}
TECHNIQUE_VALUES = {"cv", "ocpt", "lsv", "imp"}
STEP_PARAMETER_COMMANDS = (
    VALUE_COMMANDS - {"folder", "tech", "save", "tsave", "forcequit"}
) | {"autosens", "eio", "impft", "impautosens"}
WINDOWS_COMPONENT_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def _format_number(value: int | float) -> str:
    if isinstance(value, int):
        return str(value)
    if not math.isfinite(value):
        raise MacroValidationError("number", "宏参数必须是有限数值。")
    if value == 0:
        return "0"
    return format(value, ".12g")


def _validated_windows_path(raw: str, label: str) -> PureWindowsPath:
    if not isinstance(raw, str) or not raw.strip():
        raise MacroValidationError("windows_path", f"{label}不能为空。")
    raw = raw.strip()
    try:
        raw.encode("ascii")
    except UnicodeEncodeError as exc:
        raise MacroValidationError(
            "windows_path_ascii",
            f"{label}必须使用 ASCII 路径。",
        ) from exc
    if len(raw) > 240:
        raise MacroValidationError("windows_path_length", f"{label}长度不能超过 240。")
    path = PureWindowsPath(raw.replace("/", "\\"))
    if not path.drive or path.root != "\\" or path.drive.startswith("\\"):
        raise MacroValidationError(
            "windows_path_absolute",
            f"{label}必须是带盘符的本机绝对路径。",
        )
    components = path.parts[1:]
    if not components:
        raise MacroValidationError("windows_path_root", f"{label}不能是磁盘根目录。")
    for component in components:
        if (
            component in {".", ".."}
            or component.endswith(".")
            or not WINDOWS_COMPONENT_PATTERN.fullmatch(component)
        ):
            raise MacroValidationError(
                "windows_path_component",
                f"{label}只能包含无空格的 ASCII 字母、数字、点、下划线和连字符。",
            )
        stem = component.split(".", 1)[0].upper()
        if stem in WINDOWS_RESERVED_NAMES:
            raise MacroValidationError(
                "windows_reserved_name",
                f"{label}包含 Windows 保留名称。",
            )
    return path


def normalize_run_folder(output_folder: str, allowed_run_root: str) -> str:
    """Return a forward-slash Windows folder strictly below an allowed root."""
    output = _validated_windows_path(output_folder, "输出目录")
    root = _validated_windows_path(allowed_run_root, "允许的运行根目录")
    output_parts = tuple(part.casefold() for part in output.parts)
    root_parts = tuple(part.casefold() for part in root.parts)
    if len(output_parts) <= len(root_parts) or output_parts[: len(root_parts)] != root_parts:
        raise MacroValidationError(
            "run_root_escape",
            "输出目录必须是允许的运行根目录下的新子目录。",
        )
    return output.as_posix()


def _sensitivity_lines(params: dict[str, Any]) -> list[str]:
    if params["auto_sensitivity"]:
        return ["autosens"]
    return [f"sens={_format_number(params['sensitivity_a_v'])}"]


def _step_lines(step: NormalizedStep) -> list[str]:
    params = step.params
    lines: list[str] = []
    if step.technique == "cv":
        lines.extend(
            [
                "tech=cv",
                f"ei={_format_number(params['initial_v'])}",
                f"eh={_format_number(params['high_v'])}",
                f"el={_format_number(params['low_v'])}",
                f"pn={params['direction']}",
                f"v={_format_number(params['scan_rate_v_s'])}",
                f"cl={params['segments']}",
                f"si={_format_number(params['sample_interval_v'])}",
                f"qt={_format_number(params['quiet_time_s'])}",
                *_sensitivity_lines(params),
            ]
        )
    elif step.technique == "ocpt":
        lines.extend(
            [
                "celloff",
                "tech=ocpt",
                f"st={_format_number(params['duration_s'])}",
                f"eh={_format_number(params['upper_limit_v'])}",
                f"el={_format_number(params['lower_limit_v'])}",
                f"si={_format_number(params['sample_interval_s'])}",
                f"qt={_format_number(params['quiet_time_s'])}",
            ]
        )
    elif step.technique == "lsv":
        lines.extend(
            [
                "tech=lsv",
                f"ei={_format_number(params['initial_v'])}",
                f"ef={_format_number(params['final_v'])}",
                f"v={_format_number(params['scan_rate_v_s'])}",
                f"si={_format_number(params['sample_interval_v'])}",
                f"qt={_format_number(params['quiet_time_s'])}",
                *_sensitivity_lines(params),
            ]
        )
    elif step.technique == "eis":
        lines.append("tech=imp")
        if params["bias_mode"] == "ocp":
            lines.append("eio")
        else:
            lines.append(f"ei={_format_number(params['dc_potential_v'])}")
        lines.extend(
            [
                f"fh={_format_number(params['high_frequency_hz'])}",
                f"fl={_format_number(params['low_frequency_hz'])}",
                f"amp={_format_number(params['amplitude_v'])}",
                f"qt={_format_number(params['quiet_time_s'])}",
                "impft",
                "impautosens",
            ]
        )
    else:
        raise MacroValidationError(
            "unsupported_technique",
            "规范化协议包含未支持的技术。",
        )
    lines.extend(
        [
            "run",
            f"save:{step.save_basename}",
            f"tsave:{step.save_basename}",
        ]
    )
    return lines


def _parse_command(line: str, line_number: int) -> MacroCommand:
    if "\t" in line:
        raise MacroValidationError("macro_tab", "宏正文不允许制表符。")
    if "#" in line or ";" in line:
        raise MacroValidationError("macro_comment", "生产宏正文不允许注释。")
    colon = line.find(":")
    equals = line.find("=")
    indices = [index for index in (colon, equals) if index >= 0]
    if indices:
        split_at = min(indices)
        name = line[:split_at].strip().lower()
        value = line[split_at + 1 :].strip()
        if not value:
            raise MacroValidationError("macro_value", "带参数命令不能为空。")
    else:
        name = line.strip().lower()
        value = None
    if not re.fullmatch(r"[a-z][a-z0-9]*", name):
        raise MacroValidationError("macro_command_name", "宏命令名称格式无效。")
    if name in FORBIDDEN_COMMANDS:
        raise MacroValidationError(
            "forbidden_command",
            f"阶段 A 禁止宏命令：{name}",
        )
    if name not in NO_VALUE_COMMANDS | VALUE_COMMANDS:
        raise MacroValidationError("unknown_command", f"宏包含未知命令：{name}")
    if name in NO_VALUE_COMMANDS and value is not None:
        raise MacroValidationError("unexpected_value", f"{name} 不允许带参数。")
    if name in VALUE_COMMANDS and value is None:
        raise MacroValidationError("missing_value", f"{name} 必须带参数。")
    if name in NUMERIC_COMMANDS:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise MacroValidationError("numeric_value", f"{name} 必须是数值。") from exc
        if not math.isfinite(number):
            raise MacroValidationError("numeric_value", f"{name} 必须是有限数值。")
        minimum, maximum, inclusive_minimum = NUMERIC_RANGES[name]
        below_minimum = number < minimum if inclusive_minimum else number <= minimum
        if below_minimum or number > maximum:
            raise MacroValidationError("numeric_range", f"{name} 超出离线编译白名单。")
    if name == "cl":
        try:
            integer = int(value)
        except (TypeError, ValueError) as exc:
            raise MacroValidationError("integer_value", "cl 必须是整数。") from exc
        if str(integer) != value or not 1 <= integer <= 20_000:
            raise MacroValidationError("integer_value", "cl 必须是 1 到 20000 的整数。")
    if name == "tech" and value not in TECHNIQUE_VALUES:
        raise MacroValidationError("technique", "宏包含未支持的技术。")
    if name == "pn" and value not in {"n", "p"}:
        raise MacroValidationError("direction", "pn 只允许 n 或 p。")
    if name in {"save", "tsave"} and not SAVE_BASENAME_PATTERN.fullmatch(value):
        raise MacroValidationError("save_basename", "保存文件名不符合白名单。")
    return MacroCommand(name=name, value=value, line_number=line_number)


def _float_value(params: dict[str, MacroCommand], name: str) -> float:
    value = params[name].value
    assert value is not None
    return float(value)


def _validate_configured_step(
    technique: str,
    params: dict[str, MacroCommand],
) -> None:
    names = set(params)
    sensitivity = {"autosens", "sens"}
    if technique == "cv":
        required = {"ei", "eh", "el", "pn", "v", "cl", "si", "qt"}
        allowed = required | sensitivity
        if not required <= names or not names <= allowed or len(names & sensitivity) != 1:
            raise MacroValidationError("cv_commands", "CV 工步命令集合不完整或含额外命令。")
        initial = _float_value(params, "ei")
        high = _float_value(params, "eh")
        low = _float_value(params, "el")
        if high <= low or not low <= initial <= high:
            raise MacroValidationError("cv_window", "CV 电位窗口或初始电位无效。")
    elif technique == "ocpt":
        required = {"st", "eh", "el", "si", "qt"}
        if names != required:
            raise MacroValidationError("ocp_commands", "OCP 工步命令集合不完整或含额外命令。")
        if _float_value(params, "eh") <= _float_value(params, "el"):
            raise MacroValidationError("ocp_window", "OCP 电位记录上下限无效。")
        if _float_value(params, "si") > _float_value(params, "st"):
            raise MacroValidationError("ocp_interval", "OCP 采样间隔不能大于时长。")
    elif technique == "lsv":
        required = {"ei", "ef", "v", "si", "qt"}
        allowed = required | sensitivity
        if not required <= names or not names <= allowed or len(names & sensitivity) != 1:
            raise MacroValidationError("lsv_commands", "LSV 工步命令集合不完整或含额外命令。")
        if math.isclose(
            _float_value(params, "ei"),
            _float_value(params, "ef"),
            abs_tol=1e-12,
        ):
            raise MacroValidationError("lsv_window", "LSV 起止电位不能相同。")
    elif technique == "imp":
        required = {"fh", "fl", "amp", "qt", "impft", "impautosens"}
        bias = {"ei", "eio"}
        allowed = required | bias
        if not required <= names or not names <= allowed or len(names & bias) != 1:
            raise MacroValidationError("eis_commands", "EIS 工步命令集合不完整或含额外命令。")
        if _float_value(params, "fh") <= _float_value(params, "fl"):
            raise MacroValidationError("eis_frequency", "EIS 高频必须大于低频。")
    else:
        raise MacroValidationError("technique", "宏包含未支持的技术。")


def inspect_macro(payload: bytes, allowed_run_root: str) -> MacroInspection:
    """Statically verify a compiled macro without executing or writing it."""
    if not isinstance(payload, bytes):
        raise MacroValidationError("macro_type", "宏必须是 bytes。")
    if len(payload) <= len(CHI_MACRO_HEADER) or len(payload) > MAX_MACRO_BYTES:
        raise MacroValidationError("macro_size", "宏文件大小无效。")
    if not payload.startswith(CHI_MACRO_HEADER):
        raise MacroValidationError(
            "macro_header",
            "宏文件头必须精确匹配 43 02 00 00 0A。",
        )
    body_bytes = payload[len(CHI_MACRO_HEADER) :]
    if any(byte != 0x0A and not 0x20 <= byte <= 0x7E for byte in body_bytes):
        raise MacroValidationError(
            "macro_encoding",
            "宏正文只允许可打印 ASCII 字符和 LF 换行。",
        )
    try:
        body = body_bytes.decode("ascii")
    except UnicodeDecodeError as exc:
        raise MacroValidationError("macro_ascii", "宏正文必须是纯 ASCII。") from exc
    if "\r" in body or "\x00" in body or not body.endswith("\n"):
        raise MacroValidationError(
            "macro_encoding",
            "宏正文必须使用 LF 换行、不能含 NUL，并以换行结束。",
        )
    commands = tuple(
        _parse_command(line.strip(), line_number)
        for line_number, line in enumerate(body.splitlines(), start=1)
        if line.strip()
    )
    if len(commands) < 8:
        raise MacroValidationError("macro_structure", "宏命令数量不足。")
    preamble = tuple(command.name for command in commands[:5])
    if preamble != ("folder", "celloff", "initeon", "fullcycleon", "abortov"):
        raise MacroValidationError("macro_preamble", "宏安全前导命令不完整或顺序错误。")
    ending = tuple(command.name for command in commands[-3:])
    if ending != ("celloff", "beep", "forcequit"):
        raise MacroValidationError("macro_ending", "宏必须以 celloff、beep、forcequit 结束。")
    if commands[-1].value != "yesiamsure":
        raise MacroValidationError("forcequit_value", "forcequit 确认值无效。")
    for unique_command in (
        "folder",
        "initeon",
        "fullcycleon",
        "abortov",
        "beep",
        "forcequit",
    ):
        if sum(command.name == unique_command for command in commands) != 1:
            raise MacroValidationError(
                "command_count",
                f"宏必须且只能包含一个 {unique_command}。",
            )
    folder = commands[0].value
    assert folder is not None
    normalized_folder = normalize_run_folder(folder, allowed_run_root)
    if normalized_folder.casefold() != folder.replace("\\", "/").casefold():
        raise MacroValidationError("folder_normalization", "宏 folder 路径未规范化。")

    active_technique: str | None = None
    configured_params: dict[str, MacroCommand] = {}
    state = "idle"
    techniques: list[str] = []
    save_basenames: list[str] = []
    seen_outputs: set[str] = set()
    run_count = 0
    for index, command in enumerate(commands):
        if command.name == "tech":
            if state != "idle":
                raise MacroValidationError("step_sequence", "上一工步保存尚未闭合。")
            active_technique = command.value
            configured_params = {}
            state = "configured"
        elif command.name == "run":
            if state != "configured" or active_technique is None:
                raise MacroValidationError("run_sequence", "run 前缺少唯一技术配置。")
            _validate_configured_step(active_technique, configured_params)
            if index + 2 >= len(commands):
                raise MacroValidationError("save_sequence", "run 后缺少 save/tsave。")
            if commands[index + 1].name != "save" or commands[index + 2].name != "tsave":
                raise MacroValidationError("save_sequence", "run 后必须立即执行 save 和 tsave。")
            run_count += 1
            state = "ran"
        elif command.name == "save":
            if state != "ran":
                raise MacroValidationError("save_sequence", "save 的位置无效。")
            state = "saved"
        elif command.name == "tsave":
            if state != "saved" or active_technique is None:
                raise MacroValidationError("save_sequence", "tsave 的位置无效。")
            save_command = commands[index - 1]
            if save_command.value != command.value:
                raise MacroValidationError("save_pair", "save 与 tsave 文件名必须一致。")
            assert command.value is not None
            output_key = command.value.casefold()
            if output_key in seen_outputs:
                raise MacroValidationError("duplicate_output", "宏输出文件名不能重复。")
            seen_outputs.add(output_key)
            save_basenames.append(command.value)
            techniques.append(active_technique)
            active_technique = None
            configured_params = {}
            state = "idle"
        elif state == "configured":
            if command.name in configured_params:
                raise MacroValidationError("duplicate_command", "工步中存在重复参数命令。")
            configured_params[command.name] = command
        elif command.name in STEP_PARAMETER_COMMANDS:
            raise MacroValidationError("misplaced_command", "工步参数出现在技术配置之外。")
    if state != "idle" or active_technique is not None:
        raise MacroValidationError("step_sequence", "最后一个工步没有完整保存。")
    if run_count == 0 or run_count != len(save_basenames):
        raise MacroValidationError("run_count", "run 与保存工步数量不一致。")
    return MacroInspection(
        sha256=hashlib.sha256(payload).hexdigest(),
        body=body,
        folder=normalized_folder,
        commands=commands,
        techniques=tuple(techniques),
        save_basenames=tuple(save_basenames),
        run_count=run_count,
    )


def compile_protocol(
    protocol: NormalizedProtocol | dict[str, Any],
    *,
    output_folder: str,
    allowed_run_root: str,
) -> CompiledMacro:
    """Compile a protocol to an in-memory CHI macro; never launch an executable."""
    normalized = (
        protocol if isinstance(protocol, NormalizedProtocol) else normalize_protocol(protocol)
    )
    folder = normalize_run_folder(output_folder, allowed_run_root)
    lines = [
        f"folder: {folder}",
        "celloff",
        "initeon",
        "fullcycleon",
        "abortov",
        "",
    ]
    for step in normalized.active_steps:
        lines.extend(_step_lines(step))
        lines.append("")
    lines.extend(
        [
            "celloff",
            "beep",
            "forcequit: yesiamsure",
            "",
        ]
    )
    body = "\n".join(lines)
    try:
        body_bytes = body.encode("ascii")
    except UnicodeEncodeError as exc:
        raise MacroValidationError(
            "macro_ascii",
            "宏正文包含非 ASCII 内容；用户元数据不得写入宏。",
        ) from exc
    payload = CHI_MACRO_HEADER + body_bytes
    inspection = inspect_macro(payload, allowed_run_root)
    canonical_protocol = json.dumps(
        normalized.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    protocol_sha256 = hashlib.sha256(canonical_protocol).hexdigest()
    return CompiledMacro(
        payload=payload,
        body=body,
        macro_sha256=inspection.sha256,
        protocol_sha256=protocol_sha256,
        compiler_version=CONTROL_COMPILER_VERSION,
        output_folder=folder,
        normalized_protocol=normalized,
        inspection=inspection,
    )
