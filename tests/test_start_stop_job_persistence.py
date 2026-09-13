from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import start_stop_service as SERVICE
from echem_platform.start_stop import StartStopWorkspace
from echem_platform.start_stop_database import (
    SCHEMA_VERSION,
    StartStopDatabase,
    seal_artifact_directory,
)


class StartStopJobPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = StartStopDatabase(self.root / "state.sqlite3")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def queued_progress() -> dict:
        return {
            "phase": "queued",
            "phase_label": "等待执行",
            "phase_index": 0,
            "phase_count": 7,
            "mode": "indeterminate",
            "percent": 0,
            "completed": 0,
            "total": 7,
            "unit": "steps",
            "current_item": "",
            "detail": "",
            "machines": [],
        }

    def create_job(self, job_id: str, action: str = "render") -> dict:
        return self.database.create_job(
            job_id=job_id,
            action=action,
            requested_via="manual",
            progress=self.queued_progress(),
        )

    def snapshot_and_config(self) -> tuple[dict, dict]:
        content = b"ID_GalSquareWave\nmeta\nmeta\n"
        staged = self.root / "source.txt"
        staged.write_bytes(content)
        batch_id = self.database.begin_collection_batch(machine_count=1)
        self.database.ingest_staged_file(
            staged,
            {
                "machine_id": "machine-a",
                "root_label": "desktop",
                "remote_path": r"C:\data\sample\启停.txt",
                "remote_relative_path": r"sample\启停.txt",
                "size": len(content),
                "last_write_ticks": 638_898_000_000_000_000,
                "last_write_utc": "2026-08-03T05:04:03Z",
                "is_candidate": True,
            },
            batch_id,
        )
        self.database.finish_collection_batch(batch_id)
        snapshot = self.database.freeze_snapshot(candidate_only=True)
        config = self.database.save_start_stop_config(
            dataset_fingerprint=snapshot["dataset_fingerprint"],
            expected_revision=0,
            materials=[
                {
                    "material_key": "machine-a/desktop/sample",
                    "plot_name": "sample",
                    "include_in_summary_atlas": True,
                    "notes": "",
                    "source_fingerprint": "source-fixture",
                }
            ],
        )
        return snapshot, config

    def sealed_output(
        self,
        snapshot: dict,
        config: dict,
    ) -> tuple[Path, dict, str, str, str]:
        output = self.root / f"output-{config['revision']}"
        output.mkdir()
        summary = output / "analysis_summary.json"
        summary.write_text(
            json.dumps({"counts": {"materials": 1}, "rules": {"rhe_offset_v": 0.9268}}),
            encoding="utf-8",
        )
        script = output / "analyze_and_plot_start_stop.py"
        script.write_text("print('sealed')\n", encoding="utf-8")
        script_sha = hashlib.sha256(script.read_bytes()).hexdigest()
        provenance = {
            "analysis_script": {"sha256": script_sha},
            "runtime": {"python_version": "3.12"},
        }
        seal = seal_artifact_directory(
            output,
            snapshot_id=snapshot["id"],
            config_revision=config["revision"],
            kind="render",
            provenance=provenance,
        )
        summary_sha = hashlib.sha256(summary.read_bytes()).hexdigest()
        config_sha = hashlib.sha256(b"fixed-config").hexdigest()
        return output, seal, script_sha, summary_sha, config_sha

    def test_current_schema_and_single_active_job_lifecycle_are_persistent(self) -> None:
        with self.database.session() as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 5)
            self.assertEqual(SCHEMA_VERSION, 5)
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertTrue({"jobs", "job_events", "analysis_runs"}.issubset(tables))

        created = self.create_job("job-one")
        self.assertEqual(created["status"], "queued")
        with self.assertRaises(sqlite3.IntegrityError):
            self.create_job("job-two")

        claimed = self.database.claim_job(
            "job-one", worker_instance_id="worker-one"
        )
        self.assertEqual(claimed["status"], "running")
        staged = self.database.update_job(
            "job-one",
            {"stage": "rendering", "message": "正在绘图"},
        )
        self.assertEqual(staged["stage"], "rendering")
        progressed = self.database.update_job_progress(
            "job-one", {"phase": "rendering", "percent": 50}
        )
        self.assertEqual(progressed["progress"]["percent"], 50)
        finished = self.database.finish_job(
            "job-one",
            status="completed",
            stage="completed",
            message="完成",
        )
        self.assertEqual(finished["status"], "completed")
        self.assertGreaterEqual(finished["event_cursor"], 4)
        self.assertEqual(self.create_job("job-two")["status"], "queued")

        with self.database.session() as connection:
            event_id = connection.execute(
                "SELECT id FROM job_events WHERE job_id='job-one' LIMIT 1"
            ).fetchone()[0]
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE job_events SET message='tampered' WHERE id=?", (event_id,)
                )

    def test_v4_database_fails_closed_when_job_columns_are_incomplete(self) -> None:
        database_path = self.database.path
        with sqlite3.connect(database_path) as connection:
            connection.executescript(
                """
                PRAGMA foreign_keys=OFF;
                DROP TABLE jobs;
                CREATE TABLE jobs(id TEXT PRIMARY KEY);
                PRAGMA user_version=4;
                """
            )

        with self.assertRaisesRegex(RuntimeError, "schema v4 objects are present but incomplete"):
            StartStopDatabase(database_path)

    def test_v3_migration_fails_closed_when_v4_objects_are_partial(self) -> None:
        database_path = self.database.path
        with sqlite3.connect(database_path) as connection:
            connection.executescript(
                """
                DROP TRIGGER analysis_runs_immutable_delete;
                PRAGMA user_version=3;
                """
            )

        with self.assertRaisesRegex(RuntimeError, "schema v4 objects are present but incomplete"):
            StartStopDatabase(database_path)

    def test_v4_database_fails_closed_when_required_index_is_missing(self) -> None:
        database_path = self.database.path
        with sqlite3.connect(database_path) as connection:
            connection.execute("DROP INDEX idx_job_events_job")

        with self.assertRaisesRegex(RuntimeError, "schema v4 objects are present but incomplete"):
            StartStopDatabase(database_path)

    def test_restart_marks_unpublished_job_interrupted(self) -> None:
        self.create_job("job-restart", action="scan")
        self.database.claim_job(
            "job-restart", worker_instance_id="old-worker"
        )
        self.database.update_job(
            "job-restart",
            {"stage": "scanning_files", "message": "正在扫描"},
        )

        recovered = self.database.recover_interrupted_jobs(
            worker_instance_id="new-worker"
        )

        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]["status"], "interrupted")
        self.assertEqual(recovered[0]["failure_class"], "interrupted")
        self.assertEqual(recovered[0]["failure_code"], "process_restart")
        self.assertTrue(recovered[0]["can_retry"])

    def test_failed_job_persists_before_best_effort_audit(self) -> None:
        script = self.root / "analysis.py"
        script.write_text("# fixture\n", encoding="utf-8")
        workspace = StartStopWorkspace(
            self.database,
            self.root / "published",
            analysis_script=script,
            scratch_dir=self.root / "scratch",
            repository_mode=True,
        )
        created = self.create_job("job-audit-failure", action="render")
        claimed = self.database.claim_job(
            created["id"],
            worker_instance_id="worker",
        )
        workspace._job = claimed
        workspace._active_job_id = created["id"]

        with mock.patch.object(
            self.database,
            "audit",
            side_effect=OSError("audit unavailable"),
        ):
            workspace._finish_failed(created["id"], "render failed")

        persisted = self.database.get_job(created["id"])
        self.assertEqual(persisted["status"], "failed")
        self.assertEqual(persisted["failure_class"], "fatal")
        self.assertEqual(persisted["failure_code"], "job_failed")

    def test_render_analysis_run_and_current_generation_commit_atomically(self) -> None:
        snapshot, config = self.snapshot_and_config()
        self.create_job("job-render")
        self.database.claim_job("job-render", worker_instance_id="worker")
        output, seal, script_sha, summary_sha, config_sha = self.sealed_output(
            snapshot, config
        )
        published = self.database.publish_sealed_artifacts(
            output,
            snapshot["id"],
            config["revision"],
            "render",
            job_id="job-render",
            analysis_run={
                "dataset_fingerprint": snapshot["dataset_fingerprint"],
                "artifact_manifest_sha256": seal["manifest_sha256"],
                "analysis_script_sha256": script_sha,
                "material_config_sha256": config_sha,
                "analysis_summary_sha256": summary_sha,
                "rules": {"rhe_offset_v": 0.9268},
                "runtime": {"python_version": "3.12"},
                "result_summary": {"materials": 1},
            },
        )

        provenance = self.database.current_analysis_run()
        persisted_job = self.database.get_job("job-render")
        self.assertEqual(provenance["state"], "sealed")
        self.assertEqual(
            provenance["artifact_generation_id"], published["generation_id"]
        )
        self.assertEqual(persisted_job["artifact_generation_id"], published["generation_id"])

        recovered = self.database.recover_interrupted_jobs(
            worker_instance_id="replacement"
        )
        self.assertEqual(recovered[0]["status"], "completed")
        self.assertEqual(self.database.current_analysis_run()["state"], "sealed")

    def test_invalid_analysis_run_rolls_back_generation_and_current_pointer(self) -> None:
        snapshot, config = self.snapshot_and_config()
        self.create_job("job-bad")
        self.database.claim_job("job-bad", worker_instance_id="worker")
        output, seal, script_sha, _summary_sha, config_sha = self.sealed_output(
            snapshot, config
        )
        with self.assertRaises(ValueError):
            self.database.publish_sealed_artifacts(
                output,
                snapshot["id"],
                config["revision"],
                "render",
                job_id="job-bad",
                analysis_run={
                    "dataset_fingerprint": snapshot["dataset_fingerprint"],
                    "artifact_manifest_sha256": seal["manifest_sha256"],
                    "analysis_script_sha256": script_sha,
                    "material_config_sha256": config_sha,
                    "analysis_summary_sha256": "0" * 64,
                    "rules": {},
                    "runtime": {},
                    "result_summary": {},
                },
            )
        with self.database.session() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM artifact_generations").fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM artifact_current").fetchone()[0],
                0,
            )
        self.assertIsNone(self.database.get_job("job-bad")["artifact_generation_id"])

    def test_old_render_generation_is_explicitly_legacy_unverified(self) -> None:
        snapshot, config = self.snapshot_and_config()
        output, _seal, _script_sha, _summary_sha, _config_sha = self.sealed_output(
            snapshot, config
        )
        self.database.publish_sealed_artifacts(
            output,
            snapshot["id"],
            config["revision"],
            "render",
        )
        provenance = self.database.current_analysis_run()
        self.assertEqual(provenance["state"], "legacy_unverified")
        self.assertGreater(provenance["artifact_generation_id"], 0)

    def test_public_status_whitelists_persistent_job_and_provenance_fields(self) -> None:
        digest = "a" * 64
        public = SERVICE.public_status(
            {
                "available": True,
                "job": {
                    "id": "job-safe",
                    "action": "render",
                    "requested_via": "manual",
                    "status": "interrupted",
                    "stage": "rendering",
                    "failure_class": "interrupted",
                    "failure_code": "process_restart",
                    "created_utc": "2026-08-04T10:00:00+00:00",
                    "updated_utc": "2026-08-04T10:01:00+00:00",
                    "event_cursor": 8,
                    "can_retry": True,
                    "message": "/private/path",
                    "progress": {},
                    "result": {},
                    "worker_instance_id": "must-not-leak",
                },
                "analysis_provenance": {
                    "state": "sealed",
                    "analysis_run_id": 1,
                    "job_id": "job-safe",
                    "snapshot_id": 2,
                    "config_revision": 3,
                    "artifact_generation_id": 4,
                    "dataset_fingerprint": digest,
                    "artifact_manifest_sha256": digest,
                    "analysis_script_sha256": digest,
                    "material_config_sha256": digest,
                    "analysis_summary_sha256": digest,
                    "created_utc": "2026-08-04T10:01:00+00:00",
                    "runtime": {
                        "python_version": "3.12",
                        "private_path": "/private/runtime",
                    },
                    "rules": {
                        "water_reference": "NiMo reference",
                        "water_reference_display_name": "NiMo reference",
                        "water_reference_key": "machine/root/reference",
                        "water_reference_series_id": "M23-main",
                    },
                    "secret": "/private/secret",
                },
                "safety": {
                    "storage": {"preflight_ok": True},
                    "backup": {
                        "configured": True,
                        "available": True,
                        "latest_valid": True,
                        "latest_created_with_full_verification": True,
                        "latest_verification_level": "manifest_and_size",
                        "destination_preflight_ok": True,
                        "destination_required_bytes": 1234,
                        "destination_shortfall_bytes": 0,
                        "private_path": "/private/backup",
                    },
                },
            },
            lan_read_only=False,
        )
        self.assertEqual(public["job"]["failure_code"], "process_restart")
        self.assertTrue(public["job"]["can_retry"])
        self.assertNotIn("worker_instance_id", public["job"])
        self.assertNotIn("secret", public["analysis_provenance"])
        self.assertNotIn("private_path", public["analysis_provenance"]["runtime"])
        self.assertEqual(
            public["analysis_provenance"]["rules"]["water_reference_key"],
            "machine/root/reference",
        )
        self.assertTrue(
            public["safety"]["backup"]["latest_created_with_full_verification"]
        )
        self.assertEqual(
            public["safety"]["backup"]["latest_verification_level"],
            "manifest_and_size",
        )
        self.assertTrue(public["safety"]["backup"]["destination_preflight_ok"])
        self.assertNotIn("/private", json.dumps(public, ensure_ascii=False))

    def test_public_status_rejects_unknown_backup_verification_level(self) -> None:
        public = SERVICE.public_status(
            {
                "available": True,
                "safety": {
                    "backup": {
                        "latest_verification_level": "/private/backup",
                    }
                },
            },
            lan_read_only=False,
        )

        self.assertNotIn(
            "latest_verification_level",
            public["safety"]["backup"],
        )


