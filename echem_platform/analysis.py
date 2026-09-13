from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .parsers import ParsedTable, normalized_header, unit_from_header


ANALYSIS_SCHEMA_VERSION = "1"
EIS_ALGORITHM_ID = "eis.real-axis-zero-crossing"
EIS_ALGORITHM_VERSION = "1.1.0"
CV_ALGORITHM_ID = "cv.rhe-ir-target-current"
CV_ALGORITHM_VERSION = "1.1.0"
NERNST_RHE_SLOPE_V_PER_PH_25C = 0.05916


class AnalysisValidationError(ValueError):
    """A parameter/source validation error safe to return through the API."""

    def __init__(
        self,
        message: str,
        *,
        field: str = "",
        code: str = "analysis_validation_error",
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.field = field
        self.code = code
        self.details = dict(details or {})

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
        }
        if self.field:
            payload["field"] = self.field
        if self.details:
            payload["details"] = self.details
        return payload


def _normalized_parameters(
    parameters: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if parameters is None:
        return {}
    if not isinstance(parameters, Mapping):
        raise AnalysisValidationError(
            "分析参数必须是 JSON 对象。",
            field="parameters",
            code="invalid_parameters",
        )
    return dict(parameters)


def _required_text(parameters: Mapping[str, Any], field: str) -> str:
    value = parameters.get(field)
    if not isinstance(value, str) or not value.strip():
        raise AnalysisValidationError(
            f"{field} 为必填项。",
            field=field,
            code="missing_parameter",
        )
    return value.strip()


def _number(
    parameters: Mapping[str, Any],
    field: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    exclusive_minimum: bool = False,
) -> float:
    value = parameters.get(field)
    if isinstance(value, bool):
        value = None
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise AnalysisValidationError(
            f"{field} 必须是有限数值。",
            field=field,
            code="invalid_parameter",
        ) from None
    if not math.isfinite(number):
        raise AnalysisValidationError(
            f"{field} 必须是有限数值。",
            field=field,
            code="invalid_parameter",
        )
    if minimum is not None:
        outside = number <= minimum if exclusive_minimum else number < minimum
        if outside:
            comparator = "大于" if exclusive_minimum else "不小于"
            raise AnalysisValidationError(
                f"{field} 必须{comparator} {minimum:g}。",
                field=field,
                code="parameter_out_of_range",
            )
    if maximum is not None and number > maximum:
        raise AnalysisValidationError(
            f"{field} 必须不大于 {maximum:g}。",
            field=field,
            code="parameter_out_of_range",
        )
    return number


def _optional_number(
    parameters: Mapping[str, Any],
    field: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    exclusive_minimum: bool = False,
) -> float | None:
    value = parameters.get(field)
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return _number(
        parameters,
        field,
        minimum=minimum,
        maximum=maximum,
        exclusive_minimum=exclusive_minimum,
    )


def _validate_table(table: ParsedTable, expected_technique: str) -> None:
    if not isinstance(table, ParsedTable):
        raise AnalysisValidationError(
            "分析输入不是完整数值表。",
            field="source",
            code="invalid_source",
        )
    if table.status != "parsed" or not table.rows:
        raise AnalysisValidationError(
            table.error or "源文件没有可分析的完整数值表。",
            field="source",
            code="source_not_parsed",
        )
    if table.technique != expected_technique:
        raise AnalysisValidationError(
            f"当前文件识别为 {table.technique}，不能执行 {expected_technique} 分析。",
            field="analysis_type",
            code="technique_mismatch",
            details={
                "detected_technique": table.technique,
                "expected_technique": expected_technique,
            },
        )


def _first_column(
    headers: list[str],
    patterns: Iterable[str],
    *,
    excluded: set[int] | None = None,
) -> int | None:
    excluded = excluded or set()
    normalized = [normalized_header(header) for header in headers]
    for index, header in enumerate(normalized):
        if index in excluded:
            continue
        if any(pattern in header for pattern in patterns):
            return index
    return None


def _eis_columns(headers: list[str]) -> tuple[int, int, int]:
    frequency_index = _first_column(
        headers,
        ("frequency", "freq", "f(hz", "f/"),
    )
    imag_index = _first_column(
        headers,
        ("zimag", 'z"', "z''", "z”", "z″", "zdoubleprime", "imz", "zim"),
    )
    real_index = _first_column(
        headers,
        ("zreal", "z'", "z’", "z′", "zprime", "rez", "zre"),
        excluded={imag_index} if imag_index is not None else None,
    )
    missing = [
        label
        for label, index in (
            ("frequency", frequency_index),
            ("z_real", real_index),
            ("z_imaginary", imag_index),
        )
        if index is None
    ]
    if missing:
        raise AnalysisValidationError(
            "EIS 分析需要频率、实部和虚部三列。",
            field="source",
            code="missing_columns",
            details={"missing": missing, "headers": headers},
        )
    return frequency_index, real_index, imag_index  # type: ignore[return-value]


def _unit_probe(header: str) -> str:
    return (
        header
        .lower()
        .replace("μ", "µ")
        .replace("ω", "ohm")
        .replace("−", "-")
        .replace("⁻", "-")
        .replace("²", "2")
        .replace("^", "")
        .replace("·", ".")
        .replace(" ", "")
    )


def _frequency_factor(header: str) -> float:
    raw_unit = unit_from_header(header).replace(" ", "")
    if raw_unit == "MHz":
        return 1_000_000.0
    probe = raw_unit.lower()
    if probe == "khz":
        return 1_000.0
    if probe == "mhz":
        return 0.001
    if probe == "hz":
        return 1.0
    raise AnalysisValidationError(
        "无法确认频率列单位；仅支持 Hz、kHz 或 mHz。",
        field="source",
        code="unsupported_unit",
        details={"header": header},
    )


def _resistance_spec(header: str) -> tuple[float, str]:
    probe = _unit_probe(header)
    if not any(token in probe for token in ("ohm", "ω")):
        raise AnalysisValidationError(
            "无法确认阻抗列单位；需要 Ω、mΩ、kΩ 或相应的面积归一化单位。",
            field="source",
            code="unsupported_unit",
            details={"header": header},
        )
    raw_unit = unit_from_header(header).replace(" ", "")
    normalized_raw = (
        raw_unit.replace("Ω", "Ohm")
        .replace("ω", "Ohm")
        .replace("·", ".")
    )
    if normalized_raw.startswith("MOhm"):
        factor = 1_000_000.0
    elif normalized_raw.lower().startswith("kohm"):
        factor = 1_000.0
    elif normalized_raw.startswith("mOhm"):
        factor = 0.001
    else:
        factor = 1.0
    area_normalized = any(
        token in probe
        for token in ("cm2", "cm²", "cm-2", "/cm2", ".cm2", ".cm²")
    )
    return factor, ("Ω·cm²" if area_normalized else "Ω")


def _deduplicate_crossings(
    crossings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for crossing in crossings:
        if result:
            previous = result[-1]
            same_frequency = math.isclose(
                float(previous["frequency_hz"]),
                float(crossing["frequency_hz"]),
                rel_tol=1e-12,
                abs_tol=1e-15,
            )
            same_intercept = math.isclose(
                float(previous["real_axis_intercept"]),
                float(crossing["real_axis_intercept"]),
                rel_tol=1e-12,
                abs_tol=1e-15,
            )
            if same_frequency and same_intercept:
                continue
        result.append(crossing)
    return result


def _eis_zero_crossings(
    points: list[tuple[float, float, float, int]],
) -> list[dict[str, Any]]:
    crossings: list[dict[str, Any]] = []
    for position, (frequency, real, imaginary, source_row) in enumerate(points):
        if imaginary == 0.0:
            crossings.append(
                {
                    "frequency_hz": frequency,
                    "real_axis_intercept": real,
                    "source_rows": [source_row],
                    "interpolation_fraction": 0.0,
                }
            )
        if position + 1 >= len(points):
            continue
        next_frequency, next_real, next_imaginary, next_source_row = points[position + 1]
        if imaginary == 0.0 or next_imaginary == 0.0:
            continue
        if (imaginary < 0.0) == (next_imaginary < 0.0):
            continue
        fraction = -imaginary / (next_imaginary - imaginary)
        if not 0.0 < fraction < 1.0:
            continue
        real_intercept = real + fraction * (next_real - real)
        log_frequency = math.log10(frequency) + fraction * (
            math.log10(next_frequency) - math.log10(frequency)
        )
        crossings.append(
            {
                "frequency_hz": 10.0**log_frequency,
                "real_axis_intercept": real_intercept,
                "source_rows": [source_row, next_source_row],
                "interpolation_fraction": fraction,
            }
        )
    return _deduplicate_crossings(crossings)


def calculate_eis_resistance(
    table: ParsedTable,
    parameters: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Estimate real-axis intercepts from measured EIS zero crossings.

    The function never extrapolates beyond the measured imaginary-impedance
    sign changes and deliberately does not claim an equivalent-circuit fit.
    """

    _validate_table(table, "EIS")
    normalized_parameters = _normalized_parameters(parameters)
    frequency_index, real_index, imag_index = _eis_columns(table.headers)
    frequency_factor = _frequency_factor(table.headers[frequency_index])
    real_factor, real_unit = _resistance_spec(table.headers[real_index])
    imag_factor, imag_unit = _resistance_spec(table.headers[imag_index])
    if real_unit != imag_unit:
        raise AnalysisValidationError(
            "阻抗实部与虚部单位不一致。",
            field="source",
            code="unit_mismatch",
            details={"real_unit": real_unit, "imaginary_unit": imag_unit},
        )

    points: list[tuple[float, float, float, int]] = []
    for row_number, row in enumerate(table.rows, start=1):
        frequency = row[frequency_index]
        real = row[real_index]
        imaginary = row[imag_index]
        if frequency is None or real is None or imaginary is None:
            continue
        frequency_hz = frequency * frequency_factor
        if frequency_hz <= 0.0:
            continue
        points.append(
            (
                frequency_hz,
                real * real_factor,
                imaginary * imag_factor,
                row_number,
            )
        )
    points.sort(key=lambda item: item[0], reverse=True)
    if len(points) < 2:
        raise AnalysisValidationError(
            "EIS 分析至少需要两个频率不同的完整阻抗点。",
            field="source",
            code="insufficient_points",
        )

    crossings = _eis_zero_crossings(points)
    warnings: list[str] = []
    solution_resistance: float | None = None
    low_frequency_intercept: float | None = None
    apparent_polarization_resistance: float | None = None
    if not crossings:
        warnings.append(
            "测量频率范围内虚部没有穿过零轴；未外推溶液电阻，请扩展高频范围或检查符号约定。"
        )
    else:
        crossings[0]["kind"] = "high_frequency"
        high_frequency_intercept = float(crossings[0]["real_axis_intercept"])
        if high_frequency_intercept < 0.0:
            crossings[0]["kind"] = "high_frequency_invalid"
            warnings.append(
                "高频实轴交点为负值，未报告溶液电阻；请检查开短路校准、接线和高频数据质量。"
            )
        else:
            solution_resistance = high_frequency_intercept
        if len(crossings) >= 2 and solution_resistance is not None:
            crossings[-1]["kind"] = "low_frequency"
            low_frequency_intercept = float(crossings[-1]["real_axis_intercept"])
            diameter = low_frequency_intercept - solution_resistance
            if diameter >= 0.0:
                apparent_polarization_resistance = diameter
            else:
                warnings.append(
                    "低频截距小于高频截距，未报告表观极化电阻；请检查数据顺序和符号。"
                )
        elif len(crossings) < 2:
            warnings.append(
                "只检测到一个实轴交点；未报告低频截距和表观极化电阻。"
            )
        if len(crossings) > 2:
            for crossing in crossings[1:-1]:
                crossing["kind"] = "intermediate"
            warnings.append(
                "检测到多个实轴交点；仅将最高频和最低频交点用于筛查性截距结果。"
            )

    warnings.append(
        "截距结果是零交叉线性插值的筛查值，不是等效电路拟合，也不应直接标记为正式 Rct。"
    )
    if solution_resistance is None:
        quality = {
            "level": "not_calculable",
            "label": "不可计算",
            "reasons": [
                "测量频率范围内没有得到有效的高频实轴交点，无法计算溶液电阻 Rs。"
            ],
        }
    else:
        quality = {
            "level": "screening",
            "label": "仅筛查",
            "reasons": [
                "当前结果来自实轴零交叉插值，不是等效电路拟合，仅可作为筛查值。"
            ],
        }
    result = {
        "quality": quality,
        "resistance_unit": real_unit,
        "point_count": len(points),
        "frequency_range_hz": {
            "minimum": points[-1][0],
            "maximum": points[0][0],
        },
        "crossings": crossings,
        "solution_resistance": solution_resistance,
        "low_frequency_intercept": low_frequency_intercept,
        "apparent_polarization_resistance": apparent_polarization_resistance,
        "extrapolated": False,
    }
    return {
        "analysis_type": "eis_resistance",
        "schema_version": ANALYSIS_SCHEMA_VERSION,
        "algorithm_id": EIS_ALGORITHM_ID,
        "algorithm_version": EIS_ALGORITHM_VERSION,
        "parameters": normalized_parameters,
        "result": result,
        "warnings": warnings,
    }


@dataclass(frozen=True)
class _CVPoint:
    source_row: int
    potential_v: float
    current_a: float
    current_density_ma_cm2: float


def _cv_columns(headers: list[str]) -> tuple[int, int]:
    potential_index = _first_column(
        headers,
        ("potential", "voltage", "e(v", "e/v"),
    )
    current_index = _first_column(
        headers,
        (
            "currentdensity",
            "current",
            "j(ma",
            "j/a",
            "i(a",
            "i/ma",
            "i/ua",
            "i/µa",
            "i/a",
        ),
        excluded={potential_index} if potential_index is not None else None,
    )
    missing = [
        label
        for label, index in (
            ("potential", potential_index),
            ("current", current_index),
        )
        if index is None
    ]
    if missing:
        raise AnalysisValidationError(
            "CV 分析需要电位和电流（或电流密度）两列。",
            field="source",
            code="missing_columns",
            details={"missing": missing, "headers": headers},
        )
    return potential_index, current_index  # type: ignore[return-value]


def _potential_factor_to_v(header: str) -> float:
    unit = (
        unit_from_header(header)
        .strip()
        .lower()
        .replace(" ", "")
    )
    probe = _unit_probe(header)
    if unit.startswith("mv") or re.search(r"(^|[(/])mv([)/]|$)", probe):
        return 0.001
    if (
        unit.startswith("v")
        or re.search(r"(^|[(/])v([)/]|$)", probe)
        or probe.endswith("/v")
    ):
        return 1.0
    raise AnalysisValidationError(
        "无法确认电位列单位；仅支持 V 或 mV。",
        field="source",
        code="unsupported_unit",
        details={"header": header},
    )


def _current_scale(header: str) -> tuple[str, float]:
    """Return (kind, factor); total -> A, density -> mA/cm²."""

    probe = _unit_probe(header)
    unit = (
        unit_from_header(header)
        .strip()
        .lower()
        .replace("μ", "µ")
        .replace(" ", "")
    )
    is_density_cm2 = any(
        token in probe
        for token in (
            "/cm2",
            "/cm²",
            "cm-2",
            "cm²",
            "cm2",
            "ma.cm",
            "a.cm",
            "µa.cm",
        )
    )
    is_density_m2 = any(
        token in probe
        for token in ("/m2", "/m²", "m-2", "a.m2", "ma.m2", "µa.m2")
    ) and not is_density_cm2
    if unit.startswith(("µa", "ua")) or "µa" in probe or "ua" in probe:
        if is_density_cm2:
            return "density", 0.001
        if is_density_m2:
            return "density", 1e-7
        return "total", 1e-6
    if unit.startswith("ma") or "ma" in probe:
        if is_density_cm2:
            return "density", 1.0
        if is_density_m2:
            return "density", 0.0001
        return "total", 0.001
    # Test ampere last because "ma" and "µa" also contain the letter a.
    if (
        unit.startswith("a")
        or re.search(r"(^|[(/])a([./)]|$)", probe)
        or probe.endswith("/a")
    ):
        if is_density_cm2:
            return "density", 1_000.0
        if is_density_m2:
            return "density", 0.1
        return "total", 1.0
    raise AnalysisValidationError(
        "无法确认电流列单位；仅支持 A、mA、µA 及每平方厘米或每平方米电流密度。",
        field="source",
        code="unsupported_unit",
        details={"header": header},
    )


def _segment_cv(points: list[_CVPoint]) -> list[list[_CVPoint]]:
    if len(points) < 2:
        return []
    potential_span = max(point.potential_v for point in points) - min(
        point.potential_v for point in points
    )
    tolerance = max(1e-12, potential_span * 1e-12)
    segments: list[list[_CVPoint]] = []
    start = 0
    direction = 0
    for index in range(1, len(points)):
        delta = points[index].potential_v - points[index - 1].potential_v
        next_direction = 1 if delta > tolerance else -1 if delta < -tolerance else 0
        if next_direction == 0:
            continue
        if direction == 0:
            direction = next_direction
            continue
        if next_direction != direction:
            segment = points[start:index]
            if len(segment) >= 2:
                segments.append(segment)
            start = index - 1
            direction = next_direction
    final_segment = points[start:]
    if len(final_segment) >= 2:
        segments.append(final_segment)
    return segments


def _branch_options(segments: list[list[_CVPoint]]) -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []
    for index, segment in enumerate(segments, start=1):
        segment_id = f"segment_{index}"
        start_potential = segment[0].potential_v
        end_potential = segment[-1].potential_v
        direction = (
            "increasing"
            if end_potential > start_potential
            else "decreasing"
        )
        direction_label = "电位递增" if direction == "increasing" else "电位递减"
        options.append(
            {
                "id": segment_id,
                "value": segment_id,
                "direction": direction,
                "label": (
                    f"段{index}：{start_potential:.6g}→{end_potential:.6g} V"
                    f"（{direction_label}）"
                ),
                "source_row_start": segment[0].source_row,
                "source_row_end": segment[-1].source_row,
                "point_count": len(segment),
                "potential_range_v": {
                    "start": segment[0].potential_v,
                    "end": segment[-1].potential_v,
                    "minimum": min(point.potential_v for point in segment),
                    "maximum": max(point.potential_v for point in segment),
                },
                "current_density_range_ma_cm2": {
                    "minimum": min(
                        point.current_density_ma_cm2 for point in segment
                    ),
                    "maximum": max(
                        point.current_density_ma_cm2 for point in segment
                    ),
                },
            }
        )
    return options


def describe_cv_branches(
    table: ParsedTable,
    *,
    area_cm2: float,
) -> list[dict[str, Any]]:
    """Return deterministic branch choices without calculating overpotential."""

    _validate_table(table, "CV")
    if not math.isfinite(area_cm2) or area_cm2 <= 0.0:
        raise AnalysisValidationError(
            "area_cm2 必须大于 0。",
            field="area_cm2",
            code="parameter_out_of_range",
        )
    points = _cv_points(table, area_cm2)
    return _branch_options(_segment_cv(points))


def _cv_points(table: ParsedTable, area_cm2: float) -> list[_CVPoint]:
    potential_index, current_index = _cv_columns(table.headers)
    potential_factor = _potential_factor_to_v(table.headers[potential_index])
    current_kind, current_factor = _current_scale(table.headers[current_index])
    points: list[_CVPoint] = []
    for source_row, row in enumerate(table.rows, start=1):
        potential = row[potential_index]
        current = row[current_index]
        if potential is None or current is None:
            continue
        potential_v = potential * potential_factor
        if current_kind == "total":
            current_a = current * current_factor
            current_density = current_a * 1_000.0 / area_cm2
        else:
            current_density = current * current_factor
            current_a = current_density * area_cm2 / 1_000.0
        points.append(
            _CVPoint(
                source_row=source_row,
                potential_v=potential_v,
                current_a=current_a,
                current_density_ma_cm2=current_density,
            )
        )
    if len(points) < 2:
        raise AnalysisValidationError(
            "CV 分析至少需要两个完整的电位—电流点。",
            field="source",
            code="insufficient_points",
        )
    return points


def _select_branch(
    segments: list[list[_CVPoint]],
    requested: str,
) -> tuple[list[_CVPoint], dict[str, Any], list[dict[str, Any]]]:
    options = _branch_options(segments)
    if not options:
        raise AnalysisValidationError(
            "无法从电位方向识别 CV 扫描分支。",
            field="scan_branch",
            code="branch_not_detected",
        )
    if requested == "auto":
        if len(options) != 1:
            raise AnalysisValidationError(
                "文件包含多个扫描分支，请明确选择一个分支。",
                field="scan_branch",
                code="branch_required",
                details={"branch_options": options},
            )
        return segments[0], options[0], options

    aliases: dict[str, str] = {}
    for option in options:
        aliases[str(option["id"])] = str(option["id"])
        aliases[str(option["value"])] = str(option["id"])
    if len(options) == 2:
        aliases["forward"] = "segment_1"
        aliases["reverse"] = "segment_2"
    selected_id = aliases.get(requested)
    if selected_id is None:
        raise AnalysisValidationError(
            "scan_branch 与源文件可用分支不匹配。",
            field="scan_branch",
            code="invalid_branch",
            details={"requested": requested, "branch_options": options},
        )
    selected_index = int(selected_id.rsplit("_", 1)[-1]) - 1
    return segments[selected_index], options[selected_index], options


def _target_crossings(
    branch: list[_CVPoint],
    target_current_density: float,
) -> list[tuple[float, float, int, int, float]]:
    """Return potential, current, row1, row2, interpolation fraction."""

    crossings: list[tuple[float, float, int, int, float]] = []
    for index, point in enumerate(branch):
        difference = point.current_density_ma_cm2 - target_current_density
        if math.isclose(difference, 0.0, rel_tol=1e-12, abs_tol=1e-12):
            crossings.append(
                (
                    point.potential_v,
                    point.current_a,
                    point.source_row,
                    point.source_row,
                    0.0,
                )
            )
        if index + 1 >= len(branch):
            continue
        next_point = branch[index + 1]
        next_difference = (
            next_point.current_density_ma_cm2 - target_current_density
        )
        if difference == 0.0 or next_difference == 0.0:
            continue
        if (difference < 0.0) == (next_difference < 0.0):
            continue
        denominator = (
            next_point.current_density_ma_cm2
            - point.current_density_ma_cm2
        )
        if denominator == 0.0:
            continue
        fraction = (
            target_current_density - point.current_density_ma_cm2
        ) / denominator
        if not 0.0 < fraction < 1.0:
            continue
        potential = point.potential_v + fraction * (
            next_point.potential_v - point.potential_v
        )
        current = point.current_a + fraction * (
            next_point.current_a - point.current_a
        )
        crossings.append(
            (
                potential,
                current,
                point.source_row,
                next_point.source_row,
                fraction,
            )
        )
    deduplicated: list[tuple[float, float, int, int, float]] = []
    for crossing in crossings:
        if deduplicated and math.isclose(
            crossing[0],
            deduplicated[-1][0],
            rel_tol=1e-12,
            abs_tol=1e-15,
        ):
            continue
        deduplicated.append(crossing)
    return deduplicated


def calculate_cv_overpotential(
    table: ParsedTable,
    parameters: Mapping[str, Any],
) -> dict[str, Any]:
    """Calculate target-current overpotential from one selected CV branch."""

    _validate_table(table, "CV")
    raw_parameters = _normalized_parameters(parameters)
    solution = _required_text(raw_parameters, "solution")
    ph = _number(raw_parameters, "ph", minimum=-2.0, maximum=16.0)
    reaction = _required_text(raw_parameters, "reaction").upper()
    if reaction not in {"HER", "OER"}:
        raise AnalysisValidationError(
            "reaction 仅支持 HER 或 OER。",
            field="reaction",
            code="invalid_parameter",
        )
    reference_electrode = _required_text(
        raw_parameters,
        "reference_electrode",
    )
    reference_offset_v = _number(raw_parameters, "reference_offset_v")
    reference_is_rhe = reference_electrode.strip().upper() == "RHE"
    if reference_is_rhe and not math.isclose(
        reference_offset_v,
        0.0,
        rel_tol=0.0,
        abs_tol=1e-12,
    ):
        raise AnalysisValidationError(
            "原始电位已是 RHE 时，reference_offset_v 必须为 0。",
            field="reference_offset_v",
            code="invalid_reference_conversion",
        )
    compensation_percent = _number(
        raw_parameters,
        "compensation_percent",
        minimum=0.0,
        maximum=100.0,
    )
    area_cm2 = _number(
        raw_parameters,
        "area_cm2",
        minimum=0.0,
        exclusive_minimum=True,
    )
    target_magnitude = _number(
        raw_parameters,
        "target_current_density_ma_cm2",
        minimum=0.0,
        exclusive_minimum=True,
    )
    scan_branch = _required_text(raw_parameters, "scan_branch").lower()
    online_compensation_status = _required_text(
        raw_parameters,
        "online_compensation_status",
    ).lower()
    allowed_online_statuses = {
        "not_compensated",
        "already_compensated",
        "unknown",
    }
    if online_compensation_status not in allowed_online_statuses:
        raise AnalysisValidationError(
            "online_compensation_status 仅支持 not_compensated、already_compensated 或 unknown。",
            field="online_compensation_status",
            code="invalid_parameter",
        )
    solution_resistance_ohm = (
        _optional_number(
            raw_parameters,
            "solution_resistance_ohm",
            minimum=0.0,
            exclusive_minimum=True,
        )
        if compensation_percent > 0.0
        else None
    )
    if compensation_percent > 0:
        if online_compensation_status == "already_compensated":
            raise AnalysisValidationError(
                "源数据已在线补偿，不能再次应用非零补偿因子。",
                field="compensation_percent",
                code="double_compensation",
            )
        if online_compensation_status == "unknown":
            raise AnalysisValidationError(
                "源数据在线补偿状态未知；确认未在线补偿前不能应用非零补偿因子。",
                field="compensation_percent",
                code="compensation_status_unknown",
            )
        if solution_resistance_ohm is None:
            raise AnalysisValidationError(
                "应用非零离线 iR 补偿时，solution_resistance_ohm 必须大于 0。",
                field="solution_resistance_ohm",
                code="missing_parameter",
            )

    normalized_parameters: dict[str, Any] = {
        "solution": solution,
        "ph": ph,
        "reaction": reaction,
        "reference_electrode": reference_electrode,
        "reference_offset_v": reference_offset_v,
        "reference_conversion_mode": (
            "already_rhe" if reference_is_rhe else "reference_vs_she_plus_ph"
        ),
        "compensation_percent": compensation_percent,
        "solution_resistance_ohm": solution_resistance_ohm,
        "area_cm2": area_cm2,
        "target_current_density_ma_cm2": target_magnitude,
        "scan_branch": scan_branch,
        "online_compensation_status": online_compensation_status,
        "temperature_c": 25.0,
    }

    points = _cv_points(table, area_cm2)
    segments = _segment_cv(points)
    branch, selected_branch, branch_options = _select_branch(
        segments,
        scan_branch,
    )
    # Persist the stable segment identifier even when an older client submits
    # the legacy forward/reverse alias.
    normalized_parameters["scan_branch"] = selected_branch["id"]
    target_signed = -target_magnitude if reaction == "HER" else target_magnitude
    crossings = _target_crossings(branch, target_signed)
    warnings: list[str] = []
    target_point: dict[str, Any] | None = None
    if not crossings:
        warnings.append(
            "所选分支未达到目标电流密度；未外推过电位，请调整目标或检查电极面积和电流单位。"
        )
    else:
        if len(crossings) > 1:
            warnings.append(
                "所选分支多次穿过目标电流密度；结果使用扫描顺序中的第一次交点。"
            )
        (
            measured_potential_v,
            interpolated_current_a,
            source_row_start,
            source_row_end,
            interpolation_fraction,
        ) = crossings[0]
        # Use the signed target and supplied geometric area for the correction.
        # This is algebraically identical to interpolating total current when
        # the source is internally consistent, while keeping the target traceable.
        target_current_a = target_signed * area_cm2 / 1_000.0
        reference_conversion_v = (
            0.0
            if reference_is_rhe
            else reference_offset_v + NERNST_RHE_SLOPE_V_PER_PH_25C * ph
        )
        potential_rhe_v = measured_potential_v + reference_conversion_v
        applied_ir_drop_v = (
            0.0
            if compensation_percent == 0.0
            else (
                compensation_percent
                / 100.0
                * target_current_a
                * float(solution_resistance_ohm)
            )
        )
        compensated_potential_v = potential_rhe_v - applied_ir_drop_v
        equilibrium_potential_v = 0.0 if reaction == "HER" else 1.229
        signed_overpotential_mv = (
            compensated_potential_v - equilibrium_potential_v
        ) * 1_000.0
        direction_mismatch = (
            reaction == "HER" and signed_overpotential_mv > 1e-9
        ) or (
            reaction == "OER" and signed_overpotential_mv < -1e-9
        )
        if direction_mismatch:
            expected_direction = "不大于 0" if reaction == "HER" else "不小于 0"
            raise AnalysisValidationError(
                (
                    f"{reaction} 的有符号过电位应{expected_direction}；"
                    "请检查参比换算、电流符号、反应类型和 iR 补偿参数。"
                ),
                field="reaction",
                code="reaction_direction_mismatch",
                details={
                    "reaction": reaction,
                    "signed_overpotential_mv": signed_overpotential_mv,
                    "compensated_potential_v": compensated_potential_v,
                    "equilibrium_potential_v": equilibrium_potential_v,
                },
            )
        overpotential_mv = abs(signed_overpotential_mv)
        target_point = {
            "measured_potential_v": measured_potential_v,
            "interpolated_current_a": interpolated_current_a,
            "current_a": target_current_a,
            "current_density_ma_cm2": target_signed,
            "reference_conversion_v": reference_conversion_v,
            "potential_rhe_v": potential_rhe_v,
            "applied_ir_drop_v": applied_ir_drop_v,
            "compensated_potential_v": compensated_potential_v,
            "equilibrium_potential_v": equilibrium_potential_v,
            "signed_overpotential_mv": signed_overpotential_mv,
            "overpotential_mv": overpotential_mv,
            "source_rows": [source_row_start, source_row_end],
            "interpolation_fraction": interpolation_fraction,
            "extrapolated": False,
        }
    if online_compensation_status == "unknown":
        warnings.append(
            "源数据在线补偿状态未知；本次未应用离线 iR 补偿，确认状态后方可使用非零补偿。"
        )
    warnings.append(
        (
            "原始电位已声明为 RHE，本次未重复叠加参比电势或 pH 项。"
            if reference_is_rhe
            else "RHE 换算按 25 °C 的 0.05916 V/pH 执行；参考电极偏移量由本次参数显式提供。"
        )
    )
    warnings.append(
        "CV 结果是所选扫描支路上的表观过电位；正式活性比较应同时核对扫描速率、循环迟滞和稳态/LSV 数据。"
    )

    not_calculable_reasons: list[str] = []
    screening_reasons: list[str] = []
    if target_point is None:
        not_calculable_reasons.append(
            "所选分支未达到目标电流密度，未计算目标过电位。"
        )
    if len(crossings) > 1:
        screening_reasons.append(
            "所选分支存在多个目标电流交点，结果仅使用扫描顺序中的第一个交点。"
        )
    if online_compensation_status == "unknown":
        screening_reasons.append(
            "源数据在线补偿状态未知，结果仅可用于筛查。"
        )
    elif online_compensation_status == "already_compensated":
        screening_reasons.append(
            "源数据已在线补偿，但当前记录没有在线补偿比例和对应 Rs，结果仅可用于筛查。"
        )
    elif compensation_percent == 0.0:
        screening_reasons.append(
            "源数据确认未在线补偿，但本次未应用 iR 修正，表观过电位仅可用于筛查。"
        )

    if not_calculable_reasons:
        quality_level = "not_calculable"
        quality_label = "不可计算"
        quality_reasons = not_calculable_reasons + screening_reasons
    elif screening_reasons:
        quality_level = "screening"
        quality_label = "仅筛查"
        quality_reasons = screening_reasons
    else:
        quality_level = "quantitative"
        quality_label = "可定量"
        quality_reasons = [
            "目标电流交点唯一，且源数据在线补偿状态已明确。"
        ]

    result = {
        "quality": {
            "level": quality_level,
            "label": quality_label,
            "reasons": quality_reasons,
        },
        "branch_options": branch_options,
        "selected_branch": selected_branch,
        "source_point_count": len(points),
        "selected_branch_point_count": len(branch),
        "target_current_density_ma_cm2": target_signed,
        "target_point": target_point,
        "formula": {
            "rhe": (
                "E_RHE = E_measured（原始电位已是 RHE）"
                if reference_is_rhe
                else "E_RHE = E_measured + E_reference_vs_SHE + 0.05916 × pH"
            ),
            "ir": "E_compensated = E_RHE - compensation_fraction × I × R_solution",
            "overpotential": (
                "η_signed = E_compensated - 0.000；HER 要求 η_signed ≤ 0，η = -η_signed"
                if reaction == "HER"
                else "η_signed = E_compensated - 1.229；OER 要求 η_signed ≥ 0，η = η_signed"
            ),
        },
    }
    return {
        "analysis_type": "cv_overpotential",
        "schema_version": ANALYSIS_SCHEMA_VERSION,
        "algorithm_id": CV_ALGORITHM_ID,
        "algorithm_version": CV_ALGORITHM_VERSION,
        "parameters": normalized_parameters,
        "result": result,
        "warnings": warnings,
    }


def calculate_analysis(
    table: ParsedTable,
    analysis_type: str,
    parameters: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_type = analysis_type.strip().lower()
    if normalized_type in {"eis", "eis_resistance"}:
        return calculate_eis_resistance(table, parameters)
    if normalized_type in {"cv", "cv_overpotential"}:
        return calculate_cv_overpotential(table, parameters or {})
    raise AnalysisValidationError(
        "不支持的分析类型。",
        field="analysis_type",
        code="unsupported_analysis_type",
        details={"analysis_type": analysis_type},
    )


__all__ = [
    "ANALYSIS_SCHEMA_VERSION",
    "AnalysisValidationError",
    "CV_ALGORITHM_ID",
    "CV_ALGORITHM_VERSION",
    "EIS_ALGORITHM_ID",
    "EIS_ALGORITHM_VERSION",
    "NERNST_RHE_SLOPE_V_PER_PH_25C",
    "calculate_analysis",
    "calculate_cv_overpotential",
    "calculate_eis_resistance",
    "describe_cv_branches",
]
