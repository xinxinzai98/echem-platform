from __future__ import annotations

import math
import re
from typing import Any

from .models import (
    PROTOCOL_SCHEMA_VERSION,
    NormalizedProtocol,
    NormalizedStep,
    ProtocolValidationError,
    ValidationIssue,
)


PROTOCOL_KEYS = {
    "schema_version",
    "name",
    "sample_id",
    "material",
    "electrolyte",
    "reference",
    "area_cm2",
    "operator",
    "notes",
    "execution_mode",
    "steps",
}
STEP_KEYS = {"id", "name", "technique", "enabled", "save_basename", "params"}
METADATA_STRING_LIMITS = {
    "sample_id": 120,
    "material": 200,
    "electrolyte": 200,
    "reference": 120,
    "operator": 120,
    "notes": 4000,
}
STEP_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
SAVE_BASENAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
TECHNIQUE_ALIASES = {
    "cv": "cv",
    "ocp": "ocpt",
    "ocpt": "ocpt",
    "lsv": "lsv",
    "eis": "eis",
    "imp": "eis",
}
POTENTIAL_MIN_V = -10.0
POTENTIAL_MAX_V = 10.0
TIME_MAX_S = 31_536_000.0


class _Collector:
    def __init__(self) -> None:
        self.issues: list[ValidationIssue] = []

    def add(self, path: str, code: str, message: str) -> None:
        self.issues.append(ValidationIssue(path, code, message))

    def unknown_fields(
        self,
        payload: dict[str, Any],
        allowed: set[str],
        path: str,
    ) -> None:
        for key in sorted(set(payload) - allowed):
            self.add(
                f"{path}.{key}" if path else key,
                "unknown_field",
                "字段不在当前离线编译器白名单中。",
            )


