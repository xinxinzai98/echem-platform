from __future__ import annotations

import csv
import base64
import json
import tempfile
import unittest
from pathlib import Path

from echem_platform.start_stop_collection import RemoteFileError
from echem_platform.start_stop_database import StartStopDatabase
from echem_platform.start_stop_lanbts_import import (
    EXPECTED_COLUMNS,
    META_PREFIX,
    LanbtsStabilityImporter,
    _profile_normalized_csv,
    _extract_timeout_seconds,
    classify_lanbts_stability,
    lanbts_record_export_script,
)


def process(*currents: float) -> list[dict]:
    rows = []
    for index, current in enumerate(currents, start=1):
        mode = "充电" if current >= 0 else "放电"
        rows.append(
            {
                "stepnum": str(index),
                "stepname": f"恒流{mode}     :{abs(current):g} mA",
                "finishconditon1": "步骤时间 ≥ 10:00",
            }
        )
    return rows


class LanbtsStabilityClassifierTests(unittest.TestCase):
    def measured(self, *, span: float, transitions: int) -> dict:
        return {
            "current_span_ma": span,
            "current_fluctuation_threshold_ma": 5.0,
            "current_level_transition_count": transitions,
            "current_low_fraction": 0.45,
            "current_high_fraction": 0.45,
        }

    def test_two_protocol_levels_are_start_stop_even_when_both_are_negative(self) -> None:
        classified = classify_lanbts_stability(
            process_rows=process(-500, -200),
            measured_profile=self.measured(span=300, transitions=8),
        )

        self.assertEqual(classified["analysis_mode"], "start_stop")
        self.assertEqual(classified["candidate_kind"], "lanbts_start_stop")
        self.assertEqual(classified["classification_method"], "embedded_protocol")

    def test_bipolar_protocol_is_start_stop(self) -> None:
        classified = classify_lanbts_stability(
            process_rows=process(-1200, 120),
            measured_profile=self.measured(span=1320, transitions=18),
        )

        self.assertEqual(classified["analysis_mode"], "start_stop")

    def test_measured_sustained_fluctuation_overrides_single_protocol_level(self) -> None:
        classified = classify_lanbts_stability(
            process_rows=process(-500),
            measured_profile=self.measured(span=180, transitions=4),
        )

        self.assertEqual(classified["analysis_mode"], "start_stop")
        self.assertEqual(
            classified["classification_method"],
            "measured_current_fluctuation",
        )

    def test_single_level_without_repeated_transition_is_constant_current(self) -> None:
        classified = classify_lanbts_stability(
            process_rows=process(-500),
            measured_profile=self.measured(span=2, transitions=0),
        )

        self.assertEqual(classified["analysis_mode"], "constant_current")
        self.assertEqual(
            classified["candidate_kind"],
            "lanbts_constant_current",
        )

    def test_extract_timeout_scales_with_file_size_and_is_bounded(self) -> None:
        self.assertEqual(_extract_timeout_seconds(2 * 1024 * 1024), 90)
        self.assertEqual(_extract_timeout_seconds(10 * 1024 * 1024), 180)
        self.assertEqual(_extract_timeout_seconds(10**12), 1800)

    def test_normalized_csv_profile_measures_robust_levels_and_transitions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "records.csv"
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=EXPECTED_COLUMNS)
                writer.writeheader()
                row_id = 0
                for cycle in range(3):
                    for current in (-500.0, -200.0):
                        for second in range(10):
                            row_id += 1
                            writer.writerow(
                                {
                                    "time_s": row_id,
                                    "potential_v": -1.5,
                                    "current_ma": current,
                                    "current_density_ma_cm2": "",
                                    "cycle_id": cycle + 1,
                                    "step_id": row_id,
                                    "step_name": "恒流放电",
                                    "absolute_time": "2026/09/01 00:00:00",
                                    "record_id": row_id,
                                    "voltage_raw_uv": -1_500_000,
                                    "current_raw_ua": current * 1000,
                                    "temperature_c": 0,
                                }
                            )

            profile = _profile_normalized_csv(path)

        self.assertEqual(profile["valid_point_count"], 60)
        self.assertAlmostEqual(profile["current_span_ma"], 300.0)
        self.assertGreaterEqual(profile["current_level_transition_count"], 5)


