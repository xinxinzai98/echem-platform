from __future__ import annotations

import argparse
import datetime as dt
import http.client
import io
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import start_stop_service as SERVICE
from echem_platform.start_stop import StartStopWorkspaceError
from echem_platform.start_stop_auto_update import AutoUpdateScheduler
from echem_platform.start_stop_database import (
    AUTO_UPDATE_INTERVAL_MINUTES,
    AutoUpdateConfigConflict,
    SCHEMA_VERSION,
    StartStopDatabase,
)


UTC = dt.timezone.utc
TEST_LAN_AUTH = SERVICE.BasicAuthCredentials("reader", "test-password")
TEST_LAN_AUTH_HEADER = "Basic cmVhZGVyOnRlc3QtcGFzc3dvcmQ="


class MutableClock:
    def __init__(self, value: dt.datetime) -> None:
        self.value = value

    def __call__(self) -> dt.datetime:
        return self.value


class FakeWorkspace:
    def __init__(self, database: StartStopDatabase) -> None:
        self.database = database
        self.collection_config = None
        self.calls: list[str] = []
        self.request_sources: list[str | None] = []
        self.job: dict = {"id": "", "status": "idle", "result": {}}
        self.busy = False

    def status(self) -> dict:
        return {
            "available": True,
            "execution": {"ready": True, "update_ready": True},
            "job": dict(self.job),
        }

    def start_job(
        self,
        action: str,
        *,
        requested_via: str | None = None,
        upload_ids=None,
        render_data_mode: str = "both",
        render_material_scope: str = "all",
        export_pdf: bool = False,
    ) -> dict:
        del upload_ids, render_data_mode, render_material_scope, export_pdf
        self.calls.append(action)
        self.request_sources.append(requested_via)
        if self.busy:
            raise StartStopWorkspaceError("已有任务", 409)
        self.job = {
            "id": f"job-{len(self.calls)}",
            "action": action,
            "status": "queued",
            "result": {},
        }
        return dict(self.job)


class ExplodingBody:
    def read(self, *_args, **_kwargs):
        raise AssertionError("LAN 自动更新请求不应读取请求体")


class AutoUpdateHttpHarness:
    def __init__(
        self,
        workspace: FakeWorkspace,
        *,
        lan_read_only: bool = False,
        live_preview_snapshot_provider=None,
    ):
        self.lan_read_only = lan_read_only
        self.port = 18788 if lan_read_only else 18787
        self.host = "192.168.110.158" if lan_read_only else "127.0.0.1"
        self.handler_type = SERVICE.create_handler(
            workspace,
            lan_read_only=lan_read_only,
            lan_auth_credentials=TEST_LAN_AUTH if lan_read_only else None,
            public_host=self.host,
            public_port=self.port,
            live_preview_snapshot_provider=live_preview_snapshot_provider,
        )

    def request(
        self,
        method: str,
        *,
        path: str = "/api/start-stop/auto-update",
        body: dict | None = None,
        explode_on_read: bool = False,
    ) -> tuple[int, object]:
        encoded = json.dumps(body).encode("utf-8") if body is not None else b""
        headers = http.client.HTTPMessage()
        headers.add_header("Host", f"{self.host}:{self.port}")
        if self.lan_read_only:
            headers.add_header("Authorization", TEST_LAN_AUTH_HEADER)
        if body is not None or explode_on_read:
            headers.add_header("Content-Type", "application/json")
            headers.add_header("Content-Length", str(max(1, len(encoded))))
            headers.add_header("Origin", f"http://{self.host}:{self.port}")
        handler = self.handler_type.__new__(self.handler_type)
        handler.path = path
        handler.headers = headers
        handler.rfile = ExplodingBody() if explode_on_read else io.BytesIO(encoded)
        handler.client_address = (
            "192.168.110.50" if self.lan_read_only else "127.0.0.1",
            55000,
        )
        handler.server = type(
            "TestServer", (), {"server_address": (self.host, self.port)}
        )()
        responses: list[tuple[int, object]] = []
        handler.send_json = lambda payload, status=200: responses.append(
            (int(status), payload)
        )
        getattr(handler, f"do_{method}")()
        if len(responses) != 1:
            raise AssertionError(f"expected one response, got {responses!r}")
        return responses[0]


class StartStopAutoUpdateDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "start-stop.sqlite3"
        self.database = StartStopDatabase(self.path)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _drop_v4(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            DROP TRIGGER analysis_runs_immutable_update;
            DROP TRIGGER analysis_runs_immutable_delete;
            DROP TRIGGER job_events_immutable_update;
            DROP TRIGGER job_events_immutable_delete;
            DROP TABLE analysis_runs;
            DROP TABLE job_events;
            DROP TABLE jobs;
            """
        )

    @staticmethod
    def _drop_v5(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            DROP TRIGGER live_preview_revisions_immutable_update;
            DROP TRIGGER live_preview_revisions_immutable_delete;
            DROP TABLE live_preview_runtime;
            DROP TABLE live_preview_current;
            DROP TABLE live_preview_revisions;
            """
        )

    @staticmethod
    def _drop_v3(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            DROP TRIGGER auto_update_revisions_immutable_update;
            DROP TRIGGER auto_update_revisions_immutable_delete;
            DROP TABLE auto_update_runtime;
            DROP TABLE auto_update_current;
            DROP TABLE auto_update_revisions;
            """
        )

    @staticmethod
    def _drop_v2(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            DROP TRIGGER collection_config_revisions_immutable_update;
            DROP TRIGGER collection_config_revisions_immutable_delete;
            DROP TABLE collection_config_current;
            DROP TABLE collection_config_revisions;
            """
        )

    def test_new_database_is_current_and_automatic_updates_are_disabled(self) -> None:
        with self.database.session() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            revision_count = connection.execute(
                "SELECT COUNT(*) FROM auto_update_revisions"
            ).fetchone()[0]

        payload = self.database.get_auto_update_config()

        self.assertEqual(version, SCHEMA_VERSION, 5)
        self.assertEqual(revision_count, 0)
        self.assertEqual(payload["revision"], 0)
        self.assertFalse(payload["enabled"])
        self.assertEqual(payload["interval_minutes"], 60)
        self.assertEqual(
            payload["allowed_interval_minutes"],
            list(AUTO_UPDATE_INTERVAL_MINUTES),
        )
        self.assertEqual(payload["next_run_utc"], "")

    def test_v1_v2_and_v3_migrate_sequentially_to_current_without_losing_old_rows(self) -> None:
        for old_version in (1, 2, 3):
            with self.subTest(old_version=old_version), tempfile.TemporaryDirectory() as root:
                path = Path(root) / "state.sqlite3"
                database = StartStopDatabase(path)
                database.audit("before-migration", "safe", {"count": old_version})
                if old_version == 2:
                    database.save_collection_config(
                        expected_revision=0,
                        machines=[{"id": "machine", "paths": []}],
                    )
                connection = sqlite3.connect(path)
                connection.execute("PRAGMA foreign_keys=OFF")
                self._drop_v5(connection)
                self._drop_v4(connection)
                if old_version < 3:
                    self._drop_v3(connection)
                if old_version == 1:
                    self._drop_v2(connection)
                connection.execute(f"PRAGMA user_version={old_version}")
                connection.commit()
                connection.close()

                migrated = StartStopDatabase(path)

                with migrated.session() as check:
                    self.assertEqual(
                        check.execute("PRAGMA user_version").fetchone()[0], 5
                    )
                    self.assertEqual(
                        check.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0],
                        1 + (1 if old_version == 2 else 0),
                    )
                if old_version == 2:
                    self.assertEqual(migrated.get_collection_config()["revision"], 1)
                self.assertFalse(migrated.get_auto_update_config()["enabled"])

    def test_save_uses_expected_revision_and_only_whitelisted_intervals(self) -> None:
        now = "2026-08-03T10:00:00+00:00"
        saved = self.database.save_auto_update_config(
            expected_revision=0,
            enabled=True,
            interval_minutes=180,
            now_utc=now,
        )

        self.assertEqual(saved["revision"], 1)
        self.assertTrue(saved["enabled"])
        self.assertEqual(saved["next_run_utc"], "2026-08-03T13:00:00+00:00")
        with self.assertRaises(AutoUpdateConfigConflict):
            self.database.save_auto_update_config(
                expected_revision=0,
                enabled=False,
                interval_minutes=60,
                now_utc=now,
            )
        with self.assertRaises(ValueError):
            self.database.save_auto_update_config(
                expected_revision=1,
                enabled=True,
                interval_minutes=45,
                now_utc=now,
            )

    def test_disabling_does_not_clear_the_current_automatic_job(self) -> None:
        self.database.save_auto_update_config(
            expected_revision=0,
            enabled=True,
            interval_minutes=60,
            now_utc="2026-08-03T10:00:00+00:00",
        )
        self.database.record_auto_update_started(
            job_id="job-running", now_utc="2026-08-03T11:00:00+00:00"
        )

        disabled = self.database.save_auto_update_config(
            expected_revision=1,
            enabled=False,
            interval_minutes=60,
            now_utc="2026-08-03T11:01:00+00:00",
        )
        internal = self.database._get_auto_update_state()

        self.assertFalse(disabled["enabled"])
        self.assertEqual(disabled["next_run_utc"], "")
        self.assertEqual(disabled["last_status"], "running")
        self.assertEqual(internal["active_job_id"], "job-running")


class StartStopAutoUpdateSchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = StartStopDatabase(
            Path(self.temporary.name) / "start-stop.sqlite3"
        )
        self.workspace = FakeWorkspace(self.database)
        self.clock = MutableClock(dt.datetime(2026, 8, 3, 10, 0, tzinfo=UTC))
        self.scheduler = AutoUpdateScheduler(
            self.workspace, self.database, now=self.clock, poll_seconds=3600
        )

    def tearDown(self) -> None:
        self.scheduler.close(timeout=0)
        self.temporary.cleanup()

    def _enable(self, interval: int = 60) -> None:
        self.database.save_auto_update_config(
            expected_revision=0,
            enabled=True,
            interval_minutes=interval,
            now_utc=self.clock().isoformat(),
        )

    def test_enabling_waits_one_interval_then_launches_only_scan_once(self) -> None:
        self._enable(60)

        self.scheduler.run_once()
        self.assertEqual(self.workspace.calls, [])

        self.clock.value = dt.datetime(2026, 8, 3, 11, 0, tzinfo=UTC)
        launched = self.scheduler.run_once()
        self.scheduler.run_once()

        self.assertEqual(self.workspace.calls, ["scan"])
        self.assertEqual(self.workspace.request_sources, ["automatic"])
        self.assertEqual(launched["last_status"], "running")
        self.assertEqual(launched["next_run_utc"], "2026-08-03T12:00:00+00:00")
        self.assertNotIn("active_job_id", launched)

        self.workspace.job["status"] = "completed"
        finished = self.scheduler.run_once()
        self.assertEqual(finished["last_status"], "completed")
        self.assertEqual(self.workspace.calls, ["scan"])

    def test_overdue_restart_catches_up_once_and_skips_missed_intervals(self) -> None:
        self._enable(30)
        self.clock.value = dt.datetime(2026, 8, 3, 15, 0, tzinfo=UTC)

        first = self.scheduler.run_once()
        self.scheduler.run_once()

        self.assertEqual(self.workspace.calls, ["scan"])
        self.assertEqual(first["next_run_utc"], "2026-08-03T15:30:00+00:00")

    def test_busy_workspace_retries_in_five_minutes(self) -> None:
        self._enable(60)
        self.clock.value = dt.datetime(2026, 8, 3, 11, 0, tzinfo=UTC)
        self.workspace.busy = True

        payload = self.scheduler.run_once()

        self.assertEqual(self.workspace.calls, ["scan"])
        self.assertEqual(payload["last_status"], "busy")
        self.assertEqual(payload["next_run_utc"], "2026-08-03T11:05:00+00:00")

    def test_restart_marks_an_orphaned_job_and_retries_only_after_five_minutes(self) -> None:
        self._enable(60)
        self.clock.value = dt.datetime(2026, 8, 3, 11, 0, tzinfo=UTC)
        self.scheduler.run_once()
        restarted_workspace = FakeWorkspace(self.database)
        restarted = AutoUpdateScheduler(
            restarted_workspace,
            self.database,
            now=self.clock,
            poll_seconds=3600,
        )
        try:
            interrupted = restarted.run_once()
            self.assertEqual(
                interrupted["last_status"], "interrupted_by_restart"
            )
            self.assertEqual(
                interrupted["next_run_utc"], "2026-08-03T11:05:00+00:00"
            )
            self.assertEqual(restarted_workspace.calls, [])

            self.clock.value = dt.datetime(2026, 8, 3, 11, 5, tzinfo=UTC)
            restarted.run_once()
            restarted.run_once()
            self.assertEqual(restarted_workspace.calls, ["scan"])
        finally:
            restarted.close(timeout=0)

    def test_only_one_live_scheduler_can_own_the_same_database(self) -> None:
        second = AutoUpdateScheduler(
            self.workspace,
            self.database,
            now=self.clock,
            poll_seconds=3600,
        )
        self.scheduler.start()
        try:
            with self.assertRaises(RuntimeError):
                second.start()
            self.assertEqual(self.workspace.calls, [])
        finally:
            self.scheduler.close()
        second.start()
        second.close()

    def test_config_save_and_due_launch_are_serialized(self) -> None:
        self._enable(60)
        self.clock.value = dt.datetime(2026, 8, 3, 11, 0, tzinfo=UTC)
        entered = threading.Event()
        release = threading.Event()
        save_finished = threading.Event()

        def blocking_start(
            action: str,
            *,
            requested_via: str | None = None,
        ) -> dict:
            self.assertEqual(requested_via, "automatic")
            self.workspace.calls.append(action)
            entered.set()
            if not release.wait(2):
                raise AssertionError("test did not release the queued scan")
            self.workspace.job = {
                "id": "job-race",
                "action": action,
                "status": "queued",
                "result": {},
            }
            return dict(self.workspace.job)

        self.workspace.start_job = blocking_start
        launch_thread = threading.Thread(target=self.scheduler.run_once)
        launch_thread.start()
        self.assertTrue(entered.wait(1))

        def disable() -> None:
            self.scheduler.save_config(
                expected_revision=1,
                enabled=False,
                interval_minutes=60,
            )
            save_finished.set()

        save_thread = threading.Thread(target=disable)
        save_thread.start()
        self.assertFalse(save_finished.wait(0.05))
        release.set()
        launch_thread.join(2)
        save_thread.join(2)

        self.assertFalse(launch_thread.is_alive())
        self.assertFalse(save_thread.is_alive())
        self.assertEqual(self.workspace.calls, ["scan"])
        state = self.database._get_auto_update_state()
        self.assertFalse(state["enabled"])
        self.assertEqual(state["active_job_id"], "job-race")
        self.assertEqual(state["next_run_utc"], "")

    def test_manual_job_reconciles_completed_automatic_job_before_replacing_slot(self) -> None:
        self._enable(60)
        self.clock.value = dt.datetime(2026, 8, 3, 11, 0, tzinfo=UTC)
        self.scheduler.run_once()
        self.workspace.job["status"] = "completed"

        manual = self.scheduler.start_manual_job("render")

        self.assertEqual(manual["action"], "render")
        self.assertEqual(self.workspace.calls, ["scan", "render"])
        state = self.database.get_auto_update_config()
        self.assertEqual(state["last_status"], "completed")
        self.assertEqual(state["next_run_utc"], "2026-08-03T12:00:00+00:00")


class StartStopAutoUpdateApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        database = StartStopDatabase(
            Path(self.temporary.name) / "start-stop.sqlite3"
        )
        self.workspace = FakeWorkspace(database)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_local_get_and_put_use_versioned_safe_payloads(self) -> None:
        server = AutoUpdateHttpHarness(self.workspace)

        get_status, initial = server.request("GET")
        put_status, saved = server.request(
            "PUT",
            body={
                "expected_revision": 0,
                "enabled": True,
                "interval_minutes": 360,
            },
        )
        conflict_status, _ = server.request(
            "PUT",
            body={
                "expected_revision": 0,
                "enabled": False,
                "interval_minutes": 60,
            },
        )

        self.assertEqual(get_status, 200)
        self.assertFalse(initial["enabled"])
        self.assertTrue(initial["can_edit"])
        self.assertEqual(put_status, 200)
        self.assertEqual(saved["revision"], 1)
        self.assertEqual(saved["interval_minutes"], 360)
        self.assertEqual(conflict_status, 409)
        serialized = json.dumps(saved, ensure_ascii=False)
        for forbidden in ("active_job_id", "identity_file", "remote_path", "stderr"):
            self.assertNotIn(forbidden, serialized)

    def test_lan_get_and_put_are_forbidden_before_any_body_read(self) -> None:
        server = AutoUpdateHttpHarness(self.workspace, lan_read_only=True)

        get_status, _ = server.request("GET", explode_on_read=True)
        put_status, _ = server.request("PUT", explode_on_read=True)

        self.assertEqual(get_status, 403)
        self.assertEqual(put_status, 403)

    def test_unknown_fields_and_non_whitelisted_intervals_are_rejected(self) -> None:
        server = AutoUpdateHttpHarness(self.workspace)
        extra_status, _ = server.request(
            "PUT",
            body={
                "expected_revision": 0,
                "enabled": True,
                "interval_minutes": 60,
                "command": "render",
            },
        )
        interval_status, _ = server.request(
            "PUT",
            body={
                "expected_revision": 0,
                "enabled": True,
                "interval_minutes": 45,
            },
        )

        self.assertEqual(extra_status, 400)
        self.assertEqual(interval_status, 400)
        self.assertEqual(self.workspace.calls, [])


class StartStopLivePreviewApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        database = StartStopDatabase(
            Path(self.temporary.name) / "start-stop.sqlite3"
        )
        self.workspace = FakeWorkspace(database)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_local_get_and_put_toggle_fixed_five_minute_preview(self) -> None:
        server = AutoUpdateHttpHarness(self.workspace)
        path = "/api/start-stop/live-preview"

        get_status, initial = server.request("GET", path=path)
        put_status, saved = server.request(
            "PUT",
            path=path,
            body={"expected_revision": 0, "enabled": True},
        )

        self.assertEqual(get_status, 200)
        self.assertFalse(initial["enabled"])
        self.assertTrue(initial["can_edit"])
        self.assertEqual(initial["interval_minutes"], 5)
        self.assertEqual(put_status, 200)
        self.assertTrue(saved["enabled"])
        self.assertEqual(saved["revision"], 1)
        self.assertEqual(saved["interval_minutes"], 5)
        self.assertTrue(saved["next_run_utc"])

    def test_lan_can_read_preview_but_cannot_change_it(self) -> None:
        server = AutoUpdateHttpHarness(
            self.workspace,
            lan_read_only=True,
            live_preview_snapshot_provider=lambda: {
                "available": True,
                "revision": 3,
                "enabled": True,
                "interval_minutes": 5,
                "preview": {"schema_version": 1, "items": [{"source_id": "one"}]},
            },
        )
        path = "/api/start-stop/live-preview"

        get_status, payload = server.request("GET", path=path)
        put_status, _ = server.request(
            "PUT", path=path, explode_on_read=True
        )

        self.assertEqual(get_status, 200)
        self.assertFalse(payload["can_edit"])
        self.assertTrue(payload["enabled"])
        self.assertEqual(len(payload["preview"]["items"]), 1)
        self.assertEqual(put_status, 403)

    def test_preview_toggle_rejects_unknown_fields(self) -> None:
        server = AutoUpdateHttpHarness(self.workspace)
        status, _ = server.request(
            "PUT",
            path="/api/start-stop/live-preview",
            body={
                "expected_revision": 0,
                "enabled": True,
                "interval_minutes": 10,
            },
        )

        self.assertEqual(status, 400)


class StartStopAutoUpdateMainTests(unittest.TestCase):
    def test_only_the_local_main_process_starts_a_scheduler(self) -> None:
        workspace = mock.Mock()
        workspace.database = mock.Mock()
        workspace.collection_config = None
        workspace.collection_config_provider = None
        server = mock.Mock()
        scheduler = mock.Mock()
        required = [
            "--database",
            "/tmp/start-stop-test.sqlite3",
            "--analysis-dir",
            "/tmp/start-stop-test-analysis",
            "--scratch-dir",
            "/tmp/start-stop-test-scratch",
        ]
        with mock.patch.object(SERVICE, "build_workspace", return_value=workspace), mock.patch.object(
            SERVICE, "StartStopHTTPServer", return_value=server
        ), mock.patch.object(
            SERVICE, "AutoUpdateScheduler", return_value=scheduler
        ) as scheduler_type:
            self.assertEqual(SERVICE.main(required), 0)
            scheduler_type.assert_called_once_with(workspace, workspace.database)
            scheduler.start.assert_called_once_with()
            scheduler.close.assert_called_once_with()

        lan_server = mock.Mock()
        with tempfile.TemporaryDirectory() as temporary:
            auth_file = Path(temporary) / "lan-auth"
            auth_file.write_text("reader:test-password\n", encoding="utf-8")
            auth_file.chmod(0o600)
            with mock.patch.object(SERVICE, "build_workspace", return_value=workspace), mock.patch.object(
                SERVICE, "StartStopHTTPServer", return_value=lan_server
            ), mock.patch.object(SERVICE, "AutoUpdateScheduler") as scheduler_type:
                self.assertEqual(
                    SERVICE.main(
                        required
                        + [
                            "--lan-read-only",
                            "--lan-auth-file",
                            str(auth_file),
                            "--bind",
                            "192.168.110.158",
                            "--port",
                            "18788",
                        ]
                    ),
                    0,
                )
                scheduler_type.assert_not_called()


if __name__ == "__main__":
    unittest.main()
