from __future__ import annotations

import contextlib
import datetime as dt
import importlib.util
import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from echem_platform.start_stop_backup import create_backup
from echem_platform.start_stop_database import StartStopDatabase


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "create_start_stop_backup.py"
SPEC = importlib.util.spec_from_file_location("create_start_stop_backup", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
backup_cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup_cli)


class StartStopBackupCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "private-state" / "start-stop.sqlite3"
        self.database.parent.mkdir()
        with sqlite3.connect(self.database) as connection:
            connection.execute("CREATE TABLE samples(id INTEGER PRIMARY KEY, name TEXT)")
            connection.execute("INSERT INTO samples(name) VALUES('NiMo')")
            connection.commit()
        self.backup_dir = self.root / "backups"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_cli(self, *extra: str) -> tuple[int, dict, str]:
        arguments = [
            "--database",
            str(self.database),
            "--backup-dir",
            str(self.backup_dir),
            *extra,
        ]
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            exit_code = backup_cli.main(arguments)
        raw = stream.getvalue()
        return exit_code, json.loads(raw), raw

    def test_create_uses_unique_utc_name_and_safe_json_summary(self) -> None:
        exit_code, result, raw = self.run_cli(
            "--safety-margin-bytes",
            "0",
            "--safety-margin-ratio",
            "0",
        )

        self.assertEqual(exit_code, backup_cli.EXIT_SUCCESS)
        self.assertTrue(result["ok"])
        self.assertEqual(result["command"], "create")
        self.assertRegex(
            result["backup"]["database_file"],
            r"^start-stop-\d{8}T\d{12}Z-[0-9a-f]{8}\.sqlite3$",
        )
        self.assertNotIn(str(self.database), raw)
        self.assertNotIn("local_paths", result)
        backup = self.backup_dir / result["backup"]["database_file"]
        manifest = self.backup_dir / result["backup"]["manifest_file"]
        self.assertTrue(backup.is_file())
        self.assertTrue(manifest.is_file())

        second_code, second, _ = self.run_cli(
            "--safety-margin-bytes",
            "0",
            "--safety-margin-ratio",
            "0",
        )
        self.assertEqual(second_code, backup_cli.EXIT_SUCCESS)
        self.assertNotEqual(
            result["backup"]["database_file"],
            second["backup"]["database_file"],
        )

    def test_script_runs_from_outside_project_with_the_active_python(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "--database",
                str(self.database),
                "--backup-dir",
                str(self.backup_dir),
                "--safety-margin-bytes",
                "0",
                "--safety-margin-ratio",
                "0",
                "--compact",
            ],
            cwd=self.root,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(completed.returncode, backup_cli.EXIT_SUCCESS)
        result = json.loads(completed.stdout)
        self.assertTrue(result["ok"])
        self.assertNotIn(str(self.database), completed.stdout)
        self.assertEqual(completed.stderr, "")

    def test_verify_only_checks_all_backups_and_creates_nothing(self) -> None:
        created = create_backup(
            self.database,
            self.backup_dir,
            backup_name="valid.sqlite3",
            safety_margin_bytes=0,
            safety_margin_ratio=0,
        )
        before = sorted(path.name for path in self.backup_dir.iterdir())

        exit_code, result, raw = self.run_cli("--verify-only")

        self.assertEqual(exit_code, backup_cli.EXIT_SUCCESS)
        self.assertTrue(result["ok"])
        self.assertEqual(result["backup_count"], 1)
        self.assertTrue(result["backups"][0]["hash_verified"])
        self.assertTrue(result["backups"][0]["quick_check_verified"])
        self.assertNotIn(str(self.database), raw)
        self.assertEqual(before, sorted(path.name for path in self.backup_dir.iterdir()))

        backup_path = Path(created["backup_path"])
        payload = bytearray(backup_path.read_bytes())
        payload[-1] ^= 0xFF
        backup_path.write_bytes(payload)
        failed_code, failed, _ = self.run_cli("--verify-only")
        self.assertEqual(failed_code, backup_cli.EXIT_VERIFICATION_FAILED)
        self.assertFalse(failed["ok"])
        self.assertEqual(failed["invalid_count"], 1)

    def test_verify_empty_directory_has_stable_failure_code(self) -> None:
        self.backup_dir.mkdir()
        exit_code, result, _ = self.run_cli("--verify-only")

        self.assertEqual(exit_code, backup_cli.EXIT_VERIFICATION_FAILED)
        self.assertEqual(result["exit_code"], backup_cli.EXIT_VERIFICATION_FAILED)
        self.assertFalse(result["available"])

    def test_check_live_needs_no_backup_directory_and_creates_nothing(self) -> None:
        repository = self.root / "repository" / "start-stop.sqlite3"
        repository.parent.mkdir()
        StartStopDatabase(repository)
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            exit_code = backup_cli.main(
                [
                    "--database",
                    str(repository),
                    "--check-live",
                    "--compact",
                ]
            )

        result = json.loads(stream.getvalue())
        self.assertEqual(exit_code, backup_cli.EXIT_SUCCESS)
        self.assertEqual(result["command"], "check_live")
        self.assertEqual(result["check"]["mode"], "operational_metadata")
        self.assertEqual(result["check"]["schema_version"], 5)
        self.assertFalse(self.backup_dir.exists())

    def test_full_backup_reason_is_recorded_in_manifest(self) -> None:
        exit_code, result, _ = self.run_cli(
            "--reason",
            "schema_migration",
            "--safety-margin-bytes",
            "0",
            "--safety-margin-ratio",
            "0",
        )

        self.assertEqual(exit_code, backup_cli.EXIT_SUCCESS)
        manifest = json.loads(
            (self.backup_dir / result["backup"]["manifest_file"]).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(manifest["metadata"]["reason"], "schema_migration")

    def test_preflight_failure_uses_stable_code_and_does_not_publish(self) -> None:
        exit_code, result, raw = self.run_cli(
            "--safety-margin-bytes",
            str(10**18),
            "--safety-margin-ratio",
            "0",
        )

        self.assertEqual(exit_code, backup_cli.EXIT_INSUFFICIENT_STORAGE)
        self.assertEqual(result["error"]["code"], "insufficient_storage")
        self.assertFalse(result["error"]["details"]["preflight"]["ok"])
        self.assertNotIn(str(self.database), raw)
        self.assertFalse(self.backup_dir.exists())

    def test_retention_plan_is_output_only_and_sanitized(self) -> None:
        now = dt.datetime.now(dt.timezone.utc)
        for index, age_days in enumerate((10, 20)):
            create_backup(
                self.database,
                self.backup_dir,
                backup_name=f"old-{index}.sqlite3",
                created_at=now - dt.timedelta(days=age_days),
                safety_margin_bytes=0,
                safety_margin_ratio=0,
            )
        before = {
            path.name: path.read_bytes() for path in self.backup_dir.iterdir()
        }

        exit_code, result, raw = self.run_cli(
            "--retention-plan",
            "--keep-latest",
            "0",
            "--keep-daily-days",
            "0",
            "--keep-weekly-weeks",
            "0",
        )

        self.assertEqual(exit_code, backup_cli.EXIT_SUCCESS)
        plan = result["retention_plan"]
        self.assertEqual(plan["mode"], "plan_only")
        self.assertEqual(plan["files_deleted"], 0)
        self.assertEqual(len(plan["delete_candidates"]), 2)
        self.assertNotIn(str(self.database), raw)
        self.assertNotIn(str(self.backup_dir), raw)
        after = {path.name: path.read_bytes() for path in self.backup_dir.iterdir()}
        self.assertEqual(before, after)

    def test_invalid_database_error_is_json_and_redacts_absolute_path(self) -> None:
        missing = self.root / "secret" / "missing.sqlite3"
        self.database = missing

        exit_code, result, raw = self.run_cli()

        self.assertEqual(exit_code, backup_cli.EXIT_USAGE)
        self.assertEqual(result["error"]["code"], "invalid_input")
        self.assertNotIn(str(missing), raw)
        self.assertIn("<database>", result["error"]["message"])

    def test_local_details_explicitly_include_absolute_paths(self) -> None:
        exit_code, result, raw = self.run_cli(
            "--show-local-paths",
            "--safety-margin-bytes",
            "0",
            "--safety-margin-ratio",
            "0",
        )

        self.assertEqual(exit_code, backup_cli.EXIT_SUCCESS)
        self.assertEqual(result["local_paths"]["database"], str(self.database.resolve()))
        self.assertIn(str(self.database.resolve()), raw)

    def test_required_explicit_paths_and_mode_validation_use_argparse_code(self) -> None:
        with self.assertRaises(SystemExit) as missing:
            backup_cli.parse_args([])
        self.assertEqual(missing.exception.code, backup_cli.EXIT_USAGE)

        with self.assertRaises(SystemExit) as incompatible:
            backup_cli.parse_args(
                [
                    "--database",
                    str(self.database),
                    "--backup-dir",
                    str(self.backup_dir),
                    "--verify-only",
                    "--backup-name",
                    "not-used.sqlite3",
                ]
            )
        self.assertEqual(incompatible.exception.code, backup_cli.EXIT_USAGE)


if __name__ == "__main__":
    unittest.main()
