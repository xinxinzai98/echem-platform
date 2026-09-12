from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import statistics
import threading
from collections import OrderedDict
from itertools import groupby
from typing import Any, Iterable, Mapping, Sequence


class StabilityAnalysisError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = int(status)


MAX_SERIES = 16
MAX_POINTS = 20_000
DEFAULT_POINTS = 6000
STATISTICS_VERSION = "lanbts-stability-statistics/3-complete-steps"
STATISTICS_CACHE_BYTES = 64 * 1024 * 1024
MAX_SOURCE_BYTES = 128 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
ANOMALY_BOUNDARY_S = 15.0
EXPECTED_COLUMNS = {
    "time_s",
    "potential_v",
    "current_ma",
    "current_density_ma_cm2",
    "cycle_id",
    "step_id",
    "step_name",
    "absolute_time",
    "record_id",
}
MODE_LABELS = {
    "start_stop": "启停分析",
    "constant_current": "恒流分析",
}
METRICS = {
    "start_stop": {
        "overview": ("全程电压", "V", "time_h", "电压仅按 LANBTS 原始标尺显示"),
        "stress_endpoint": ("高负载段末端电压", "V", "cycle", "阶段最后 1 s 中位数"),
        "recovery_endpoint": ("低负载段末端电压", "V", "cycle", "阶段最后 1 s 中位数"),
        "negative_shift": ("高负载电压负移", "mV", "cycle", "相对首个完整循环"),
        "minimum_time": ("高负载段最低点位置", "s", "cycle", "最低点 <15 s 为异常"),
        "current": ("实测电流", "mA", "time_h", "标准化后的原始电流"),
        "current_density": ("实测电流密度", "mA/cm²", "time_h", "仅限已填写本次电极面积的数据"),
    },
    "constant_current": {
        "overview": ("恒流电压走势", "V", "time_h", "电压仅按 LANBTS 原始标尺显示"),
        "current": ("实测电流", "mA", "time_h", "用于核对恒流稳定性"),
        "current_density": ("实测电流密度", "mA/cm²", "time_h", "仅限已填写本次电极面积的数据"),
    },
}


def _safe_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _safe_int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return None


def _series_id(row: Mapping[str, Any]) -> str:
    seed = "\0".join(
        (
            str(row.get("repository_path") or ""),
            str(row.get("metadata", {}).get("parent_sha256") or row.get("sha256") or ""),
        )
    ).encode("utf-8")
    return "lanbts-" + hashlib.sha256(seed).hexdigest()[:20]


def _public_source(row: Mapping[str, Any]) -> dict[str, Any] | None:
    metadata = row.get("metadata")
    if not isinstance(metadata, Mapping):
        return None
    mode = str(metadata.get("analysis_mode") or "")
    if mode not in MODE_LABELS:
        return None
    source_file = str(metadata.get("source_file") or "")[:220]
    material_name = str(metadata.get("material_name") or "").strip()[:120]
    display_name = material_name or source_file or str(row.get("repository_path") or "")
    protocol = metadata.get("protocol")
    if not isinstance(protocol, Mapping):
        protocol = {}
    area = _safe_float(metadata.get("electrode_area_cm2"))
    parent_sha = str(metadata.get("parent_sha256") or "")
    return {
        "series_id": _series_id(row),
        "source_version_id": int(row["source_version_id"]),
        "repository_path": str(row["repository_path"]),
        "sha256": str(row["sha256"]),
        "size_bytes": int(row["size_bytes"]),
        "source_modified_utc": str(row.get("source_modified_utc") or ""),
        "database_created_utc": str(row.get("created_utc") or ""),
        "analysis_mode": mode,
        "analysis_mode_label": MODE_LABELS[mode],
        "display_name": display_name,
        "material_name": material_name,
        "source_file": source_file,
        "channel": _safe_int(metadata.get("channel")),
        "electrode_area_cm2": area,
        "current_unit": "mA/cm²" if area is not None else "mA",
        "voltage_unit": "V（LANBTS 原始标尺）",
        "voltage_reference": "unconfirmed",
        "protocol_kind": str(protocol.get("kind") or "unknown")[:80],
        "protocol_category": str(protocol.get("category") or "方案待识别")[:80],
        "protocol_label": str(protocol.get("label") or "工步待识别")[:240],
        "protocol_key": str(protocol.get("key") or "")[:80],
        "classification_method": str(metadata.get("classification_method") or "")[:120],
        "classification_reason": str(metadata.get("classification_reason") or "")[:500],
        "classifier_version": str(metadata.get("classifier_version") or "")[:120],
        "extractor_version": str(metadata.get("extractor_version") or "")[:120],
        "current_p05_ma": _safe_float(metadata.get("current_p05_ma")),
        "current_p95_ma": _safe_float(metadata.get("current_p95_ma")),
        "current_span_ma": _safe_float(metadata.get("current_span_ma")),
        "current_level_transition_count": _safe_int(
            metadata.get("current_level_transition_count")
        ),
        "record_count": _safe_int(metadata.get("record_count")) or 0,
        "point_count": _safe_int(metadata.get("exported_point_count")) or 0,
        "downsample_stride": _safe_int(metadata.get("downsample_stride")) or 1,
        "parent_source_version_id": _safe_int(
            metadata.get("parent_source_version_id")
        ),
        "parent_repository_path": str(
            metadata.get("parent_repository_path") or ""
        )[:1000],
        "parent_sha256": parent_sha if len(parent_sha) == 64 else "",
        "notes": str(metadata.get("notes") or "")[:300],
    }