class LanbtsRemoteExtractionScriptTests(unittest.TestCase):
    def test_script_is_read_only_and_exports_normalized_units(self) -> None:
        script = lanbts_record_export_script(
            {
                "installation_root": "D:\\LANBTS",
                "system_root": "D:\\LANBTS\\LANBTSSystem",
            },
            data_path="D:\\LANBTS\\Data\\sample.bts",
            expected_size=1234,
            expected_ticks=638000000000000000,
            area_cm2=2.5,
        )

        self.assertNotIn("GetAllRecordData", script)
        self.assertIn("GetCycleData", script)
        self.assertIn("GetStepData", script)
        self.assertIn("GetRecordData", script)
        self.assertIn("GetProcessData", script)
        self.assertNotIn("$recordList", script)
        self.assertNotIn("$stride", script)
        self.assertNotIn("ReadAllBytes($exportPath)", script)
        self.assertIn("sampling_policy='full_records'", script)
        self.assertIn("$csvStream.CopyTo", script)
        self.assertNotIn("DdaServices]::Initialize", script)
        self.assertIn("[Environment]::Exit(0)", script)
        self.assertIn("StartStopLanbtsReadOnlyV1", script)
        self.assertIn("$voltageRaw/1000000.0", script)
        self.assertIn("$currentRaw/1000.0", script)
        self.assertIn("$currentMa/[double]$areaCm2", script)
        self.assertIn("source_changed_before_extract", script)
        self.assertIn("source_changed_after_extract", script)
        for forbidden in (
            "Stop-Process",
            "Start-Process",
            "Remove-Item",
            "Move-Item",
            "Set-Content",
            "Add-Content",
        ):
            self.assertNotIn(forbidden, script)


class FakeImportTransport:
    def __init__(self) -> None:
        self.fetch_calls = 0
        self.extract_calls = 0

    def inventory_root(self, machine, root, extensions):
        return {
            "exists": True,
            "root": root["remote_path"],
            "scanned_at_utc": "2026-09-01T02:00:00Z",
            "files": [
                {
                    "path": "D:\\LANBTS\\Data\\sample.bts",
                    "relative": "sample.bts",
                    "size": 7,
                    "last_write_ticks": 123,
                    "last_write_utc": "2026-09-01T01:00:00Z",
                }
            ],
        }

    def fetch_file_verified(
        self,
        machine,
        remote_path,
        destination,
        expected_size,
        expected_last_write_ticks,
    ):
        self.fetch_calls += 1
        destination.write_bytes(b"raw-bts")
        return {"size": expected_size, "last_write_ticks": expected_last_write_ticks}

    def stream_stdin_script(self, machine, script, destination, timeout=3600):
        self.extract_calls += 1
        with destination.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=EXPECTED_COLUMNS)
            writer.writeheader()
            row_id = 0
            for cycle in range(3):
                for current in (-300.0, 30.0):
                    for second in range(20):
                        row_id += 1
                        writer.writerow(
                            {
                                "time_s": row_id,
                                "potential_v": -1.5,
                                "current_ma": current,
                                "current_density_ma_cm2": "",
                                "cycle_id": cycle + 1,
                                "step_id": row_id,
                                "step_name": "恒流放电" if current < 0 else "恒流充电",
                                "absolute_time": "2026/09/01 00:00:00",
                                "record_id": row_id,
                                "voltage_raw_uv": -1_500_000,
                                "current_raw_ua": current * 1000,
                                "temperature_c": 0,
                            }
                        )
        metadata = {
            "ok": True,
            "source_size": 7,
            "source_ticks": 123,
            "source_modified_utc": "2026-09-01T01:00:00Z",
            "record_count": 120,
            "exported_point_count": 120,
            "downsample_stride": 1,
            "elapsed_ms": 10,
            "process": process(-300, 30),
        }
        encoded = base64.b64encode(
            json.dumps(metadata, ensure_ascii=False).encode("utf-8")
        )
        return META_PREFIX + encoded + b"\n"


class TimeoutImportTransport(FakeImportTransport):
    def stream_stdin_script(self, machine, script, destination, timeout=3600):
        self.extract_calls += 1
        raise RemoteFileError("实验电脑远端数据解析超时")


