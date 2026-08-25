from __future__ import annotations

import math
import re
import statistics
import unicodedata
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

from .start_stop import _work_step_metadata


STEADY_WINDOW_S = 1.0
ANOMALY_BOUNDARY_S = 15.0
BASELINE_CYCLES = 10
MIN_STEADY_POINTS = 8
MIN_PHASE_POINTS = 240
MIN_COMPLETE_PHASE_ELAPSED_S = 29.0
ADT_CATHODIC_THRESHOLD_A_CM2 = -0.10
ADT_MIN_STEADY_POINTS = 1
ADT_MIN_PHASE_POINTS = 12
ADT_MIN_COMPLETE_PHASE_ELAPSED_S = 25.0
VARIABLE_MIN_STEADY_POINTS = 8
VARIABLE_MIN_PHASE_POINTS = 80
VARIABLE_MIN_COMPLETE_PHASE_ELAPSED_S = 9.0
CURRENT_ATOL = 1e-6
TIME_ATOL_S = 1e-6
OVERVIEW_INTERVAL_S = 5.0
LIVE_ANALYSIS_MAX_CYCLES = 2400


def _number(value: Any, default: float | None = None) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _median(values: Iterable[float]) -> float:
    finite = [float(value) for value in values if math.isfinite(float(value))]
    return float(statistics.median(finite)) if finite else math.nan


def _positive_median_step(values: Sequence[float]) -> float:
    positive = [
        float(right) - float(left)
        for left, right in zip(values, values[1:])
        if float(right) - float(left) > 0
    ]
    return _median(positive)


def _phase_endpoint_indices(
    elapsed_s: Sequence[float],
    phase_duration_s: float,
    sample_interval_s: float,
) -> list[int]:
    effective_window_s = max(STEADY_WINDOW_S, sample_interval_s)
    return [
        index
        for index, elapsed in enumerate(elapsed_s)
        if elapsed >= phase_duration_s - effective_window_s - TIME_ATOL_S
        and elapsed < phase_duration_s + TIME_ATOL_S
    ]


def _sampling_point_requirements(
    sample_interval_s: float,
    test_type: str,
) -> tuple[int, int, float]:
    if test_type == "adt_start_stop":
        phase_cap = ADT_MIN_PHASE_POINTS
        endpoint_cap = ADT_MIN_STEADY_POINTS
        minimum_duration_s = ADT_MIN_COMPLETE_PHASE_ELAPSED_S
    elif test_type == "variable_start_stop":
        phase_cap = VARIABLE_MIN_PHASE_POINTS
        endpoint_cap = VARIABLE_MIN_STEADY_POINTS
        minimum_duration_s = VARIABLE_MIN_COMPLETE_PHASE_ELAPSED_S
    else:
        phase_cap = MIN_PHASE_POINTS
        endpoint_cap = MIN_STEADY_POINTS
        minimum_duration_s = MIN_COMPLETE_PHASE_ELAPSED_S
    if not math.isfinite(sample_interval_s) or sample_interval_s <= 0:
        return phase_cap, endpoint_cap, minimum_duration_s
    duration_points = max(
        2,
        int(math.ceil((minimum_duration_s - TIME_ATOL_S) / sample_interval_s)),
    )
    endpoint_window_s = max(STEADY_WINDOW_S, sample_interval_s)
    endpoint_points = max(
        1,
        int(math.floor((endpoint_window_s + TIME_ATOL_S) / sample_interval_s)),
    )
    return (
        min(phase_cap, duration_points),
        min(endpoint_cap, endpoint_points),
        minimum_duration_s,
    )


def _current_levels(rows: Sequence[tuple[float, float, float]]) -> list[float]:
    return sorted({round(float(row[2]), 6) for row in rows})[:12]