def _downsample(rows: Sequence[dict[str, Any]], maximum: int) -> list[dict[str, Any]]:
    if len(rows) <= maximum:
        return list(rows)
    if maximum <= 2:
        return [rows[0], rows[-1]][:maximum]
    stride = (len(rows) - 1) / (maximum - 1)
    indexes = sorted({round(index * stride) for index in range(maximum)})
    return [rows[index] for index in indexes]


def _median_last_window(
    rows: Sequence[dict[str, Any]],
    *,
    seconds: float = 1.0,
) -> float | None:
    if not rows:
        return None
    end = max(row["time_s"] for row in rows)
    values = [
        row["potential_v"]
        for row in rows
        if row["time_s"] >= end - seconds - 1e-9
    ]
    return float(statistics.median(values)) if values else None


def _linear_slope(x: Sequence[float], y: Sequence[float]) -> float | None:
    if len(x) < 2 or len(x) != len(y):
        return None
    mean_x = sum(x) / len(x)
    mean_y = sum(y) / len(y)
    denominator = sum((value - mean_x) ** 2 for value in x)
    if denominator <= 0:
        return None
    return sum(
        (x_value - mean_x) * (y_value - mean_y)
        for x_value, y_value in zip(x, y)
    ) / denominator


def _read_rows(content: bytes) -> list[dict[str, Any]]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StabilityAnalysisError("蓝博标准化数据编码无效。", 422) from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if not EXPECTED_COLUMNS.issubset(set(reader.fieldnames or [])):
        raise StabilityAnalysisError("蓝博标准化数据表头无效。", 422)
    rows: list[dict[str, Any]] = []
    previous_time = -math.inf
    for raw in reader:
        time_s = _safe_float(raw.get("time_s"))
        potential_v = _safe_float(raw.get("potential_v"))
        current_ma = _safe_float(raw.get("current_ma"))
        if time_s is None or potential_v is None or current_ma is None:
            continue
        if time_s < previous_time - 1e-9:
            raise StabilityAnalysisError("蓝博标准化数据时间发生倒退。", 422)
        previous_time = time_s
        rows.append(
            {
                "time_s": time_s,
                "time_h": time_s / 3600.0,
                "potential_v": potential_v,
                "current_ma": current_ma,
                "current_density_ma_cm2": _safe_float(
                    raw.get("current_density_ma_cm2")
                ),
                "cycle_id": _safe_int(raw.get("cycle_id")),
                "step_id": _safe_int(raw.get("step_id")),
                "step_name": str(raw.get("step_name") or "")[:80],
                "absolute_time": str(raw.get("absolute_time") or "")[:80],
                "record_id": _safe_int(raw.get("record_id")),
            }
        )
    if len(rows) < 2:
        raise StabilityAnalysisError("蓝博标准化数据有效点不足。", 422)
    return rows