class LanbtsStabilityImporterTests(unittest.TestCase):
    def test_truncated_csv_cannot_be_published_as_full_records(self) -> None:
        class TruncatedTransport(FakeImportTransport):
            def stream_stdin_script(self, machine, script, destination, timeout=3600):
                metadata = super().stream_stdin_script(machine, script, destination, timeout)
                lines = destination.read_text(encoding="utf-8").splitlines()
                destination.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
                return metadata
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = StartStopDatabase(root / "repository.sqlite3")
            importer = LanbtsStabilityImporter(database, {
                "id":"fixture", "hostname":"fixture", "ip":"192.0.2.1", "installation_root":"D:\\LANBTS",
                "system_root":"D:\\LANBTS\\LANBTSSystem", "data_root":"D:\\LANBTS\\Data",
            }, transport=TruncatedTransport())
            result = importer.run(root / "scratch", settle_seconds=300)
            self.assertNotEqual(result["status"], "completed")
            self.assertEqual(result["totals"]["raw_ingested"], 1)
            self.assertEqual(result["totals"]["derived_ingested"], 0)
            self.assertEqual(database.stability_sources(), [])

    def test_raw_and_normalized_sources_are_ingested_and_reused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = StartStopDatabase(root / "start-stop.sqlite3")
            transport = FakeImportTransport()
            importer = LanbtsStabilityImporter(
                database,
                {
                    "id": "04_蓝博八通道_AGHID-P_192.168.110.144",
                    "hostname": "AGHID-P",
                    "ip": "192.168.110.144",
                    "user": "aghid",
                    "identity_file": str(root / "key"),
                    "known_hosts_file": str(root / "known_hosts"),
                    "installation_root": "D:\\LANBTS",
                    "data_root": "D:\\LANBTS\\Data",
                    "system_root": "D:\\LANBTS\\LANBTSSystem",
                },
                transport=transport,
            )

            progress_path = root / "progress.json"
            first = importer.run(
                root / "scratch-one",
                settle_seconds=300,
                progress_path=progress_path,
            )
            second = importer.run(root / "scratch-two", settle_seconds=300)
            sources = database.stability_sources()
            progress = json.loads(progress_path.read_text(encoding="utf-8"))

        self.assertEqual(first["status"], "completed")
        self.assertEqual(first["totals"]["raw_ingested"], 1)
        self.assertEqual(first["totals"]["derived_ingested"], 1)
        self.assertEqual(first["totals"]["start_stop"], 1)
        self.assertEqual(second["totals"]["derived_reused"], 1)
        self.assertEqual(transport.fetch_calls, 1)
        self.assertEqual(transport.extract_calls, 1)
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["candidate_kind"], "lanbts_start_stop")
        self.assertEqual(sources[0]["metadata"]["parent_size_bytes"], 7)
        self.assertEqual(progress["phase"], "importing_lanbts")
        self.assertEqual(progress["completed"], 1)
        self.assertEqual(progress["total"], 1)

    def test_repeated_timeout_for_unchanged_raw_version_is_backed_off(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = StartStopDatabase(root / "start-stop.sqlite3")
            transport = TimeoutImportTransport()
            importer = LanbtsStabilityImporter(
                database,
                {
                    "id": "04_蓝博八通道_AGHID-P_192.168.110.144",
                    "hostname": "AGHID-P",
                    "ip": "192.168.110.144",
                    "user": "aghid",
                    "identity_file": str(root / "key"),
                    "known_hosts_file": str(root / "known_hosts"),
                    "installation_root": "D:\\LANBTS",
                    "data_root": "D:\\LANBTS\\Data",
                    "system_root": "D:\\LANBTS\\LANBTSSystem",
                },
                transport=transport,
            )

            first = importer.run(root / "scratch-one", settle_seconds=300)
            second = importer.run(root / "scratch-two", settle_seconds=300)
            third = importer.run(root / "scratch-three", settle_seconds=300)

        self.assertEqual(first["status"], "partial")
        self.assertEqual(second["status"], "partial")
        self.assertEqual(third["status"], "completed")
        self.assertEqual(third["totals"]["known_failure_skipped"], 1)
        self.assertEqual(third["totals"]["errors"], 0)
        self.assertEqual(transport.extract_calls, 2)


if __name__ == "__main__":
    unittest.main()
