"""Expiring, read-only projection of management storage and backup status."""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any, Callable

RUNTIME_STATUS_NAME = "runtime-safety-status.json"
MAX_BYTES = 64 * 1024
MAX_AGE_SECONDS = 120


def read_runtime_safety(path: Path, *, now: dt.datetime | None = None) -> dict[str, Any] | None:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("updated_utc"), str):
            return None
        timestamp = dt.datetime.fromisoformat(payload["updated_utc"].replace("Z", "+00:00"))
        if timestamp.tzinfo is None or payload.get("schema_version") != 1:
            return None
        age = ((now or dt.datetime.now(dt.timezone.utc)) - timestamp).total_seconds()
        if not -5 <= age <= MAX_AGE_SECONDS:
            return None
        safety = payload["safety"]
        if not isinstance(safety, dict):
            return None
        return {key: safety.get(key, {}) for key in ("storage", "backup", "provenance")
                if isinstance(safety.get(key, {}), dict)}
    except (OSError, ValueError, TypeError, KeyError):
        return None


class RuntimeSafetyPublisher:
    def __init__(self, path: Path, provider: Callable[[], dict[str, Any]], interval: float = 30):
        self.path = path
        self.provider = provider
        self.interval = max(1.0, interval)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def publish(self) -> None:
        safety = self.provider()
        if not isinstance(safety, dict):
            raise ValueError("Runtime safety provider must return an object")
        payload = {
            "schema_version": 1,
            "updated_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "safety": {key: safety.get(key, {}) for key in ("storage", "backup", "provenance")},
        }
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(data) > MAX_BYTES:
            raise ValueError("Runtime safety projection exceeds size limit")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(data)
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.publish()
            except (OSError, ValueError, TypeError, RuntimeError, sqlite3.Error):
                # Readers expire the last successful projection on provider failure.
                pass
            if self._stop.wait(self.interval):
                break

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="runtime-safety-publisher", daemon=True)
            self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
