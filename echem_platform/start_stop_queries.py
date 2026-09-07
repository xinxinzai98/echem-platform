"""Read-only chart queries, bounded presentation caches and stability adapters."""
from __future__ import annotations

import copy
import csv
import json
import sqlite3
import sys
from collections import OrderedDict
from pathlib import Path
from typing import Any, Iterable
from .start_stop_stability import StabilityAnalysisError
from .start_stop_stability_export import export_stability
from .start_stop_contracts import StartStopWorkspaceError, _bool, _number, _integer, _work_step_metadata


class ChartQueryMixin:
    @staticmethod
    def _payload_size_bytes(payload: dict[str, Any]) -> int:
        """Estimate the retained payload size without retaining encoded JSON."""
        try:
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, OverflowError):
            return sys.maxsize
        # Python dictionaries and numeric objects require more memory than the
        # compact wire representation.  A conservative 2x estimate keeps the
        # resident cache materially below the source CSV size.
        return min(sys.maxsize, len(encoded) * 2)

    def _cache_generation_token(self) -> tuple[Any, ...]:
        """Return a path-free token that changes with the published generation."""
        if not self.repository_mode:
            return ("filesystem",)
        getter = getattr(self.database, "current_analysis_run", None)
        if callable(getter):
            try:
                payload = getter("render")
            except (OSError, RuntimeError, ValueError, sqlite3.Error):
                payload = None
            if isinstance(payload, dict):
                return (
                    "repository",
                    str(payload.get("state") or ""),
                    _integer(payload.get("artifact_generation_id"), -1),
                    str(payload.get("artifact_manifest_sha256") or ""),
                )
        status_getter = getattr(self.database, "repository_status", None)
        if callable(status_getter):
            try:
                payload = self._public_repository_status(status_getter())
            except (OSError, RuntimeError, ValueError, sqlite3.Error):
                payload = {}
            return (
                "repository",
                "status",
                _integer(payload.get("latest_artifact_generation_id"), -1),
                _integer(payload.get("artifact_generation_count"), -1),
            )
        return ("repository", "unavailable")

    @staticmethod
    def _file_cache_signature(path: Path) -> tuple[Any, ...]:
        try:
            stat = path.stat()
        except OSError as exc:
            raise StartStopWorkspaceError(
                "无法读取启停分析文件状态。",
                500,
            ) from exc
        return (
            str(path),
            int(stat.st_dev),
            int(stat.st_ino),
            int(stat.st_size),
            int(stat.st_mtime_ns),
            int(stat.st_ctime_ns),
        )

    def _chart_cache_get_locked(
        self,
        key: tuple[Any, ...],
    ) -> dict[str, Any] | None:
        cached = self._chart_cache.pop(key, None)
        if cached is None:
            return None
        self._chart_cache[key] = cached
        try:
            payload = json.loads(cached[1])
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._chart_cache.pop(key, None)
            self._chart_cache_bytes -= cached[0]
            return None
        if not isinstance(payload, dict):
            self._chart_cache.pop(key, None)
            self._chart_cache_bytes -= cached[0]
            return None
        return payload

    def _chart_cache_put_locked(
        self,
        key: tuple[Any, ...],
        payload: dict[str, Any],
    ) -> None:
        maximum_entries = max(0, int(self.CHART_CACHE_MAX_ENTRIES))
        maximum_bytes = max(0, int(self.CHART_CACHE_MAX_BYTES))
        try:
            encoded_payload = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, OverflowError):
            return
        estimated_bytes = len(encoded_payload)
        if (
            maximum_entries <= 0
            or maximum_bytes <= 0
            or estimated_bytes > maximum_bytes
        ):
            return
        previous = self._chart_cache.pop(key, None)
        if previous is not None:
            self._chart_cache_bytes -= previous[0]
        while self._chart_cache and (
            len(self._chart_cache) >= maximum_entries
            or self._chart_cache_bytes + estimated_bytes > maximum_bytes
        ):
            _, (removed_bytes, _) = self._chart_cache.popitem(last=False)
            self._chart_cache_bytes -= removed_bytes
        self._chart_cache[key] = (estimated_bytes, encoded_payload)
        self._chart_cache_bytes += estimated_bytes

    def _series_payload(
        self,
    ) -> tuple[dict[str, Any], tuple[Any, ...]]:
        path = self._path(self.SERIES_NAME, require=True)
        generation = self._cache_generation_token()
        signature = self._file_cache_signature(path)
        cache_key = ("series", generation, signature)
        with self._chart_cache_lock:
            if (
                cache_key == self._series_cache_key
                and self._series_cache_payload is not None
            ):
                return copy.deepcopy(self._series_cache_payload), cache_key

            rows = self._read_csv(self.SERIES_NAME)
            public = []
            for row in rows:
                work_step = _work_step_metadata(row)
                public.append(
                    {
                        "series_id": str(row.get("series_id") or ""),
                        "series_order": _integer(row.get("series_order")),
                        "series_display_name": str(
                            row.get("series_display_name") or ""
                        ),
                        "material_id": str(row.get("material_id") or ""),
                        "material_relative_path": str(
                            row.get("material_relative_path") or ""
                        ),
                        "test_type": str(row.get("test_type") or ""),
                        "test_type_label_zh": str(
                            row.get("test_type_label_zh") or ""
                        ),
                        "include_in_summary_atlas": _bool(
                            row.get("include_in_summary_atlas")
                        ),
                        "complete_cycles": _integer(row.get("complete_cycles")),
                        "duration_h": _number(row.get("duration_h"), 0.0),
                        "normal_cycles": _integer(row.get("normal_cycles")),
                        "abnormal_cycles": _integer(row.get("abnormal_cycles")),
                        "abnormal_fraction": _number(
                            row.get("abnormal_fraction"), 0.0
                        ),
                        "segment_count": _integer(row.get("segment_count")),
                        "source_files": self._json_list(row.get("source_files")),
                        "endpoint_statistic": str(
                            row.get("endpoint_statistic") or ""
                        ),
                        "cathodic_current_median_a_cm2": _number(
                            row.get("cathodic_current_median_a_cm2")
                        ),
                        "recovery_current_median_a_cm2": _number(
                            row.get("recovery_current_median_a_cm2")
                        ),
                        "median_cathodic_phase_duration_s": _number(
                            row.get("median_cathodic_phase_duration_s")
                        ),
                        "median_reverse_phase_duration_s": _number(
                            row.get("median_reverse_phase_duration_s")
                        ),
                        "median_cycle_duration_s": _number(
                            row.get("median_cycle_duration_s")
                        ),
                        "cathodic_negative_shift_first10_to_last10_mv": _number(
                            row.get(
                                "cathodic_negative_shift_first10_to_last10_mv"
                            )
                        ),
                        **work_step,
                    }
                )
            public.sort(key=lambda item: (item["series_order"], item["series_id"]))
            work_steps_by_key: OrderedDict[str, dict[str, Any]] = OrderedDict()
            work_step_materials: dict[str, set[str]] = {}
            for item in public:
                key = item["work_step_key"]
                if key not in work_steps_by_key:
                    work_steps_by_key[key] = {
                        field: item[field]
                        for field in (
                            "work_step_key",
                            "work_step_label",
                            "work_step_test_type",
                            "work_step_test_type_label_zh",
                            "work_step_cathodic_current_ma_cm2",
                            "work_step_recovery_current_ma_cm2",
                            "work_step_cathodic_duration_s",
                            "work_step_recovery_duration_s",
                        )
                    }
                    work_steps_by_key[key]["series_count"] = 0
                    work_step_materials[key] = set()
                work_steps_by_key[key]["series_count"] += 1
                material_key = str(item.get("material_relative_path") or "")
                if material_key:
                    work_step_materials[key].add(material_key)
            work_steps = []
            for key, work_step in work_steps_by_key.items():
                work_step["material_count"] = len(work_step_materials[key])
                work_steps.append(work_step)
            payload = {
                "series": public,
                "count": len(public),
                "work_steps": work_steps,
                "work_step_count": len(work_steps),
            }

            final_signature = self._file_cache_signature(path)
            final_generation = self._cache_generation_token()
            estimated_bytes = self._payload_size_bytes(payload)
            if (
                final_signature == signature
                and final_generation == generation
                and estimated_bytes <= max(0, int(self.SERIES_CACHE_MAX_BYTES))
            ):
                self._series_cache_key = cache_key
                self._series_cache_payload = copy.deepcopy(payload)
                self._series_cache_bytes = estimated_bytes
            return payload, cache_key

    def series(self) -> dict[str, Any]:
        payload, _ = self._series_payload()
        return payload

    def stability_catalog(self) -> dict[str, Any]:
        try:
            return self.stability_analyzer.catalog()
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            raise StartStopWorkspaceError(
                "稳定性数据目录当前无法读取。",
                503,
            ) from exc

    def stability_chart_export(self, *, series_ids, analysis_mode, metric, export_format):
        return export_stability(self, series_ids=series_ids, analysis_mode=analysis_mode,
                                metric=metric, export_format=export_format)

    def stability_chart(
        self,
        *,
        series_ids: Iterable[str],
        analysis_mode: str,
        metric: str,
        max_points: int = 6000,
    ) -> dict[str, Any]:
        try:
            return self.stability_analyzer.chart(
                series_ids=series_ids,
                analysis_mode=analysis_mode,
                metric=metric,
                max_points=max_points,
            )
        except StabilityAnalysisError as exc:
            raise StartStopWorkspaceError(str(exc), exc.status) from exc
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            raise StartStopWorkspaceError(
                "稳定性曲线当前无法读取。",
                503,
            ) from exc

    def live_analysis_context(self) -> dict[str, Any]:
        """Return bounded formal metadata used to extend active files safely."""
        payload = self.series()
        raw_rows = self._read_csv(self.SERIES_NAME)
        raw_by_id = {
            str(row.get("series_id") or ""): row
            for row in raw_rows
            if str(row.get("series_id") or "")
        }
        water_by_id: dict[str, dict[str, str]] = {}
        water_path = self._path(self.WATER_SERIES_NAME, require=False)
        if water_path.is_file():
            water_by_id = {
                str(row.get("series_id") or ""): row
                for row in self._read_csv(self.WATER_SERIES_NAME)
                if str(row.get("series_id") or "")
            }
        series: list[dict[str, Any]] = []
        for public in payload.get("series", []):
            if not isinstance(public, dict):
                continue
            item = dict(public)
            series_id = str(item.get("series_id") or "")
            raw = raw_by_id.get(series_id, {})
            water = water_by_id.get(series_id, {})
            item.update(
                {
                    "is_primary_series": _bool(raw.get("is_primary_series")),
                    "is_special_series": _bool(raw.get("is_special_series")),
                    "special_file_name": str(raw.get("special_file_name") or ""),
                    "cathodic_baseline_first10_median_raw_v": _number(
                        raw.get("cathodic_baseline_first10_median_raw_v")
                    ),
                    "reverse_baseline_first10_median_raw_v": _number(
                        raw.get("reverse_baseline_first10_median_raw_v")
                    ),
                    "water_comp_cathodic_baseline_first10_raw_v": _number(
                        water.get("water_comp_cathodic_baseline_first10_raw_v")
                    ),
                    "water_comp_recovery_baseline_first10_raw_v": _number(
                        water.get("water_comp_recovery_baseline_first10_raw_v")
                    ),
                }
            )
            series.append(item)

        segments: list[dict[str, Any]] = []
        segment_path = self._path(self.SEGMENT_NAME, require=False)
        if segment_path.is_file():
            for row in self._read_csv(self.SEGMENT_NAME):
                segments.append(
                    {
                        "series_id": str(row.get("series_id") or ""),
                        "segment_index": _integer(row.get("segment_index")),
                        "source_file": str(row.get("source_file") or ""),
                        "complete_cycles": _integer(row.get("complete_cycles")),
                        "continuous_time_start_h": _number(
                            row.get("continuous_time_start_h"), 0.0
                        ),
                        "continuous_time_end_h": _number(
                            row.get("continuous_time_end_h"), 0.0
                        ),
                    }
                )
        summary = self._read_json(self.SUMMARY_NAME, optional=True)
        water = summary.get("water_compensation")
        water = water if isinstance(water, dict) else {}
        model = water.get("model")
        model = model if isinstance(model, dict) else water
        return {
            "series": series,
            "segments": segments,
            "rules": {
                "water_resistance_drift_ohm_cm2_per_h": _number(
                    model.get("area_specific_resistance_drift_ohm_cm2_per_h")
                ),
            },
        }

    @staticmethod
    def _json_list(value: Any) -> list[str]:
        try:
            parsed = json.loads(str(value or "[]"))
        except json.JSONDecodeError:
            return []
        if not isinstance(parsed, list):
            return []
        return [str(item) for item in parsed]

    def _read_csv(self, relative: str) -> list[dict[str, str]]:
        path = self._path(relative, require=True)
        try:
            if path.stat().st_size > self.MAX_CSV_BYTES:
                raise StartStopWorkspaceError(
                    "启停分析 CSV 超过网页安全读取上限。",
                    413,
                )
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                return list(csv.DictReader(handle))
        except StartStopWorkspaceError:
            raise
        except (OSError, UnicodeDecodeError, csv.Error) as exc:
            raise StartStopWorkspaceError(f"无法读取启停分析表：{relative}", 500) from exc

    @staticmethod
    def _downsample(points: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        if len(points) <= limit:
            return points
        if limit <= 2:
            return [points[0], points[-1]]
        indices = {
            round(index * (len(points) - 1) / (limit - 1))
            for index in range(limit)
        }
        return [points[index] for index in sorted(indices)]

    def _chart_request_context(
        self,
        *,
        series_ids: Iterable[str],
        metric: str,
        x_axis: str,
        mode: str,
        maximum_series: int | None = None,
    ) -> dict[str, Any]:
        metric = str(metric or "cathodic")
        x_axis = str(x_axis or "cycle")
        mode = str(mode or "raw")
        if metric not in self.METRICS:
            raise StartStopWorkspaceError("不支持的启停图表指标。")
        if x_axis not in {"cycle", "time"}:
            raise StartStopWorkspaceError("横轴只支持 cycle 或 time。")
        if mode not in {"raw", "water", "compare"}:
            raise StartStopWorkspaceError("曲线模式只支持 raw、water 或 compare。")
        requested = [str(item) for item in series_ids if str(item)]
        if not requested:
            raise StartStopWorkspaceError("至少需要选择 1 条分析序列。")
        limit = self.MAX_SERIES_PER_CHART if maximum_series is None else int(maximum_series)
        if len(requested) > limit:
            raise StartStopWorkspaceError(f"一次最多选择 {limit} 条分析序列。")
        if len(set(requested)) != len(requested):
            raise StartStopWorkspaceError("分析序列不能重复。")

        series_payload, series_cache_key = self._series_payload()
        known = {row["series_id"]: row for row in series_payload["series"]}
        if not set(requested).issubset(known):
            raise StartStopWorkspaceError("选中的分析序列不在当前结果中。", 404)
        requested_work_steps = {
            str(known[series_id].get("work_step_key") or "")
            for series_id in requested
        }
        if len(requested_work_steps) != 1:
            raise StartStopWorkspaceError(
                "不同启停工步不能合并比较；请先选择同一类型、电流和阶段时长的工步。"
            )
        if metric == "minimum_time":
            material_keys = {
                str(known[series_id].get("material_relative_path") or f"series:{series_id}")
                for series_id in requested
            }
            if len(material_keys) != 1:
                raise StartStopWorkspaceError(
                    "异常判断一次只能分析一个材料；同一材料的多条接续序列可以同时判断。"
                )

        overview = metric == "overview"
        relative = (
            self.WATER_OVERVIEW_NAME
            if overview and mode in {"water", "compare"}
            else self.OVERVIEW_NAME
            if overview
            else self.WATER_CYCLE_NAME
            if mode in {"water", "compare"}
            else self.CYCLE_NAME
        )
        path = self._path(relative, require=True)
        data_signature = self._file_cache_signature(path)
        if data_signature[3] > self.MAX_CSV_BYTES:
            raise StartStopWorkspaceError(
                "当前交互曲线文件超过安全读取上限。",
                413,
            )
        return {
            "metric": metric,
            "metric_spec": self.METRICS[metric],
            "x_axis": x_axis,
            "mode": mode,
            "requested": requested,
            "known": known,
            "work_step_key": next(iter(requested_work_steps)),
            "overview": overview,
            "variants": ["raw", "water"] if mode == "compare" else [mode],
            "path": path,
            "data_signature": data_signature,
            "generation": self._cache_generation_token(),
            "series_cache_key": series_cache_key,
        }

    def _read_chart_groups(
        self,
        context: dict[str, Any],
        *,
        maximum_points: int | None = None,
    ) -> tuple[dict[tuple[str, str], list[dict[str, Any]]], dict[str, str], int]:
        requested = context["requested"]
        variants = context["variants"]
        overview = context["overview"]
        x_axis = context["x_axis"]
        metric_spec = context["metric_spec"]
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {
            (series_id, variant): []
            for series_id in requested
            for variant in variants
        }
        requested_set = set(requested)
        names: dict[str, str] = {}
        total_points = 0
        try:
            with context["path"].open("r", encoding="utf-8-sig", newline="") as handle:
                for row in csv.DictReader(handle):
                    series_id = str(row.get("series_id") or "")
                    if series_id not in requested_set:
                        continue
                    names[series_id] = str(
                        row.get("series_display_name")
                        or row.get("material_display_name")
                        or series_id
                    )
                    x = _number(
                        row.get("continuous_time_h")
                        if overview or x_axis == "time"
                        else row.get("cycle")
                    )
                    if x is None:
                        continue
                    for variant in variants:
                        y = _number(row.get(metric_spec[variant]))
                        if y is None:
                            continue
                        total_points += 1
                        if maximum_points is not None and total_points > maximum_points:
                            raise StartStopWorkspaceError(
                                "当前高亮数据量过大，请减少高亮曲线后再导出。",
                                413,
                            )
                        grouped[(series_id, variant)].append(
                            {
                                "x": x,
                                "y": y,
                                "cycle": _integer(row.get("cycle")),
                                "status": str(row.get("cathodic_shift_status") or ""),
                                "source_file": str(row.get("source_file") or ""),
                                "segment_index": _integer(row.get("segment_index")),
                            }
                        )
        except StartStopWorkspaceError:
            raise
        except (OSError, UnicodeDecodeError, csv.Error) as exc:
            raise StartStopWorkspaceError(
                "无法读取交互曲线数据。",
                500,
            ) from exc
        return grouped, names, total_points

    def chart_data(
        self,
        *,
        series_ids: Iterable[str],
        metric: str,
        x_axis: str,
        mode: str,
        max_points: int,
    ) -> dict[str, Any]:
        context = self._chart_request_context(
            series_ids=series_ids,
            metric=metric,
            x_axis=x_axis,
            mode=mode,
        )
        metric = context["metric"]
        x_axis = context["x_axis"]
        mode = context["mode"]
        requested = context["requested"]
        known = context["known"]
        variants = context["variants"]
        metric_spec = context["metric_spec"]
        overview = context["overview"]
        path = context["path"]
        data_signature = context["data_signature"]
        generation = context["generation"]
        series_cache_key = context["series_cache_key"]
        requested_max_points = int(max_points)
        limit = max(200, min(requested_max_points, self.MAX_POINTS_PER_SERIES))
        cache_key = (
            "chart",
            generation,
            series_cache_key,
            data_signature,
            tuple(requested),
            metric,
            x_axis,
            mode,
            requested_max_points,
            limit,
        )
        with self._chart_cache_lock:
            cached = self._chart_cache_get_locked(cache_key)
            if cached is not None:
                return cached
            grouped, names, _ = self._read_chart_groups(context)

            payload_series = []
            for series_id in requested:
                for variant in variants:
                    points = self._downsample(
                        grouped[(series_id, variant)],
                        limit,
                    )
                    payload_series.append(
                        {
                            "series_id": series_id,
                            "name": names.get(series_id, series_id),
                            "variant": variant,
                            "points": points,
                        }
                    )
            payload = {
                "metric": metric,
                "metric_label": metric_spec["label"],
                "unit": metric_spec["unit"],
                "x_axis": x_axis,
                "x_label": (
                    "累计时间 / h"
                    if x_axis == "time" or overview
                    else "循环数"
                ),
                "mode": mode,
                "work_step_key": known[requested[0]]["work_step_key"],
                "work_step_label": known[requested[0]]["work_step_label"],
                "series": payload_series,
                "anomaly_boundary_s": 15.0,
            }

            # A render may atomically replace ``current`` while a request is
            # reading the prior file.  That response is still internally
            # usable, but only a fully stable read may populate the cache.
            stable = (
                self._file_cache_signature(path) == data_signature
                and self._cache_generation_token() == generation
                and series_cache_key[1] == generation
                and self._file_cache_signature(
                    self._path(self.SERIES_NAME, require=True)
                )
                == series_cache_key[2]
            )
            if stable:
                self._chart_cache_put_locked(cache_key, payload)
            return payload
