from __future__ import annotations

import http.client
import io
import json
import tempfile
import unittest
from pathlib import Path

import start_stop_service as SERVICE
from echem_platform.start_stop_collection import (
    CollectionError,
    SSHWindowsTransport,
    directory_probe_script,
)
from echem_platform.start_stop_collection_config import CollectionConfigStore
from echem_platform.start_stop_database import StartStopDatabase

TEST_LAN_AUTH = SERVICE.BasicAuthCredentials("reader", "test-password")
TEST_LAN_AUTH_HEADER = "Basic cmVhZGVyOnRlc3QtcGFzc3dvcmQ="

MACHINE_IDS = (
    "01_测试室1_AGHID-G_192.168.110.153",
    "02_测试室2_AGHID-H_192.168.110.155",
    "03_制备室_A-9_192.168.110.164",
)


def machine(
    machine_id: str,
    *,
    name: str,
    hostname: str,
    ip: str,
    user: str,
    identity: str,
    label: str,
    path: str,
) -> dict:
    return {
        "id": machine_id,
        "name": name,
        "hostname": hostname,
        "ip": ip,
        "user": user,
        "identity_file": identity,
        "roots": [
            {
                "label": label,
                "remote_path": path,
                "exclude_directories": [],
            }
        ],
    }


class CollectionConfigWorkspace:
    def __init__(self, config_path: Path, database: StartStopDatabase) -> None:
        self.collection_config = config_path
        self.database = database
        self.job_status = "idle"

    def status(self) -> dict:
        return {
            "available": True,
            "execution": {"ready": True, "update_ready": True},
            "job": {"status": self.job_status, "result": {}},
        }


class ExplodingBody:
    def read(self, *_args, **_kwargs):
        raise AssertionError("LAN 写请求不应读取请求体")


class FakeDirectoryTransport:
    calls: list[dict] = []
    failure: Exception | None = None
    exists = True

    def __init__(self, *, known_hosts_file=None) -> None:
        self.known_hosts_file = known_hosts_file

    def check_directory(self, machine, remote_path, *, timeout):
        self.__class__.calls.append(
            {
                "machine_id": machine["id"],
                "remote_path": remote_path,
                "timeout": timeout,
                "known_hosts_file": self.known_hosts_file,
            }
        )
        if self.__class__.failure is not None:
            raise self.__class__.failure
        return {
            "reachable": self.__class__.exists,
            "message": "目录可访问" if self.__class__.exists else "目录不存在",
            "elapsed_ms": 23,
            "checked_at_utc": "2026-08-03T12:30:00Z",
        }


class CollectionConfigHttpHarness:
    def __init__(
        self,
        workspace: CollectionConfigWorkspace,
        *,
        lan_read_only: bool = False,
        connectivity_monitor=None,
        collection_config_store=None,
    ) -> None:
        self.workspace = workspace
        self.lan_read_only = lan_read_only
        self.port = 18788 if lan_read_only else 18787
        self.public_host = "192.168.110.158" if lan_read_only else "127.0.0.1"
        self.handler_type = SERVICE.create_handler(
            workspace,
            lan_read_only=lan_read_only,
            lan_auth_credentials=TEST_LAN_AUTH if lan_read_only else None,
            public_host=self.public_host,
            public_port=self.port,
            connectivity_monitor=connectivity_monitor,
            collection_config_store=collection_config_store,
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        explode_on_read: bool = False,
    ) -> tuple[int, object]:
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else b""
        headers = http.client.HTTPMessage()
        headers.add_header("Host", f"{self.public_host}:{self.port}")
        if self.lan_read_only:
            headers.add_header("Authorization", TEST_LAN_AUTH_HEADER)
        if body is not None or explode_on_read:
            headers.add_header("Content-Type", "application/json")
            headers.add_header("Content-Length", str(max(1, len(encoded))))
            headers.add_header("Origin", f"http://{self.public_host}:{self.port}")
        handler = self.handler_type.__new__(self.handler_type)
        handler.path = path
        handler.headers = headers
        handler.rfile = ExplodingBody() if explode_on_read else io.BytesIO(encoded)
        handler.client_address = (
            "192.168.110.50" if self.lan_read_only else "127.0.0.1",
            55000,
        )
        handler.server = type(
            "TestServer",
            (),
            {"server_address": (self.public_host, self.port)},
        )()
        responses: list[tuple[int, object]] = []
        handler.send_json = lambda payload, status=200: responses.append(
            (int(status), payload)
        )
        getattr(handler, f"do_{method}")()
        if len(responses) != 1:
            raise AssertionError(f"expected one response, got {responses!r}")
        return responses[0]


class StartStopCollectionConfigSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config_path = self.root / "docker" / "collection-config.json"
        self.config_path.parent.mkdir(parents=True)
        self.database_path = self.root / "state" / "database" / "start-stop.sqlite3"
        self.database_path.parent.mkdir(parents=True)
        self.config = {
            "version": 1,
            "settle_seconds": 900,
            "known_hosts_file": "/Users/private/.ssh/known_hosts",
            "extensions": [".txt", ".csv"],
            "machines": [
                machine(
                    MACHINE_IDS[0],
                    name="测试室1",
                    hostname="AGHID-G",
                    ip="192.168.110.153",
                    user=r"aghid-g\private-user",
                    identity="/Users/private/.ssh/key-153",
                    label="D盘_data_XY",
                    path=r"D:\data\XY",
                ),
                machine(
                    MACHINE_IDS[1],
                    name="测试室2",
                    hostname="AGHID-H",
                    ip="192.168.110.155",
                    user="private-user-155",
                    identity="/Users/private/.ssh/key-155",
                    label="D盘_数据",
                    path=r"D:\数据",
                ),
                machine(
                    MACHINE_IDS[2],
                    name="制备室",
                    hostname="A-9",
                    ip="192.168.110.164",
                    user="private-user-164",
                    identity="/Users/private/.ssh/key-164",
                    label="桌面_XY电镀工艺",
                    path=r"C:\Users\89464\Desktop\XY电镀工艺",
                ),
            ],
        }
        self.config_path.write_text(
            json.dumps(self.config, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.template_bytes = self.config_path.read_bytes()
        self.workspace = CollectionConfigWorkspace(
            self.config_path,
            StartStopDatabase(self.database_path),
        )
        self.local = CollectionConfigHttpHarness(self.workspace)
        FakeDirectoryTransport.calls = []
        FakeDirectoryTransport.failure = None
        FakeDirectoryTransport.exists = True

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _paths_payload(revision: int) -> dict:
        return {
            "expected_revision": revision,
            "machines": [
                {
                    "id": MACHINE_IDS[0],
                    "paths": [
                        {
                            "label": "新位置 G",
                            "path": r"D:\experiment\G",
                            "enabled": True,
                        }
                    ],
                },
                {
                    "id": MACHINE_IDS[1],
                    "paths": [
                        {
                            "label": "新位置 H",
                            "path": r"D:\experiment\H",
                            "enabled": True,
                        }
                    ],
                },
                {
                    "id": MACHINE_IDS[2],
                    "paths": [
                        {
                            "label": "新位置 A9",
                            "path": r"C:\Users\89464\Desktop\experiment",
                            "enabled": True,
                        }
                    ],
                },
            ],
        }

    def get_local_config(self) -> dict:
        status, payload = self.local.request(
            "GET", "/api/start-stop/collection-config"
        )
        self.assertEqual(status, 200)
        self.assertIsInstance(payload, dict)
        return payload

    def path_check_harness(self) -> CollectionConfigHttpHarness:
        store = CollectionConfigStore(self.config_path, self.workspace.database)
        monitor = SERVICE.MachineConnectivityMonitor(
            self.config_path,
            transport_factory=FakeDirectoryTransport,
            timeout_seconds=9,
            config_loader=store.fixed_config,
        )
        return CollectionConfigHttpHarness(
            self.workspace,
            connectivity_monitor=monitor,
            collection_config_store=store,
        )

    def assert_rejected_without_changing_revision(self, body: dict) -> None:
        before = self.get_local_config()
        status, _ = self.local.request(
            "PUT",
            "/api/start-stop/collection-config",
            body=body,
        )
        self.assertEqual(status, 400)
        after = self.get_local_config()
        self.assertEqual(after["revision"], before["revision"])
        self.assertEqual(after["machines"], before["machines"])

    def test_local_get_exposes_paths_but_never_ssh_credentials_or_commands(self) -> None:
        payload = self.get_local_config()

        self.assertEqual([item["id"] for item in payload["machines"]], list(MACHINE_IDS))
        self.assertTrue(all(isinstance(item.get("paths"), list) for item in payload["machines"]))
        self.assertEqual(
            payload["machines"][0]["paths"][0]["path"],
            r"D:\data\XY",
        )
        serialized = json.dumps(payload, ensure_ascii=False)
        for secret in (
            "private-user",
            "identity_file",
            "/Users/private",
            "known_hosts",
            "powershell",
            "command",
        ):
            self.assertNotIn(secret, serialized)

    def test_lan_cannot_read_collection_locations_or_credentials(self) -> None:
        lan = CollectionConfigHttpHarness(self.workspace, lan_read_only=True)

        status, payload = lan.request("GET", "/api/start-stop/collection-config")

        self.assertEqual(status, 403)
        serialized = json.dumps(payload, ensure_ascii=False)
        for private in (
            "D:\\",
            "C:\\Users",
            "private-user",
            "identity_file",
            "/Users/private",
            "known_hosts",
        ):
            self.assertNotIn(private, serialized)

    def test_put_updates_only_paths_and_survives_a_new_service_instance(self) -> None:
        initial = self.get_local_config()
        body = self._paths_payload(initial["revision"])

        status, saved = self.local.request(
            "PUT",
            "/api/start-stop/collection-config",
            body=body,
        )

        self.assertEqual(status, 200)
        self.assertEqual(saved["revision"], initial["revision"] + 1)
        self.assertEqual(self.config_path.read_bytes(), self.template_bytes)
        with self.workspace.database.session() as connection:
            stored_json = str(
                connection.execute(
                    "SELECT machines_json FROM collection_config_revisions WHERE revision=1"
                ).fetchone()[0]
            )
            audit_json = str(
                connection.execute(
                    "SELECT detail_json FROM audit_log "
                    "WHERE action='start_stop_collection_config.save'"
                ).fetchone()[0]
            )
        for forbidden in (
            "hostname",
            "ip",
            "user",
            "identity_file",
            "known_hosts",
            "command",
            "/Users/private",
        ):
            self.assertNotIn(forbidden, stored_json)
        self.assertNotIn(r"D:\experiment", audit_json)
        replacement = CollectionConfigHttpHarness(
            CollectionConfigWorkspace(
                self.config_path,
                StartStopDatabase(self.database_path),
            )
        )
        restart_status, restarted = replacement.request(
            "GET", "/api/start-stop/collection-config"
        )
        self.assertEqual(restart_status, 200)
        self.assertEqual(restarted["revision"], saved["revision"])
        self.assertEqual(restarted["machines"], saved["machines"])
        self.assertEqual(
            restarted["machines"][0]["paths"][0]["path"],
            r"D:\experiment\G",
        )
        serialized = json.dumps(restarted, ensure_ascii=False)
        self.assertNotIn("private-user", serialized)

    def test_put_rejects_identity_command_and_unknown_fields(self) -> None:
        revision = self.get_local_config()["revision"]
        cases: list[dict] = []

        top_level = self._paths_payload(revision)
        top_level["collection_command"] = "whoami"
        cases.append(top_level)

        for field, value in (
            ("hostname", "attacker"),
            ("ip", "192.168.110.99"),
            ("user", "attacker"),
            ("identity_file", "/tmp/key"),
            ("known_hosts_file", "/tmp/known_hosts"),
            ("command", "Remove-Item C:\\data"),
        ):
            payload = self._paths_payload(revision)
            payload["machines"][0][field] = value
            cases.append(payload)

        path_unknown = self._paths_payload(revision)
        path_unknown["machines"][0]["paths"][0]["exclude_directories"] = []
        cases.append(path_unknown)

        wrong_machine = self._paths_payload(revision)
        wrong_machine["machines"][0]["id"] = "192.168.110.99"
        cases.append(wrong_machine)

        missing_machine = self._paths_payload(revision)
        missing_machine["machines"].pop()
        cases.append(missing_machine)

        duplicate_machine = self._paths_payload(revision)
        duplicate_machine["machines"][2]["id"] = MACHINE_IDS[0]
        cases.append(duplicate_machine)

        for index, payload in enumerate(cases):
            with self.subTest(case=index):
                self.assert_rejected_without_changing_revision(payload)

    def test_put_rejects_non_windows_unsafe_or_overbroad_paths(self) -> None:
        revision = self.get_local_config()["revision"]
        invalid_paths = (
            "relative\\data",
            "C:relative\\data",
            "/private/data",
            r"\\server\share\data",
            r"\\?\C:\data",
            r"\??\C:\data",
            "C:\\",
            "D:\\",
            r"C:\Windows",
            r"C:\Windows\System32",
            r"C:\Windows\Temp\experiment",
            r"D:\Windows\Temp\experiment",
            r"C:\Program Files",
            r"C:\ProgramData",
            r"C:\Boot",
            r"C:\Recovery",
            r"C:\Users",
            r"C:\$Recycle.Bin",
            r"C:\System Volume Information",
            r"C:\data\..\Windows\Temp",
            r"C:\data\folder.",
            "C:\\data\\folder ",
            r"C:\Windows.\Temp",
            r"C:\data\CON",
            r"C:\data\NUL.txt",
            r"C:\data\COM1",
            r"C:\data\LPT9.log",
            "C:\\data\x00hidden".replace("x00", "\x00"),
            "C:\\data\nline".replace("nline", "\nline"),
            "C:\\data\tline".replace("tline", "\tline"),
            "C:\\" + ("x" * 5000),
        )

        for path in invalid_paths:
            with self.subTest(path=repr(path)):
                payload = self._paths_payload(revision)
                payload["machines"][0]["paths"][0]["path"] = path
                self.assert_rejected_without_changing_revision(payload)

    def test_put_bounds_labels_paths_and_enabled_types(self) -> None:
        revision = self.get_local_config()["revision"]
        cases: list[dict] = []
        for label in ("控制\n字符", "x" * 1000):
            payload = self._paths_payload(revision)
            payload["machines"][0]["paths"][0]["label"] = label
            cases.append(payload)

        enabled = self._paths_payload(revision)
        enabled["machines"][0]["paths"][0]["enabled"] = "true"
        cases.append(enabled)

        too_many = self._paths_payload(revision)
        too_many["machines"][0]["paths"] = [
            {
                "label": f"位置 {index}",
                "path": fr"D:\experiment\{index}",
                "enabled": True,
            }
            for index in range(129)
        ]
        cases.append(too_many)

        for index, payload in enumerate(cases):
            with self.subTest(case=index):
                self.assert_rejected_without_changing_revision(payload)

    def test_revision_and_running_job_conflicts_do_not_change_saved_locations(self) -> None:
        initial = self.get_local_config()

        stale = self._paths_payload(initial["revision"] + 1)
        stale_status, _ = self.local.request(
            "PUT", "/api/start-stop/collection-config", body=stale
        )
        self.assertEqual(stale_status, 409)
        self.assertEqual(self.get_local_config()["revision"], initial["revision"])

        self.workspace.job_status = "running"
        busy = self._paths_payload(initial["revision"])
        busy_status, _ = self.local.request(
            "PUT", "/api/start-stop/collection-config", body=busy
        )
        self.assertEqual(busy_status, 409)
        self.workspace.job_status = "idle"
        self.assertEqual(self.get_local_config()["machines"], initial["machines"])

    def test_lan_rejects_location_writes_and_checks_before_reading_body(self) -> None:
        lan = CollectionConfigHttpHarness(self.workspace, lan_read_only=True)

        for method, path in (
            ("PUT", "/api/start-stop/collection-config"),
            ("POST", "/api/start-stop/connectivity/path-check"),
        ):
            with self.subTest(method=method, path=path):
                status, payload = lan.request(
                    method,
                    path,
                    explode_on_read=True,
                )
                self.assertEqual(status, 403)
                self.assertIn("只读", payload["error"])

    def test_path_check_uses_only_a_fixed_machine_and_bounded_read_only_probe(self) -> None:
        harness = self.path_check_harness()
        before = self.get_local_config()

        status, payload = harness.request(
            "POST",
            "/api/start-stop/connectivity/path-check",
            body={
                "machine_id": MACHINE_IDS[1],
                "path": "d:/experiment//new-data",
            },
        )

        self.assertEqual(status, 200)
        self.assertTrue(payload["reachable"])
        self.assertEqual(payload["machine_id"], MACHINE_IDS[1])
        self.assertEqual(payload["path"], r"D:\experiment\new-data")
        self.assertEqual(payload["elapsed_ms"], 23)
        self.assertEqual(
            FakeDirectoryTransport.calls,
            [
                {
                    "machine_id": MACHINE_IDS[1],
                    "remote_path": r"D:\experiment\new-data",
                    "timeout": 9,
                    "known_hosts_file": Path("/Users/private/.ssh/known_hosts"),
                }
            ],
        )
        after = self.get_local_config()
        self.assertEqual(after["revision"], before["revision"])
        self.assertEqual(after["machines"], before["machines"])
        serialized = json.dumps(payload, ensure_ascii=False)
        for secret in ("private-user", "identity_file", "/Users/private", "known_hosts"):
            self.assertNotIn(secret, serialized)

    def test_path_check_rejects_unknown_machine_path_and_fields_before_transport(self) -> None:
        harness = self.path_check_harness()
        cases = (
            (
                {"machine_id": "unknown", "path": r"D:\experiment"},
                404,
            ),
            (
                {"machine_id": MACHINE_IDS[0], "path": r"\\server\share"},
                400,
            ),
            (
                {
                    "machine_id": MACHINE_IDS[0],
                    "path": r"D:\experiment",
                    "command": "whoami",
                },
                400,
            ),
            ({"machine_id": MACHINE_IDS[0]}, 400),
        )

        for body, expected_status in cases:
            with self.subTest(body=body):
                status, _ = harness.request(
                    "POST",
                    "/api/start-stop/connectivity/path-check",
                    body=body,
                )
                self.assertEqual(status, expected_status)
        self.assertEqual(FakeDirectoryTransport.calls, [])

    def test_path_check_returns_a_safe_failure_and_busy_409(self) -> None:
        harness = self.path_check_harness()
        FakeDirectoryTransport.failure = CollectionError(
            "SSH 密钥不存在：/Users/private/.ssh/should-never-leak"
        )

        status, payload = harness.request(
            "POST",
            "/api/start-stop/connectivity/path-check",
            body={"machine_id": MACHINE_IDS[2], "path": r"D:\experiment"},
        )

        self.assertEqual(status, 200)
        self.assertFalse(payload["reachable"])
        self.assertIn("密钥", payload["message"])
        self.assertNotIn("/Users/private", json.dumps(payload, ensure_ascii=False))
        self.assertEqual(len(FakeDirectoryTransport.calls), 1)

        FakeDirectoryTransport.failure = None
        self.workspace.job_status = "queued"
        busy_status, _ = harness.request(
            "POST",
            "/api/start-stop/connectivity/path-check",
            body={"machine_id": MACHINE_IDS[2], "path": r"D:\experiment"},
        )
        self.workspace.job_status = "idle"
        self.assertEqual(busy_status, 409)
        self.assertEqual(len(FakeDirectoryTransport.calls), 1)

    def test_status_endpoints_never_publish_collection_credentials(self) -> None:
        for harness in (
            self.local,
            CollectionConfigHttpHarness(self.workspace, lan_read_only=True),
        ):
            with self.subTest(lan=harness.lan_read_only):
                status, payload = harness.request("GET", "/api/start-stop/status")
                self.assertEqual(status, 200)
                serialized = json.dumps(payload, ensure_ascii=False)
                for secret in (
                    "private-user",
                    "identity_file",
                    "/Users/private",
                    "known_hosts",
                ):
                    self.assertNotIn(secret, serialized)


class DirectoryProbeSecurityTests(unittest.TestCase):
    def test_probe_script_treats_the_path_as_data_and_contains_no_inventory_or_write(self) -> None:
        hostile = r"C:\data'; Remove-Item -Recurse C:\important; #'"

        script = directory_probe_script(hostile)

        self.assertNotIn(hostile, script)
        self.assertIn("FromBase64String", script)
        self.assertIn("Test-Path -LiteralPath $path -PathType Container", script)
        for forbidden in (
            "Get-ChildItem",
            "Get-Item",
            "Remove-Item",
            "Set-Item",
            "New-Item",
            "Copy-Item",
            "Move-Item",
            "OpenWrite",
            "FileMode]::Create",
        ):
            self.assertNotIn(forbidden, script)

    def test_transport_clamps_probe_timeout_and_returns_only_safe_summary_fields(self) -> None:
        calls: list[dict] = []

        class CapturingTransport(SSHWindowsTransport):
            def _run_payload(self, machine, script, *, timeout):
                calls.append(
                    {
                        "machine_id": machine["id"],
                        "script": script,
                        "timeout": timeout,
                    }
                )
                return {
                    "exists": True,
                    "checked_at_utc": "2026-08-03T12:00:00Z",
                    "private": "/Users/private/.ssh/key",
                }

        transport = CapturingTransport()
        machine_payload = {"id": MACHINE_IDS[0]}

        high = transport.check_directory(
            machine_payload,
            r"D:\experiment",
            timeout=999,
        )
        low = transport.check_directory(
            machine_payload,
            r"D:\experiment",
            timeout=0,
        )

        self.assertEqual([call["timeout"] for call in calls], [30, 1])
        self.assertEqual(
            high,
            {
                "reachable": True,
                "message": "目录可访问",
                "elapsed_ms": high["elapsed_ms"],
                "checked_at_utc": "2026-08-03T12:00:00Z",
            },
        )
        self.assertEqual(low["reachable"], True)
        self.assertNotIn("private", json.dumps({"high": high, "low": low}))


if __name__ == "__main__":
    unittest.main()
