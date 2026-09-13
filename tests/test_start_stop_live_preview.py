from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path

from echem_platform.start_stop_database import SCHEMA_VERSION, StartStopDatabase
from echem_platform.start_stop_live_analysis import analyze_live_start_stop_rows
from echem_platform.start_stop_live_preview import (
    LIVE_PREVIEW_MAX_ANALYSIS_CYCLE_POINTS,
    LIVE_PREVIEW_MAX_ANALYSIS_OVERVIEW_POINTS,
    LIVE_PREVIEW_MAX_SOURCES,
    LivePreviewScheduler,
    LivePreviewStateFile,
    build_live_preview_sources,
    parse_live_preview_file,
)
from echem_platform.start_stop_workstations import infer_task


MACHINE_ID = "02_测试室2_AGHID-H_192.168.110.155"
ROOT_LABEL = "桌面_电镀工艺"
FOLDER = "260824/徕阳x3-300-30-5min"
FORMAL_WORK_STEP_KEY = "standard_start_stop|jc=-300|jr=30|tc=30|tr=30"


def active_monitor_payload() -> dict:
    return {
        "status": "ready",
        "machines": [
            {
                "id": MACHINE_ID,
                "name": "测试室2",
                "hostname": "AGHID-H",
                "material_activities": [
                    {
                        "activity_status": "active",
                        "material_key": f"{MACHINE_ID}/{ROOT_LABEL}/{FOLDER}",
                        "display_name": "徕阳x3",
                        "favorite": True,
                        "root_label": ROOT_LABEL,
                        "relative_folder": FOLDER,
                        "current_file": "启停.txt",
                        "task": infer_task("徕阳x3-300-30-5min", "启停.txt"),
                    }
                ],
            }
        ],
    }


def collection_config() -> dict:
    return {
        "machines": [
            {
                "id": MACHINE_ID,
                "name": "测试室2",
                "hostname": "AGHID-H",
                "ip": "192.168.110.155",
                "user": "private-user",
                "identity_file": "/private/key",
                "roots": [
                    {
                        "label": ROOT_LABEL,
                        "remote_path": "C:\\Users\\aghid\\Desktop\\电镀工艺",
                    }
                ],
            }
        ]
    }


