from __future__ import annotations

import concurrent.futures
import csv
import datetime as dt
import io
import json
import sys
import tempfile
import textwrap
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from echem_platform.start_stop import (
    StartStopWorkspace,
    StartStopWorkspaceError,
    _work_step_metadata,
)
from echem_platform.start_stop_database import StartStopDatabase


APP = types.SimpleNamespace(
    Database=StartStopDatabase,
    StartStopWorkspace=StartStopWorkspace,
    StartStopWorkspaceError=StartStopWorkspaceError,
    dt=dt,
)


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


class StartStopWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.analysis = self.root / "analysis"
        self.analysis.mkdir()
        self.database = APP.Database(self.root / "state" / "workspace.sqlite3")
        self.workspace = APP.StartStopWorkspace(
            self.database,
            self.analysis,
            python_executable=str(self.root / "missing-python"),
        )
        self._build_fixture()

    def tearDown(self):
        self.temporary.cleanup()

    def _build_fixture(self):
        materials = [
            {
                "key": "lab/A",
                "auto_name": "A",
                "standard_file_count": 1,
                "test_types": "standard_start_stop",
                "cathodic_current_median_a_cm2": -0.3,
                "recovery_current_median_a_cm2": 0.03,
                "total_data_points": 100,
                "ordered_source_files": ["lab/A/启停.txt"],
                "fingerprint": "fingerprint-a",
            },
            {
                "key": "lab/B",
                "auto_name": "B",
                "standard_file_count": 1,
                "test_types": "adt_start_stop",
                "cathodic_current_median_a_cm2": -0.5,
                "recovery_current_median_a_cm2": 0.0,
                "total_data_points": 80,
                "ordered_source_files": ["lab/B/ADT.txt"],
                "fingerprint": "fingerprint-b",
            },
        ]
        write_json(
            self.analysis / "material_config_snapshot.json",
            {
                "generated_at": "2026-08-02T10:00:00",
                "dataset_fingerprint": "dataset-1",
                "materials": materials,
                "files": [
                    {
                        "relative_path": "lab/A/启停.txt",
                        "file_mtime": "2026-08-02T09:30:00+08:00",
                        "included_in_analysis": True,
                    },
                    {
                        "relative_path": "lab/B/ADT.txt",
                        "file_mtime": "2026-08-01T11:20:00+08:00",
                        "included_in_analysis": True,
                    },
                ],
            },
        )
        write_json(
            self.analysis / ".material_config_readback.json",
            {
                "dataset_fingerprint": "dataset-1",
                "materials": [
                    {
                        "key": "lab/A",
                        "plot_name": "材料 A 完整名称",
                        "include_in_summary_atlas": True,
                        "fingerprint": "fingerprint-a",
                        "notes": "",
                    },
                    {
                        "key": "lab/B",
                        "plot_name": "材料 B 完整名称",
                        "include_in_summary_atlas": False,
                        "fingerprint": "fingerprint-b",
                        "notes": "ADT",
                    },
                ],
            },
        )
        write_json(
            self.analysis / "analysis_summary.json",
            {
                "created_at": "2026-08-02T10:30:00",
                "material_config": {
                    "dataset_fingerprint": "dataset-1",
                    "selected_materials": [
                        {
                            "material_relative_path": "lab/A",
                            "material_display_name": "材料 A 完整名称",
                            "material_user_notes": "",
                        }
                    ],
                    "excluded_from_atlas": [
                        {
                            "material_relative_path": "lab/B",
                            "material_display_name": "材料 B 完整名称",
                            "material_user_notes": "ADT",
                        }
                    ],
                },
                "counts": {
                    "materials": 2,
                    "summary_atlas_materials": 1,
                    "included_standard_files": 2,
                    "complete_cycles": 4,
                    "normal_cycles": 2,
                    "abnormal_cycles": 2,
                },
                "water_compensation": {
                    "model": {
                        "reference_material": "材料 A 完整名称",
                        "reference_fit_slope_mv_per_h": -6.34,
                    }
                },
            },
        )
        series_rows = []
        for index, key in enumerate(("lab/A", "lab/B"), start=1):
            series_rows.append(
                {
                    "series_id": f"M0{index}-main",
                    "series_order": index,
                    "series_display_name": f"材料 {chr(64 + index)} 完整名称",
                    "material_id": f"M0{index}",
                    "material_relative_path": key,
                    "test_type": "standard_start_stop" if index == 1 else "adt_start_stop",
                    "test_type_label_zh": "标准启停" if index == 1 else "ADT 启停",
                    "include_in_summary_atlas": "True" if index == 1 else "False",
                    "complete_cycles": 2,
                    "duration_h": 1,
                    "normal_cycles": 1,
                    "abnormal_cycles": 1,
                    "abnormal_fraction": 0.5,
                    "segment_count": 1,
                    "source_files": json.dumps([f"{key}/启停.txt"]),
                    "endpoint_statistic": "阶段最后 1 s 中位数",
                    "cathodic_current_median_a_cm2": -0.3 if index == 1 else -0.5,
                    "recovery_current_median_a_cm2": 0.03 if index == 1 else 0.0,
                    "median_cathodic_phase_duration_s": 30 if index == 1 else 28,
                    "median_reverse_phase_duration_s": 30 if index == 1 else 28,
                    "median_cycle_duration_s": 60 if index == 1 else 56,
                    "cathodic_negative_shift_first10_to_last10_mv": 10,
                }
            )
        write_csv(self.analysis / "series_summary_raw.csv", series_rows)

        cycle_rows = []
        water_rows = []
        for series_index in (1, 2):
            for cycle in (1, 2):
                status = "normal" if cycle == 1 else "abnormal"
                base = {
                    "series_id": f"M0{series_index}-main",
                    "series_display_name": f"材料 {chr(64 + series_index)} 完整名称",
                    "cycle": cycle,
                    "cathodic_endpoint_time_h": cycle / 60,
                    "continuous_time_h": cycle / 60,
                    "cathodic_last1s_median_raw_v": -0.5 - 0.01 * cycle,
                    "cathodic_negative_shift_mv": 10 * cycle,
                    "reverse_last1s_median_raw_v": 1.5 + 0.01 * cycle,
                    "cathodic_phase_min_time_s": 20 if status == "normal" else 5,
                    "cathodic_shift_status": status,
                    "source_file": f"lab/{series_index}/启停.txt",
                    "segment_index": 1,
                }
                cycle_rows.append(base)
                water_rows.append(
                    {
                        **base,
                        "cathodic_last1s_median_water_compensated_v": base[
                            "cathodic_last1s_median_raw_v"
                        ] + 0.001,
                        "cathodic_negative_shift_water_compensated_mv": 9 * cycle,
                        "reverse_last1s_median_water_compensated_v": base[
                            "reverse_last1s_median_raw_v"
                        ] - 0.001,
                    }
                )
        write_csv(self.analysis / "cycle_summary_raw.csv", cycle_rows)
        write_csv(
            self.analysis / "water_compensation" / "cycle_summary_water_compensated.csv",
            water_rows,
        )
        (self.analysis / "启停数据_原始电位_全量图集.pdf").write_bytes(
            b"%PDF-1.4\nfixture\n%%EOF"
        )
        (self.analysis / "启停数据_水位补偿图集.pdf").write_bytes(
            b"%PDF-1.4\nwater\n%%EOF"
        )
        (self.analysis / "analyze_and_plot_start_stop.py").write_text(
            "print('fixture')\n",
            encoding="utf-8",
        )

    def test_reads_current_materials_status_and_pdf_without_exposing_paths(self):
        status = self.workspace.status()
        materials = self.workspace.materials()

        self.assertTrue(status["available"])
        self.assertTrue(status["analysis_ready"])
        self.assertTrue(status["export_ready"])
        self.assertEqual(status["counts"]["materials"], 2)
        self.assertEqual(status["counts"]["selected_materials"], 1)
        self.assertEqual(status["last_data_update_at"], "2026-08-02T10:00:00")
        self.assertEqual(status["created_at"], "2026-08-02T10:30:00")
        self.assertFalse(status["execution"]["ready"])
        self.assertEqual(materials["counts"]["changed"], 0)
        self.assertEqual(
            materials["materials"][0]["latest_source_modified_at"],
            "2026-08-02T09:30:00+08:00",
        )
        self.assertEqual(
            materials["materials"][1]["latest_source_modified_at"],
            "2026-08-01T11:20:00+08:00",
        )
        self.assertNotIn(str(self.analysis), json.dumps(status, ensure_ascii=False))

        pdf, name = self.workspace.pdf("standard")
        self.assertTrue(pdf.read_bytes().startswith(b"%PDF-"))
        self.assertEqual(name, "启停数据_原始电位_全量图集.pdf")

    def test_current_analysis_remains_ready_without_forcing_pdf_export(self):
        (self.analysis / self.workspace.STANDARD_PDF).unlink()
        (self.analysis / self.workspace.WATER_PDF).unlink()

        status = self.workspace.status()

        self.assertTrue(status["analysis_ready"])
        self.assertFalse(status["export_ready"])
        self.assertEqual(status["pdf"], {"standard": False, "water": False})
        with self.assertRaises(APP.StartStopWorkspaceError) as caught:
            self.workspace.pdf("standard")
        self.assertEqual(caught.exception.status, 409)
        self.assertIn("材料库", str(caught.exception))

    def test_last_data_update_falls_back_to_snapshot_mtime(self):
        snapshot_path = self.analysis / "material_config_snapshot.json"
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        snapshot.pop("generated_at")
        write_json(snapshot_path, snapshot)

        status = self.workspace.status()

        parsed = APP.dt.datetime.fromisoformat(status["last_data_update_at"])
        self.assertIsNotNone(parsed.tzinfo)
        self.assertAlmostEqual(
            parsed.timestamp(),
            snapshot_path.stat().st_mtime,
            delta=1.1,
        )

    def test_config_save_is_revision_guarded_and_marks_render_stale(self):
        payload = self.workspace.materials()
        rows = [
            {
                "key": row["key"],
                "plot_name": row["plot_name"] + " 新",
                "include_in_summary_atlas": row["key"] == "lab/B",
                "notes": row["notes"],
            }
            for row in payload["materials"]
        ]
        saved = self.workspace.save_materials(
            dataset_fingerprint=payload["dataset_fingerprint"],
            expected_revision=0,
            materials=rows,
        )

        self.assertEqual(saved["revision"], 1)
        self.assertEqual(saved["counts"]["selected"], 1)
        self.assertTrue(self.workspace.status()["configuration_stale"])
        with self.assertRaises(APP.StartStopWorkspaceError) as caught:
            self.workspace.save_materials(
                dataset_fingerprint=payload["dataset_fingerprint"],
                expected_revision=0,
                materials=rows,
            )
        self.assertEqual(caught.exception.status, 409)

    def test_material_favorites_persist_without_changing_atlas_selection(self):
        payload = self.workspace.materials()
        rows = [
            {
                "key": row["key"],
                "plot_name": row["plot_name"],
                "include_in_summary_atlas": row["include_in_summary_atlas"],
                "favorite": row["key"] == "lab/B",
                "notes": row["notes"],
            }
            for row in payload["materials"]
        ]

        saved = self.workspace.save_materials(
            dataset_fingerprint=payload["dataset_fingerprint"],
            expected_revision=payload["revision"],
            materials=rows,
        )

        self.assertEqual(saved["counts"]["favorites"], 1)
        self.assertEqual(saved["counts"]["selected"], 1)
        self.assertFalse(self.workspace.status()["configuration_stale"])
        by_key = {row["key"]: row for row in self.workspace.materials()["materials"]}
        self.assertFalse(by_key["lab/A"]["favorite"])
        self.assertTrue(by_key["lab/B"]["favorite"])

    def test_chart_compare_returns_raw_and_water_points_with_anomaly_status(self):
        self.assertEqual(self.workspace.MAX_SERIES_PER_CHART, 64)
        chart = self.workspace.chart_data(
            series_ids=["M01-main"],
            metric="cathodic",
            x_axis="cycle",
            mode="compare",
            max_points=400,
        )

        self.assertEqual(len(chart["series"]), 2)
        self.assertEqual(
            {row["variant"] for row in chart["series"]},
            {"raw", "water"},
        )
        self.assertEqual(
            chart["work_step_label"],
            "标准启停 · −300 ↔ +30 mA·cm⁻² · 30 + 30 s",
        )
        first = chart["series"][0]["points"]
        self.assertEqual([point["status"] for point in first], ["normal", "abnormal"])
        self.assertAlmostEqual(first[0]["y"], -0.51)

    def test_series_exposes_distinct_scientific_work_steps(self):
        payload = self.workspace.series()

        self.assertEqual(payload["work_step_count"], 2)
        self.assertEqual(
            [item["work_step_label"] for item in payload["work_steps"]],
            [
                "标准启停 · −300 ↔ +30 mA·cm⁻² · 30 + 30 s",
                "ADT 启停 · −500 ↔ 0 mA·cm⁻² · 28 + 28 s",
            ],
        )
        self.assertEqual(
            {item["material_count"] for item in payload["work_steps"]},
            {1},
        )
        by_id = {item["series_id"]: item for item in payload["series"]}
        self.assertNotEqual(
            by_id["M01-main"]["work_step_key"],
            by_id["M02-main"]["work_step_key"],
        )

    def test_live_analysis_context_exposes_only_formal_continuation_metadata(self):
        series_path = self.analysis / "series_summary_raw.csv"
        with series_path.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        rows[0]["cathodic_baseline_first10_median_raw_v"] = "-1.55"
        rows[0]["reverse_baseline_first10_median_raw_v"] = "0.18"
        write_csv(series_path, rows)
        write_csv(
            self.analysis
            / "water_compensation"
            / "series_summary_water_compensated.csv",
            [
                {
                    "series_id": "M01-main",
                    "water_comp_cathodic_baseline_first10_raw_v": -1.54,
                    "water_comp_recovery_baseline_first10_raw_v": 0.17,
                }
            ],
        )
        write_csv(
            self.analysis / "segment_summary_raw.csv",
            [
                {
                    "series_id": "M01-main",
                    "segment_index": 1,
                    "source_file": "lab/A/启停.txt",
                    "complete_cycles": 2,
                    "continuous_time_start_h": 0.0,
                    "continuous_time_end_h": 1.0,
                }
            ],
        )
        summary_path = self.analysis / "analysis_summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        summary["water_compensation"]["model"][
            "area_specific_resistance_drift_ohm_cm2_per_h"
        ] = 0.2
        write_json(summary_path, summary)

        context = self.workspace.live_analysis_context()

        by_id = {item["series_id"]: item for item in context["series"]}
        self.assertAlmostEqual(
            by_id["M01-main"]["cathodic_baseline_first10_median_raw_v"],
            -1.55,
        )
        self.assertAlmostEqual(
            by_id["M01-main"]["water_comp_cathodic_baseline_first10_raw_v"],
            -1.54,
        )
        self.assertEqual(context["segments"][0]["source_file"], "lab/A/启停.txt")
        self.assertEqual(context["segments"][0]["complete_cycles"], 2)
        self.assertAlmostEqual(
            context["rules"]["water_resistance_drift_ohm_cm2_per_h"],
            0.2,
        )
        self.assertNotIn(str(self.analysis), json.dumps(context, ensure_ascii=False))

    def test_300_30_5min_analysis_step_has_a_distinct_key_and_readable_label(self):
        work_step = _work_step_metadata(
            {
                "test_type": "standard_start_stop",
                "test_type_label_zh": "标准启停",
                "cathodic_current_median_a_cm2": -0.3,
                "recovery_current_median_a_cm2": 0.03,
                "median_cathodic_phase_duration_s": 300,
                "median_reverse_phase_duration_s": 300,
            }
        )

        self.assertEqual(
            work_step["work_step_key"],
            "standard_start_stop|jc=-300|jr=30|tc=300|tr=300",
        )
        self.assertEqual(
            work_step["work_step_label"],
            "标准启停 · −300 ↔ +30 mA·cm⁻² · 5 + 5 min",
        )

    def test_chart_rejects_mixed_work_steps_before_reading_cycle_data(self):
        (self.analysis / "cycle_summary_raw.csv").unlink()

        with self.assertRaises(APP.StartStopWorkspaceError) as caught:
            self.workspace.chart_data(
                series_ids=["M01-main", "M02-main"],
                metric="cathodic",
                x_axis="cycle",
                mode="raw",
                max_points=400,
            )

        self.assertEqual(caught.exception.status, 400)
        self.assertIn("不同启停工步不能合并比较", str(caught.exception))

    def test_render_options_capture_changed_materials_and_cli_arguments(self):
        payload = {
            "dataset_fingerprint": "dataset-current",
            "materials": [
                {
                    "key": "material-a",
                    "auto_name": "A",
                    "plot_name": "A",
                    "include_in_summary_atlas": True,
                    "favorite": True,
                    "status": "未变化",
                    "fingerprint": "same",
                    "notes": "",
                },
                {
                    "key": "material-b",
                    "auto_name": "B",
                    "plot_name": "B",
                    "include_in_summary_atlas": True,
                    "status": "数据已更新",
                    "fingerprint": "new",
                    "notes": "",
                },
            ],
        }
        changed = self.workspace._changed_material_keys(
            payload,
            {"material-a": "same", "material-b": "old"},
        )
        config = self.workspace._config_for_render(
            payload,
            data_mode="raw",
            material_scope="updated",
            updated_material_keys=changed,
        )
        arguments = self.workspace._render_command_options(config)

        self.assertEqual(changed, {"material-b"})
        self.assertEqual(
            config["render_options"]["updated_material_keys"],
            ["material-b"],
        )
        self.assertFalse(config["render_options"]["export_pdf"])
        self.assertNotIn("favorite", config["materials"][0])
        self.assertEqual(
            arguments,
            [
                "--data-mode",
                "raw",
                "--material-scope",
                "updated",
                "--updated-material-key",
                "material-b",
            ],
        )

        export_config = self.workspace._config_for_render(
            payload,
            export_pdf=True,
        )
        self.assertEqual(
            self.workspace._render_command_options(export_config)[-1],
            "--export-pdf",
        )

    def test_anomaly_chart_rejects_cross_material_series_before_reading_cycle_data(self):
        series_path = self.analysis / "series_summary_raw.csv"
        with series_path.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        rows[1].update(
            {
                "test_type": "standard_start_stop",
                "test_type_label_zh": "标准启停",
                "cathodic_current_median_a_cm2": -0.3,
                "recovery_current_median_a_cm2": 0.03,
                "median_cathodic_phase_duration_s": 30,
                "median_reverse_phase_duration_s": 30,
                "median_cycle_duration_s": 60,
            }
        )
        write_csv(series_path, rows)
        (self.analysis / "cycle_summary_raw.csv").unlink()

        with self.assertRaises(APP.StartStopWorkspaceError) as caught:
            self.workspace.chart_data(
                series_ids=["M01-main", "M02-main"],
                metric="minimum_time",
                x_axis="cycle",
                mode="raw",
                max_points=400,
            )

        self.assertEqual(caught.exception.status, 400)
        self.assertIn("一次只能分析一个材料", str(caught.exception))

    def test_anomaly_chart_allows_multiple_series_from_the_same_material(self):
        series_path = self.analysis / "series_summary_raw.csv"
        with series_path.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        second_segment = dict(rows[0])
        second_segment["series_id"] = "M01-segment-2"
        second_segment["series_order"] = "3"
        rows.append(second_segment)
        write_csv(series_path, rows)

        chart = self.workspace.chart_data(
            series_ids=["M01-main", "M01-segment-2"],
            metric="minimum_time",
            x_axis="cycle",
            mode="raw",
            max_points=400,
        )

        self.assertEqual(
            [item["series_id"] for item in chart["series"]],
            ["M01-main", "M01-segment-2"],
        )

    def test_chart_cache_reuses_csv_and_returns_independent_payloads(self):
        self.workspace.series()
        target = (self.analysis / "cycle_summary_raw.csv").resolve()
        real_open = io.open
        read_count = 0

        def counting_open(file, *args, **kwargs):
            nonlocal read_count
            try:
                candidate = Path(file).resolve()
            except TypeError:
                candidate = None
            if candidate == target:
                read_count += 1
            return real_open(file, *args, **kwargs)

        arguments = {
            "series_ids": ["M01-main"],
            "metric": "cathodic",
            "x_axis": "cycle",
            "mode": "raw",
            "max_points": 400,
        }
        with mock.patch("io.open", side_effect=counting_open):
            first = self.workspace.chart_data(**arguments)
            first["series"][0]["points"][0]["y"] = 999.0
            second = self.workspace.chart_data(**arguments)

        self.assertEqual(read_count, 1)
        self.assertAlmostEqual(second["series"][0]["points"][0]["y"], -0.51)

    def test_chart_cache_invalidates_for_file_and_generation_changes(self):
        self.workspace.series()
        target = (self.analysis / "cycle_summary_raw.csv").resolve()
        real_open = io.open
        read_count = 0
        generation = ["generation-1"]

        def counting_open(file, *args, **kwargs):
            nonlocal read_count
            try:
                candidate = Path(file).resolve()
            except TypeError:
                candidate = None
            if candidate == target:
                read_count += 1
            return real_open(file, *args, **kwargs)

        arguments = {
            "series_ids": ["M01-main"],
            "metric": "cathodic",
            "x_axis": "cycle",
            "mode": "raw",
            "max_points": 400,
        }
        token = lambda: ("repository", generation[0])
        with mock.patch.object(
            self.workspace,
            "_cache_generation_token",
            side_effect=token,
        ), mock.patch("io.open", side_effect=counting_open):
            self.workspace.chart_data(**arguments)
            self.workspace.chart_data(**arguments)
            generation[0] = "generation-2"
            self.workspace.chart_data(**arguments)

        self.assertEqual(read_count, 2)

        with target.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        rows[0]["cathodic_last1s_median_raw_v"] = "-0.777"
        write_csv(target, rows)
        with mock.patch("io.open", side_effect=counting_open):
            changed = self.workspace.chart_data(**arguments)

        self.assertEqual(read_count, 3)
        self.assertAlmostEqual(changed["series"][0]["points"][0]["y"], -0.777)

    def test_chart_cache_is_single_flight_for_concurrent_requests(self):
        self.workspace.series()
        target = (self.analysis / "cycle_summary_raw.csv").resolve()
        real_open = io.open
        read_count = 0
        count_lock = threading.Lock()
        barrier = threading.Barrier(4)

        def counting_open(file, *args, **kwargs):
            nonlocal read_count
            try:
                candidate = Path(file).resolve()
            except TypeError:
                candidate = None
            if candidate == target:
                with count_lock:
                    read_count += 1
                time.sleep(0.05)
            return real_open(file, *args, **kwargs)

        def request_chart():
            barrier.wait(timeout=2)
            return self.workspace.chart_data(
                series_ids=["M01-main"],
                metric="cathodic",
                x_axis="cycle",
                mode="raw",
                max_points=400,
            )

        with mock.patch("io.open", side_effect=counting_open):
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                results = list(executor.map(lambda _: request_chart(), range(4)))

        self.assertEqual(read_count, 1)
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(len({id(result) for result in results}), 4)

    def test_chart_cache_query_dimensions_are_strictly_isolated(self):
        self.workspace.series()
        targets = {
            (self.analysis / "cycle_summary_raw.csv").resolve(),
            (
                self.analysis
                / "water_compensation"
                / "cycle_summary_water_compensated.csv"
            ).resolve(),
        }
        real_open = io.open
        read_count = 0

        def counting_open(file, *args, **kwargs):
            nonlocal read_count
            try:
                candidate = Path(file).resolve()
            except TypeError:
                candidate = None
            if candidate in targets:
                read_count += 1
            return real_open(file, *args, **kwargs)

        base = {
            "series_ids": ["M01-main"],
            "metric": "cathodic",
            "x_axis": "cycle",
            "mode": "raw",
            "max_points": 400,
        }
        queries = [
            base,
            {**base, "series_ids": ["M02-main"]},
            {**base, "metric": "reverse"},
            {**base, "x_axis": "time"},
            {**base, "mode": "water"},
            {**base, "max_points": 401},
        ]
        with mock.patch("io.open", side_effect=counting_open):
            for query in queries:
                self.workspace.chart_data(**query)
            for query in queries:
                self.workspace.chart_data(**query)

        self.assertEqual(read_count, len(queries))

    def test_chart_cache_entry_limit_evicts_lru(self):
        self.workspace.series()
        self.workspace.CHART_CACHE_MAX_ENTRIES = 2
        target = (self.analysis / "cycle_summary_raw.csv").resolve()
        real_open = io.open
        read_count = 0

        def counting_open(file, *args, **kwargs):
            nonlocal read_count
            try:
                candidate = Path(file).resolve()
            except TypeError:
                candidate = None
            if candidate == target:
                read_count += 1
            return real_open(file, *args, **kwargs)

        base = {
            "series_ids": ["M01-main"],
            "metric": "cathodic",
            "x_axis": "cycle",
            "mode": "raw",
            "max_points": 400,
        }
        different_metric = {**base, "metric": "reverse"}
        different_limit = {**base, "max_points": 401}
        with mock.patch("io.open", side_effect=counting_open):
            self.workspace.chart_data(**base)
            self.workspace.chart_data(**different_metric)
            self.workspace.chart_data(**different_limit)
            self.workspace.chart_data(**base)

        self.assertEqual(read_count, 4)
        self.assertLessEqual(
            len(self.workspace._chart_cache),
            self.workspace.CHART_CACHE_MAX_ENTRIES,
        )
        self.assertLessEqual(
            self.workspace._chart_cache_bytes,
            self.workspace.CHART_CACHE_MAX_BYTES,
        )

    def test_chart_cache_skips_payload_above_byte_limit(self):
        self.workspace.series()
        self.workspace.CHART_CACHE_MAX_BYTES = 1
        target = (self.analysis / "cycle_summary_raw.csv").resolve()
        real_open = io.open
        read_count = 0

        def counting_open(file, *args, **kwargs):
            nonlocal read_count
            try:
                candidate = Path(file).resolve()
            except TypeError:
                candidate = None
            if candidate == target:
                read_count += 1
            return real_open(file, *args, **kwargs)

        arguments = {
            "series_ids": ["M01-main"],
            "metric": "cathodic",
            "x_axis": "cycle",
            "mode": "raw",
            "max_points": 400,
        }
        with mock.patch("io.open", side_effect=counting_open):
            self.workspace.chart_data(**arguments)
            self.workspace.chart_data(**arguments)

        self.assertEqual(read_count, 2)
        self.assertEqual(self.workspace._chart_cache_bytes, 0)
        self.assertEqual(len(self.workspace._chart_cache), 0)

    def test_failed_chart_read_is_not_cached(self):
        self.workspace.series()
        target = (self.analysis / "cycle_summary_raw.csv").resolve()
        real_open = io.open
        failed_reads = 0

        def failing_open(file, *args, **kwargs):
            nonlocal failed_reads
            try:
                candidate = Path(file).resolve()
            except TypeError:
                candidate = None
            if candidate == target:
                failed_reads += 1
                raise OSError("simulated read failure")
            return real_open(file, *args, **kwargs)

        arguments = {
            "series_ids": ["M01-main"],
            "metric": "cathodic",
            "x_axis": "cycle",
            "mode": "raw",
            "max_points": 400,
        }
        with mock.patch("io.open", side_effect=failing_open):
            for _ in range(2):
                with self.assertRaises(APP.StartStopWorkspaceError):
                    self.workspace.chart_data(**arguments)

        self.assertEqual(failed_reads, 2)
        self.assertEqual(len(self.workspace._chart_cache), 0)

    def test_background_jobs_refuse_to_start_without_the_fixed_runtime(self):
        with self.assertRaises(APP.StartStopWorkspaceError) as caught:
            self.workspace.start_job("render")
        self.assertEqual(caught.exception.status, 503)
        self.assertIn("运行环境", str(caught.exception))

    def test_execution_readiness_requires_the_fixed_analysis_script(self):
        workspace = APP.StartStopWorkspace(
            self.database,
            self.analysis,
            python_executable=sys.executable,
        )
        (self.analysis / workspace.SCRIPT_NAME).unlink()

        status = workspace.status()

        self.assertFalse(status["execution"]["ready"])
        self.assertTrue(status["execution"]["python_ready"])
        self.assertFalse(status["execution"]["analysis_script_ready"])
        with self.assertRaises(APP.StartStopWorkspaceError) as caught:
            workspace.start_job("scan")
        self.assertEqual(caught.exception.status, 503)
        self.assertIn("分析脚本", str(caught.exception))


class StartStopCollectionPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.analysis = self.root / "analysis"
        self.collection = self.root / "脚本文件"
        self.analysis.mkdir()
        self.collection.mkdir()
        self.database = APP.Database(self.root / "state" / "workspace.sqlite3")
        self.events = self.root / "events.txt"
        self.identity = self.root / "fake_identity"
        self.identity.write_text("fixture", encoding="utf-8")
        (self.collection / "采集配置.json").write_text(
            json.dumps(
                {
                    "machines": [
                        {
                            "identity_file": str(self.identity),
                            "roots": [{"label": "fixture"}],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        self.temporary.cleanup()

    def write_analyzer(self) -> None:
        (self.analysis / APP.StartStopWorkspace.SCRIPT_NAME).write_text(
            textwrap.dedent(
                f"""
                import json
                import sys
                from pathlib import Path
                events = Path({str(self.events)!r})
                if "--prepare-config" in sys.argv:
                    if events.read_text(encoding="utf-8").splitlines() != ["collect"]:
                        raise SystemExit("collector did not run first")
                    with events.open("a", encoding="utf-8") as handle:
                        handle.write("prepare\\n")
                    print(json.dumps({{"stage": "prepared", "materials": 3,
                                      "start_stop_candidates": 5,
                                      "included_start_stop_files": 4}}))
                else:
                    print(json.dumps({{"stage": "rendered", "complete_cycles": 8}}))
                """
            ),
            encoding="utf-8",
        )

    def write_collector(
        self,
        *,
        returncode: int,
        roots: list[dict],
        totals: dict,
    ) -> Path:
        script = self.collection / "collect_echem_data.py"
        payload = {
            "mode": "collect",
            "machines": [
                {
                    "id": "machine-1",
                    "roots": roots,
                    "errors": ["offline"] if totals.get("errors") else [],
                }
            ],
            "totals": {
                "inventoried": 0,
                "already_collected": 0,
                "planned_files": 0,
                "planned_bytes": 0,
                "copied": 0,
                "versioned": 0,
                "unchanged_content": 0,
                "unsettled_skipped": 0,
                "changed_during_collection": 0,
                "errors": 0,
                **totals,
            },
        }
        script.write_text(
            textwrap.dedent(
                f"""
                import argparse
                import json
                from pathlib import Path
                parser = argparse.ArgumentParser()
                parser.add_argument("--config")
                parser.add_argument("--result-json", type=Path, required=True)
                args = parser.parse_args()
                Path({str(self.events)!r}).write_text("collect\\n", encoding="utf-8")
                args.result_json.write_text(
                    json.dumps({payload!r}, ensure_ascii=False),
                    encoding="utf-8",
                )
                raise SystemExit({returncode})
                """
            ),
            encoding="utf-8",
        )
        return script

    def workspace(self, collector: Path) -> APP.StartStopWorkspace:
        return APP.StartStopWorkspace(
            self.database,
            self.analysis,
            collection_script=collector,
            python_executable=sys.executable,
            timeout_seconds=30,
        )

    def wait_for_job(self, workspace: APP.StartStopWorkspace) -> dict:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = workspace._public_job()
            if job["status"] not in {"queued", "running"}:
                return job
            time.sleep(0.02)
        self.fail("background collection job did not finish")

    def test_scan_collects_remote_files_before_refreshing_materials(self):
        self.write_analyzer()
        collector = self.write_collector(
            returncode=0,
            roots=[{"label": "root", "inventoried": 10}],
            totals={"inventoried": 10, "already_collected": 8, "copied": 2},
        )
        workspace = self.workspace(collector)

        self.assertTrue(workspace.status()["execution"]["update_ready"])
        workspace.start_job("scan")
        job = self.wait_for_job(workspace)

        self.assertEqual(job["status"], "completed")
        self.assertEqual(self.events.read_text(encoding="utf-8").splitlines(), ["collect", "prepare"])
        self.assertEqual(job["result"]["collection_copied"], 2)
        self.assertEqual(job["result"]["materials"], 3)

    def test_partial_collection_refreshes_materials_with_a_warning(self):
        self.write_analyzer()
        collector = self.write_collector(
            returncode=1,
            roots=[
                {"label": "online", "inventoried": 4},
                {"label": "offline", "error": "offline"},
            ],
            totals={"inventoried": 4, "copied": 1, "errors": 1},
        )
        workspace = self.workspace(collector)

        workspace.start_job("scan")
        job = self.wait_for_job(workspace)

        self.assertEqual(job["status"], "completed_with_warnings")
        self.assertEqual(self.events.read_text(encoding="utf-8").splitlines(), ["collect", "prepare"])
        self.assertEqual(job["result"]["collection_roots_ok"], 1)
        self.assertEqual(job["result"]["collection_roots_failed"], 1)

    def test_all_remote_sources_failing_does_not_refresh_materials(self):
        self.write_analyzer()
        collector = self.write_collector(
            returncode=1,
            roots=[{"label": "offline", "error": "offline"}],
            totals={"errors": 1},
        )
        workspace = self.workspace(collector)

        workspace.start_job("scan")
        job = self.wait_for_job(workspace)

        self.assertEqual(job["status"], "failed")
        self.assertEqual(self.events.read_text(encoding="utf-8").splitlines(), ["collect"])
        self.assertEqual(job["result"]["collection_roots_ok"], 0)

    def test_missing_collector_blocks_scan_but_not_render_preflight(self):
        self.write_analyzer()
        workspace = self.workspace(self.collection / "missing.py")

        execution = workspace.status()["execution"]

        self.assertFalse(execution["update_ready"])
        self.assertTrue(execution["render_ready"])
        with self.assertRaises(APP.StartStopWorkspaceError) as caught:
            workspace.start_job("scan")
        self.assertEqual(caught.exception.status, 503)


if __name__ == "__main__":
    unittest.main()
