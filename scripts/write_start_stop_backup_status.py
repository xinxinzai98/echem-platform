#!/usr/bin/env python3
"""Atomically publish the small scheduled-backup status document."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import uuid
from pathlib import Path
from typing import Sequence


SCHEMA_VERSION = 1
STATUS_NAME = "scheduled-backup-status.json"
STATES = ("never_run", "running", "completed", "failed", "skipped")


def _utc_text(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return parsed.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--status-file", type=Path, required=True)
    parser.add_argument("--state", choices=STATES, required=True)
    parser.add_argument("--started-utc", default="")
    parser.add_argument("--completed-utc", default="")
    parser.add_argument("--due-utc", default="")
    parser.add_argument("--next-due-utc", default="")
    parser.add_argument("--exit-code", type=int, default=0)
    parser.add_argument("--if-missing", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    path = args.status_file
    if path.name != STATUS_NAME or path.parent == path:
        raise ValueError("status-file must name scheduled-backup-status.json")
    if path.is_symlink():
        raise ValueError("status-file must not be a symbolic link")
    parent = path.parent.resolve(strict=True)
    if not parent.is_dir():
        raise NotADirectoryError(parent)
    try:
        # This named Docker volume contains only a non-sensitive status JSON.
        # World-writable directory permissions let the unprivileged scheduler
        # atomically replace the root-initialized file without extra caps.
        os.chmod(parent, 0o777)
    except PermissionError:
        pass
    if args.if_missing and path.exists():
        print(json.dumps({"ok": True, "written": False}, separators=(",", ":")))
        return 0
    exit_code = int(args.exit_code)
    if not 0 <= exit_code <= 255:
        raise ValueError("exit-code must be between 0 and 255")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "state": args.state,
        "started_utc": _utc_text(args.started_utc),
        "completed_utc": _utc_text(args.completed_utc),
        "due_utc": _utc_text(args.due_utc),
        "next_due_utc": _utc_text(args.next_due_utc),
        "exit_code": exit_code,
    }
    temporary = parent / f".{STATUS_NAME}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        # The helper runs as root with every Linux capability dropped.  A
        # root-owned 0644 file is readable by the unprivileged web container
        # without requiring CAP_CHOWN; the status contains no secrets.
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
        try:
            directory_fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    print(json.dumps({"ok": True, "written": True, "state": args.state}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
