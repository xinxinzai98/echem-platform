from __future__ import annotations

import datetime as dt
import threading
from typing import Any, Callable

from .start_stop import StartStopWorkspaceError


AUTO_UPDATE_BUSY_RETRY_MINUTES = 5
AUTO_UPDATE_POLL_SECONDS = 5.0
_TERMINAL_JOB_STATUSES = {"completed", "completed_with_warnings", "failed"}
_active_scheduler_keys: set[str] = set()
_active_scheduler_lock = threading.Lock()


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _utc_text(value: dt.datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("automatic update clock must return a timezone-aware datetime")
    return value.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def _parse_utc(value: Any) -> dt.datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(dt.timezone.utc)


class AutoUpdateScheduler:
    """Single background scheduler for the existing incremental scan job.

    The scheduler never invokes rendering and never owns a second collection
    implementation.  All actual work goes through ``workspace.start_job('scan')``
    so manual and automatic updates share the workspace's existing job lock.
    """

    def __init__(
        self,
        workspace: Any,
        database: Any,
        *,
        now: Callable[[], dt.datetime] = _utc_now,
        poll_seconds: float = AUTO_UPDATE_POLL_SECONDS,
    ) -> None:
        required = (
            "_get_auto_update_state",
            "get_auto_update_config",
            "ensure_auto_update_schedule",
            "record_auto_update_started",
            "record_auto_update_busy",
            "record_auto_update_launch_failure",
            "record_auto_update_finished",
            "record_auto_update_interrupted",
        )
        if any(not callable(getattr(database, name, None)) for name in required):
            raise TypeError("database does not support automatic update scheduling")
        if not callable(getattr(workspace, "start_job", None)):
            raise TypeError("workspace does not support background scan jobs")
        self.workspace = workspace
        self.database = database
        self._now = now
        self.poll_seconds = max(0.05, float(poll_seconds))
        self._coordination_lock = threading.RLock()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None
        database_path = getattr(database, "path", None)
        self._singleton_key = (
            str(database_path.resolve())
            if database_path is not None and callable(getattr(database_path, "resolve", None))
            else f"database-object:{id(database)}"
        )
        self._owns_singleton_key = False

    def snapshot(self) -> dict[str, Any]:
        """Return the redacted status safe for the local settings endpoint."""
        return self.database.get_auto_update_config()

    def wake(self) -> None:
        """Re-evaluate a changed schedule without executing it synchronously."""
        self._wake_event.set()

    def save_config(
        self,
        *,
        expected_revision: int,
        enabled: bool,
        interval_minutes: int,
    ) -> dict[str, Any]:
        """Serialize a UI settings change with the due-job launch decision."""
        with self._coordination_lock:
            saved = self.database.save_auto_update_config(
                expected_revision=expected_revision,
                enabled=enabled,
                interval_minutes=interval_minutes,
                now_utc=_utc_text(self._now()),
            )
            self.wake()
            return saved

    def start_manual_job(
        self,
        action: str,
        *,
        upload_ids: list[Any] | None = None,
        render_data_mode: str = "both",
        render_material_scope: str = "all",
        export_pdf: bool = False,
    ) -> dict[str, Any]:
        """Reconcile an automatic job before the HTTP API replaces its slot."""
        with self._coordination_lock:
            now_text = _utc_text(self._now())
            state = self.database.ensure_auto_update_schedule(now_utc=now_text)
            self._reconcile_active(state, now_text=now_text)
            if upload_ids is None:
                return self.workspace.start_job(
                    action,
                    render_data_mode=render_data_mode,
                    render_material_scope=render_material_scope,
                    export_pdf=export_pdf,
                )
            return self.workspace.start_job(action, upload_ids=upload_ids)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        with _active_scheduler_lock:
            if self._singleton_key in _active_scheduler_keys:
                raise RuntimeError("an automatic update scheduler is already active")
            _active_scheduler_keys.add(self._singleton_key)
            self._owns_singleton_key = True
        self._stop_event.clear()
        self._wake_event.clear()
        thread = threading.Thread(
            target=self._run_loop,
            name="start-stop-auto-update",
            daemon=True,
        )
        self._thread = thread
        try:
            thread.start()
        except BaseException:
            self._release_singleton_key()
            self._thread = None
            raise

    def close(self, timeout: float = 2.0) -> None:
        self._stop_event.set()
        self._wake_event.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(max(0.0, float(timeout)))
        if thread is None or not thread.is_alive():
            self._thread = None
            self._release_singleton_key()

    def _release_singleton_key(self) -> None:
        if not self._owns_singleton_key:
            return
        with _active_scheduler_lock:
            _active_scheduler_keys.discard(self._singleton_key)
        self._owns_singleton_key = False

    def _run_loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                try:
                    self.run_once()
                except Exception:
                    # Do not log details: exceptions can contain remote paths or
                    # credentials. Expected launch failures are normalized by
                    # run_once; an unexpected scheduler fault is retried later
                    # without rewriting a possibly active job's state.
                    pass
                self._wake_event.wait(self.poll_seconds)
                self._wake_event.clear()
        finally:
            self._release_singleton_key()

    def _workspace_job(self) -> dict[str, Any]:
        try:
            status = self.workspace.status()
        except Exception:
            return {}
        if not isinstance(status, dict):
            return {}
        job = status.get("job")
        return job if isinstance(job, dict) else {}

    def _reconcile_active(
        self,
        state: dict[str, Any],
        *,
        now_text: str,
    ) -> dict[str, Any]:
        active_job_id = str(state.get("active_job_id") or "")
        if not active_job_id:
            return state
        job = self._workspace_job()
        if str(job.get("id") or "") != active_job_id:
            return self.database.record_auto_update_interrupted(
                now_utc=now_text,
                retry_minutes=AUTO_UPDATE_BUSY_RETRY_MINUTES,
            )
        status = str(job.get("status") or "")
        if status in _TERMINAL_JOB_STATUSES:
            return self.database.record_auto_update_finished(
                job_id=active_job_id,
                status=status,
                now_utc=now_text,
            )
        if status not in {"queued", "running"}:
            return self.database.record_auto_update_interrupted(
                now_utc=now_text,
                retry_minutes=AUTO_UPDATE_BUSY_RETRY_MINUTES,
            )
        return state

    def run_once(self) -> dict[str, Any]:
        """Reconcile one active job or launch at most one overdue scan."""
        with self._coordination_lock:
            return self._run_once_locked()

    def _run_once_locked(self) -> dict[str, Any]:
        now = self._now()
        now_text = _utc_text(now)
        state = self.database.ensure_auto_update_schedule(now_utc=now_text)
        state = self._reconcile_active(state, now_text=now_text)
        if state.get("active_job_id"):
            return self.snapshot()
        if not bool(state.get("enabled")):
            return self.snapshot()
        due = _parse_utc(state.get("next_run_utc"))
        if due is None or due > now.astimezone(dt.timezone.utc):
            return self.snapshot()

        try:
            job = self.workspace.start_job("scan", requested_via="automatic")
        except StartStopWorkspaceError as exc:
            if int(getattr(exc, "status", 500)) == 409:
                self.database.record_auto_update_busy(
                    now_utc=now_text,
                    retry_minutes=AUTO_UPDATE_BUSY_RETRY_MINUTES,
                )
            else:
                self.database.record_auto_update_launch_failure(now_utc=now_text)
            return self.snapshot()
        except Exception:
            self.database.record_auto_update_launch_failure(now_utc=now_text)
            return self.snapshot()

        if not isinstance(job, dict) or not str(job.get("id") or ""):
            self.database.record_auto_update_launch_failure(now_utc=now_text)
            return self.snapshot()
        job_id = str(job["id"])
        self.database.record_auto_update_started(job_id=job_id, now_utc=now_text)
        returned_status = str(job.get("status") or "")
        if returned_status in _TERMINAL_JOB_STATUSES:
            self.database.record_auto_update_finished(
                job_id=job_id,
                status=returned_status,
                now_utc=now_text,
            )
        return self.snapshot()

    def __enter__(self) -> "AutoUpdateScheduler":
        self.start()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()
