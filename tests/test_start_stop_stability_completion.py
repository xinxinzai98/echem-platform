"""Completion is established from full consecutive SDK steps, never a tail point."""
import copy
import io
import unittest

from openpyxl import load_workbook

from echem_platform.start_stop_stability import StabilityRepositoryAnalyzer
from tests.test_start_stop_stability import FakeDatabase, csv_bytes, start_stop_rows
from tests import test_start_stop_stability_export as export_tests


class StabilityCompletionTests(unittest.TestCase):
    def analyzer(self, rows, durations=(20.0, 20.0)):
        database = FakeDatabase()
        database.contents[11] = csv_bytes(rows)
        database.rows = [database.source(11, "lanbts_start_stop", "start_stop", "completion", [-300.0, 30.0])]
        steps = database.rows[0]["metadata"]["protocol"]["steps"]
        for step, duration in zip(steps, durations):
            step["duration_s"] = duration
        analyzer = StabilityRepositoryAnalyzer(database)
        identifier = analyzer.catalog()["series"][0]["series_id"]
        return analyzer, identifier

    def chart(self, analyzer, identifier, metric="recovery_endpoint"):
        return analyzer.chart(series_ids=[identifier], analysis_mode="start_stop", metric=metric)["series"][0]

    def test_truncated_recovery_is_excluded_from_all_cycle_statistics_but_kept_in_preview(self):
        analyzer, identifier = self.analyzer(start_stop_rows()[:61])
        for metric in ("stress_endpoint", "recovery_endpoint", "minimum_time", "negative_shift"):
            with self.subTest(metric=metric):
                result = self.chart(analyzer, identifier, metric)
                self.assertEqual(result["summary"]["complete_cycles"], 1)
                self.assertEqual(result["summary"]["normal_cycles"], 0)
                self.assertEqual(result["summary"]["abnormal_cycles"], 1)
                self.assertEqual([point["cycle"] for point in result["points"]], [1])
        preview = self.chart(analyzer, identifier, "overview")
        self.assertEqual(preview["source_point_count"], 61)
        self.assertEqual(len(preview["points"]), 61)

    def test_each_phase_uses_its_own_interval_and_accepts_complete_eof(self):
        original = start_stop_rows()
        for coarse_phase in (-300.0, 30.0):
            rows = [row for row in original if row["current_ma"] != coarse_phase or row["time_s"] % 2 == 0]
            with self.subTest(coarse_phase=coarse_phase):
                analyzer, identifier = self.analyzer(rows)
                self.assertEqual(self.chart(analyzer, identifier)["summary"]["complete_cycles"], 2)
                # Remove the last sample from the coarse phase of cycle 2.
                truncated = rows.copy()
                last = max(i for i, row in enumerate(rows) if row["cycle_id"] == 2 and row["current_ma"] == coarse_phase)
                truncated.pop(last)
                analyzer, identifier = self.analyzer(truncated)
                self.assertEqual(self.chart(analyzer, identifier)["summary"]["complete_cycles"], 1)

    def test_missing_duration_requires_observed_closure_and_more_than_one_point_per_phase(self):
        analyzer, identifier = self.analyzer(start_stop_rows(), (None, None))
        self.assertEqual(self.chart(analyzer, identifier)["summary"]["complete_cycles"], 1)
        singleton = [row for row in start_stop_rows() if row["time_s"] in (0, 20, 40, 60)]
        analyzer, identifier = self.analyzer(singleton, (None, None))
        self.assertEqual(self.chart(analyzer, identifier)["summary"]["complete_cycles"], 0)
        # A protocol timer alone also cannot establish single-point coverage.
        analyzer, identifier = self.analyzer(singleton)
        self.assertEqual(self.chart(analyzer, identifier)["summary"]["complete_cycles"], 0)

    def test_split_or_repeated_phase_is_not_merged_into_a_complete_cycle(self):
        for change in ("current", "step_id"):
            rows = copy.deepcopy(start_stop_rows())
            for row in rows[45:50]:
                row[change if change == "step_id" else "current_ma"] = 30.0 if change == "current" else 99
            with self.subTest(change=change):
                analyzer, identifier = self.analyzer(rows)
                self.assertEqual(self.chart(analyzer, identifier)["summary"]["complete_cycles"], 1)

    def test_protocol_duration_change_invalidates_cached_completion(self):
        analyzer, identifier = self.analyzer(start_stop_rows())
        self.assertEqual(self.chart(analyzer, identifier)["summary"]["complete_cycles"], 2)
        analyzer.database.rows[0]["metadata"]["protocol"]["steps"][1]["duration_s"] = 25.0
        self.assertEqual(self.chart(analyzer, identifier)["summary"]["complete_cycles"], 0)

    def test_step_ids_can_repeat_per_cycle(self):
        rows = start_stop_rows()
        for row in rows:
            row["step_id"] = 1 if row["current_ma"] < 0 else 2
        analyzer, identifier = self.analyzer(rows)
        self.assertEqual(self.chart(analyzer, identifier)["summary"]["complete_cycles"], 2)


class TruncatedStabilityExportTests(unittest.TestCase):
    def test_export_keeps_truncated_raw_records_and_only_complete_cycle_results(self):
        fixture = export_tests.StabilityExportTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        source = next(raw for public, raw in fixture.workspace.stability_analyzer._sources()
                      if public["analysis_mode"] == "start_stop")
        content = csv_bytes(start_stop_rows()[:61])
        path = fixture.root / "truncated.csv"
        path.write_bytes(content)
        fixture.database.ingest_staged_file(path, {
            **source["metadata"], "machine_id": source["machine_id"], "root_label": source["root_label"],
            "remote_path": source["remote_path"], "repository_path": source["repository_path"],
            "size_bytes": len(content), "last_write_ticks": 2, "is_candidate": True,
            "candidate_kind": source["candidate_kind"], "record_count": 61, "exported_point_count": 61,
        })
        data, _, _ = fixture.export("recovery_endpoint")
        workbook = load_workbook(io.BytesIO(data), read_only=True)
        self.addCleanup(workbook.close)
        self.assertEqual(len(list(workbook["01_原始记录_1"].values)), 62)
        self.assertEqual(len(list(workbook["01_处理数据_1"].values)), 2)
        summary = {row[2]: row[3] for row in list(workbook["统计汇总"].values)[1:]}
        self.assertEqual(summary["complete_cycles"], 1)


if __name__ == "__main__":
    unittest.main()
