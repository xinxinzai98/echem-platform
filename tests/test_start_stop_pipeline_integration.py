"""Real SQLite, worker subprocess, sealed publication and PDF export regression."""
import hashlib
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from echem_platform.start_stop import StartStopWorkspace, StartStopWorkspaceError
from echem_platform.start_stop_database import StartStopDatabase, validate_sealed_artifact_directory
from tests.test_start_stop_scientific_golden import make_standard_cycle


class RepositoryPipelineIntegrationTests(unittest.TestCase):
    def test_upload_analyze_export_reuses_sealed_results_without_source_materialization(self):
        self.exercise_export("raw")

    def test_existing_water_pdf_preserves_reference_model_and_compensated_data(self):
        self.exercise_export("both")

    def exercise_export(self, data_mode):
        project = Path(__file__).resolve().parents[1]
        script = project / "docker/start_stop_analysis/analyze_and_plot_start_stop.py"
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(os.environ, {
            "START_STOP_PROJECT_ROOT": temporary,
            "START_STOP_WORKBOOK_BUILDER": str(script.parent / "material_config_workbook.py"),
            "START_STOP_WATER_COMP_REFERENCE_KEY": "手动上传/集成测试/完整样品名",
        }):
            root = Path(temporary)
            database = StartStopDatabase(root / "database" / "repository.sqlite3")
            workspace = StartStopWorkspace(database, root / "published" / "current", repository_mode=True,
                                           analysis_script=script, scratch_dir=root / "scratch",
                                           python_executable=sys.executable, timeout_seconds=60)

            def run(action, **options):
                workspace.start_job(action, **options)
                deadline = time.monotonic() + 60
                while workspace._active_job_id and time.monotonic() < deadline:
                    time.sleep(0.02)
                job = workspace._public_job()
                self.assertEqual(job["status"], "completed", json.dumps(job, ensure_ascii=False))
                return job

            frame = make_standard_cycle()
            if data_mode == "both":
                cycles = []
                for cycle in range(120):
                    item = make_standard_cycle(sample_interval_s=1)
                    item["time_s"] += cycle * 60
                    item["potential_hghgo_v"] -= 0.006 * item["time_s"] / 3600
                    cycles.append(item)
                frame = pd.concat(cycles, ignore_index=True)
            text = "CSStudioFile,ID_GalSquareWave,fixture\nE(V)\ti(A/cm²)\tT(s)\n"
            text += frame[["potential_hghgo_v", "current_a_cm2", "time_s"]].to_csv(sep="\t", index=False, header=False)
            staged = root / "启停.txt"
            staged.write_text(text)
            uploaded = workspace.upload_file(staged, filename="启停.txt", relative_path="完整样品名/启停.txt",
                                             group="集成测试", last_modified=1788755830000, size_bytes=staged.stat().st_size)
            run("prepare_upload", upload_ids=[uploaded["upload_id"]])
            run("render", render_data_mode=data_mode)
            self.assertTrue(workspace.status()["analysis_ready"])
            if data_mode == "raw":
                self.assertIsNone(workspace.status()["rules"]["water_slope_mv_per_h"])
            self.assertFalse(workspace.status()["pdf"]["standard"])
            if data_mode == "raw":
                # An unchanged source still needs publication after a naming
                # change; updated scope must reuse its calculation, not reject.
                payload = workspace.materials()
                workspace.save_materials(
                    dataset_fingerprint=payload["dataset_fingerprint"],
                    expected_revision=payload["revision"],
                    materials=[{key: ("完整重命名" if key == "plot_name" else row.get(key))
                                for key in ("key", "plot_name", "include_in_summary_atlas", "favorite", "notes")}
                               for row in payload["materials"]],
                )
                repeated = run("render", render_data_mode="raw", render_material_scope="updated")
                self.assertEqual(repeated["result"]["materials_analyzed"], 0)
                self.assertEqual(repeated["result"]["materials_skipped_unchanged"], 1)
                self.assertTrue(workspace.status()["analysis_ready"])
            before = database.current_analysis_run()
            cycle_path = workspace.analysis_dir / workspace.CYCLE_NAME
            before_hash = hashlib.sha256(cycle_path.read_bytes()).hexdigest()
            water_hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                            for path in (workspace.analysis_dir / "water_compensation").glob("*")
                            if path.is_file() and path.suffix in {".csv", ".json"}}
            with mock.patch.object(workspace, "_materialize_snapshot_source", side_effect=AssertionError("PDF must not unpack raw files")):
                exported = run("render", render_data_mode=data_mode, export_pdf=True)
            self.assertEqual(exported["result"]["analysis_reused"], 1)
            self.assertEqual(exported["result"]["materials_analyzed"], 0)
            self.assertEqual(hashlib.sha256(cycle_path.read_bytes()).hexdigest(), before_hash)
            self.assertTrue(workspace.pdf("standard")[0].read_bytes().startswith(b"%PDF"))
            self.assertTrue(workspace.status()["analysis_ready"])
            self.assertTrue(workspace.status()["export_ready"])
            validate_sealed_artifact_directory(workspace.analysis_dir)
            summary = json.loads((workspace.analysis_dir / workspace.SUMMARY_NAME).read_text())
            self.assertEqual(summary["export_source_generation_id"], before["artifact_generation_id"])
            self.assertEqual(summary["export_mode"], "existing_analysis")
            if data_mode == "raw":
                with self.assertRaises(StartStopWorkspaceError) as caught:
                    workspace.start_job("render", render_data_mode="water", export_pdf=True)
                self.assertEqual(caught.exception.status, 409)
            else:
                self.assertTrue(workspace.pdf("water")[0].read_bytes().startswith(b"%PDF"))
                self.assertEqual(water_hashes, {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in (workspace.analysis_dir / "water_compensation").glob("*")
                    if path.is_file() and path.suffix in {".csv", ".json"}})
            # The rejected mode does not create a second generation or touch raw bytes.
            self.assertEqual(database.current_analysis_run()["snapshot_id"], before["snapshot_id"])
            self.assertEqual(hashlib.sha256(staged.read_bytes()).hexdigest(), uploaded["sha256"])
