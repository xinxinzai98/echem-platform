from __future__ import annotations

import copy
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from echem_platform.start_stop import StartStopWorkspace, StartStopWorkspaceError
from echem_platform.start_stop_database import validate_sealed_artifact_directory


class FakeRepositoryDatabase:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch()
        self.snapshot = {
            "id": 7,
            "dataset_fingerprint": "repository-dataset-1",
            "file_count": 1,
            "total_bytes": 7,
            "created_utc": "2026-08-03T05:00:00+00:00",
        }
        self.generation: Path | None = None
        self.publish_calls: list[tuple[object, int, str]] = []
        self.audit_rows: list[tuple[str, str, str]] = []
        self.recovery_calls: list[dict] = []
        self.status_override: dict | None = None
        self.config_override: dict | None = None

    def freeze_snapshot(self, candidate_only=True, **_kwargs):
        assert candidate_only is True
        return dict(self.snapshot)

    def latest_snapshot(self):
        return dict(self.snapshot)

    def materialize_snapshot(self, snapshot_id, source_root):
        assert snapshot_id == 7
        path = Path(source_root) / "machine" / "root" / "sample" / "启停.txt"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"fixture")
        return {"file_count": 1, "total_bytes": 7}

    def publish_artifacts(
        self,
        output_dir,
        snapshot_id,
        config_revision,
        kind,
    ):
        destination = self.path.parent / f"generation-{len(self.publish_calls) + 1}"
        shutil.copytree(output_dir, destination)
        self.generation = destination
        self.publish_calls.append((snapshot_id, config_revision, kind))
        return {"id": len(self.publish_calls), "generation_id": f"g{len(self.publish_calls)}"}

    def restore_current_artifacts(self, target, kind=None):
        del kind
        if self.generation is None:
            raise FileNotFoundError("no current generation")
        shutil.copytree(self.generation, target)
        return {"restored": True}

    def repository_status(self):
        if self.status_override is not None:
            return dict(self.status_override)
        return {
            "database_size_bytes": 4096,
            "blobs": {"count": 1, "total_bytes": 7},
            "sources": {
                "count": 1,
                "version_count": 1,
                "current_count": 1,
                "candidate_count": 1,
            },
            "snapshots": {"count": 1, "latest": dict(self.snapshot)},
            "artifacts": {"generation_count": len(self.publish_calls)},
            "source_first_modified_utc": "2026-08-01T01:00:00+00:00",
            "source_last_modified_utc": "2026-08-03T02:00:00+00:00",
            "latest_collection_utc": "2026-08-03T04:00:00+00:00",
            "database_path": "/must/not/leak.sqlite3",
        }

    def get_start_stop_config(self):
        if self.config_override is not None:
            return copy.deepcopy(self.config_override)
        return {
            "dataset_fingerprint": "",
            "revision": 0,
            "updated_utc": "",
            "materials": [],
        }

    def audit(self, action, target, detail):
        self.audit_rows.append((action, target, detail))

    def fail_collection_batch_if_running(
        self,
        batch_id,
        *,
        reason,
        returncode=None,
        parent_job_id="",
    ):
        record = {
            "batch_id": batch_id,
            "reason": reason,
            "returncode": returncode,
            "parent_job_id": parent_job_id,
        }
        self.recovery_calls.append(record)
        return dict(record, status="failed")


