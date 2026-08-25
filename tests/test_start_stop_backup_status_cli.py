from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "write_start_stop_backup_status.py"
SPEC = importlib.util.spec_from_file_location(
    "write_start_stop_backup_status",
    SCRIPT_PATH,
)
assert SPEC is not None and SPEC.loader is not None
status_cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(status_cli)


class StartStopBackupStatusCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.status_file = self.root / "scheduled-backup-status.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_cli(self, *arguments: str) -> dict:
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            exit_code = status_cli.main(
                [
                    "--status-file",
                    str(self.status_file),
                    *arguments,
                ]
            )
        self.assertEqual(exit_code, 0)
        return json.loads(stream.getvalue())

    def test_status_is_atomic_private_and_timestamp_normalized(self) -> None:
        result = self.run_cli(
            "--state",
            "completed",
            "--started-utc",
            "2026-08-25T03:00:00+08:00",
            "--completed-utc",
            "2026-08-25T04:00:00+08:00",
            "--due-utc",
            "2026-08-30T03:00:00+08:00",
            "--next-due-utc",
            "2026-09-27T03:00:00+08:00",
            "--exit-code",
            "0",
        )

        self.assertTrue(result["written"])
        payload = json.loads(self.status_file.read_text(encoding="utf-8"))
        self.assertEqual(payload["state"], "completed")
        self.assertEqual(payload["started_utc"], "2026-08-24T19:00:00+00:00")
        self.assertEqual(payload["completed_utc"], "2026-08-24T20:00:00+00:00")
        self.assertEqual(payload["due_utc"], "2026-08-29T19:00:00+00:00")
        self.assertEqual(payload["next_due_utc"], "2026-09-26T19:00:00+00:00")
        self.assertEqual(self.status_file.stat().st_mode & 0o777, 0o644)
        self.assertFalse(any(path.name.endswith(".tmp") for path in self.root.iterdir()))

    def test_if_missing_preserves_existing_status(self) -> None:
        self.run_cli("--state", "running")

        result = self.run_cli("--state", "never_run", "--if-missing")

        self.assertFalse(result["written"])
        payload = json.loads(self.status_file.read_text(encoding="utf-8"))
        self.assertEqual(payload["state"], "running")

    def test_rejects_unexpected_status_filename(self) -> None:
        self.status_file = self.root / "other.json"
        with self.assertRaisesRegex(ValueError, "scheduled-backup-status"):
            self.run_cli("--state", "never_run")
        self.assertFalse(self.status_file.exists())


if __name__ == "__main__":
    unittest.main()