class StartStopRecoveryLaunchTests(unittest.TestCase):
    def run_main(self, *, lan_read_only: bool) -> mock.Mock:
        workspace = mock.Mock()
        workspace.database = mock.Mock()
        scheduler = mock.Mock()
        server = mock.Mock()
        arguments = [
            "--database", "/tmp/test.sqlite3",
            "--analysis-dir", "/tmp/analysis",
            "--scratch-dir", "/tmp/scratch",
        ]
        if lan_read_only:
            arguments.append("--lan-read-only")
        with (
            mock.patch.object(
                SERVICE, "_validated_launch", return_value=("127.0.0.1", "127.0.0.1", 8787)
            ),
            mock.patch.object(SERVICE, "build_workspace", return_value=workspace),
            mock.patch.object(
                SERVICE,
                "load_lan_auth_file",
                return_value=SERVICE.BasicAuthCredentials("reader", "test-password"),
            ),
            mock.patch.object(SERVICE, "AutoUpdateScheduler", return_value=scheduler),
            mock.patch.object(SERVICE, "create_handler", return_value=mock.Mock()),
            mock.patch.object(SERVICE, "StartStopHTTPServer", return_value=server),
        ):
            self.assertEqual(SERVICE.main(arguments), 0)
        return workspace

    def test_only_local_service_runs_restart_recovery(self) -> None:
        local = self.run_main(lan_read_only=False)
        local.recover_after_restart.assert_called_once_with()
        lan = self.run_main(lan_read_only=True)
        lan.recover_after_restart.assert_not_called()

    def test_local_instance_lock_precedes_workspace_build_and_recovery(self) -> None:
        events: list[str] = []
        workspace = mock.Mock()
        workspace.database = mock.Mock()
        workspace.recover_after_restart.side_effect = lambda: events.append("recover")
        instance_lock = mock.Mock()
        instance_lock.acquire.side_effect = lambda: events.append("lock")
        instance_lock.close.side_effect = lambda: events.append("unlock")
        scheduler = mock.Mock()
        server = mock.Mock()
        server.serve_forever.side_effect = lambda: events.append("serve")
        arguments = [
            "--database", "/tmp/start-stop-lock-order.sqlite3",
            "--analysis-dir", "/tmp/start-stop-lock-order-analysis",
            "--scratch-dir", "/tmp/start-stop-lock-order-scratch",
        ]

        with (
            mock.patch.object(
                SERVICE,
                "_validated_launch",
                return_value=("127.0.0.1", "127.0.0.1", 8787),
            ),
            mock.patch.object(
                SERVICE, "DatabaseInstanceLock", return_value=instance_lock
            ) as lock_type,
            mock.patch.object(
                SERVICE,
                "build_workspace",
                side_effect=lambda _args: events.append("build") or workspace,
            ),
            mock.patch.object(SERVICE, "AutoUpdateScheduler", return_value=scheduler),
            mock.patch.object(SERVICE, "create_handler", return_value=mock.Mock()),
            mock.patch.object(SERVICE, "StartStopHTTPServer", return_value=server),
        ):
            self.assertEqual(SERVICE.main(arguments), 0)

        lock_type.assert_called_once_with("/tmp/start-stop-lock-order.sqlite3")
        self.assertEqual(events, ["lock", "build", "recover", "serve", "unlock"])

    def test_local_instance_lock_is_released_when_server_startup_fails(self) -> None:
        workspace = mock.Mock()
        workspace.database = mock.Mock()
        instance_lock = mock.Mock()
        arguments = [
            "--database", "/tmp/start-stop-lock-cleanup.sqlite3",
            "--analysis-dir", "/tmp/start-stop-lock-cleanup-analysis",
            "--scratch-dir", "/tmp/start-stop-lock-cleanup-scratch",
        ]

        with (
            mock.patch.object(
                SERVICE,
                "_validated_launch",
                return_value=("127.0.0.1", "127.0.0.1", 8787),
            ),
            mock.patch.object(
                SERVICE, "DatabaseInstanceLock", return_value=instance_lock
            ),
            mock.patch.object(SERVICE, "build_workspace", return_value=workspace),
            mock.patch.object(SERVICE, "AutoUpdateScheduler", return_value=mock.Mock()),
            mock.patch.object(SERVICE, "create_handler", return_value=mock.Mock()),
            mock.patch.object(
                SERVICE,
                "StartStopHTTPServer",
                side_effect=OSError("监听失败"),
            ),
            mock.patch("sys.stderr"),
        ):
            with self.assertRaises(SystemExit):
                SERVICE.main(arguments)

        instance_lock.acquire.assert_called_once_with()
        instance_lock.close.assert_called_once_with()


