from __future__ import annotations

import datetime as dt
import importlib.util
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "run_start_stop_backup_scheduler.py"
SPEC = importlib.util.spec_from_file_location(
    "run_start_stop_backup_scheduler",
    SCRIPT_PATH,
)
assert SPEC is not None and SPEC.loader is not None
scheduler = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scheduler)


UTC = dt.timezone.utc


class StartStopBackupSchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.anchor = dt.datetime(2026, 8, 29, 19, 0, tzinfo=UTC)
        self.window = dt.timedelta(minutes=15)

    def test_future_anchor_waits_for_first_due(self) -> None:
        due, next_due = scheduler.schedule_slot(
            self.anchor - dt.timedelta(days=1),
            self.anchor,
            window=self.window,
        )

        self.assertIsNone(due)
        self.assertEqual(next_due, self.anchor)

    def test_active_window_runs_once_and_advances_four_weeks(self) -> None:
        due, next_due = scheduler.schedule_slot(
            self.anchor + dt.timedelta(minutes=5),
            self.anchor,
            window=self.window,
        )

        self.assertEqual(due, self.anchor)
        self.assertEqual(next_due, self.anchor + dt.timedelta(days=28))
        self.assertTrue(
            scheduler._already_attempted(
                {
                    "state": "completed",
                    "due_utc": self.anchor.isoformat(),
                },
                self.anchor,
                self.window,
            )
        )

    def test_missed_window_never_catches_up(self) -> None:
        due, next_due = scheduler.schedule_slot(
            self.anchor + dt.timedelta(minutes=16),
            self.anchor,
            window=self.window,
        )

        self.assertIsNone(due)
        self.assertEqual(next_due, self.anchor + dt.timedelta(days=28))

    def test_running_status_becomes_stale_after_twelve_hours(self) -> None:
        self.assertEqual(scheduler.STALE_RUNNING_RECOVERY_STATE, "skipped")
        payload = {
            "state": "running",
            "started_utc": self.anchor.isoformat(),
        }

        self.assertFalse(
            scheduler._running_status_is_stale(
                payload, self.anchor + dt.timedelta(hours=11)
            )
        )
        self.assertTrue(
            scheduler._running_status_is_stale(
                payload, self.anchor + dt.timedelta(hours=12)
            )
        )
        self.assertTrue(
            scheduler._running_status_is_stale(
                {"state": "running", "started_utc": ""}, self.anchor
            )
        )


if __name__ == "__main__":
    unittest.main()