class SimulatedRepositoryWorkspace(StartStopWorkspace):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fail_render = False
        self.duplicate_prepare_error = False
        self.seed_config_payloads: list[dict] = []
        self.collection_returncode = 0
        self.collection_timeout = False
        self.collection_has_changes = True
        self.observed_analysis_roots: list[tuple[Path, Path]] = []

    def _run_process(self, command, *, cwd, environment):
        del cwd
        if "echem_platform.start_stop_collection" in command:
            if self.collection_timeout:
                raise subprocess.TimeoutExpired(command, 60)
            if self.collection_returncode:
                return subprocess.CompletedProcess(
                    command,
                    self.collection_returncode,
                    "",
                    "",
                )
            result_path = Path(command[command.index("--result-json") + 1])
            progress_path = Path(command[command.index("--progress-json") + 1])
            batch_id = command[command.index("--batch-id") + 1]
            progress_path.write_text(
                json.dumps(
                    {
                        "phase": "storing_database",
                        "phase_label": "写入数据库",
                        "phase_index": 4,
                        "phase_count": 4,
                        "mode": "determinate",
                        "percent": 100,
                        "completed": 1,
                        "total": 1,
                        "unit": "files",
                        "current_item": "C:\\private\\sample\\启停.txt",
                        "detail": "实验机 AGHID-G，数据源 桌面数据",
                        "machines": [
                            {
                                "machine_id": "AGHID-G",
                                "name": "测试室1",
                                "status": "storing",
                                "completed": 0,
                                "total": 1,
                                "message": "正在入库 启停.txt",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            result_path.write_text(
                json.dumps(
                    {
                        "run_id": "collect-1",
                        "batch_id": batch_id,
                        "mode": "collect",
                        "machines": [
                            {
                                "id": "machine",
                                "roots": [{"label": "root", "inventoried": 1}],
                                "errors": [],
                            }
                        ],
                        "totals": {
                            "inventoried": 1,
                            "stable": 1,
                            "already_collected": (
                                0 if self.collection_has_changes else 1
                            ),
                            "planned_files": (
                                1 if self.collection_has_changes else 0
                            ),
                            "planned_bytes": (
                                7 if self.collection_has_changes else 0
                            ),
                            "downloaded": (
                                1 if self.collection_has_changes else 0
                            ),
                            "ingested": (
                                1 if self.collection_has_changes else 0
                            ),
                            "unchanged_content": 0,
                            "unsettled_skipped": 0,
                            "changed_during_collection": 0,
                            "roots_succeeded": 1,
                            "roots_failed": 0,
                            "errors": 0,
                        },
                    }
                ),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(command, 0, "", "")

        if "material_config_workbook.py" in str(command[1]):
            self.seed_config_payloads.append(
                json.loads(Path(command[4]).read_text(encoding="utf-8"))
            )
            return subprocess.CompletedProcess(command, 0, "{}", "")

        if "--prepare-config" in command and self.duplicate_prepare_error:
            return subprocess.CompletedProcess(
                command,
                1,
                "",
                (
                    "- machine/root/30s-material：绘图名称“NiMoP-30s-30s”"
                    "与 machine/root/10s-material 重复"
                ),
            )

        source_root = Path(environment["START_STOP_SOURCE_ROOT"])
        output_dir = Path(environment["START_STOP_OUTPUT_DIR"])
        self.observed_analysis_roots.append((source_root, output_dir))
        self.assert_private_path(source_root)
        self.assert_private_path(output_dir)
        if "--render" in command and self.fail_render:
            (output_dir / "partial.txt").write_text("must not publish", encoding="utf-8")
            return subprocess.CompletedProcess(command, 1, "", "render failed")

        material = {
            "key": "machine/root/sample",
            "auto_name": "sample",
            "standard_file_count": 1,
            "total_data_points": 2,
            "ordered_source_files": ["machine/root/sample/启停.txt"],
            "fingerprint": "material-1",
        }
        (output_dir / "material_config_snapshot.json").write_text(
            json.dumps(
                {
                    "dataset_fingerprint": "analysis-dataset-1",
                    "generated_at": "2026-08-03T05:10:00+00:00",
                    "materials": [material],
                    "files": [{"included_in_analysis": True}],
                }
            ),
            encoding="utf-8",
        )
        (output_dir / ".material_config_readback.json").write_text(
            json.dumps(
                {
                    "dataset_fingerprint": "analysis-dataset-1",
                    "materials": [
                        {
                            "key": material["key"],
                            "plot_name": "完整样品名",
                            "include_in_summary_atlas": True,
                            "fingerprint": material["fingerprint"],
                            "notes": "",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        workbook = output_dir.joinpath(
            *Path(StartStopWorkspace.CONFIG_WORKBOOK_RELATIVE).parts
        )
        workbook.parent.mkdir(parents=True, exist_ok=True)
        workbook.write_bytes(b"simulated workbook")
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps({"stage": "prepared", "materials": 1}),
            "",
        )

    def assert_private_path(self, path: Path):
        if self.analysis_dir == path or self.analysis_dir in path.parents:
            raise AssertionError("analysis ran inside published cache")
        if self.scratch_dir != path and self.scratch_dir not in path.parents:
            raise AssertionError("analysis escaped the private scratch root")


class StartStopRepositoryWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = FakeRepositoryDatabase(self.root / "state" / "repository.sqlite3")
        self.analysis_script = self.root / "app" / "analyze.py"
        self.analysis_script.parent.mkdir()
        self.analysis_script.write_text("# simulated", encoding="utf-8")
        (self.analysis_script.parent / "material_config_workbook.py").write_text(
            "# simulated builder", encoding="utf-8"
        )
        self.collection_config = self.root / "config" / "collection.json"
        self.collection_config.parent.mkdir()
        identity = self.root / "identity"
        identity.write_text("fixture", encoding="utf-8")
        self.collection_config.write_text(
            json.dumps(
                {
                    "machines": [
                        {
                            "identity_file": str(identity),
                            "roots": [{"label": "root"}],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        self.workspace = SimulatedRepositoryWorkspace(
            self.database,
            self.root / "published",
            analysis_script=self.analysis_script,
            collection_config=self.collection_config,
            scratch_dir=self.root / "scratch",
            repository_mode=True,
            python_executable=sys.executable,
            timeout_seconds=60,
        )

    def tearDown(self):
        self.temporary.cleanup()

    def wait_for_job(self):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = self.workspace._public_job()
            if job["status"] not in {"queued", "running"}:
                return job
            time.sleep(0.01)
        self.fail("repository job did not finish")

    def test_verified_snapshot_cache_materializes_once_and_reuses_source_tree(self):
        content = b"fixture"
        relative = "machine/root/sample/启停.txt"
        sha256 = hashlib.sha256(content).hexdigest()
        fingerprint = "a" * 64
        snapshot = {
            "id": 7,
            "dataset_fingerprint": fingerprint,
            "file_count": 1,
            "total_bytes": len(content),
            "files": [
                {
                    "path": relative,
                    "sha256": sha256,
                    "size_bytes": len(content),
                }
            ],
        }
        calls: list[Path] = []

        def materialize(snapshot_id, target):
            self.assertEqual(snapshot_id, 7)
            target = Path(target)
            calls.append(target)
            output = target.joinpath(*Path(relative).parts)
            output.parent.mkdir(parents=True)
            output.write_bytes(content)
            (target / self.workspace.SNAPSHOT_MANIFEST_NAME).write_text(
                json.dumps(
                    {
                        "snapshot_id": 7,
                        "dataset_fingerprint": fingerprint,
                        "files": snapshot["files"],
                    }
                ),
                encoding="utf-8",
            )
            return {"file_count": 1, "total_bytes": len(content)}

        self.database.materialize_snapshot = materialize
        first, first_hit, first_trusted = self.workspace._materialize_snapshot_source(
            snapshot,
            7,
            self.root / "fallback-1",
        )
        second, second_hit, second_trusted = self.workspace._materialize_snapshot_source(
            snapshot,
            7,
            self.root / "fallback-2",
        )

        self.assertEqual(first, second)
        self.assertFalse(first_hit)
        self.assertTrue(second_hit)
        self.assertTrue(first_trusted)
        self.assertTrue(second_trusted)
        self.assertEqual(len(calls), 1)
        self.assertEqual(first.joinpath(*Path(relative).parts).read_bytes(), content)

    def test_unchanged_scan_skips_materialization_analysis_and_publication(self):
        self.workspace.collection_has_changes = False
        script_sha256, _size = self.workspace._hash_file(self.analysis_script)

        def current_analysis_run(kind="render"):
            if kind != "render":
                return {"state": "none"}
            return {
                "state": "sealed",
                "snapshot_id": 7,
                "config_revision": 0,
                "analysis_script_sha256": script_sha256,
            }

        self.database.current_analysis_run = current_analysis_run

        self.workspace.start_job("scan")
        job = self.wait_for_job()

        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["result"]["analysis_skipped_unchanged"], 1)
        self.assertEqual(job["progress"]["current_item"], "没有新文件")
        self.assertIn("跳过重复", job["progress"]["detail"])
        self.assertEqual(self.database.publish_calls, [])
        self.assertEqual(self.workspace.observed_analysis_roots, [])
        self.assertEqual(list(self.workspace.scratch_dir.iterdir()), [])

    def test_scan_uses_private_snapshot_tree_publishes_and_cleans_scratch(self):
        self.assertTrue(self.workspace.status()["execution"]["update_ready"])

        self.workspace.start_job("scan")
        job = self.wait_for_job()

        self.assertEqual(job["status"], "completed")
        self.assertEqual(self.database.publish_calls, [(7, 0, "scan")])
        self.assertTrue((self.workspace.analysis_dir / "material_config_snapshot.json").is_file())
        self.assertEqual(job["result"]["collection_copied"], 1)
        self.assertEqual(job["result"]["repository_files"], 1)
        self.assertEqual(job["result"]["repository_bytes"], 7)
        self.assertEqual(job["progress"]["percent"], 100)
        self.assertEqual(job["progress"]["phase_label"], "更新完成")
        self.assertEqual(job["progress"]["machines"][0]["machine_id"], "AGHID-G")
        self.assertNotIn("private", json.dumps(job["progress"], ensure_ascii=False))
        self.assertEqual(self.database.recovery_calls, [])
        self.assertEqual(list(self.workspace.scratch_dir.iterdir()), [])
        status = self.workspace.status()
        self.assertEqual(status["repository"]["file_count"], 1)
        self.assertEqual(status["repository"]["candidate_count"], 1)
        self.assertEqual(status["repository"]["database_size_bytes"], 4096)
        self.assertEqual(
            status["repository"]["latest_collection_utc"],
            "2026-08-03T04:00:00+00:00",
        )
        self.assertEqual(
            status["repository"]["source_last_modified_utc"],
            "2026-08-03T02:00:00+00:00",
        )

        self.assertNotIn("database_path", status["repository"])
        generation = self.database.generation
        self.assertIsNotNone(generation)
        assert generation is not None
        self.assertEqual(
            (generation / self.workspace.SCRIPT_NAME).read_bytes(),
            self.analysis_script.read_bytes(),
        )
        sealed = validate_sealed_artifact_directory(generation)
        self.assertGreater(sealed["payload_file_count"], 0)
        provenance = json.loads(
            (generation / self.workspace.PROVENANCE_NAME).read_text(encoding="utf-8")
        )
        self.assertEqual(
            provenance["analysis_script"]["sha256"],
            sealed["manifest"]["provenance"]["analysis_script"]["sha256"],
        )

    def test_scan_applies_saved_database_names_to_private_seed_workbook(self):
        self.workspace.start_job("scan")
        self.assertEqual(self.wait_for_job()["status"], "completed")
        self.database.config_override = {
            "dataset_fingerprint": "analysis-dataset-1",
            "revision": 9,
            "updated_utc": "2026-08-18T06:00:00+00:00",
            "materials": [
                {
                    "material_key": "machine/root/sample",
                    "plot_name": "用户保存的完整名称",
                    "include_in_summary_atlas": True,
                    "notes": "",
                    "source_fingerprint": "material-1",
                }
            ],
        }

        self.workspace.start_job("scan")
        job = self.wait_for_job()

        self.assertEqual(job["status"], "completed")
        self.assertEqual(len(self.workspace.seed_config_payloads), 1)
        saved = self.workspace.seed_config_payloads[0]
        self.assertEqual(saved["dataset_fingerprint"], "analysis-dataset-1")
        self.assertEqual(
            saved["materials"][0]["plot_name"], "用户保存的完整名称"
        )

    def test_automatic_scan_preserves_request_provenance(self):
        self.workspace.start_job("scan", requested_via="automatic")
        job = self.wait_for_job()

        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["requested_via"], "automatic")

        with self.assertRaisesRegex(StartStopWorkspaceError, "来源与任务类型不匹配"):
            self.workspace.start_job("render", requested_via="automatic")

    def test_analysis_run_seals_unique_water_reference_identity(self):
        output = self.root / "analysis-record"
        output.mkdir()
        summary = {
            "water_compensation": {
                "model": {
                    "reference_material": "duplicate display name",
                    "reference_material_display_name": "duplicate display name",
                    "reference_material_key": "machine/root/unique-reference",
                    "reference_series_id": "M23-main",
                    "reference_fit_slope_mv_per_h": -6.3419,
                }
            }
        }
        (output / self.workspace.SUMMARY_NAME).write_text(
            json.dumps(summary),
            encoding="utf-8",
        )

        record = self.workspace._analysis_run_record(
            output,
            snapshot={"dataset_fingerprint": "dataset-fixture"},
            render_config={"materials": []},
            provenance={
                "analysis_script": {"sha256": "a" * 64},
                "runtime": {"python_version": "3.12"},
            },
            sealed_manifest_sha256="b" * 64,
            result={"materials": 1},
        )

        self.assertEqual(
            record["rules"]["water_reference_key"],
            "machine/root/unique-reference",
        )
        self.assertEqual(record["rules"]["water_reference_series_id"], "M23-main")
        self.assertEqual(
            record["rules"]["water_reference_display_name"],
            "duplicate display name",
        )

    def test_status_exposes_path_free_safety_and_maps_provenance(self):
        digest = "a" * 64
        self.workspace.backup_dir = self.root / "backups"
        safety_payload = {
            "storage": {
                "ok": True,
                "preflight_ok": True,
                "free_bytes": 5_000_000_000,
                "total_bytes": 10_000_000_000,
                "used_bytes": 5_000_000_000,
                "used_percent": 50.0,
                "low_space": False,
                "low_space_ratio": 0.15,
                "required_bytes": 2_000_000_000,
                "shortfall_bytes": 0,
            },
            "backup": {
                "configured": True,
                "available": True,
                "backup_count": 1,
                "invalid_count": 0,
                "latest_valid": True,
                "same_filesystem": True,
                "off_disk": False,
            },
        }
        provenance = {
            "state": "legacy_unverified",
            "artifact_generation_id": 4,
            "snapshot_id": 7,
            "config_revision": 2,
            "artifact_manifest_sha256": digest,
            "runtime": {
                "image_or_service_version": "registry.example/start-stop@sha256:" + digest,
                "private_path": "/must/not/leak",
            },
            "secret_path": "/must/not/leak",
        }

        with (
            mock.patch.object(
                self.workspace,
                "_analysis_provenance_status",
                return_value=provenance,
            ),
            mock.patch(
                "echem_platform.start_stop.build_safety_status",
                return_value=safety_payload,
            ),
        ):
            status = self.workspace.status()

        self.assertTrue(status["safety"]["storage"]["preflight_ok"])
        self.assertTrue(status["safety"]["backup"]["available"])
        self.assertEqual(status["safety"]["provenance"]["state"], "legacy")
        self.assertEqual(status["safety"]["provenance"]["artifact_generation_id"], 4)
        self.assertNotIn("/must/not/leak", json.dumps(status["safety"]))

    def test_repository_job_refuses_only_failed_capacity_preflight(self):
        self.workspace.backup_dir = self.root / "backups"
        insufficient = {
            "storage": {
                "ok": False,
                "preflight_ok": False,
                "low_space": True,
                "shortfall_bytes": 1024,
            },
            "backup": {"configured": True, "available": False},
        }

        with mock.patch(
            "echem_platform.start_stop.build_safety_status",
            return_value=insufficient,
        ):
            with self.assertRaises(StartStopWorkspaceError) as caught:
                self.workspace.start_job("scan")

        self.assertEqual(caught.exception.status, 507)
        self.assertEqual(self.workspace._public_job()["status"], "idle")
        self.assertEqual(self.database.publish_calls, [])

    def test_routine_job_blocks_on_fast_database_check_without_creating_backup(self):
        self.database.operational_check = lambda: {
            "ok": False,
            "mode": "operational_metadata",
            "errors": ["fixture mismatch"],
        }

        with self.assertRaises(StartStopWorkspaceError) as caught:
            self.workspace.start_job("scan")

        self.assertEqual(caught.exception.status, 503)
        self.assertIn("快速一致性检查", str(caught.exception))
        self.assertEqual(self.workspace._public_job()["status"], "idle")
        self.assertFalse((self.root / "backups").exists())

    def test_low_space_ratio_is_warning_when_capacity_preflight_passes(self):
        self.workspace.backup_dir = self.root / "backups"
        warning_only = {
            "storage": {
                "ok": True,
                "preflight_ok": True,
                "low_space": True,
                "low_space_ratio": 0.15,
                "shortfall_bytes": 0,
            },
            "backup": {"configured": True, "available": False},
        }

        with mock.patch(
            "echem_platform.start_stop.build_safety_status",
            return_value=warning_only,
        ):
            self.workspace.start_job("scan")
            job = self.wait_for_job()

        self.assertEqual(job["status"], "completed")
        self.assertEqual(len(self.database.publish_calls), 1)

    def test_unreadable_backup_status_degrades_without_breaking_workspace(self):
        self.workspace.backup_dir = self.root / "backups"
        with mock.patch(
            "echem_platform.start_stop.build_safety_status",
            side_effect=PermissionError("private backup path"),
        ):
            status = self.workspace.status()

        self.assertTrue(status["available"])
        self.assertEqual(status["safety"]["storage"], {})
        self.assertTrue(status["safety"]["backup"]["configured"])
        self.assertFalse(status["safety"]["backup"]["available"])
        self.assertNotIn("private backup path", json.dumps(status["safety"]))

    def test_render_starts_clean_and_never_carries_previous_images_or_caches(self):
        self.workspace.start_job("scan")
        self.assertEqual(self.wait_for_job()["status"], "completed")
        previous = self.database.generation
        assert previous is not None
        stale_figure = previous / "figures" / "stale.png"
        stale_figure.parent.mkdir(parents=True)
        stale_figure.write_bytes(b"stale")
        stale_cache = previous / "__pycache__" / "old.pyc"
        stale_cache.parent.mkdir()
        stale_cache.write_bytes(b"cache")

        self.workspace.start_job("render")
        job = self.wait_for_job()

        self.assertEqual(job["status"], "completed")
        current = self.database.generation
        assert current is not None
        self.assertNotEqual(current, previous)
        self.assertFalse((current / "figures" / "stale.png").exists())
        self.assertFalse((current / "__pycache__").exists())
        self.assertFalse((current / ".matplotlib").exists())
        validate_sealed_artifact_directory(current)

    def test_cache_refresh_failure_keeps_committed_generation_successful(self):
        self.workspace.start_job("scan")
        self.assertEqual(self.wait_for_job()["status"], "completed")
        previous_generation = self.database.generation

        with mock.patch.object(
            self.workspace,
            "_restore_published_cache_atomic",
            side_effect=OSError("cache unavailable"),
        ):
            self.workspace.start_job("render")
            job = self.wait_for_job()

        self.assertEqual(job["status"], "completed_with_warnings")
        self.assertEqual(job["failure_class"], "partial")
        self.assertEqual(job["failure_code"], "cache_refresh_failed")
        self.assertTrue(job["result"]["cache_refresh_failed"])
        self.assertIsNot(self.database.generation, previous_generation)
        assert self.database.generation is not None
        validate_sealed_artifact_directory(self.database.generation)

    def test_post_publish_audit_failure_is_a_warning_not_failed(self):
        self.workspace.start_job("scan")
        self.assertEqual(self.wait_for_job()["status"], "completed")
        previous_generation = self.database.generation

        with mock.patch.object(
            self.database,
            "audit",
            side_effect=OSError("audit unavailable"),
        ):
            self.workspace.start_job("render")
            job = self.wait_for_job()

        self.assertEqual(job["status"], "completed_with_warnings")
        self.assertEqual(job["failure_class"], "partial")
        self.assertEqual(job["failure_code"], "post_publish_audit_failed")
        self.assertTrue(job["result"]["audit_record_failed"])
        self.assertIsNot(self.database.generation, previous_generation)
        assert self.database.generation is not None
        validate_sealed_artifact_directory(self.database.generation)

    def test_post_publish_progress_database_error_is_warning_not_failed(self):
        self.workspace.start_job("scan")
        self.assertEqual(self.wait_for_job()["status"], "completed")
        previous_generation = self.database.generation
        original_update_progress = self.workspace._update_progress

        def fail_terminal_progress(job_id, **changes):
            if changes.get("phase") == "completed":
                raise sqlite3.OperationalError("temporary progress write failure")
            return original_update_progress(job_id, **changes)

        with mock.patch.object(
            self.workspace,
            "_update_progress",
            side_effect=fail_terminal_progress,
        ):
            self.workspace.start_job("render")
            job = self.wait_for_job()

        self.assertEqual(job["status"], "completed_with_warnings")
        self.assertEqual(job["failure_class"], "partial")
        self.assertEqual(job["failure_code"], "post_publish_finalization_failed")
        self.assertIsNot(self.database.generation, previous_generation)
        assert self.database.generation is not None
        validate_sealed_artifact_directory(self.database.generation)

    def test_terminal_state_waits_for_private_scratch_cleanup_and_blocks_overlap(self):
        cleanup_entered = threading.Event()
        release_cleanup = threading.Event()
        real_temporary_directory = tempfile.TemporaryDirectory

        class DelayedTemporaryDirectory:
            def __init__(inner_self, *args, **kwargs):
                inner_self.delegate = real_temporary_directory(*args, **kwargs)

            def __enter__(inner_self):
                return inner_self.delegate.__enter__()

            def __exit__(inner_self, exc_type, exc, traceback):
                cleanup_entered.set()
                if not release_cleanup.wait(3):
                    raise RuntimeError("test did not release cleanup")
                return inner_self.delegate.__exit__(exc_type, exc, traceback)

        with mock.patch(
            "echem_platform.start_stop.tempfile.TemporaryDirectory",
            DelayedTemporaryDirectory,
        ):
            self.workspace.start_job("scan")
            self.assertTrue(cleanup_entered.wait(3))
            try:
                running = self.workspace._public_job()
                self.assertEqual(running["status"], "running")
                self.assertEqual(running["stage"], "publishing")
                self.assertLess(running["progress"]["percent"], 100)
                with self.assertRaises(StartStopWorkspaceError) as raised:
                    self.workspace.start_job("render")
                self.assertEqual(raised.exception.status, 409)
            finally:
                release_cleanup.set()

        completed = self.wait_for_job()
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["progress"]["percent"], 100)
        self.assertEqual(list(self.workspace.scratch_dir.iterdir()), [])

    def test_unexpected_worker_exception_finishes_with_generic_failure(self):
        with mock.patch.object(
            self.workspace,
            "_run_job",
            side_effect=TypeError("private implementation detail"),
        ):
            self.workspace.start_job("scan")
            job = self.wait_for_job()

        self.assertEqual(job["status"], "failed")
        self.assertNotIn("private implementation detail", job["message"])
        deadline = time.monotonic() + 1
        while self.workspace._active_job_id and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.workspace._active_job_id, "")

    def test_new_collector_ingest_counts_keep_legacy_public_fields(self):
        payload = {
            "machines": [
                {
                    "roots": [{"label": "root", "inventoried": 2}],
                    "errors": [],
                }
            ],
            "totals": {
                "inventoried": 2,
                "ingested": 2,
                "downloaded": 2,
                "unchanged_content": 1,
                "roots_succeeded": 1,
                "errors": 0,
            },
        }

        result = self.workspace._public_collection_result(payload, returncode=0)

        self.assertEqual(result["collection_copied"], 1)
        self.assertEqual(result["collection_versioned"], 0)
        self.assertEqual(result["collection_ingested"], 2)

    def test_render_child_progress_updates_real_stage_and_rejects_regression(self):
        job_id = "render-progress-job"
        with self.workspace._job_lock:
            self.workspace._job = {
                "id": job_id,
                "action": "render",
                "status": "running",
                "stage": "rendering",
                "message": "正在重新绘图",
                "progress": {
                    "phase": "materializing_snapshot",
                    "phase_label": "准备分析数据",
                    "phase_index": 1,
                    "phase_count": 7,
                    "mode": "indeterminate",
                    "percent": 4,
                    "completed": 0,
                    "total": 1,
                    "unit": "steps",
                    "current_item": "数据库快照",
                    "detail": "正在准备",
                    "machines": [],
                },
            }
        progress_path = self.root / "render-progress.json"
        progress_path.write_text(
            json.dumps(
                {
                    "phase": "rendering_standard",
                    "phase_label": "生成标准图集",
                    "phase_index": 4,
                    "percent": 72.5,
                    "completed": 11,
                    "total": 28,
                    "unit": "pages",
                    "current_item": "第 12／28 页 · 阴极偏移异常判断",
                    "detail": "正在生成 PNG、SVG 与 PDF",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        self.workspace._apply_render_progress(job_id, progress_path)
        current = self.workspace._public_job()["progress"]

        self.assertEqual(current["phase_index"], 4)
        self.assertEqual(current["percent"], 72.5)
        self.assertEqual(current["unit"], "pages")
        self.assertEqual(current["completed"], 11)
        self.assertEqual(current["total"], 28)
        self.assertIn("阴极偏移异常判断", current["current_item"])

        progress_path.write_text(
            json.dumps(
                {
                    "phase": "loading_files",
                    "phase_label": "读取文件",
                    "phase_index": 2,
                    "percent": 12,
                    "completed": 1,
                    "total": 50,
                    "unit": "files",
                }
            ),
            encoding="utf-8",
        )
        self.workspace._apply_render_progress(job_id, progress_path)
        regressed = self.workspace._public_job()["progress"]
        self.assertEqual(regressed["phase_index"], 4)
        self.assertEqual(regressed["percent"], 72.5)

        progress_path.write_text(
            json.dumps({"phase": "run_shell", "percent": 99}),
            encoding="utf-8",
        )
        self.workspace._apply_render_progress(job_id, progress_path)
        self.assertEqual(self.workspace._public_job()["progress"], regressed)

    def test_abnormal_collector_exit_reports_signal_and_recovers_exact_batch(self):
        self.workspace.collection_returncode = -9

        self.workspace.start_job("scan")
        job = self.wait_for_job()

        self.assertEqual(job["status"], "failed")
        self.assertIn("SIGKILL", job["message"])
        self.assertIn("returncode=-9", job["message"])
        self.assertNotIn("未知错误", job["message"])
        self.assertEqual(len(self.database.recovery_calls), 1)
        recovered = self.database.recovery_calls[0]
        self.assertRegex(recovered["batch_id"], r"^[0-9a-f]{32}$")
        self.assertEqual(recovered["returncode"], -9)
        self.assertEqual(recovered["parent_job_id"], job["id"])
        self.assertEqual(self.database.publish_calls, [])

    def test_collector_timeout_recovers_exact_batch_without_inventing_exit_code(self):
        self.workspace.collection_timeout = True

        self.workspace.start_job("scan")
        job = self.wait_for_job()

        self.assertEqual(job["status"], "failed")
        self.assertIn("超时", job["message"])
        self.assertEqual(len(self.database.recovery_calls), 1)
        recovered = self.database.recovery_calls[0]
        self.assertRegex(recovered["batch_id"], r"^[0-9a-f]{32}$")
        self.assertIsNone(recovered["returncode"])
        self.assertIn("未返回退出码", recovered["reason"])

    def test_process_error_keeps_plain_nonzero_exit_code(self):
        completed = subprocess.CompletedProcess([], 137, "", "")

        message = self.workspace._process_error(completed)

        self.assertEqual(message, "子进程退出码 137")

    def test_duplicate_plot_name_failure_is_actionable_and_hides_paths(self):
        self.workspace.duplicate_prepare_error = True

        self.workspace.start_job("scan")
        job = self.wait_for_job()

        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["failure_code"], "duplicate_material_name")
        self.assertEqual(
            job["message"],
            (
                "检测到重复的绘图名称：“NiMoP-30s-30s”。"
                "新数据已保留，请到“材料库”修改完整绘图名称后重试更新。"
            ),
        )
        self.assertNotIn("machine/root", job["message"])
        self.assertEqual(self.database.publish_calls, [])

    def test_render_failure_keeps_last_published_generation_and_cleans_scratch(self):
        self.workspace.start_job("scan")
        self.assertEqual(self.wait_for_job()["status"], "completed")
        before = (self.workspace.analysis_dir / "material_config_snapshot.json").read_bytes()
        generation = self.database.generation
        self.workspace.fail_render = True

        self.workspace.start_job("render")
        job = self.wait_for_job()

        self.assertEqual(job["status"], "failed")
        self.assertEqual(self.database.generation, generation)
        self.assertEqual(len(self.database.publish_calls), 1)
        self.assertEqual(
            (self.workspace.analysis_dir / "material_config_snapshot.json").read_bytes(),
            before,
        )
        self.assertEqual(list(self.workspace.scratch_dir.iterdir()), [])

    def test_repository_status_falls_back_to_published_artifact_for_read_only_db(self):
        self.workspace.start_job("scan")
        self.assertEqual(self.wait_for_job()["status"], "completed")
        self.database.status_override = {
            "file_count": 0,
            "version_count": 0,
            "total_bytes": 0,
        }

        repository = self.workspace.status()["repository"]

        self.assertEqual(repository["file_count"], 1)
        self.assertEqual(repository["total_bytes"], 7)
        self.assertEqual(repository["artifact_generation_count"], 1)

    def test_read_only_cache_reports_committed_material_config_revision(self):
        self.workspace.start_job("scan")
        self.assertEqual(self.wait_for_job()["status"], "completed")
        (self.workspace.analysis_dir / ".start-stop-artifacts.json").write_text(
            json.dumps({"config_revision": 7}), encoding="utf-8"
        )

        self.assertEqual(self.workspace.materials()["revision"], 7)

    def test_startup_restore_replaces_a_stale_nonempty_cache(self):
        self.workspace.start_job("scan")
        self.assertEqual(self.wait_for_job()["status"], "completed")
        snapshot_path = self.workspace.analysis_dir / "material_config_snapshot.json"
        expected = snapshot_path.read_bytes()
        snapshot_path.write_bytes(b"stale-cache")

        restored = self.workspace.ensure_published_cache()

        self.assertTrue(restored)
        self.assertEqual(snapshot_path.read_bytes(), expected)


if __name__ == "__main__":
    unittest.main()