def _test_type(
    rows: Sequence[tuple[float, float, float]],
    task: Mapping[str, Any],
    file_name: str,
) -> tuple[str, str]:
    combined = f"{task.get('test_type', '')} {file_name}".casefold()
    if "adt" in combined:
        return "adt_start_stop", "ADT 启停"
    levels = _current_levels(rows)
    has_standard_levels = (
        len(levels) == 2
        and abs(levels[0] + 0.30) <= CURRENT_ATOL
        and abs(levels[1] - 0.03) <= CURRENT_ATOL
    )
    nominal_cathodic = _number(task.get("cathodic_current_ma_cm2"))
    nominal_recovery = _number(task.get("recovery_current_ma_cm2"))
    has_standard_task = (
        nominal_cathodic is not None
        and nominal_recovery is not None
        and abs(nominal_cathodic + 300.0) <= 0.5
        and abs(nominal_recovery - 30.0) <= 0.5
    )
    if has_standard_levels or (len(levels) < 2 and has_standard_task):
        return "standard_start_stop", "标准启停"
    if len(levels) > 2:
        raise ValueError("活动文件电流档位超过两种，与正式启停规则不符")
    if len(levels) == 2 and not (
        levels[0] < -CURRENT_ATOL and levels[1] >= -CURRENT_ATOL
    ):
        raise ValueError("活动文件未识别到有效的阴极/恢复电流档位")
    return "variable_start_stop", "变工步启停"


def _complete_cycle_pairs(
    rows: Sequence[tuple[float, float, float]],
    test_type: str,
) -> tuple[list[tuple[int, int, int, int]], dict[str, int]]:
    if not rows:
        return [], {
            "phase_count": 0,
            "cathodic_phase_candidates": 0,
            "incomplete_cycle_candidates": 0,
        }
    threshold = ADT_CATHODIC_THRESHOLD_A_CM2 if test_type == "adt_start_stop" else 0.0
    signs = [-1 if float(row[2]) < threshold else 1 for row in rows]
    starts = [0]
    for index in range(1, len(signs)):
        if signs[index] != signs[index - 1]:
            starts.append(index)
    ends = starts[1:] + [len(rows)]
    times = [float(row[0]) for row in rows]
    pairs: list[tuple[int, int, int, int]] = []
    cathodic_candidates = 0
    incomplete_candidates = 0
    for phase_index, (start, end) in enumerate(zip(starts, ends)):
        if signs[start] >= 0:
            continue
        cathodic_candidates += 1
        if phase_index + 1 >= len(starts):
            incomplete_candidates += 1
            continue
        reverse_start = starts[phase_index + 1]
        reverse_end = ends[phase_index + 1]
        if signs[reverse_start] <= 0:
            incomplete_candidates += 1
            continue
        cathodic_elapsed = [value - times[start] for value in times[start:end]]
        reverse_elapsed = [
            value - times[reverse_start]
            for value in times[reverse_start:reverse_end]
        ]
        cathodic_step = _positive_median_step(cathodic_elapsed)
        reverse_step = _positive_median_step(reverse_elapsed)
        if not math.isfinite(cathodic_step) or not math.isfinite(reverse_step):
            incomplete_candidates += 1
            continue
        cathodic_duration = cathodic_elapsed[-1] + cathodic_step
        reverse_duration = reverse_elapsed[-1] + reverse_step
        cathodic_last = _phase_endpoint_indices(
            cathodic_elapsed, cathodic_duration, cathodic_step
        )
        reverse_last = _phase_endpoint_indices(
            reverse_elapsed, reverse_duration, reverse_step
        )
        cathodic_min_phase, cathodic_min_endpoint, minimum_duration = (
            _sampling_point_requirements(cathodic_step, test_type)
        )
        reverse_min_phase, reverse_min_endpoint, _ = _sampling_point_requirements(
            reverse_step, test_type
        )
        complete = (
            len(cathodic_elapsed) >= cathodic_min_phase
            and len(reverse_elapsed) >= reverse_min_phase
            and cathodic_duration >= minimum_duration - TIME_ATOL_S
            and reverse_duration >= minimum_duration - TIME_ATOL_S
            and len(cathodic_last) >= cathodic_min_endpoint
            and len(reverse_last) >= reverse_min_endpoint
        )
        if complete:
            pairs.append((start, end, reverse_start, reverse_end))
        else:
            incomplete_candidates += 1
    return pairs, {
        "phase_count": len(starts),
        "cathodic_phase_candidates": cathodic_candidates,
        "incomplete_cycle_candidates": incomplete_candidates,
    }