def preview_bytes(rows: int = 120, *, partial_tail: bool = True) -> bytes:
    lines = ["ID_GalSquareWave", "E(V)\ti(A/cm²)\tT(s)"]
    for index in range(rows):
        current = -0.3 if (index // 20) % 2 == 0 else 0.03
        potential = -1.55 + 0.0001 * index
        lines.append(f"{potential:.6E}\t{current:.6E}\t{index * 0.2:.5f}")
    payload = ("\r\n".join(lines) + "\r\n").encode("utf-8")
    if partial_tail:
        payload += b"-1.50000E+00\t-3.00000E-01\t"
    return payload


def complete_standard_rows(cycles: int = 2) -> list[tuple[float, float, float]]:
    rows: list[tuple[float, float, float]] = []
    sample_interval_s = 0.1
    phase_points = 300
    for cycle_index in range(cycles):
        cycle_start_s = cycle_index * 60.0
        minimum_time_s = 10.0 if cycle_index == 0 else 20.0
        for sample in range(phase_points):
            elapsed_s = sample * sample_interval_s
            potential_v = -1.50 - 0.01 * cycle_index
            if elapsed_s >= 29.0:
                potential_v = -1.60 - 0.01 * cycle_index
            if abs(elapsed_s - minimum_time_s) < 1e-9:
                potential_v = -1.90 - 0.01 * cycle_index
            rows.append((cycle_start_s + elapsed_s, potential_v, -0.30))
        for sample in range(phase_points):
            elapsed_s = sample * sample_interval_s
            potential_v = 0.10 + 0.01 * cycle_index
            if elapsed_s >= 29.0:
                potential_v = 0.20 + 0.01 * cycle_index
            rows.append((cycle_start_s + 30.0 + elapsed_s, potential_v, 0.03))
    return rows


class FakeMonitor:
    def snapshot(self) -> dict:
        return active_monitor_payload()


class FakeTransport:
    def __init__(self) -> None:
        self.remote_paths: list[str] = []

    def fetch_file_snapshot(self, machine, remote_path, destination):
        self.remote_paths.append(remote_path)
        data = preview_bytes()
        destination.write_bytes(data)
        return {
            "size": len(data),
            "source_modified_utc": "2026-08-25T01:30:00Z",
            "grew_during_snapshot": True,
        }


def failing_formal_context() -> dict:
    raise RuntimeError("正式分析尚未就绪")


class LivePreviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_parser_skips_partial_tail_and_downsamples_complete_rows(self) -> None:
        path = self.root / "active.txt"
        path.write_bytes(preview_bytes(rows=150, partial_tail=True))

        parsed = parse_live_preview_file(path, max_points=40)

        self.assertEqual(parsed["complete_row_count"], 150)
        self.assertTrue(parsed["skipped_partial_tail"])
        self.assertLessEqual(len(parsed["points"]), 40)
        self.assertEqual(parsed["last_current_a_cm2"], 0.03)
        self.assertEqual(parsed["phase"], "恢复段")
        self.assertEqual(parsed["time_end_s"], 29.8)
        self.assertEqual(parsed["current_levels_a_cm2"], [-0.3, 0.03])

    def test_source_resolution_keeps_private_remote_path_internal(self) -> None:
        sources = build_live_preview_sources(
            active_monitor_payload(), collection_config()
        )

        self.assertEqual(len(sources), 1)
        self.assertEqual(
            sources[0]["remote_path"],
            "C:\\Users\\aghid\\Desktop\\电镀工艺\\260824\\徕阳x3-300-30-5min\\启停.txt",
        )
        self.assertEqual(sources[0]["display_name"], "徕阳x3")
        self.assertNotIn("remote_path", sources[0]["task"])

    def test_source_resolution_is_capped_to_six_workstations(self) -> None:
        monitor = active_monitor_payload()
        activity = monitor["machines"][0]["material_activities"][0]
        monitor["machines"][0]["material_activities"] = [
            {
                **activity,
                "material_key": f"material-{index}",
                "display_name": f"材料 {index}",
                "relative_folder": f"260824/材料-{index}-300-30-5min",
            }
            for index in range(9)
        ]

        sources = build_live_preview_sources(monitor, collection_config())

        self.assertEqual(len(sources), LIVE_PREVIEW_MAX_SOURCES)

    def test_live_analysis_uses_formal_endpoint_and_anomaly_rules(self) -> None:
        analysis = analyze_live_start_stop_rows(
            complete_standard_rows(),
            task={"test_type": "start_stop"},
            file_name="启停.txt",
            material_key="lab/A",
            logical_source_file="lab/A/启停.txt",
        )

        self.assertEqual(analysis["calculation"], "formal_start_stop_rules_v3")
        self.assertEqual(analysis["work_step_key"], FORMAL_WORK_STEP_KEY)
        self.assertEqual(analysis["complete_cycles"], 2)
        first, second = analysis["cycle_points"]
        self.assertAlmostEqual(first["cathodic_last1s_median_raw_v"], -1.60)
        self.assertAlmostEqual(first["reverse_last1s_median_raw_v"], 0.20)
        self.assertAlmostEqual(first["cathodic_phase_min_time_s"], 10.0)
        self.assertEqual(first["status"], "abnormal")
        self.assertAlmostEqual(second["cathodic_phase_min_time_s"], 20.0)
        self.assertEqual(second["status"], "normal")

    def test_new_active_file_appends_cycle_and_time_to_formal_series(self) -> None:
        continuation_file = "启停_2026_08_25_13_00_00.txt"
        continuation_path = f"lab/A/{continuation_file}"
        context = {
            "series": [
                {
                    "series_id": "M01-main",
                    "series_order": 1,
                    "material_relative_path": "lab/A",
                    "work_step_key": FORMAL_WORK_STEP_KEY,
                    "complete_cycles": 13,
                    "duration_h": 1.25,
                    "segment_count": 2,
                    "source_files": ["lab/A/启停1.txt", "lab/A/启停2.txt"],
                    "cathodic_baseline_first10_median_raw_v": -1.55,
                    "reverse_baseline_first10_median_raw_v": 0.18,
                }
            ],
            "segments": [],
            "rules": {},
        }

        analysis = analyze_live_start_stop_rows(
            complete_standard_rows(),
            file_name=continuation_file,
            material_key="lab/A",
            logical_source_file=continuation_path,
            formal_context=context,
        )

        self.assertEqual(analysis["formal_series_id"], "M01-main")
        self.assertEqual(analysis["continuation_mode"], "append_new_segment")
        self.assertEqual(analysis["continuation_cycle_offset"], 13)
        self.assertEqual(analysis["continuation_time_offset_h"], 1.25)
        self.assertEqual(analysis["segment_index"], 3)
        self.assertEqual(analysis["cycle_points"][0]["cycle"], 14)
        self.assertGreater(
            analysis["cycle_points"][0]["cathodic_endpoint_time_h"],
            1.25,
        )
        self.assertEqual(analysis["ordered_source_files"][-1], continuation_path)

    def test_rewritten_active_segment_replaces_old_tail_and_keeps_water_model(self) -> None:
        context = {
            "series": [
                {
                    "series_id": "M01-main",
                    "series_order": 1,
                    "material_relative_path": "lab/A",
                    "work_step_key": FORMAL_WORK_STEP_KEY,
                    "complete_cycles": 13,
                    "duration_h": 1.0,
                    "segment_count": 2,
                    "source_files": ["lab/A/启停1.txt", "lab/A/启停.txt"],
                    "cathodic_baseline_first10_median_raw_v": -1.55,
                    "reverse_baseline_first10_median_raw_v": 0.18,
                    "water_comp_cathodic_baseline_first10_raw_v": -1.54,
                    "water_comp_recovery_baseline_first10_raw_v": 0.17,
                }
            ],
            "segments": [
                {
                    "series_id": "M01-main",
                    "segment_index": 1,
                    "source_file": "lab/A/启停1.txt",
                    "complete_cycles": 5,
                    "continuous_time_start_h": 0.0,
                    "continuous_time_end_h": 0.5,
                },
                {
                    "series_id": "M01-main",
                    "segment_index": 2,
                    "source_file": "lab/A/启停.txt",
                    "complete_cycles": 8,
                    "continuous_time_start_h": 0.5,
                    "continuous_time_end_h": 1.0,
                },
            ],
            "rules": {"water_resistance_drift_ohm_cm2_per_h": 0.2},
        }

        analysis = analyze_live_start_stop_rows(
            complete_standard_rows(),
            file_name="启停.txt",
            material_key="lab/A",
            logical_source_file="lab/A/启停.txt",
            formal_context=context,
        )

        self.assertEqual(analysis["continuation_mode"], "replace_active_segment")
        self.assertEqual(analysis["replace_from_segment_index"], 2)
        self.assertEqual(analysis["continuation_cycle_offset"], 5)
        self.assertEqual(analysis["continuation_time_offset_h"], 0.5)
        self.assertEqual(analysis["cycle_points"][0]["cycle"], 6)
        self.assertTrue(analysis["water_compensation_available"])
        first = analysis["cycle_points"][0]
        expected = (
            first["cathodic_last1s_median_raw_v"]
            - first["cathodic_current_median_a_cm2"]
            * 0.2
            * first["cathodic_endpoint_time_h"]
        )
        self.assertAlmostEqual(
            first["cathodic_last1s_median_water_compensated_v"],
            expected,
        )

    def test_special_marked_files_stay_separate_but_nimop_qiting2_continues(self) -> None:
        context = {
            "series": [
                {
                    "series_id": "M01-main",
                    "series_order": 1,
                    "is_primary_series": True,
                    "material_relative_path": "lab/A",
                    "work_step_key": FORMAL_WORK_STEP_KEY,
                    "complete_cycles": 10,
                    "duration_h": 1.0,
                    "segment_count": 1,
                    "source_files": ["lab/A/启停.txt"],
                },
                {
                    "series_id": "M01-special-01",
                    "series_order": 2,
                    "is_special_series": True,
                    "material_relative_path": "lab/A",
                    "work_step_key": FORMAL_WORK_STEP_KEY,
                    "complete_cycles": 2,
                    "duration_h": 0.1,
                    "segment_count": 1,
                    "source_files": ["lab/A/启停3.txt"],
                },
            ],
            "segments": [
                {
                    "series_id": "M01-main",
                    "segment_index": 1,
                    "source_file": "lab/A/启停.txt",
                    "complete_cycles": 10,
                    "continuous_time_start_h": 0.0,
                },
                {
                    "series_id": "M01-special-01",
                    "segment_index": 1,
                    "source_file": "lab/A/启停3.txt",
                    "complete_cycles": 2,
                    "continuous_time_start_h": 0.0,
                },
            ],
        }

        existing_special = analyze_live_start_stop_rows(
            complete_standard_rows(),
            file_name="启停3.txt",
            material_key="lab/A",
            logical_source_file="lab/A/启停3.txt",
            formal_context=context,
        )
        new_special = analyze_live_start_stop_rows(
            complete_standard_rows(),
            file_name="启停4.txt",
            material_key="lab/A",
            logical_source_file="lab/A/启停4.txt",
            formal_context=context,
        )

        self.assertEqual(existing_special["formal_series_id"], "M01-special-01")
        self.assertEqual(existing_special["continuation_mode"], "replace_active_segment")
        self.assertEqual(new_special["formal_series_id"], "")
        self.assertEqual(new_special["continuation_mode"], "new_live_series")
        self.assertEqual(new_special["continuation_cycle_offset"], 0)

        nimop_context = {
            "series": [
                {
                    "series_id": "M02-main",
                    "series_order": 1,
                    "is_primary_series": True,
                    "material_relative_path": "lab/NiMoP/A",
                    "work_step_key": FORMAL_WORK_STEP_KEY,
                    "complete_cycles": 7,
                    "duration_h": 0.5,
                    "segment_count": 1,
                    "source_files": ["lab/NiMoP/A/启停.txt"],
                }
            ],
            "segments": [],
        }
        nimop_qiting2 = analyze_live_start_stop_rows(
            complete_standard_rows(),
            file_name="启停2.txt",
            material_key="lab/NiMoP/A",
            logical_source_file="lab/NiMoP/A/启停2.txt",
            formal_context=nimop_context,
        )

        self.assertEqual(nimop_qiting2["formal_series_id"], "M02-main")
        self.assertEqual(nimop_qiting2["continuation_mode"], "append_new_segment")
        self.assertEqual(nimop_qiting2["cycle_points"][0]["cycle"], 8)

    def test_shared_state_file_round_trips_without_edit_capability(self) -> None:
        state_file = LivePreviewStateFile(self.root / "live-preview.json")
        state_file.write(
            {
                "enabled": True,
                "interval_minutes": 5,
                "last_status": "completed",
                "preview": {"schema_version": 1, "items": [], "errors": []},
                "can_edit": True,
            }
        )

        cached = state_file.snapshot()

        self.assertTrue(cached["available"])
        self.assertTrue(cached["enabled"])
        self.assertNotIn("can_edit", cached)
        self.assertEqual(cached["interval_minutes"], 5)

    def test_enabled_scheduler_immediately_captures_and_persists_preview(self) -> None:
        database = StartStopDatabase(self.root / "start-stop.sqlite3")
        with database.session() as connection:
            self.assertEqual(
                int(connection.execute("PRAGMA user_version").fetchone()[0]),
                SCHEMA_VERSION,
            )
        now = dt.datetime(2026, 8, 25, 1, 30, tzinfo=dt.timezone.utc)
        transport = FakeTransport()
        scheduler = LivePreviewScheduler(
            database,
            FakeMonitor(),
            collection_config,
            self.root / "scratch",
            transport=transport,
            state_file=self.root / "live-preview.json",
            formal_context_provider=failing_formal_context,
            now=lambda: now,
            poll_seconds=0.05,
        )
        saved = scheduler.save_config(expected_revision=0, enabled=True)
        self.assertTrue(saved["enabled"])
        self.assertEqual(saved["next_run_utc"], now.isoformat(timespec="seconds"))

        result = scheduler.run_once()

        self.assertEqual(result["last_status"], "completed")
        self.assertEqual(result["interval_minutes"], 5)
        self.assertEqual(len(result["preview"]["items"]), 1)
        item = result["preview"]["items"][0]
        self.assertEqual(item["display_name"], "徕阳x3")
        self.assertTrue(item["grew_during_snapshot"])
        self.assertEqual(item["potential_basis"], "Hg/HgO 原始电位")
        self.assertIn("300", item["task"]["work_step_key"])
        self.assertEqual(item["analysis"]["calculation"], "formal_start_stop_rules_v3")
        self.assertEqual(item["analysis"]["work_step_cathodic_duration_s"], 300)
        self.assertLessEqual(
            len(item["analysis"]["cycle_points"]),
            LIVE_PREVIEW_MAX_ANALYSIS_CYCLE_POINTS,
        )
        self.assertLessEqual(
            len(item["analysis"]["overview_points"]),
            LIVE_PREVIEW_MAX_ANALYSIS_OVERVIEW_POINTS,
        )
        self.assertEqual(len(transport.remote_paths), 1)
        shared = LivePreviewStateFile(self.root / "live-preview.json").snapshot()
        self.assertTrue(shared["available"])
        self.assertEqual(shared["last_status"], "completed")
        self.assertEqual(len(shared["preview"]["items"]), 1)


if __name__ == "__main__":
    unittest.main()