class StartStopDatabaseInstanceLockTests(unittest.TestCase):
    def test_lock_is_private_exclusive_and_reusable_after_close(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database_path = root / "state.sqlite3"
            lock_path = root / "state.sqlite3.service.lock"
            first = SERVICE.DatabaseInstanceLock(database_path)
            second = SERVICE.DatabaseInstanceLock(database_path)
            first.acquire()
            try:
                self.assertEqual(lock_path.stat().st_mode & 0o777, 0o600)
                with self.assertRaisesRegex(
                    ValueError,
                    "同一启停数据库已有本机服务运行",
                ) as conflict:
                    second.acquire()
                self.assertNotIn(str(root), str(conflict.exception))
            finally:
                first.close()

            second.acquire()
            second.close()

    def test_symbolic_link_lock_is_rejected_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database_path = root / "state.sqlite3"
            lock_path = root / "state.sqlite3.service.lock"
            target = root / "unrelated.txt"
            target.write_text("keep", encoding="utf-8")
            target.chmod(0o640)
            lock_path.symlink_to(target)

            with self.assertRaisesRegex(ValueError, "无法安全创建") as unsafe:
                SERVICE.DatabaseInstanceLock(database_path).acquire()

            self.assertNotIn(str(root), str(unsafe.exception))
            self.assertEqual(target.read_text(encoding="utf-8"), "keep")
            self.assertEqual(target.stat().st_mode & 0o777, 0o640)


if __name__ == "__main__":
    unittest.main()