def _protocol_levels(source: Mapping[str, Any]) -> list[float]:
    metadata = source.get("metadata")
    if not isinstance(metadata, Mapping):
        return []
    values = metadata.get("protocol_current_levels_ma")
    if not isinstance(values, list):
        return []
    return sorted(
        {
            float(value)
            for value in values
            if isinstance(value, (int, float)) and math.isfinite(float(value))
        }
    )


def _start_stop_cycles(
    rows: Sequence[dict[str, Any]],
    source: Mapping[str, Any],
    *,
    last_cycle_closed: bool = False,
) -> list[dict[str, Any]]:
    levels = _protocol_levels(source)
    if len(levels) < 2:
        currents = sorted(row["current_ma"] for row in rows)
        levels = [currents[max(0, int(len(currents) * 0.05) - 1)], currents[min(len(currents) - 1, int(len(currents) * 0.95))]]
    if len(levels) < 2 or math.isclose(levels[0], levels[-1], abs_tol=1e-9):
        return []
    stress_level = levels[0] if levels[0] < 0 else max(levels, key=abs)
    recovery_level = max(levels, key=lambda value: (value != stress_level, value))

    def phase(current: float) -> str:
        return "stress" if abs(current - stress_level) <= abs(current - recovery_level) else "recovery"

    metadata = source.get("metadata", {})
    protocol = metadata.get("protocol", {}) if isinstance(metadata, Mapping) else {}
    steps = protocol.get("steps", []) if isinstance(protocol, Mapping) else []
    # Embedded bipolar protocols put optional conditioning steps before the
    # final repeating pair. SDK step_id may reset per cycle or keep increasing.
    expected: list[tuple[str, float | None]] = []
    if isinstance(steps, list) and len(steps) >= 2:
        for step in steps[-2:]:
            if not isinstance(step, Mapping):
                break
            current = _safe_float(step.get("current_ma"))
            if current is None:
                break
            expected.append((phase(current), _safe_float(step.get("duration_s"))))
    if len(expected) != 2 or {item[0] for item in expected} != {"stress", "recovery"}:
        expected = []

    grouped = [(cycle, list(group)) for cycle, group in groupby(rows, key=lambda row: row.get("cycle_id"))]
    result: list[dict[str, Any]] = []
    for cycle_index, (cycle, cycle_rows) in enumerate(grouped):
        if not isinstance(cycle, int):
            continue
        segments = [(key, list(group)) for key, group in groupby(
            cycle_rows, key=lambda row: (row.get("step_id"), phase(row["current_ma"]))
        )]
        if (
            len(segments) != 2
            or any(not isinstance(key[0], int) for key, _ in segments)
            or segments[0][0][0] == segments[1][0][0]
            or {key[1] for key, _ in segments} != {"stress", "recovery"}
            or (expected and [key[1] for key, _ in segments] != [item[0] for item in expected])
        ):
            continue
        complete = True
        for index, (key, segment) in enumerate(segments):
            intervals = [right["time_s"] - left["time_s"] for left, right in zip(segment, segment[1:])
                         if right["time_s"] > left["time_s"]]
            if not intervals:
                complete = False
                break
            duration = expected[index][1] if expected else None
            if duration is not None and duration > 0:
                # A phase's own interval covers its final sampling bin. One
                # point cannot establish an interval, even with a known timer.
                covered = segment[-1]["time_s"] - segment[0]["time_s"] + statistics.median(intervals)
                if covered < duration - max(1e-9, duration * 1e-9):
                    complete = False
                    break
            else:
                # Without an identified timer only an observed next SDK step
                # or explicit caller evidence proves closure. Source metadata
                # cannot claim closure for an otherwise unknown EOF tail.
                following = segments[1][1][0] if index == 0 else (
                    grouped[cycle_index + 1][1][0] if cycle_index + 1 < len(grouped) else None
                )
                externally_closed = last_cycle_closed is True and index == 1 and cycle_index == len(grouped) - 1
                if not externally_closed and (
                    following is None or not isinstance(following.get("step_id"), int)
                    or (following.get("cycle_id"), following["step_id"]) == (cycle, key[0])
                ):
                    complete = False
                    break
        if not complete:
            continue
        phase_rows = {key[1]: segment for key, segment in segments}
        stress_rows, recovery_rows = phase_rows["stress"], phase_rows["recovery"]
        stress_endpoint = _median_last_window(stress_rows)
        recovery_endpoint = _median_last_window(recovery_rows)
        if stress_endpoint is None or recovery_endpoint is None:
            continue
        stress_start = min(row["time_s"] for row in stress_rows)
        minimum_row = min(stress_rows, key=lambda row: row["potential_v"])
        minimum_time_s = minimum_row["time_s"] - stress_start
        result.append(
            {
                "cycle": cycle,
                "time_h": max(row["time_h"] for row in cycle_rows),
                "stress_endpoint_v": stress_endpoint,
                "recovery_endpoint_v": recovery_endpoint,
                "minimum_time_s": minimum_time_s,
                "status": (
                    "normal"
                    if minimum_time_s >= ANOMALY_BOUNDARY_S
                    else "abnormal"
                ),
            }
        )
    if result:
        baseline = result[0]["stress_endpoint_v"]
        for row in result:
            row["negative_shift_mv"] = (
                baseline - row["stress_endpoint_v"]
            ) * 1000.0
    return result


