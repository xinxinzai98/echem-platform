from __future__ import annotations

import csv
import copy
import hashlib
import io
import unittest
from unittest import mock

from echem_platform.start_stop_stability import (
    StabilityAnalysisError,
    StabilityRepositoryAnalyzer,
)


FIELDS = (
    "time_s",
    "potential_v",
    "current_ma",
    "current_density_ma_cm2",
    "cycle_id",
    "step_id",
    "step_name",
    "absolute_time",
    "record_id",
    "voltage_raw_uv",
    "current_raw_ua",
    "temperature_c",
)


def csv_bytes(rows: list[dict]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=FIELDS)
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def start_stop_rows() -> list[dict]:
    rows = []
    record_id = 0
    for cycle in (1, 2):
        for step, current in ((1, -300.0), (2, 30.0)):
            for second in range(20):
                record_id += 1
                if step == 1 and cycle == 1:
                    potential = -2.0 if second == 0 else -1.5 - second * 0.001
                elif step == 1:
                    potential = -1.5 - second * 0.01
                else:
                    potential = 0.6 + second * 0.001
                rows.append(
                    {
                        "time_s": record_id - 1,
                        "potential_v": potential,
                        "current_ma": current,
                        "current_density_ma_cm2": "",
                        "cycle_id": cycle,
                        "step_id": (cycle - 1) * 2 + step,
                        "step_name": "恒流放电" if step == 1 else "恒流充电",
                        "absolute_time": "2026/09/01 00:00:00",
                        "record_id": record_id,
                        "voltage_raw_uv": potential * 1_000_000,
                        "current_raw_ua": current * 1000,
                        "temperature_c": 0,
                    }
                )
    return rows


def constant_rows() -> list[dict]:
    rows = []
    for index in range(61):
        time_s = index * 60
        time_h = time_s / 3600
        potential = -1.5 + 0.01 * time_h
        rows.append(
            {
                "time_s": time_s,
                "potential_v": potential,
                "current_ma": -500.0,
                "current_density_ma_cm2": "",
                "cycle_id": 1,
                "step_id": 1,
                "step_name": "恒流放电",
                "absolute_time": "2026/09/01 00:00:00",
                "record_id": index + 1,
                "voltage_raw_uv": potential * 1_000_000,
                "current_raw_ua": -500_000,
                "temperature_c": 0,
            }
        )
    return rows


class FakeDatabase:
    def __init__(self) -> None:
        self.contents = {
            11: csv_bytes(start_stop_rows()),
            12: csv_bytes(constant_rows()),
        }
        self.rows = [
            self.source(
                11,
                "lanbts_start_stop",
                "start_stop",
                "NMP 启停",
                [-300.0, 30.0],
            ),
            self.source(
                12,
                "lanbts_constant_current",
                "constant_current",
                "NMP 恒流",
                [-500.0],
            ),
        ]

    def source(
        self,
        source_version_id: int,
        candidate_kind: str,
        mode: str,
        name: str,
        levels: list[float],
    ) -> dict:
        content = self.contents[source_version_id]
        sha = hashlib.sha256(content).hexdigest()
        return {
            "source_version_id": source_version_id,
            "version_number": 1,
            "repository_path": f"lanbts/{source_version_id}.csv",
            "size_bytes": len(content),
            "sha256": sha,
            "source_modified_utc": "2026-09-01T00:00:00Z",
            "created_utc": "2026-09-01T00:01:00Z",
            "machine_id": "lanbts",
            "root_label": "data",
            "remote_path": f"remote-{source_version_id}",
            "candidate_kind": candidate_kind,
            "metadata": {
                "analysis_mode": mode,
                "source_file": f"source-{source_version_id}.bts",
                "material_name": name,
                "protocol": {
                    "kind": "bipolar" if mode == "start_stop" else "constant_current",
                    "category": "反向启停" if mode == "start_stop" else "恒流长时运行",
                    "label": "−300/+30 mA" if mode == "start_stop" else "−500 mA",
                    "key": f"key-{source_version_id}",
                },
                "protocol_current_levels_ma": levels,
                "classification_method": "embedded_protocol",
                "classification_reason": "测试分类依据",
                "classifier_version": "v1",
                "extractor_version": "v1",
                "parent_sha256": "a" * 64,
                "parent_source_version_id": source_version_id - 2,
                "parent_repository_path": f"raw/{source_version_id}.bts",
                "record_count": len(content),
                "exported_point_count": len(content),
                "downsample_stride": 1,
            },
        }

    def stability_sources(self):
        return self.rows

    def read_source_export_content(
        self,
        *,
        source_version_id,
        expected_sha256,
        expected_size_bytes,
        maximum_file_bytes,
    ):
        content = self.contents[source_version_id]
        if len(content) > maximum_file_bytes:
            raise OverflowError
        assert hashlib.sha256(content).hexdigest() == expected_sha256
        assert len(content) == expected_size_bytes
        return content


class StabilityRepositoryAnalyzerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.analyzer = StabilityRepositoryAnalyzer(FakeDatabase())
        catalog = self.analyzer.catalog()
        self.by_mode = {
            row["analysis_mode"]: row["series_id"]
            for row in catalog["series"]
        }

    def test_protocol_comparison_and_single_record_anomaly_are_enforced_before_read(self):
        database = FakeDatabase()
        other = copy.deepcopy(database.rows[0])
        other["source_version_id"] = 13
        other["repository_path"] = "lanbts/13.csv"
        database.contents[13] = database.contents[11]
        other["metadata"]["protocol"]["key"] = "different-step"
        database.rows.append(other)
        analyzer = StabilityRepositoryAnalyzer(database)
        ids = [row["series_id"] for row in analyzer.catalog()["series"] if row["analysis_mode"] == "start_stop"]
        with mock.patch.object(database, "read_source_export_content", side_effect=AssertionError("reject before reading")):
            with self.assertRaisesRegex(StabilityAnalysisError, "同一测试工步"):
                analyzer.chart(series_ids=ids, analysis_mode="start_stop", metric="overview")
            other["metadata"]["protocol"]["key"] = database.rows[0]["metadata"]["protocol"]["key"]
            with self.assertRaisesRegex(StabilityAnalysisError, "只选择一条"):
                analyzer.chart(series_ids=ids, analysis_mode="start_stop", metric="minimum_time")
            other["metadata"]["protocol"]["key"] = ""
            database.rows[0]["metadata"]["protocol"]["key"] = ""
            with self.assertRaisesRegex(StabilityAnalysisError, "未知工步"):
                analyzer.chart(series_ids=ids, analysis_mode="start_stop", metric="overview")

    def test_catalog_separates_start_stop_and_constant_current(self) -> None:
        catalog = self.analyzer.catalog()

        self.assertEqual(catalog["counts"]["total"], 2)
        self.assertEqual(catalog["counts"]["start_stop"], 1)
        self.assertEqual(catalog["counts"]["constant_current"], 1)
        self.assertEqual(
            catalog["measurement_boundary"]["voltage_reference"],
            "unconfirmed",
        )

    def test_start_stop_cycle_analysis_uses_last_second_and_15_second_rule(self) -> None:
        payload = self.analyzer.chart(
            series_ids=[self.by_mode["start_stop"]],
            analysis_mode="start_stop",
            metric="stress_endpoint",
        )

        series = payload["series"][0]
        self.assertEqual(series["summary"]["complete_cycles"], 2)
        self.assertEqual(series["summary"]["normal_cycles"], 1)
        self.assertEqual(series["summary"]["abnormal_cycles"], 1)
        self.assertEqual([point["status"] for point in series["points"]], ["abnormal", "normal"])

    def test_constant_current_summary_reports_linear_drift(self) -> None:
        payload = self.analyzer.chart(
            series_ids=[self.by_mode["constant_current"]],
            analysis_mode="constant_current",
            metric="overview",
        )

        summary = payload["series"][0]["summary"]
        self.assertAlmostEqual(summary["duration_h"], 1.0)
        self.assertAlmostEqual(summary["linear_drift_mv_per_h"], 10.0, places=6)
        self.assertAlmostEqual(summary["current_median_ma"], -500.0)

    def test_mode_mismatch_is_rejected(self) -> None:
        with self.assertRaises(StabilityAnalysisError):
            self.analyzer.chart(
                series_ids=[self.by_mode["constant_current"]],
                analysis_mode="start_stop",
                metric="overview",
            )

    def test_legacy_downsampled_source_is_preview_only_not_formal_statistics(self):
        self.analyzer.database.rows[0]["metadata"]["downsample_stride"] = 20
        identifier = self.by_mode["start_stop"]
        with self.assertRaises(StabilityAnalysisError) as rejected:
            self.analyzer.chart(series_ids=[identifier], analysis_mode="start_stop", metric="minimum_time")
        self.assertEqual(rejected.exception.status, 422)
        preview = self.analyzer.chart(series_ids=[identifier], analysis_mode="start_stop", metric="overview")
        self.assertTrue(preview["series"][0]["points"])
        self.assertIsNone(preview["series"][0]["summary"]["complete_cycles"])
        self.assertEqual(preview["series"][0]["summary"]["calculation_status"], "preview_only")

    def test_cached_statistics_reuse_reads_and_return_independent_payloads(self):
        database = self.analyzer.database
        identifier = self.by_mode["start_stop"]
        with mock.patch.object(database, "read_source_export_content", wraps=database.read_source_export_content) as reader:
            first = self.analyzer.chart(series_ids=[identifier], analysis_mode="start_stop", metric="stress_endpoint")
            first["series"][0]["summary"]["complete_cycles"] = -1
            second = self.analyzer.chart(series_ids=[identifier], analysis_mode="start_stop", metric="minimum_time")
            self.assertEqual(reader.call_count, 1)
            self.assertEqual(second["series"][0]["summary"]["complete_cycles"], 2)
            database.rows[0]["metadata"]["protocol_current_levels_ma"] = [-500, -200]
            self.analyzer.chart(series_ids=[identifier], analysis_mode="start_stop", metric="overview")
            self.assertEqual(reader.call_count, 2)

    def test_over_200000_full_records_keep_statistics_independent_of_display_sampling(self):
        database = FakeDatabase()
        out = io.StringIO(newline="")
        writer = csv.DictWriter(out, fieldnames=FIELDS)
        writer.writeheader()
        cycle_count = 334
        for cycle in range(cycle_count):
            for local in range(600):
                second = local / 10
                voltage = -1.5 if local < 300 else 0.5
                if local == 149 + cycle % 3:
                    voltage = -2.0
                writer.writerow({"time_s":cycle*60+second,"potential_v":voltage,
                                 "current_ma":-300 if local<300 else 30,"cycle_id":cycle+1,
                                 "step_id":1 if local<300 else 2,"record_id":cycle*600+local+1})
        database.contents[11] = out.getvalue().encode()
        database.rows = [database.source(11,"lanbts_start_stop","start_stop","large-full",[-300,30])]
        database.rows[0]["metadata"].update(record_count=cycle_count*600,exported_point_count=cycle_count*600)
        analyzer = StabilityRepositoryAnalyzer(database)
        identifier = analyzer.catalog()["series"][0]["series_id"]
        sparse = analyzer.chart(series_ids=[identifier],analysis_mode="start_stop",metric="minimum_time",max_points=100)["series"][0]
        dense = analyzer.chart(series_ids=[identifier],analysis_mode="start_stop",metric="minimum_time",max_points=6000)["series"][0]
        self.assertGreater(cycle_count*600,200000)
        self.assertEqual(sparse["summary"],dense["summary"])
        self.assertEqual(dense["summary"]["complete_cycles"],334)
        self.assertEqual(dense["summary"]["abnormal_cycles"],112)
        self.assertEqual(dense["summary"]["normal_cycles"],222)
        self.assertEqual([p["status"] for p in dense["points"][:3]],["abnormal","normal","normal"])
        self.assertEqual(len(sparse["points"]),100)
        self.assertEqual(len(dense["points"]),334)


if __name__ == "__main__":
    unittest.main()