def _normalized_path(value: Any) -> str:
    raw = unicodedata.normalize("NFC", str(value or "")).replace("\\", "/").strip("/")
    if not raw:
        return ""
    parts = PurePosixPath(raw).parts
    if any(part in {"", ".", ".."} for part in parts):
        return ""
    return PurePosixPath(*parts).as_posix()


def _path_key(value: Any) -> str:
    return _normalized_path(value).casefold()


def _is_special_marked_source(
    logical_source_file: str,
    material_key: str,
) -> bool:
    source = PurePosixPath(_normalized_path(logical_source_file))
    stem = source.stem
    if re.fullmatch(r"启停\d+", stem) is None:
        return False
    material_parts = {
        unicodedata.normalize("NFC", part).casefold()
        for part in PurePosixPath(_normalized_path(material_key)).parts
    }
    return not (stem == "启停2" and "nimop" in material_parts)


def _matching_context(
    *,
    material_key: str,
    logical_source_file: str,
    work_step_key: str,
    formal_context: Mapping[str, Any],
) -> dict[str, Any]:
    raw_series = formal_context.get("series")
    series = [item for item in raw_series if isinstance(item, Mapping)] if isinstance(raw_series, list) else []
    matches = [
        item
        for item in series
        if _path_key(item.get("material_relative_path")) == _path_key(material_key)
        and str(item.get("work_step_key") or "") == work_step_key
    ]
    matches.sort(
        key=lambda item: (
            not bool(item.get("is_primary_series")),
            not str(item.get("series_id") or "").endswith("-main"),
            _integer(item.get("series_order"), 10**9),
            str(item.get("series_id") or ""),
        )
    )
    raw_segments = formal_context.get("segments")
    all_segments = [
        dict(item)
        for item in raw_segments
        if isinstance(item, Mapping)
    ] if isinstance(raw_segments, list) else []
    candidate_ids = {str(item.get("series_id") or "") for item in matches}
    logical_key = _path_key(logical_source_file)
    exact_segment = next(
        (
            item
            for item in all_segments
            if str(item.get("series_id") or "") in candidate_ids
            and _path_key(item.get("source_file")) == logical_key
        ),
        None,
    )
    if exact_segment is not None:
        exact_series_id = str(exact_segment.get("series_id") or "")
        matched = dict(
            next(
                item
                for item in matches
                if str(item.get("series_id") or "") == exact_series_id
            )
        )
    elif _is_special_marked_source(logical_source_file, material_key):
        matched = {}
    else:
        matched = dict(matches[0]) if matches else {}
    series_id = str(matched.get("series_id") or "")
    segments = [
        item
        for item in all_segments
        if str(item.get("series_id") or "") == series_id
    ]
    segments.sort(key=lambda item: _integer(item.get("segment_index")))
    existing = next(
        (item for item in segments if _path_key(item.get("source_file")) == logical_key),
        None,
    )
    if existing is not None:
        segment_index = max(1, _integer(existing.get("segment_index"), 1))
        previous_segments = [
            item
            for item in segments
            if _integer(item.get("segment_index")) < segment_index
        ]
        cycle_offset = sum(_integer(item.get("complete_cycles")) for item in previous_segments)
        time_offset_h = _number(existing.get("continuous_time_start_h"), 0.0) or 0.0
        mode = "replace_active_segment"
        replace_from_segment_index: int | None = segment_index
    elif matched:
        segment_index = max(1, _integer(matched.get("segment_count")) + 1)
        cycle_offset = max(0, _integer(matched.get("complete_cycles")))
        time_offset_h = max(0.0, _number(matched.get("duration_h"), 0.0) or 0.0)
        mode = "append_new_segment"
        replace_from_segment_index = None
    else:
        segment_index = 1
        cycle_offset = 0
        time_offset_h = 0.0
        mode = "new_live_series"
        replace_from_segment_index = None
    ordered = [str(value) for value in matched.get("source_files", []) if str(value)]
    if not ordered or _path_key(ordered[-1]) != logical_key:
        ordered.append(logical_source_file)
    return {
        "formal_series": matched,
        "formal_series_id": series_id,
        "continuation_mode": mode,
        "cycle_offset": cycle_offset,
        "time_offset_h": time_offset_h,
        "segment_index": segment_index,
        "replace_from_segment_index": replace_from_segment_index,
        "ordered_source_files": ordered,
    }