def _constant_summary(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    start = rows[0]["time_s"]
    end = rows[-1]["time_s"]
    duration = max(0.0, end - start)
    settle = min(300.0, duration * 0.05)
    fitted = [row for row in rows if row["time_s"] >= start + settle]
    if len(fitted) < 2:
        fitted = list(rows)
    slope = _linear_slope(
        [row["time_h"] for row in fitted],
        [row["potential_v"] for row in fitted],
    )
    window = min(300.0, max(60.0, duration * 0.02))
    start_values = [
        row["potential_v"]
        for row in fitted
        if row["time_s"] <= fitted[0]["time_s"] + window
    ]
    end_values = [
        row["potential_v"]
        for row in fitted
        if row["time_s"] >= fitted[-1]["time_s"] - window
    ]
    current_values = [row["current_ma"] for row in fitted]
    start_median = float(statistics.median(start_values))
    end_median = float(statistics.median(end_values))
    return {
        "duration_h": duration / 3600.0,
        "settling_excluded_s": settle,
        "endpoint_window_s": window,
        "start_voltage_median_v": start_median,
        "end_voltage_median_v": end_median,
        "voltage_change_mv": (end_median - start_median) * 1000.0,
        "linear_drift_mv_per_h": slope * 1000.0 if slope is not None else None,
        "current_median_ma": float(statistics.median(current_values)),
        "current_p05_ma": float(sorted(current_values)[max(0, int(len(current_values) * 0.05) - 1)]),
        "current_p95_ma": float(sorted(current_values)[min(len(current_values) - 1, int(len(current_values) * 0.95))]),
    }


class StabilityRepositoryAnalyzer:
    def __init__(self, database: Any):
        self.database = database
        self._statistics_cache: OrderedDict[str, bytes] = OrderedDict()
        self._statistics_cache_bytes = 0
        self._statistics_lock = threading.RLock()

    def _prepared_source(self, public: dict[str, Any], raw_source: dict[str, Any]) -> dict[str, Any]:
        """Compute once per immutable data+rules identity; cache only bounded previews and statistics."""
        key = json.dumps([
            public["sha256"], public["size_bytes"], public["analysis_mode"],
            public["downsample_stride"], _protocol_levels(raw_source),
            raw_source.get("metadata", {}).get("protocol"), STATISTICS_VERSION,
        ], separators=(",", ":"))
        with self._statistics_lock:
            cached = self._statistics_cache.pop(key, None)
            if cached is not None:
                self._statistics_cache[key] = cached
                return json.loads(cached)
            content = self.database.read_source_export_content(
                source_version_id=public["source_version_id"], expected_sha256=public["sha256"],
                expected_size_bytes=public["size_bytes"], maximum_file_bytes=MAX_SOURCE_BYTES,
            )
            rows = _read_rows(content)
            preview_only = public["downsample_stride"] > 1
            cycles = []
            if public["analysis_mode"] == "start_stop":
                cycles = [] if preview_only else _start_stop_cycles(rows, raw_source)
                summary = {
                    "calculation_status": "preview_only" if preview_only else "full_records",
                    "complete_cycles": None if preview_only else len(cycles),
                    "normal_cycles": None if preview_only else sum(row["status"] == "normal" for row in cycles),
                    "abnormal_cycles": None if preview_only else sum(row["status"] == "abnormal" for row in cycles),
                }
            else:
                summary = {"calculation_status": "preview_only"} if preview_only else _constant_summary(rows)
            prepared = {
                "cycles": cycles, "summary": summary,
                "preview": _downsample(rows, MAX_POINTS), "row_count": len(rows),
                "density_point_count": sum(row["current_density_ma_cm2"] is not None for row in rows),
            }
            encoded = json.dumps(prepared, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
            if len(encoded) <= STATISTICS_CACHE_BYTES:
                while self._statistics_cache and self._statistics_cache_bytes + len(encoded) > STATISTICS_CACHE_BYTES:
                    _, evicted = self._statistics_cache.popitem(last=False)
                    self._statistics_cache_bytes -= len(evicted)
                self._statistics_cache[key] = encoded
                self._statistics_cache_bytes += len(encoded)
            return prepared

    def _sources(self) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        getter = getattr(self.database, "stability_sources", None)
        if not callable(getter):
            return []
        result: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for row in getter():
            public = _public_source(row)
            if public is not None:
                result.append((public, row))
        return result

    def catalog(self) -> dict[str, Any]:
        sources = [public for public, _row in self._sources()]
        counts = {
            "total": len(sources),
            "start_stop": sum(
                item["analysis_mode"] == "start_stop" for item in sources
            ),
            "constant_current": sum(
                item["analysis_mode"] == "constant_current" for item in sources
            ),
            "area_configured": sum(
                item["electrode_area_cm2"] is not None for item in sources
            ),
        }
        latest = max(
            (item["source_modified_utc"] for item in sources),
            default="",
        )
        return {
            "schema_version": 1,
            "status": "ready" if sources else "empty",
            "counts": counts,
            "latest_data_update_at": latest,
            "series": sources,
            "classification": {
                "rule": "BTS 内嵌工步优先；否则以实测电流 P5–P95 跨度、持续占比和至少 2 次档位切换判断明显波动。",
                "start_stop": "两个以上持续且显著不同的电流档",
                "constant_current": "单一电流档或没有持续重复切换",
                "manual_override_available": False,
            },
            "measurement_boundary": {
                "voltage_reference": "unconfirmed",
                "message": "蓝博电压参照尚未确认，不与 Hg/HgO 曲线共用纵轴。",
            },
        }

    def chart(
        self,
        *,
        series_ids: Iterable[str],
        analysis_mode: str,
        metric: str,
        max_points: int = DEFAULT_POINTS,
    ) -> dict[str, Any]:
        mode = str(analysis_mode or "")
        if mode not in MODE_LABELS:
            raise StabilityAnalysisError("稳定性分析模式无效。")
        metric_spec = METRICS[mode].get(str(metric or ""))
        if metric_spec is None:
            raise StabilityAnalysisError("稳定性分析指标无效。")
        requested = [str(value) for value in series_ids if str(value)]
        if not requested or len(requested) > MAX_SERIES or len(requested) != len(set(requested)):
            raise StabilityAnalysisError(f"一次必须选择 1–{MAX_SERIES} 条稳定性曲线。")
        bounded_points = max(100, min(int(max_points), MAX_POINTS))
        known = {public["series_id"]: (public, raw) for public, raw in self._sources()}
        if any(series_id not in known for series_id in requested):
            raise StabilityAnalysisError("稳定性曲线不存在或已更新。", 404)
        if any(known[series_id][0]["analysis_mode"] != mode for series_id in requested):
            raise StabilityAnalysisError("所选曲线不属于当前分析模式。")
        protocol_keys = {known[series_id][0]["protocol_key"] or f"unknown:{series_id}" for series_id in requested}
        if len(protocol_keys) != 1:
            raise StabilityAnalysisError("蓝博曲线必须属于同一测试工步；未知工步只能单独检查。")
        if metric == "minimum_time" and len(requested) > 1:
            raise StabilityAnalysisError("异常判断请只选择一条材料测试记录。")
        reader = getattr(self.database, "read_source_export_content", None)
        if not callable(reader):
            raise StabilityAnalysisError("稳定性原始数据库读取组件不可用。", 503)
        total_bytes = sum(known[series_id][0]["size_bytes"] for series_id in requested)
        if total_bytes > MAX_TOTAL_BYTES:
            raise StabilityAnalysisError("所选稳定性曲线数据量过大。", 413)
        title, unit, x_key, description = metric_spec
        output_series: list[dict[str, Any]] = []
        for series_id in requested:
            public, raw_source = known[series_id]
            preview_only = public["downsample_stride"] > 1
            if preview_only and metric not in {"overview", "current", "current_density"}:
                raise StabilityAnalysisError(
                    "所选蓝博记录为旧版抽样预览，不能计算末端电位或异常判断；请重新下载全量记录。", 422
                )
            prepared = self._prepared_source(public, raw_source)
            rows = prepared["preview"]
            summary = prepared["summary"]
            if mode == "start_stop":
                cycles = prepared["cycles"]
                if metric == "overview":
                    points = [
                        {"x": row["time_h"], "y": row["potential_v"], "current_ma": row["current_ma"]}
                        for row in rows
                    ]
                elif metric in {"current", "current_density"}:
                    current_field = (
                        "current_density_ma_cm2"
                        if metric == "current_density"
                        else "current_ma"
                    )
                    points = [
                        {"x": row["time_h"], "y": row[current_field]}
                        for row in rows
                        if row[current_field] is not None
                    ]
                else:
                    field = {
                        "stress_endpoint": "stress_endpoint_v",
                        "recovery_endpoint": "recovery_endpoint_v",
                        "negative_shift": "negative_shift_mv",
                        "minimum_time": "minimum_time_s",
                    }[metric]
                    points = [
                        {
                            "x": row[x_key],
                            "y": row[field],
                            "cycle": row["cycle"],
                            "status": row["status"],
                            "time_h": row["time_h"],
                        }
                        for row in cycles
                    ]
            else:
                field = {
                    "current": "current_ma",
                    "current_density": "current_density_ma_cm2",
                }.get(metric, "potential_v")
                points = [
                    {"x": row["time_h"], "y": row[field]}
                    for row in rows
                    if row[field] is not None
                ]
            output_series.append(
                {
                    "series_id": series_id,
                    "display_name": public["display_name"],
                    "analysis_mode": mode,
                    "protocol_label": public["protocol_label"],
                    "points": _downsample(points, bounded_points),
                    "source_point_count": prepared["density_point_count"] if metric == "current_density"
                    else prepared["row_count"] if metric in {"overview", "current"} else len(points),
                    "summary": summary,
                    "provenance": {
                        "source_file": public["source_file"],
                        "parent_sha256": public["parent_sha256"],
                        "normalized_sha256": public["sha256"],
                        "classification_method": public["classification_method"],
                        "classification_reason": public["classification_reason"],
                        "voltage_reference": "unconfirmed",
                        "sampling_policy": "preview_only" if preview_only else "full_records",
                        "statistics_version": STATISTICS_VERSION,
                    },
                }
            )
        return {
            "schema_version": 1,
            "analysis_mode": mode,
            "analysis_mode_label": MODE_LABELS[mode],
            "metric": metric,
            "metric_spec": {
                "label": title,
                "unit": unit,
                "x_key": x_key,
                "x_label": "累计时间 / h" if x_key == "time_h" else "循环数",
                "description": description,
            },
            "series": output_series,
            "measurement_boundary": {
                "voltage_reference": "unconfirmed",
                "message": "蓝博电压参照尚未确认，不与 Hg/HgO 曲线共用纵轴。",
            },
        }


__all__ = [
    "StabilityAnalysisError",
    "StabilityRepositoryAnalyzer",
]
