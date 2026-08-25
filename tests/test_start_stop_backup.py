from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from echem_platform.start_stop_backup import (
    build_safety_status,
    InsufficientStorageError,
    StartStopBackupError,
    UnsafeBackupPathError,
    create_backup,
    operational_check_database,
    plan_backup_retention,
    read_backup_status,
    storage_preflight,
    verify_backup,
)


UTC = dt.timezone.utc


class StartStopBackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database_path = self.root / "state" / "start-stop.sqlite3"
        self.database_path.parent.mkdir()
        self.connection = sqlite3.connect(self.database_path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("CREATE TABLE samples(id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
        self.connection.executemany(
            "INSERT INTO samples(name) VALUES(?)",
            [("NiMo",), ("NiFeMo",), ("NiMoP",)],
        )
        self.connection.commit()
        self.backup_dir = self.root / "backups"

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary.cleanup()

    def backup(self, name: str = "fixture.sqlite3", **kwargs) -> dict:
        return create_backup(
            self.database_path,
            self.backup_dir,
            backup_name=name,
            safety_margin_bytes=0,
            safety_margin_ratio=0,
            **kwargs,
        )

    def test_online_backup_is_consistent_verified_and_private(self) -> None:
        result = self.backup(metadata={"reason": "daily"})
        backup_path = Path(result["backup_path"])
        manifest_path = Path(result["manifest_path"])

        self.assertTrue(backup_path.is_file())
        self.assertTrue(manifest_path.is_file())
        self.assertEqual(backup_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(manifest_path.stat().st_mode & 0o777, 0o600)
        with sqlite3.connect(backup_path) as restored:
            names = [row[0] for row in restored.execute("SELECT name FROM samples ORDER BY id")]
            self.assertEqual(names, ["NiMo", "NiFeMo", "NiMoP"])
            self.assertEqual(restored.execute("PRAGMA quick_check").fetchone()[0], "ok")

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["database_file"], "fixture.sqlite3")
        self.assertEqual(manifest["quick_check"], ["ok"])
        self.assertEqual(manifest["metadata"], {"reason": "daily"})
        self.assertNotIn(str(self.database_path.parent), manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(
            manifest["sha256"], hashlib.sha256(backup_path.read_bytes()).hexdigest()
        )
        self.assertTrue(verify_backup(backup_path)["valid"])
        self.assertFalse(any(path.name.endswith(".tmp") for path in self.backup_dir.iterdir()))
        self.assertFalse(any(path.name.endswith(".lock") for path in self.backup_dir.iterdir()))

    def test_operational_check_is_read_only_and_does_not_copy_or_hash_blobs(self) -> None:
        before = self.database_path.stat().st_mtime_ns

        result = operational_check_database(
            self.database_path,
            expected_schema_version=0,
            required_tables=("samples",),
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "operational_metadata")
        self.assertEqual(result["foreign_key_errors"], 0)
        self.assertEqual(result["required_table_count"], 1)
        self.assertEqual(self.database_path.stat().st_mtime_ns, before)
        self.assertFalse(self.backup_dir.exists())

        mismatch = operational_check_database(
            self.database_path,
            expected_schema_version=5,
            required_tables=("samples",),
        )
        self.assertFalse(mismatch["ok"])
        self.assertIn("schema version", mismatch["errors"][0])

    def test_backup_sees_committed_wal_content_without_touching_live_database(self) -> None:
        self.connection.execute("INSERT INTO samples(name) VALUES('CoMoP')")
        self.connection.commit()
        before = self.database_path.stat().st_mtime_ns

        result = self.backup()

        with sqlite3.connect(result["backup_path"]) as restored:
            self.assertEqual(restored.execute("SELECT COUNT(*) FROM samples").fetchone()[0], 4)
        self.assertEqual(self.database_path.stat().st_mtime_ns, before)
        self.assertTrue(Path(f"{self.database_path}-wal").exists())

    def test_existing_target_is_never_overwritten(self) -> None:
        first = self.backup()
        original = Path(first["backup_path"]).read_bytes()
        self.connection.execute("INSERT INTO samples(name) VALUES('new')")
        self.connection.commit()

        with self.assertRaises(FileExistsError):
            self.backup()

        self.assertEqual(Path(first["backup_path"]).read_bytes(), original)
        self.assertTrue(verify_backup(first["backup_path"])["valid"])

    def test_manifest_publish_failure_removes_unpaired_database(self) -> None:
        real_replace = os.replace
        calls = 0

        def fail_second_replace(source, destination):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated manifest publication failure")
            return real_replace(source, destination)

        with mock.patch("echem_platform.start_stop_backup.os.replace", fail_second_replace):
            with self.assertRaisesRegex(OSError, "manifest publication failure"):
                self.backup("atomic.sqlite3")

        self.assertEqual(calls, 2)
        self.assertFalse((self.backup_dir / "atomic.sqlite3").exists())
        self.assertFalse((self.backup_dir / "atomic.sqlite3.manifest.json").exists())
        self.assertEqual(list(self.backup_dir.iterdir()), [])

    def test_rejects_escape_absolute_symlink_and_live_database_targets(self) -> None:
        with self.assertRaises(UnsafeBackupPathError):
            self.backup("../escape.sqlite3")
        with self.assertRaises(UnsafeBackupPathError):
            self.backup(str(self.root / "absolute.sqlite3"))
        with self.assertRaises(UnsafeBackupPathError):
            create_backup(
                self.database_path,
                self.database_path.parent,
                backup_name=self.database_path.name,
                safety_margin_bytes=0,
                safety_margin_ratio=0,
            )

        self.backup_dir.mkdir(exist_ok=True)
        target = self.backup_dir / "linked.sqlite3"
        target.symlink_to(self.database_path)
        with self.assertRaises(UnsafeBackupPathError):
            self.backup("linked.sqlite3")

    def test_corruption_is_visible_in_verification_and_status(self) -> None:
        result = self.backup()
        backup_path = Path(result["backup_path"])
        payload = bytearray(backup_path.read_bytes())
        payload[-1] ^= 0xFF
        backup_path.write_bytes(payload)

        verification = verify_backup(backup_path)
        self.assertFalse(verification["valid"])
        self.assertTrue(any("SHA-256" in error for error in verification["errors"]))
        status = read_backup_status(self.backup_dir)
        self.assertEqual(status["backup_count"], 1)
        self.assertEqual(status["invalid_count"], 1)
        self.assertFalse(status["latest"]["valid"])

    def test_status_reports_orphans_and_never_follows_manifest_escape(self) -> None:
        self.backup()
        orphan = self.backup_dir / "orphan.sqlite3"
        orphan.write_bytes(b"not a database")
        malicious = self.backup_dir / "evil.sqlite3.manifest.json"
        malicious.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "start_stop_sqlite_backup",
                    "created_utc": "2026-08-01T00:00:00+00:00",
                    "database_file": "../state/start-stop.sqlite3",
                    "sha256": "0" * 64,
                    "size_bytes": 0,
                }
            ),
            encoding="utf-8",
        )

        status = read_backup_status(self.backup_dir, verify_all=True)

        self.assertEqual(status["backup_count"], 2)
        self.assertEqual(status["orphan_databases"], ["orphan.sqlite3"])
        evil = next(item for item in status["backups"] if "evil" in item["manifest_path"])
        self.assertFalse(evil["valid"])
        self.assertIn("different database file", " ".join(evil["errors"]))

    def test_storage_preflight_accounts_for_output_wal_and_safety_margin(self) -> None:
        preflight = storage_preflight(
            self.database_path,
            self.backup_dir,
            estimated_output_bytes=12_345,
            safety_margin_bytes=10_000,
            safety_margin_ratio=0.5,
            available_bytes_override=10**9,
        )

        workload = (
            preflight["estimated_backup_bytes"]
            + preflight["database_wal_bytes"]
            + 12_345
        )
        self.assertEqual(preflight["safety_margin_bytes"], max(10_000, (workload + 1) // 2))
        self.assertEqual(
            preflight["required_bytes"], workload + preflight["safety_margin_bytes"]
        )
        self.assertTrue(preflight["ok"])

    def test_insufficient_storage_fails_before_creating_backup_directory(self) -> None:
        with self.assertRaises(InsufficientStorageError) as caught:
            create_backup(
                self.database_path,
                self.backup_dir,
                backup_name="no-space.sqlite3",
                estimated_output_bytes=1,
                safety_margin_bytes=0,
                safety_margin_ratio=0,
                available_bytes_override=0,
            )

        self.assertFalse(caught.exception.preflight["ok"])
        self.assertGreater(caught.exception.preflight["shortfall_bytes"], 0)
        self.assertFalse(self.backup_dir.exists())

    def test_invalid_database_is_not_published(self) -> None:
        self.connection.close()
        self.database_path.write_bytes(b"not sqlite")
        with self.assertRaises(StartStopBackupError):
            self.backup("invalid.sqlite3")
        self.connection = sqlite3.connect(self.database_path)
        self.assertFalse((self.backup_dir / "invalid.sqlite3").exists())

    def test_retention_is_plan_only_and_protects_marked_and_unknown_files(self) -> None:
        now = dt.datetime(2026, 8, 4, 12, tzinfo=UTC)
        names: list[str] = []
        for index, age in enumerate((0, 1, 2, 10, 40)):
            name = f"backup-{index}.sqlite3"
            names.append(name)
            self.backup(
                name,
                created_at=now - dt.timedelta(days=age),
                metadata={"retain": index == 4},
            )
        orphan = self.backup_dir / "manual.sqlite3"
        orphan.write_bytes(b"manual")
        before = {path.name: path.read_bytes() for path in self.backup_dir.iterdir()}

        plan = plan_backup_retention(
            self.backup_dir,
            keep_latest=1,
            keep_daily_days=1,
            keep_weekly_weeks=0,
            now=now,
        )

        self.assertEqual(plan["mode"], "plan_only")
        self.assertEqual(plan["files_deleted"], 0)
        self.assertGreaterEqual(len(plan["delete_candidates"]), 2)
        self.assertTrue(all(item["requires_verification"] for item in plan["delete_candidates"]))
        protected_text = json.dumps(plan["protected"])
        self.assertIn("backup-4.sqlite3", protected_text)
        self.assertIn("manual.sqlite3", protected_text)
        after = {path.name: path.read_bytes() for path in self.backup_dir.iterdir()}
        self.assertEqual(before, after)

    def test_non_json_metadata_fails_before_artifact_creation(self) -> None:
        with self.assertRaises(ValueError):
            self.backup(metadata={"bad": object()})
        self.assertFalse(self.backup_dir.exists())

    def test_public_safety_status_is_path_free_and_marks_same_disk_backup(self) -> None:
        result = self.backup(metadata={"reason": "pre-upgrade"})
        (self.backup_dir / "scheduled-backup-status.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "state": "completed",
                    "started_utc": "2026-08-25T03:00:00+00:00",
                    "completed_utc": "2026-08-25T04:00:00+00:00",
                    "exit_code": 0,
                }
            ),
            encoding="utf-8",
        )

        status = build_safety_status(
            self.database_path,
            self.backup_dir,
            estimated_output_bytes=1024,
            scheduled_background_enabled=True,
            scheduled_background_label="每 4 周周日 03:00 后台执行",
        )

        self.assertTrue(status["storage"]["preflight_ok"])
        self.assertTrue(status["backup"]["available"])
        self.assertTrue(status["backup"]["latest_valid"])
        self.assertEqual(
            status["backup"]["latest_verification_level"],
            "manifest_and_size",
        )
        self.assertTrue(
            status["backup"]["latest_created_with_full_verification"]
        )
        self.assertTrue(status["backup"]["same_filesystem"])
        self.assertEqual(status["backup"]["policy_mode"], "risk_tiered")
        self.assertFalse(status["backup"]["routine_full_backup_required"])
        self.assertTrue(status["backup"]["schema_change_full_backup_required"])
        self.assertTrue(status["backup"]["scheduled_background_enabled"])
        self.assertEqual(status["backup"]["scheduled_state"], "completed")
        self.assertEqual(status["backup"]["latest_size_bytes"], result["size_bytes"])
        serialized = json.dumps(status, ensure_ascii=False)
        self.assertNotIn(str(self.database_path), serialized)
        self.assertNotIn(str(self.backup_dir), serialized)

    def test_scheduled_status_can_use_a_separate_read_only_mount(self) -> None:
        self.backup()
        status_dir = self.root / "scheduled-status"
        status_dir.mkdir()
        status_file = status_dir / "scheduled-backup-status.json"
        status_file.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "state": "never_run",
                    "started_utc": "",
                    "completed_utc": "",
                    "next_due_utc": "2026-08-29T19:00:00+00:00",
                    "exit_code": 0,
                }
            ),
            encoding="utf-8",
        )

        status = build_safety_status(
            self.database_path,
            self.backup_dir,
            scheduled_background_enabled=True,
            scheduled_status_file=status_file,
        )

        self.assertEqual(status["backup"]["scheduled_state"], "never_run")
        self.assertEqual(
            status["backup"]["scheduled_next_due_utc"],
            "2026-08-29T19:00:00+00:00",
        )
        self.assertEqual(status["backup"]["backup_count"], 1)

    def test_task_preflight_uses_database_volume_when_backup_target_differs(self) -> None:
        self.backup(metadata={"reason": "volume-split"})

        task_result = {
            "ok": False,
            "required_bytes": 10_000,
            "shortfall_bytes": 2_000,
            "volume_count": 2,
            "blocked_roles": ["database"],
        }
        backup_result = {
            "ok": True,
            "required_bytes": 5_000,
            "shortfall_bytes": 0,
        }

        with (
            mock.patch(
                "echem_platform.start_stop_backup._task_storage_preflight",
                return_value=task_result,
            ),
            mock.patch(
                "echem_platform.start_stop_backup.storage_preflight",
                return_value=backup_result,
            ),
        ):
            status = build_safety_status(
                self.database_path,
                self.backup_dir,
                estimated_output_bytes=1024,
            )

        self.assertFalse(status["storage"]["preflight_ok"])
        self.assertEqual(status["storage"]["shortfall_bytes"], 2_000)
        self.assertEqual(status["storage"]["blocked_roles"], ["database"])
        self.assertTrue(status["backup"]["destination_preflight_ok"])

    def test_task_preflight_blocks_a_full_scratch_tmpfs(self) -> None:
        self.backup(metadata={"reason": "scratch-volume"})
        scratch = self.root / "scratch"
        cache = self.root / "cache"
        scratch.mkdir()
        cache.mkdir()

        def volume_usage(path):
            if Path(path).resolve() == scratch.resolve():
                return 2, mock.Mock(free=1)
            return 1, mock.Mock(free=20 * 1024**3)

        with mock.patch(
            "echem_platform.start_stop_backup._volume_usage",
            side_effect=volume_usage,
        ):
            status = build_safety_status(
                self.database_path,
                self.backup_dir,
                scratch_dir=scratch,
                cache_dir=cache,
                estimated_snapshot_bytes=10_000,
                estimated_output_bytes=10_000,
            )

        self.assertFalse(status["storage"]["preflight_ok"])
        self.assertEqual(status["storage"]["blocked_roles"], ["scratch"])


if __name__ == "__main__":
    unittest.main()
