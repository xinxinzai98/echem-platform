#!/usr/bin/env python3
"""Low-frequency, no-catch-up full-backup scheduler for Docker Desktop."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from echem_platform.start_stop_backup import create_backup  # noqa: E402


STATUS_WRITER = Path(__file__).with_name("write_start_stop_backup_status.py")
INTERVAL = dt.timedelta(days=28)


def _aware(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("schedule anchor must include a timezone")
    return parsed.astimezone(dt.timezone.utc)


def _iso(value: dt.datetime | None) -> str:
    if value is None:
        return ""
    return value.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def _parse_optional(value: Any) -> dt.datetime | None:
    try:
        return _aware(str(value)) if value else None
    except (TypeError, ValueError):
        return None


def schedule_slot(
    now: dt.datetime,
    anchor: dt.datetime,
    *,
    window: dt.timedelta,
) -> tuple[dt.datetime | None, dt.datetime]:
    """Return an active due slot and the next future due time."""

    if now < anchor:
        return None, anchor
    index = int((now - anchor) // INTERVAL)
    due = anchor + index * INTERVAL
    if now < due + window:
        return due, due + INTERVAL
    return None, due + INTERVAL


def _read_status(path: Path) -> dict[str, Any]:
    try:
        if path.stat().st_size > 16 * 1024:
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _publish_status(
    path: Path,
    *,
    state: str,
    started: str = "",
    completed: str = "",
    due: str = "",
    next_due: str = "",
    exit_code: int = 0,
) -> None:
    arguments = [
        sys.executable,
        str(STATUS_WRITER),
        "--status-file",
        str(path),
        "--state",
        state,
        "--exit-code",
        str(exit_code),
    ]
    for flag, value in (
        ("--started-utc", started),
        ("--completed-utc", completed),
        ("--due-utc", due),
        ("--next-due-utc", next_due),
    ):
        if value:
            arguments.extend((flag, value))
    subprocess.run(
        arguments,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )


def _already_attempted(
    payload: dict[str, Any],
    due: dt.datetime,
    window: dt.timedelta,
) -> bool:
    if payload.get("state") not in {"running", "completed", "failed", "skipped"}:
        return False
    attempted = _parse_optional(payload.get("due_utc"))
    if attempted is not None:
        return attempted == due
    started = _parse_optional(payload.get("started_utc"))
    return started is not None and due <= started < due + window


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--backup-dir", type=Path, required=True)
    parser.add_argument("--status-file", type=Path, required=True)
    parser.add_argument("--anchor-local", required=True)
    parser.add_argument("--window-minutes", type=int, default=15)
    parser.add_argument("--poll-seconds", type=int, default=300)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    anchor = _aware(args.anchor_local)
    window = dt.timedelta(minutes=max(1, min(int(args.window_minutes), 60)))
    poll_seconds = max(30, min(int(args.poll_seconds), 900))
    stopped = threading.Event()

    def request_stop(_signum, _frame) -> None:
        stopped.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    while not stopped.is_set():
        now = dt.datetime.now(dt.timezone.utc)
        due, next_due = schedule_slot(now, anchor, window=window)
        payload = _read_status(args.status_file)
        state = str(payload.get("state") or "never_run")
        if state not in {"never_run", "running", "completed", "failed", "skipped"}:
            state = "never_run"

        if due is not None and not _already_attempted(payload, due, window):
            started = _iso(now)
            _publish_status(
                args.status_file,
                state="running",
                started=started,
                due=_iso(due),
                next_due=_iso(next_due),
            )
            try:
                result = create_backup(
                    args.database,
                    args.backup_dir,
                    metadata={
                        "reason": "scheduled",
                        "scheduled_due_utc": _iso(due),
                    },
                )
            except Exception as exc:  # scheduler must remain alive for next slot
                completed = _iso(dt.datetime.now(dt.timezone.utc))
                _publish_status(
                    args.status_file,
                    state="failed",
                    started=started,
                    completed=completed,
                    due=_iso(due),
                    next_due=_iso(next_due),
                    exit_code=4,
                )
                print(
                    json.dumps(
                        {"event": "backup_failed", "error": type(exc).__name__},
                        separators=(",", ":"),
                    ),
                    file=sys.stderr,
                    flush=True,
                )
            else:
                completed = _iso(dt.datetime.now(dt.timezone.utc))
                _publish_status(
                    args.status_file,
                    state="completed",
                    started=started,
                    completed=completed,
                    due=_iso(due),
                    next_due=_iso(next_due),
                )
                print(
                    json.dumps(
                        {
                            "event": "backup_completed",
                            "size_bytes": int(result.get("size_bytes") or 0),
                        },
                        separators=(",", ":"),
                    ),
                    flush=True,
                )
        else:
            current_next = str(payload.get("next_due_utc") or "")
            desired_next = _iso(next_due)
            if current_next != desired_next:
                _publish_status(
                    args.status_file,
                    state=state,
                    started=str(payload.get("started_utc") or ""),
                    completed=str(payload.get("completed_utc") or ""),
                    due=str(payload.get("due_utc") or ""),
                    next_due=desired_next,
                    exit_code=int(payload.get("exit_code") or 0),
                )

        wait_for = min(
            float(poll_seconds),
            max(1.0, (next_due - dt.datetime.now(dt.timezone.utc)).total_seconds()),
        )
        stopped.wait(wait_for)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