def _field(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


def _string(
    collector: _Collector,
    payload: dict[str, Any],
    key: str,
    path: str,
    *,
    required: bool = True,
    default: str = "",
    maximum: int = 200,
) -> str | None:
    if key not in payload:
        if required:
            collector.add(_field(path, key), "required", "缺少必填字符串。")
            return None
        return default
    value = payload[key]
    if not isinstance(value, str):
        collector.add(_field(path, key), "type", "必须是字符串。")
        return None
    value = value.strip()
    if required and not value:
        collector.add(_field(path, key), "empty", "不能为空。")
        return None
    if len(value) > maximum:
        collector.add(_field(path, key), "too_long", f"长度不能超过 {maximum}。")
        return None
    return value


def _number(
    collector: _Collector,
    payload: dict[str, Any],
    key: str,
    path: str,
    *,
    required: bool = True,
    default: float | None = None,
    minimum: float | None = None,
    maximum: float | None = None,
    exclusive_minimum: bool = False,
) -> float | None:
    if key not in payload:
        if required:
            collector.add(_field(path, key), "required", "缺少必填数值。")
            return None
        return default
    value = payload[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        collector.add(_field(path, key), "type", "必须是有限数值。")
        return None
    number = float(value)
    if not math.isfinite(number):
        collector.add(_field(path, key), "finite", "必须是有限数值。")
        return None
    if minimum is not None:
        too_small = number <= minimum if exclusive_minimum else number < minimum
        if too_small:
            operator = "大于" if exclusive_minimum else "不小于"
            collector.add(
                _field(path, key),
                "range",
                f"必须{operator} {minimum:g}。",
            )
            return None
    if maximum is not None and number > maximum:
        collector.add(
            _field(path, key),
            "range",
            f"不能大于 {maximum:g}。",
        )
        return None
    return number


def _integer(
    collector: _Collector,
    payload: dict[str, Any],
    key: str,
    path: str,
    *,
    minimum: int,
    maximum: int,
) -> int | None:
    if key not in payload:
        collector.add(_field(path, key), "required", "缺少必填整数。")
        return None
    value = payload[key]
    if isinstance(value, bool) or not isinstance(value, int):
        collector.add(_field(path, key), "type", "必须是整数。")
        return None
    if not minimum <= value <= maximum:
        collector.add(
            _field(path, key),
            "range",
            f"必须在 {minimum} 到 {maximum} 之间。",
        )
        return None
    return value


def _boolean(
    collector: _Collector,
    payload: dict[str, Any],
    key: str,
    path: str,
    *,
    default: bool,
) -> bool | None:
    if key not in payload:
        return default
    value = payload[key]
    if not isinstance(value, bool):
        collector.add(_field(path, key), "type", "必须是布尔值。")
        return None
    return value


def _choice(
    collector: _Collector,
    payload: dict[str, Any],
    key: str,
    path: str,
    choices: set[str],
) -> str | None:
    value = _string(collector, payload, key, path, maximum=40)
    if value is None:
        return None
    value = value.lower()
    if value not in choices:
        collector.add(
            _field(path, key),
            "choice",
            f"只允许：{', '.join(sorted(choices))}。",
        )
        return None
    return value


def _sensitivity(
    collector: _Collector,
    params: dict[str, Any],
    path: str,
) -> tuple[bool | None, float | None]:
    auto = _boolean(collector, params, "auto_sensitivity", path, default=True)
    sensitivity_present = "sensitivity_a_v" in params
    sensitivity = _number(
        collector,
        params,
        "sensitivity_a_v",
        path,
        required=False,
        minimum=1e-12,
        maximum=1000,
        exclusive_minimum=False,
    )
    if auto is True and sensitivity_present:
        collector.add(
            f"{path}.sensitivity_a_v",
            "conflict",
            "自动灵敏度开启时不能同时指定固定灵敏度。",
        )
    if auto is False and not sensitivity_present:
        collector.add(
            f"{path}.sensitivity_a_v",
            "required",
            "关闭自动灵敏度时必须指定固定灵敏度。",
        )
    return auto, sensitivity


def _cv_step(
    collector: _Collector,
    params: dict[str, Any],
    path: str,
) -> tuple[dict[str, Any], float, tuple[str, ...]] | None:
    allowed = {
        "initial_v",
        "high_v",
        "low_v",
        "direction",
        "scan_rate_v_s",
        "cycles",
        "segments",
        "sample_interval_v",
        "quiet_time_s",
        "auto_sensitivity",
        "sensitivity_a_v",
    }
    collector.unknown_fields(params, allowed, path)
    initial = _number(
        collector,
        params,
        "initial_v",
        path,
        minimum=POTENTIAL_MIN_V,
        maximum=POTENTIAL_MAX_V,
    )
    high = _number(
        collector,
        params,
        "high_v",
        path,
        minimum=POTENTIAL_MIN_V,
        maximum=POTENTIAL_MAX_V,
    )
    low = _number(
        collector,
        params,
        "low_v",
        path,
        minimum=POTENTIAL_MIN_V,
        maximum=POTENTIAL_MAX_V,
    )
    direction = _choice(collector, params, "direction", path, {"n", "p"})
    scan_rate = _number(
        collector,
        params,
        "scan_rate_v_s",
        path,
        minimum=1e-9,
        maximum=1000,
        exclusive_minimum=True,
    )
    sample_interval = _number(
        collector,
        params,
        "sample_interval_v",
        path,
        minimum=1e-9,
        maximum=10,
        exclusive_minimum=True,
    )
    quiet_time = _number(
        collector,
        params,
        "quiet_time_s",
        path,
        required=False,
        default=0,
        minimum=0,
        maximum=TIME_MAX_S,
    )
    auto_sensitivity, sensitivity = _sensitivity(collector, params, path)

    has_cycles = "cycles" in params
    has_segments = "segments" in params
    if has_cycles == has_segments:
        collector.add(
            path,
            "exclusive_fields",
            "cycles 与 segments 必须且只能填写一个。",
        )
        segments = None
        cycles = None
    elif has_cycles:
        cycles = _integer(
            collector,
            params,
            "cycles",
            path,
            minimum=1,
            maximum=10_000,
        )
        segments = cycles * 2 if cycles is not None else None
    else:
        cycles = None
        segments = _integer(
            collector,
            params,
            "segments",
            path,
            minimum=1,
            maximum=20_000,
        )

    if high is not None and low is not None and high <= low:
        collector.add(path, "potential_order", "high_v 必须大于 low_v。")
    if (
        initial is not None
        and high is not None
        and low is not None
        and not low <= initial <= high
    ):
        collector.add(
            f"{path}.initial_v",
            "potential_window",
            "初始电位必须位于高低电位之间。",
        )
    if (
        cycles is not None
        and initial is not None
        and high is not None
        and low is not None
        and direction is not None
    ):
        endpoint_is_clear = (
            direction == "n" and math.isclose(initial, high, abs_tol=1e-12)
        ) or (
            direction == "p" and math.isclose(initial, low, abs_tol=1e-12)
        )
        if not endpoint_is_clear:
            collector.add(
                f"{path}.cycles",
                "ambiguous_cycle_conversion",
                "只有初始电位位于扫描端点且方向明确时才能自动把圈数换算为扫描段数；请改填 segments。",
            )

    required_values = (
        initial,
        high,
        low,
        direction,
        scan_rate,
        segments,
        sample_interval,
        quiet_time,
        auto_sensitivity,
    )
    if any(value is None for value in required_values):
        return None
    span = high - low
    first_span = high - initial if direction == "p" else initial - low
    travel = first_span + max(segments - 1, 0) * span
    expected_seconds = quiet_time + travel / scan_rate
    normalized: dict[str, Any] = {
        "initial_v": initial,
        "high_v": high,
        "low_v": low,
        "direction": direction,
        "scan_rate_v_s": scan_rate,
        "segments": segments,
        "sample_interval_v": sample_interval,
        "quiet_time_s": quiet_time,
        "auto_sensitivity": auto_sensitivity,
    }
    if cycles is not None:
        normalized["cycles"] = cycles
    if sensitivity is not None and auto_sensitivity is False:
        normalized["sensitivity_a_v"] = sensitivity
    return normalized, expected_seconds, ()


def _ocp_step(
    collector: _Collector,
    params: dict[str, Any],
    path: str,
) -> tuple[dict[str, Any], float, tuple[str, ...]] | None:
    allowed = {
        "duration_s",
        "sample_interval_s",
        "quiet_time_s",
        "upper_limit_v",
        "lower_limit_v",
    }
    collector.unknown_fields(params, allowed, path)
    duration = _number(
        collector,
        params,
        "duration_s",
        path,
        minimum=0,
        maximum=TIME_MAX_S,
        exclusive_minimum=True,
    )
    sample_interval = _number(
        collector,
        params,
        "sample_interval_s",
        path,
        minimum=0,
        maximum=TIME_MAX_S,
        exclusive_minimum=True,
    )
    quiet_time = _number(
        collector,
        params,
        "quiet_time_s",
        path,
        required=False,
        default=0,
        minimum=0,
        maximum=TIME_MAX_S,
    )
    upper = _number(
        collector,
        params,
        "upper_limit_v",
        path,
        required=False,
        default=10,
        minimum=POTENTIAL_MIN_V,
        maximum=POTENTIAL_MAX_V,
    )
    lower = _number(
        collector,
        params,
        "lower_limit_v",
        path,
        required=False,
        default=-10,
        minimum=POTENTIAL_MIN_V,
        maximum=POTENTIAL_MAX_V,
    )
    if duration is not None and sample_interval is not None and sample_interval > duration:
        collector.add(
            f"{path}.sample_interval_s",
            "range",
            "采样间隔不能大于测试时长。",
        )
    if upper is not None and lower is not None and upper <= lower:
        collector.add(path, "potential_order", "upper_limit_v 必须大于 lower_limit_v。")
    required_values = (duration, sample_interval, quiet_time, upper, lower)
    if any(value is None for value in required_values):
        return None
    warnings: tuple[str, ...] = ()
    if "upper_limit_v" not in params or "lower_limit_v" not in params:
        warnings = (
            "OCP 电位记录上下限使用 +10/-10 V 默认值；运行前应在 CHI 参数页复核。",
        )
    normalized = {
        "duration_s": duration,
        "sample_interval_s": sample_interval,
        "quiet_time_s": quiet_time,
        "upper_limit_v": upper,
        "lower_limit_v": lower,
    }
    return normalized, duration + quiet_time, warnings


def _lsv_step(
    collector: _Collector,
    params: dict[str, Any],
    path: str,
) -> tuple[dict[str, Any], float, tuple[str, ...]] | None:
    allowed = {
        "initial_v",
        "final_v",
        "scan_rate_v_s",
        "sample_interval_v",
        "quiet_time_s",
        "auto_sensitivity",
        "sensitivity_a_v",
    }
    collector.unknown_fields(params, allowed, path)
    initial = _number(
        collector,
        params,
        "initial_v",
        path,
        minimum=POTENTIAL_MIN_V,
        maximum=POTENTIAL_MAX_V,
    )
    final = _number(
        collector,
        params,
        "final_v",
        path,
        minimum=POTENTIAL_MIN_V,
        maximum=POTENTIAL_MAX_V,
    )
    scan_rate = _number(
        collector,
        params,
        "scan_rate_v_s",
        path,
        minimum=1e-9,
        maximum=1000,
        exclusive_minimum=True,
    )
    sample_interval = _number(
        collector,
        params,
        "sample_interval_v",
        path,
        minimum=1e-9,
        maximum=10,
        exclusive_minimum=True,
    )
    quiet_time = _number(
        collector,
        params,
        "quiet_time_s",
        path,
        required=False,
        default=0,
        minimum=0,
        maximum=TIME_MAX_S,
    )
    auto_sensitivity, sensitivity = _sensitivity(collector, params, path)
    if (
        initial is not None
        and final is not None
        and math.isclose(initial, final, abs_tol=1e-12)
    ):
        collector.add(f"{path}.final_v", "zero_span", "终止电位不能等于初始电位。")
    required_values = (
        initial,
        final,
        scan_rate,
        sample_interval,
        quiet_time,
        auto_sensitivity,
    )
    if any(value is None for value in required_values):
        return None
    normalized: dict[str, Any] = {
        "initial_v": initial,
        "final_v": final,
        "scan_rate_v_s": scan_rate,
        "sample_interval_v": sample_interval,
        "quiet_time_s": quiet_time,
        "auto_sensitivity": auto_sensitivity,
    }
    if sensitivity is not None and auto_sensitivity is False:
        normalized["sensitivity_a_v"] = sensitivity
    expected_seconds = quiet_time + abs(final - initial) / scan_rate
    return normalized, expected_seconds, ()


def _eis_step(
    collector: _Collector,
    params: dict[str, Any],
    path: str,
) -> tuple[dict[str, Any], None, tuple[str, ...]] | None:
    allowed = {
        "bias_mode",
        "dc_potential_v",
        "high_frequency_hz",
        "low_frequency_hz",
        "amplitude_v",
        "quiet_time_s",
    }
    collector.unknown_fields(params, allowed, path)
    bias_mode = _choice(
        collector,
        params,
        "bias_mode",
        path,
        {"ocp", "potential"},
    )
    dc_potential: float | None = None
    if bias_mode == "potential":
        dc_potential = _number(
            collector,
            params,
            "dc_potential_v",
            path,
            minimum=POTENTIAL_MIN_V,
            maximum=POTENTIAL_MAX_V,
        )
    elif bias_mode == "ocp" and "dc_potential_v" in params:
        collector.add(
            f"{path}.dc_potential_v",
            "conflict",
            "OCP 偏置模式不能同时指定固定直流电位。",
        )
    high_frequency = _number(
        collector,
        params,
        "high_frequency_hz",
        path,
        minimum=0,
        maximum=1e9,
        exclusive_minimum=True,
    )
    low_frequency = _number(
        collector,
        params,
        "low_frequency_hz",
        path,
        minimum=0,
        maximum=1e9,
        exclusive_minimum=True,
    )
    amplitude = _number(
        collector,
        params,
        "amplitude_v",
        path,
        minimum=0,
        maximum=10,
        exclusive_minimum=True,
    )
    quiet_time = _number(
        collector,
        params,
        "quiet_time_s",
        path,
        required=False,
        default=0,
        minimum=0,
        maximum=TIME_MAX_S,
    )
    if (
        high_frequency is not None
        and low_frequency is not None
        and high_frequency <= low_frequency
    ):
        collector.add(
            path,
            "frequency_order",
            "high_frequency_hz 必须大于 low_frequency_hz。",
        )
    required_values = (
        bias_mode,
        high_frequency,
        low_frequency,
        amplitude,
        quiet_time,
    )
    if bias_mode == "potential":
        required_values = (*required_values, dc_potential)
    if any(value is None for value in required_values):
        return None
    normalized: dict[str, Any] = {
        "bias_mode": bias_mode,
        "high_frequency_hz": high_frequency,
        "low_frequency_hz": low_frequency,
        "amplitude_v": amplitude,
        "quiet_time_s": quiet_time,
    }
    if dc_potential is not None:
        normalized["dc_potential_v"] = dc_potential
    warnings = (
        "EIS Points/Decade 不在当前已确认的宏参数中；运行前必须在 CHI IMP 参数页人工复核。",
        "EIS 时长受低频周期、稳定过程和自动量程影响，不生成虚假的精确预计时间。",
    )
    return normalized, None, warnings


STEP_NORMALIZERS = {
    "cv": _cv_step,
    "ocpt": _ocp_step,
    "lsv": _lsv_step,
    "eis": _eis_step,
}


def _normalize_step(
    collector: _Collector,
    raw: Any,
    index: int,
) -> NormalizedStep | None:
    path = f"steps[{index}]"
    if not isinstance(raw, dict):
        collector.add(path, "type", "工步必须是 JSON 对象。")
        return None
    collector.unknown_fields(raw, STEP_KEYS, path)
    step_id = _string(collector, raw, "id", path, maximum=64)
    if step_id is not None and not STEP_ID_PATTERN.fullmatch(step_id):
        collector.add(
            f"{path}.id",
            "format",
            "只允许小写字母、数字和连字符，最长 64 位。",
        )
    name = _string(collector, raw, "name", path, maximum=120)
    raw_technique = _string(collector, raw, "technique", path, maximum=20)
    technique = (
        TECHNIQUE_ALIASES.get(raw_technique.lower())
        if raw_technique is not None
        else None
    )
    if raw_technique is not None and technique is None:
        collector.add(
            f"{path}.technique",
            "unsupported_technique",
            "当前离线编译器只支持 CV、OCP、LSV 和 EIS。",
        )
    enabled = _boolean(collector, raw, "enabled", path, default=True)
    save_basename = _string(
        collector,
        raw,
        "save_basename",
        path,
        maximum=64,
    )
    if (
        save_basename is not None
        and not SAVE_BASENAME_PATTERN.fullmatch(save_basename)
    ):
        collector.add(
            f"{path}.save_basename",
            "format",
            "只允许 ASCII 字母、数字、下划线和连字符，最长 64 位。",
        )
    params = raw.get("params")
    if not isinstance(params, dict):
        collector.add(f"{path}.params", "type", "params 必须是 JSON 对象。")
        params = None

    normalized_params = None
    expected_seconds = None
    warnings: tuple[str, ...] = ()
    if technique is not None and params is not None:
        normalized = STEP_NORMALIZERS[technique](
            collector,
            params,
            f"{path}.params",
        )
        if normalized is not None:
            normalized_params, expected_seconds, warnings = normalized
    if any(
        value is None
        for value in (
            step_id,
            name,
            technique,
            enabled,
            save_basename,
            normalized_params,
        )
    ):
        return None
    return NormalizedStep(
        id=step_id,
        name=name,
        technique=technique,
        enabled=enabled,
        save_basename=save_basename,
        params=normalized_params,
        expected_seconds=expected_seconds,
        warnings=warnings,
    )


def normalize_protocol(raw: Any) -> NormalizedProtocol:
    """Normalize an offline CHI protocol without creating files or starting software."""
    collector = _Collector()
    if not isinstance(raw, dict):
        raise ProtocolValidationError(
            [ValidationIssue("$", "type", "协议顶层必须是 JSON 对象。")]
        )
    collector.unknown_fields(raw, PROTOCOL_KEYS, "")
    schema_version = raw.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != PROTOCOL_SCHEMA_VERSION
    ):
        collector.add(
            "schema_version",
            "schema_version",
            f"必须是 {PROTOCOL_SCHEMA_VERSION}。",
        )
    name = _string(collector, raw, "name", "", maximum=160)
    execution_mode = _string(
        collector,
        raw,
        "execution_mode",
        "",
        required=False,
        default="single_macro",
        maximum=40,
    )
    if execution_mode is not None and execution_mode != "single_macro":
        collector.add(
            "execution_mode",
            "unsupported_execution_mode",
            "阶段 A 只允许 single_macro 离线编译。",
        )

    metadata: dict[str, Any] = {}
    for key, maximum in METADATA_STRING_LIMITS.items():
        value = _string(
            collector,
            raw,
            key,
            "",
            required=False,
            default="",
            maximum=maximum,
        )
        if value:
            metadata[key] = value
    if "area_cm2" in raw:
        area = _number(
            collector,
            raw,
            "area_cm2",
            "",
            minimum=0,
            maximum=1_000_000,
            exclusive_minimum=True,
        )
        if area is not None:
            metadata["area_cm2"] = area

    raw_steps = raw.get("steps")
    normalized_steps: list[NormalizedStep] = []
    if not isinstance(raw_steps, list):
        collector.add("steps", "type", "steps 必须是数组。")
    elif not raw_steps:
        collector.add("steps", "empty", "至少需要一个工步。")
    elif len(raw_steps) > 100:
        collector.add("steps", "too_many", "单个协议最多允许 100 个工步。")
    else:
        for index, raw_step in enumerate(raw_steps):
            step = _normalize_step(collector, raw_step, index)
            if step is not None:
                normalized_steps.append(step)

    seen_ids: dict[str, int] = {}
    seen_outputs: dict[str, int] = {}
    for index, step in enumerate(normalized_steps):
        if step.id in seen_ids:
            collector.add(
                f"steps[{index}].id",
                "duplicate",
                "工步 id 不能重复。",
            )
        else:
            seen_ids[step.id] = index
        output_key = step.save_basename.casefold()
        if output_key in seen_outputs:
            collector.add(
                f"steps[{index}].save_basename",
                "duplicate",
                "工步输出文件名不区分大小写，不能重复。",
            )
        else:
            seen_outputs[output_key] = index
    if normalized_steps and not any(step.enabled for step in normalized_steps):
        collector.add("steps", "no_active_steps", "至少要启用一个工步。")

    if collector.issues:
        raise ProtocolValidationError(collector.issues)
    assert schema_version == PROTOCOL_SCHEMA_VERSION
    assert name is not None
    assert execution_mode == "single_macro"
    return NormalizedProtocol(
        schema_version=schema_version,
        name=name,
        execution_mode=execution_mode,
        metadata=metadata,
        steps=tuple(normalized_steps),
    )