def _sample_evenly(items: Sequence[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if len(items) <= limit:
        return list(items)
    indices = {
        round(index * (len(items) - 1) / (limit - 1))
        for index in range(limit)
    }
    return [dict(items[index]) for index in sorted(indices)]


def analyze_live_start_stop_rows(
    rows: Sequence[tuple[float, float, float]],
    *,
    task: Mapping[str, Any] | None = None,
    file_name: str = "启停.txt",
    material_key: str = "",
    logical_source_file: str = "",
    formal_context: Mapping[str, Any] | None = None,
    max_overview_points: int = 2400,
    max_cycle_points: int = LIVE_ANALYSIS_MAX_CYCLES,
) -> dict[str, Any]:
    """Calculate one active snapshot with the formal start-stop rules.

    The source file remains temporary.  Formal series metadata is used only to
    continue cycle/time axes or replace an already-published active segment.
    """
    if len(rows) < 2:
        raise ValueError("活动文件尚无足够数据用于启停计算")
    normalized_rows = [
        (float(time_s), float(potential_v), float(current_a_cm2))
        for time_s, potential_v, current_a_cm2 in rows
        if all(
            math.isfinite(float(value))
            for value in (time_s, potential_v, current_a_cm2)
        )
    ]
    if len(normalized_rows) < 2:
        raise ValueError("活动文件尚无足够有效数据用于启停计算")
    times = [row[0] for row in normalized_rows]
    if any(right < left - TIME_ATOL_S for left, right in zip(times, times[1:])):
        raise ValueError("活动文件时间发生倒退，暂不绘制")
    task_payload = dict(task or {})
    test_type, test_label = _test_type(normalized_rows, task_payload, file_name)
    pairs, phase_profile = _complete_cycle_pairs(normalized_rows, test_type)
    sample_interval_s = _positive_median_step(times)
    if not math.isfinite(sample_interval_s) or sample_interval_s <= 0:
        raise ValueError("活动文件采样间隔无法识别")

    local_cycles: list[dict[str, Any]] = []
    for local_cycle, (cathodic_start, cathodic_end, reverse_start, reverse_end) in enumerate(pairs, start=1):
        cathodic = normalized_rows[cathodic_start:cathodic_end]
        reverse = normalized_rows[reverse_start:reverse_end]
        cathodic_elapsed = [row[0] - cathodic[0][0] for row in cathodic]
        reverse_elapsed = [row[0] - reverse[0][0] for row in reverse]
        cathodic_duration = cathodic_elapsed[-1] + _positive_median_step(cathodic_elapsed)
        reverse_duration = reverse_elapsed[-1] + _positive_median_step(reverse_elapsed)
        cathodic_last = _phase_endpoint_indices(
            cathodic_elapsed, cathodic_duration, sample_interval_s
        )
        reverse_last = _phase_endpoint_indices(
            reverse_elapsed, reverse_duration, sample_interval_s
        )
        cathodic_values = [cathodic[index][1] for index in cathodic_last]
        reverse_values = [reverse[index][1] for index in reverse_last]
        if not cathodic_values or not reverse_values:
            continue
        minimum_index = min(range(len(cathodic)), key=lambda index: cathodic[index][1])
        minimum_time_s = float(cathodic_elapsed[minimum_index])
        cathodic_endpoint_local_s = _median(
            cathodic[index][0] - times[0] for index in cathodic_last
        )
        reverse_endpoint_local_s = _median(
            reverse[index][0] - times[0] for index in reverse_last
        )
        local_cycles.append(
            {
                "local_cycle": local_cycle,
                "cathodic_endpoint_local_h": cathodic_endpoint_local_s / 3600.0,
                "reverse_endpoint_local_h": reverse_endpoint_local_s / 3600.0,
                "cathodic_phase_duration_s": cathodic_duration,
                "reverse_phase_duration_s": reverse_duration,
                "cathodic_current_median_a_cm2": _median(row[2] for row in cathodic),
                "recovery_current_median_a_cm2": _median(row[2] for row in reverse),
                "cathodic_last1s_median_raw_v": _median(cathodic_values),
                "reverse_last1s_median_raw_v": _median(reverse_values),
                "cathodic_phase_min_time_s": minimum_time_s,
                "status": "normal" if minimum_time_s >= ANOMALY_BOUNDARY_S else "abnormal",
            }
        )

    if local_cycles:
        cathodic_current = _median(
            item["cathodic_current_median_a_cm2"] for item in local_cycles
        )
        recovery_current = _median(
            item["recovery_current_median_a_cm2"] for item in local_cycles
        )
        cathodic_duration = _median(
            item["cathodic_phase_duration_s"] for item in local_cycles
        )
        reverse_duration = _median(
            item["reverse_phase_duration_s"] for item in local_cycles
        )
    else:
        cathodic_current = (_number(task_payload.get("cathodic_current_ma_cm2")) or 0.0) / 1000.0
        recovery_current = (_number(task_payload.get("recovery_current_ma_cm2")) or 0.0) / 1000.0
        cathodic_duration = _number(task_payload.get("cathodic_duration_s"), 0.0) or 0.0
        reverse_duration = _number(task_payload.get("recovery_duration_s"), 0.0) or 0.0
    work_step = _work_step_metadata(
        {
            "test_type": test_type,
            "test_type_label_zh": test_label,
            "cathodic_current_median_a_cm2": cathodic_current,
            "recovery_current_median_a_cm2": recovery_current,
            "median_cathodic_phase_duration_s": cathodic_duration,
            "median_reverse_phase_duration_s": reverse_duration,
        }
    )
    source_file = _normalized_path(logical_source_file) or _normalized_path(
        PurePosixPath(material_key, file_name).as_posix()
    )
    context = _matching_context(
        material_key=material_key,
        logical_source_file=source_file,
        work_step_key=work_step["work_step_key"],
        formal_context=formal_context or {},
    )
    formal_series = context["formal_series"]
    cycle_offset = int(context["cycle_offset"])
    time_offset_h = float(context["time_offset_h"])
    segment_index = int(context["segment_index"])
    rules = (formal_context or {}).get("rules")
    rules = dict(rules) if isinstance(rules, Mapping) else {}
    resistance_drift = _number(
        rules.get("water_resistance_drift_ohm_cm2_per_h")
    )
    water_available = resistance_drift is not None

    raw_cathodic_baseline = _number(
        formal_series.get("cathodic_baseline_first10_median_raw_v")
    )
    raw_reverse_baseline = _number(
        formal_series.get("reverse_baseline_first10_median_raw_v")
    )
    if raw_cathodic_baseline is None:
        raw_cathodic_baseline = _median(
            item["cathodic_last1s_median_raw_v"]
            for item in local_cycles[:BASELINE_CYCLES]
        )
    if raw_reverse_baseline is None:
        raw_reverse_baseline = _median(
            item["reverse_last1s_median_raw_v"]
            for item in local_cycles[:BASELINE_CYCLES]
        )

    prepared_cycles: list[dict[str, Any]] = []
    for item in local_cycles:
        cycle = cycle_offset + int(item["local_cycle"])
        cathodic_time_h = time_offset_h + float(item["cathodic_endpoint_local_h"])
        reverse_time_h = time_offset_h + float(item["reverse_endpoint_local_h"])
        cathodic_raw = float(item["cathodic_last1s_median_raw_v"])
        reverse_raw = float(item["reverse_last1s_median_raw_v"])
        cathodic_water = (
            cathodic_raw
            - float(item["cathodic_current_median_a_cm2"])
            * float(resistance_drift)
            * cathodic_time_h
            if water_available
            else None
        )
        reverse_water = (
            reverse_raw
            - float(item["recovery_current_median_a_cm2"])
            * float(resistance_drift)
            * reverse_time_h
            if water_available
            else None
        )
        prepared_cycles.append(
            {
                "cycle": cycle,
                "cycle_in_segment": int(item["local_cycle"]),
                "segment_index": segment_index,
                "source_file": source_file,
                "cathodic_endpoint_time_h": cathodic_time_h,
                "reverse_endpoint_time_h": reverse_time_h,
                "cathodic_current_median_a_cm2": item["cathodic_current_median_a_cm2"],
                "recovery_current_median_a_cm2": item["recovery_current_median_a_cm2"],
                "cathodic_last1s_median_raw_v": cathodic_raw,
                "reverse_last1s_median_raw_v": reverse_raw,
                "cathodic_negative_shift_mv": 1000.0 * (raw_cathodic_baseline - cathodic_raw),
                "reverse_shift_mv": 1000.0 * (reverse_raw - raw_reverse_baseline),
                "cathodic_phase_min_time_s": item["cathodic_phase_min_time_s"],
                "status": item["status"],
                "cathodic_last1s_median_water_compensated_v": cathodic_water,
                "reverse_last1s_median_water_compensated_v": reverse_water,
            }
        )

    water_cathodic_baseline = _number(
        formal_series.get("water_comp_cathodic_baseline_first10_raw_v")
    )
    water_reverse_baseline = _number(
        formal_series.get("water_comp_recovery_baseline_first10_raw_v")
    )
    if water_cathodic_baseline is None:
        water_cathodic_baseline = _median(
            item["cathodic_last1s_median_water_compensated_v"]
            for item in prepared_cycles[:BASELINE_CYCLES]
            if item["cathodic_last1s_median_water_compensated_v"] is not None
        )
    if water_reverse_baseline is None:
        water_reverse_baseline = _median(
            item["reverse_last1s_median_water_compensated_v"]
            for item in prepared_cycles[:BASELINE_CYCLES]
            if item["reverse_last1s_median_water_compensated_v"] is not None
        )
    for item in prepared_cycles:
        cathodic_water = item["cathodic_last1s_median_water_compensated_v"]
        reverse_water = item["reverse_last1s_median_water_compensated_v"]
        item["cathodic_negative_shift_water_compensated_mv"] = (
            1000.0 * (water_cathodic_baseline - cathodic_water)
            if cathodic_water is not None and math.isfinite(water_cathodic_baseline)
            else None
        )
        item["reverse_shift_water_compensated_mv"] = (
            1000.0 * (reverse_water - water_reverse_baseline)
            if reverse_water is not None and math.isfinite(water_reverse_baseline)
            else None
        )

    stride = max(1, int(round(OVERVIEW_INTERVAL_S / sample_interval_s)))
    overview_rows = normalized_rows[::stride]
    if overview_rows[-1] != normalized_rows[-1]:
        overview_rows = [*overview_rows, normalized_rows[-1]]
    overview_points = []
    for time_s, potential_v, current_a_cm2 in overview_rows:
        continuous_time_h = time_offset_h + (time_s - times[0]) / 3600.0
        water_value = (
            potential_v - current_a_cm2 * float(resistance_drift) * continuous_time_h
            if water_available
            else None
        )
        overview_points.append(
            {
                "continuous_time_h": continuous_time_h,
                "potential_raw_v": potential_v,
                "potential_water_compensated_v": water_value,
                "current_a_cm2": current_a_cm2,
                "segment_index": segment_index,
                "source_file": source_file,
            }
        )

    return {
        "schema_version": 1,
        "calculation": "formal_start_stop_rules_v3",
        "potential_basis": "Hg/HgO 原始电位",
        "test_type": test_type,
        "test_type_label_zh": test_label,
        **work_step,
        "formal_series_id": context["formal_series_id"],
        "continuation_mode": context["continuation_mode"],
        "continuation_cycle_offset": cycle_offset,
        "continuation_time_offset_h": time_offset_h,
        "segment_index": segment_index,
        "replace_from_segment_index": context["replace_from_segment_index"],
        "ordered_source_files": context["ordered_source_files"],
        "complete_cycles": len(local_cycles),
        "published_cycle_points": min(len(prepared_cycles), max_cycle_points),
        "phase_count": phase_profile["phase_count"],
        "cathodic_phase_candidates": phase_profile["cathodic_phase_candidates"],
        "incomplete_cycle_candidates": phase_profile["incomplete_cycle_candidates"],
        "sample_interval_s": sample_interval_s,
        "water_compensation_available": water_available,
        "cycle_points": _sample_evenly(prepared_cycles, max(2, int(max_cycle_points))),
        "overview_points": _sample_evenly(overview_points, max(32, int(max_overview_points))),
    }


__all__ = [
    "ANOMALY_BOUNDARY_S",
    "LIVE_ANALYSIS_MAX_CYCLES",
    "analyze_live_start_stop_rows",
]
