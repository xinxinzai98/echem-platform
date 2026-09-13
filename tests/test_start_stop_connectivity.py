from __future__ import annotations

import http.client
import io
import json
import tempfile
import unittest
from pathlib import Path

import start_stop_service as SERVICE
from echem_platform.start_stop_collection import CollectionError

TEST_LAN_AUTH = SERVICE.BasicAuthCredentials("reader", "test-password")
TEST_LAN_AUTH_HEADER = "Basic cmVhZGVyOnRlc3QtcGFzc3dvcmQ="


class FakeConnectivityTransport:
    calls: list[dict[str, object]] = []
    fail_ids: set[str] = set()

    def __init__(self, *, known_hosts_file=None):
        self.known_hosts_file = known_hosts_file

    def check_connectivity(self, machine, *, timeout):
        self.__class__.calls.append(
            {
                "machine_id": machine["id"],
                "timeout": timeout,
                "known_hosts_file": self.known_hosts_file,
            }
        )
        if machine["id"] in self.__class__.fail_ids:
            raise CollectionError(
                "SSH 密钥不存在：/Users/private/.ssh/should-never-leak"
            )
        return {
            "reachable": True,
            "message": "SSH 连接与密钥认证正常",
            "elapsed_ms": 37,
            "checked_at_utc": "2026-08-03T11:45:00Z",
        }


class ConnectivityWorkspace:
    def __init__(self, config_path: Path):
        self.collection_config = config_path

    def status(self):
        return {
            "available": True,
            "execution": {"ready": True, "update_ready": True},
            "job": {"status": "idle", "result": {}},
        }


def machine(machine_id: str, name: str, hostname: str, ip: str) -> dict:
    return {
        "id": machine_id,
        "name": name,
        "hostname": hostname,
        "ip": ip,
        "user": "private-user",
        "identity_file": f"/Users/private/.ssh/{hostname}",
        "roots": [{"label": "private", "remote_path": "D:\\private"}],
    }


class ConnectivityHttpHarness:
    def __init__(self, workspace, monitor, *, lan_read_only=False):
        self.lan_read_only = lan_read_only
        self.port = 18788 if lan_read_only else 18787
        self.public_host = "192.168.110.158" if lan_read_only else "127.0.0.1"
        self.handler_type = SERVICE.create_handler(
            workspace,
            lan_read_only=lan_read_only,
            lan_auth_credentials=TEST_LAN_AUTH if lan_read_only else None,
            public_host=self.public_host,
            public_port=self.port,
            connectivity_monitor=monitor,
        )

    def request(self, method: str, path: str, *, body=None):
        encoded = json.dumps(body).encode("utf-8") if body is not None else b""
        headers = http.client.HTTPMessage()
        headers.add_header("Host", f"{self.public_host}:{self.port}")
        if self.lan_read_only:
            headers.add_header("Authorization", TEST_LAN_AUTH_HEADER)
        if body is not None:
            headers.add_header("Content-Type", "application/json")
            headers.add_header("Content-Length", str(len(encoded)))
            headers.add_header("Origin", f"http://{self.public_host}:{self.port}")
        handler = self.handler_type.__new__(self.handler_type)
        handler.path = path
        handler.headers = headers
        handler.rfile = io.BytesIO(encoded)
        handler.client_address = ("127.0.0.1", 55000)
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
        self.assert_single_response(responses)
        return responses[0]

    @staticmethod
    def assert_single_response(responses):
        if len(responses) != 1:
            raise AssertionError(f"expected one response, got {responses!r}")


class MachineConnectivityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config_path = self.root / "collection.json"
        self.known_hosts = self.root / "known_hosts"
        self.config = {
            "known_hosts_file": str(self.known_hosts),
            "machines": [
                machine("machine-153", "测试室1", "AGHID-G", "192.168.110.153"),
                machine("machine-155", "测试室2", "AGHID-H", "192.168.110.155"),
                machine("machine-164", "制备室", "A-9", "192.168.110.164"),
            ],
        }
        self.config_path.write_text(
            json.dumps(self.config, ensure_ascii=False), encoding="utf-8"
        )
        FakeConnectivityTransport.calls = []
        FakeConnectivityTransport.fail_ids = set()
        self.monitor = SERVICE.MachineConnectivityMonitor(
            self.config_path,
            transport_factory=FakeConnectivityTransport,
            timeout_seconds=12,
        )
        self.workspace = ConnectivityWorkspace(self.config_path)

    def tearDown(self):
        self.temporary.cleanup()

    def test_snapshot_lists_three_sanitized_machines_without_triggering_ssh(self):
        payload = self.monitor.snapshot(can_check=True)

        self.assertTrue(payload["available"])
        self.assertTrue(payload["can_check"])
        self.assertEqual(payload["total"], 3)
        self.assertEqual(payload["checked"], 0)
        self.assertEqual(
            [item["name"] for item in payload["machines"]],
            ["测试室1", "测试室2", "制备室"],
        )
        self.assertTrue(all(item["status"] == "unchecked" for item in payload["machines"]))
        self.assertEqual(FakeConnectivityTransport.calls, [])
        serialized = json.dumps(payload, ensure_ascii=False)
        for forbidden in ("private-user", "identity_file", "known_hosts", "D:\\private"):
            self.assertNotIn(forbidden, serialized)

    def test_single_machine_check_uses_fixed_config_timeout_and_is_cached(self):
        result = self.monitor.check("machine-155")
        snapshot = self.monitor.snapshot(can_check=True)

        self.assertTrue(result["reachable"])
        self.assertEqual(result["elapsed_ms"], 37)
        self.assertEqual(
            FakeConnectivityTransport.calls,
            [
                {
                    "machine_id": "machine-155",
                    "timeout": 12,
                    "known_hosts_file": self.known_hosts,
                }
            ],
        )
        self.assertEqual(snapshot["checked"], 1)
        self.assertEqual(snapshot["reachable"], 1)
        statuses = {item["id"]: item["status"] for item in snapshot["machines"]}
        self.assertEqual(statuses["machine-155"], "reachable")
        self.assertEqual(statuses["machine-153"], "unchecked")

    def test_failed_check_is_a_safe_unreachable_result_without_secret_path(self):
        FakeConnectivityTransport.fail_ids = {"machine-164"}

        result = self.monitor.check("machine-164")

        self.assertFalse(result["reachable"])
        self.assertEqual(result["status"], "unreachable")
        self.assertIn("密钥", result["message"])
        self.assertNotIn("/Users/private", json.dumps(result, ensure_ascii=False))
        self.assertIsInstance(result["elapsed_ms"], int)
        self.assertTrue(result["checked_at_utc"].endswith("Z"))

    def test_local_http_checks_only_an_exact_configured_machine_id(self):
        server = ConnectivityHttpHarness(self.workspace, self.monitor)

        status_code, status_payload = server.request("GET", "/api/start-stop/status")
        get_status, listing = server.request("GET", "/api/start-stop/connectivity")
        check_status, checked = server.request(
            "POST",
            "/api/start-stop/connectivity/check",
            body={"machine_id": "machine-153"},
        )
        unknown_status, _ = server.request(
            "POST",
            "/api/start-stop/connectivity/check",
            body={"machine_id": "192.168.110.99"},
        )

        self.assertEqual(status_code, 200)
        self.assertEqual(status_payload["connectivity"]["total"], 3)
        self.assertTrue(status_payload["capabilities"]["can_check_connectivity"])
        self.assertEqual(get_status, 200)
        self.assertEqual(listing["total"], 3)
        self.assertEqual(check_status, 200)
        self.assertEqual(checked["id"], "machine-153")
        self.assertEqual(unknown_status, 404)
        self.assertEqual(len(FakeConnectivityTransport.calls), 1)

    def test_lan_can_list_but_cannot_trigger_a_check(self):
        server = ConnectivityHttpHarness(
            self.workspace, self.monitor, lan_read_only=True
        )

        get_status, listing = server.request("GET", "/api/start-stop/connectivity")
        post_status, response = server.request(
            "POST",
            "/api/start-stop/connectivity/check",
            body={"machine_id": "machine-153"},
        )

        self.assertEqual(get_status, 200)
        self.assertFalse(listing["can_check"])
        self.assertEqual(post_status, 403)
        self.assertIn("只读", response["error"])
        self.assertEqual(FakeConnectivityTransport.calls, [])

    def test_invalid_or_missing_config_never_exposes_a_path(self):
        monitor = SERVICE.MachineConnectivityMonitor(self.root / "secret" / "missing.json")

        payload = monitor.snapshot(can_check=True)

        self.assertFalse(payload["available"])
        self.assertEqual(payload["machines"], [])
        self.assertNotIn(str(self.root), json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
