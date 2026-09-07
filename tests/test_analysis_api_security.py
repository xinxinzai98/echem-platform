from __future__ import annotations

import contextlib
import http.client
import importlib.util
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "echem_app_analysis_api_security_tests",
    ROOT / "app.py",
)
assert SPEC and SPEC.loader
APP = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = APP
SPEC.loader.exec_module(APP)


EIS_PAYLOAD = "\n".join(
    [
        "Method: EIS",
        "Freq/Hz,Z'/ohm,Z\"/ohm",
        "100000,1,1",
        "10000,3,-1",
    ]
)


def make_scanner(database, root: Path):
    return APP.Scanner(
        database=database,
        roots=[root],
        extensions=[".txt"],
        max_file_bytes=1024 * 1024,
        max_points=100,
        stable_age_seconds=0,
    )


class SourceContainmentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = APP.Database(self.root / "state" / "containment.sqlite3")

    def tearDown(self):
        self.temporary.cleanup()

    def test_scanner_skips_a_file_symlink_that_escapes_the_active_root(self):
        watched = self.root / "watched"
        watched.mkdir()
        outside = self.root / "outside.txt"
        outside.write_text(EIS_PAYLOAD, encoding="utf-8")
        linked = watched / "linked.txt"
        try:
            linked.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"file symlinks are unavailable: {exc}")

        result = make_scanner(self.database, watched).scan()

        self.assertEqual(result["seen"], 0)
        self.assertEqual(result["imported"], 0)
        self.assertEqual(self.database.list_runs(), [])

    def test_analysis_rejects_an_indexed_path_after_the_active_root_changes(self):
        former_root = self.root / "former"
        current_root = self.root / "current"
        former_root.mkdir()
        current_root.mkdir()
        source = former_root / "record.txt"
        source.write_text(EIS_PAYLOAD, encoding="utf-8")
        former_scanner = make_scanner(self.database, former_root)
        self.assertEqual(former_scanner.scan()["imported"], 1)
        run = self.database.list_runs()[0]

        runtime = APP.RuntimeState(
            self.database,
            make_scanner(self.database, current_root),
            APP.load_config(ROOT / "config.json"),
        )
        with self.assertRaises(APP.AnalysisRequestError) as caught:
            runtime._analysis_source(run["id"])

        self.assertEqual(caught.exception.status, 403)
        self.assertIn("当前配置的数据目录", str(caught.exception))


class AnalysisPostSecurityTests(unittest.TestCase):
    def setUp(self):
        self.port = 8787
        handler_type = APP.create_handler(object())
        self.handler = handler_type.__new__(handler_type)
        self.handler.server = type(
            "LocalServer",
            (),
            {"server_address": ("127.0.0.1", self.port)},
        )()
        self.handler.client_address = ("127.0.0.1", 55000)

    def local_headers(self, **extra: str) -> dict[str, str]:
        headers = {
            "Host": f"127.0.0.1:{self.port}",
            "Content-Type": "application/json",
        }
        headers.update(extra)
        return headers

    def require(self, headers: dict[str, str]) -> None:
        message = http.client.HTTPMessage()
        for name, value in headers.items():
            message.add_header(name, value)
        self.handler.headers = message
        self.handler.require_local_json_request()

    def test_analysis_post_rejects_text_plain(self):
        with self.assertRaises(APP.AnalysisRequestError) as caught:
            self.require(self.local_headers(**{"Content-Type": "text/plain"}))
        self.assertEqual(caught.exception.status, 415)
        self.assertIn("application/json", str(caught.exception))

    def test_analysis_post_rejects_cross_origin_and_dns_rebinding_hosts(self):
        with self.assertRaises(APP.AnalysisRequestError) as caught:
            self.require(self.local_headers(Origin="https://attacker.example"))
        self.assertEqual(caught.exception.status, 403)

        with self.assertRaises(APP.AnalysisRequestError) as caught:
            self.require(
                {
                    "Host": f"attacker.example:{self.port}",
                    "Origin": f"http://attacker.example:{self.port}",
                    "Content-Type": "application/json",
                }
            )
        self.assertEqual(caught.exception.status, 403)

    def test_analysis_post_allows_same_origin_and_originless_local_clients(self):
        self.require(
            self.local_headers(
                Origin=f"http://127.0.0.1:{self.port}",
                **{"Content-Type": "application/json; charset=utf-8"},
            )
        )
        self.require(self.local_headers())

    def test_remote_peer_cannot_spoof_a_loopback_host_for_writes(self):
        self.handler.client_address = ("192.168.110.50", 55000)
        with self.assertRaises(APP.AnalysisRequestError) as caught:
            self.require(self.local_headers())
        self.assertEqual(caught.exception.status, 403)
        self.assertIn("本机回环", str(caught.exception))

    def test_explicit_container_proxy_still_requires_a_loopback_host(self):
        public_port = 18787
        handler_type = APP.create_handler(
            object(),
            local_public_port=public_port,
            trusted_container_proxy=True,
        )
        handler = handler_type.__new__(handler_type)
        handler.server = type(
            "ContainerServer",
            (),
            {"server_address": ("0.0.0.0", self.port)},
        )()
        handler.client_address = ("172.20.0.1", 55000)
        message = http.client.HTTPMessage()
        message.add_header("Host", f"127.0.0.1:{public_port}")
        message.add_header("Content-Type", "application/json")
        handler.headers = message
        handler.require_local_json_request()

        message = http.client.HTTPMessage()
        message.add_header("Host", f"192.168.110.158:{public_port}")
        message.add_header("Content-Type", "application/json")
        handler.headers = message
        with self.assertRaises(APP.AnalysisRequestError) as caught:
            handler.require_local_json_request()
        self.assertEqual(caught.exception.status, 403)

    def test_not_calculable_persist_error_is_returned_as_structured_400(self):
        class RejectingRuntime:
            @staticmethod
            def calculate_analysis(*_args, **_kwargs):
                raise APP.AnalysisValidationError(
                    "当前分析结果不可计算，不能保存。",
                    field="quality",
                    code="analysis_not_calculable",
                    details={
                        "quality": {
                            "level": "not_calculable",
                            "label": "不可计算",
                            "reasons": ["测试质量门"],
                        }
                    },
                )

        handler_type = APP.create_handler(RejectingRuntime())
        handler = handler_type.__new__(handler_type)
        handler.path = "/api/runs/1/analyses"
        handler.require_local_json_request = lambda: None
        handler.read_json = lambda: {
            "analysis_type": "eis_resistance",
            "parameters": {},
        }
        responses: list[tuple[dict, int]] = []
        handler.send_json = lambda payload, status=200: responses.append(
            (payload, int(status))
        )

        handler.do_POST()

        self.assertEqual(len(responses), 1)
        payload, status = responses[0]
        self.assertEqual(status, 400)
        self.assertEqual(
            payload["analysis_error"]["code"],
            "analysis_not_calculable",
        )
        self.assertEqual(
            payload["analysis_error"]["details"]["quality"]["level"],
            "not_calculable",
        )


class LanReadOnlyServerTests(unittest.TestCase):
    bind = "192.168.110.158"
    port = 8787

    def handler_for(self, path: str, *, host: str | None = None):
        handler_type = APP.create_handler(
            object(),
            lan_read_only=True,
            lan_bind_host=self.bind,
        )
        handler = handler_type.__new__(handler_type)
        handler.path = path
        handler.server = type(
            "LanServer",
            (),
            {"server_address": (self.bind, self.port)},
        )()
        handler.client_address = ("192.168.110.50", 55000)
        message = http.client.HTTPMessage()
        message.add_header("Host", host or f"{self.bind}:{self.port}")
        handler.headers = message
        responses: list[tuple[int, str]] = []
        handler.send_error_json = lambda status, detail: responses.append(
            (int(status), str(detail))
        )
        return handler, responses

    def test_lan_allows_only_start_stop_reads_and_static_assets(self):
        allowed = (
            "/",
            "/start-stop",
            "/api/start-stop/status",
            "/api/start-stop/chart",
            "/static/start-stop.js",
        )
        for path in allowed:
            with self.subTest(path=path):
                self.assertTrue(APP.lan_start_stop_get_allowed(path))

        for path in ("/api/status", "/api/runs", "/analysis", "/static/app.js"):
            with self.subTest(path=path):
                self.assertFalse(APP.lan_start_stop_get_allowed(path))

    def test_lan_status_redacts_paths_and_failure_details(self):
        payload = APP.lan_start_stop_status(
            {
                "available": True,
                "execution": {"ready": True, "message": "ready"},
                "job": {
                    "status": "failed",
                    "message": "private traceback",
                    "result": {
                        "complete_cycles": 12,
                        "output_dir": "/private/server/path",
                        "stderr": "private error",
                    },
                },
            }
        )

        self.assertEqual(payload["access_mode"], "lan_read_only")
        self.assertTrue(payload["capabilities"]["server_ready"])
        self.assertFalse(payload["capabilities"]["allowed_here"])
        self.assertFalse(payload["capabilities"]["can_start_update"])
        self.assertFalse(payload["capabilities"]["can_update_data"])
        self.assertFalse(payload["capabilities"]["can_save_configuration"])
        self.assertFalse(payload["capabilities"]["can_render_atlas"])
        self.assertEqual(payload["job"]["result"], {"complete_cycles": 12})
        self.assertNotIn("/private", payload["job"]["message"])
        self.assertNotIn("traceback", payload["job"]["message"])

    def test_local_capabilities_report_when_updates_are_ready(self):
        payload = APP.local_start_stop_status(
            {
                "available": True,
                "execution": {"ready": True, "message": "ready"},
            }
        )

        self.assertEqual(payload["access_mode"], "local_read_write")
        self.assertTrue(payload["capabilities"]["server_ready"])
        self.assertTrue(payload["capabilities"]["allowed_here"])
        self.assertFalse(payload["capabilities"]["busy"])
        self.assertTrue(payload["capabilities"]["can_start_update"])
        self.assertTrue(payload["capabilities"]["can_update_data"])
        self.assertTrue(payload["capabilities"]["can_save_configuration"])
        self.assertTrue(payload["capabilities"]["can_render_atlas"])

    def test_local_capabilities_block_a_second_update_while_busy(self):
        payload = APP.local_start_stop_status(
            {
                "available": True,
                "execution": {"ready": True, "message": "ready"},
                "job": {"status": "running", "action": "scan"},
            }
        )

        self.assertTrue(payload["capabilities"]["server_ready"])
        self.assertTrue(payload["capabilities"]["busy"])
        self.assertFalse(payload["capabilities"]["can_start_update"])

    def test_lan_get_blocks_other_modules_and_dns_rebinding_hosts(self):
        handler, responses = self.handler_for("/api/status")
        handler.do_GET()
        self.assertEqual(responses, [(404, "Not found")])

        handler, responses = self.handler_for(
            "/api/start-stop/status",
            host=f"attacker.example:{self.port}",
        )
        handler.do_GET()
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0][0], 403)

    def test_lan_rejects_every_post_route_before_reading_a_body(self):
        paths = (
            "/api/scan",
            "/api/start-stop/materials",
            "/api/start-stop/jobs",
            "/api/protocols",
            "/api/control/runs",
            "/api/runs/1/analyses",
            "/api/runs/1/metadata",
        )
        for path in paths:
            with self.subTest(path=path):
                handler, responses = self.handler_for(path)
                handler.do_POST()
                self.assertEqual(len(responses), 1)
                self.assertEqual(responses[0][0], 403)


class _PausingConnection:
    def __init__(self, connection: sqlite3.Connection, owner):
        self.connection = connection
        self.owner = owner

    def execute(self, statement, parameters=()):
        normalized = " ".join(str(statement).split()).upper()
        if (
            self.owner.pause_before_analysis_insert
            and normalized.startswith("INSERT INTO ANALYSIS_RECORDS")
        ):
            self.owner.pause_before_analysis_insert = False
            self.owner.insert_reached.set()
            if not self.owner.release_insert.wait(timeout=3):
                raise TimeoutError("timed out waiting to continue analysis insert")
        return self.connection.execute(statement, parameters)

    def __getattr__(self, name):
        return getattr(self.connection, name)


class _PausingDatabase(APP.Database):
    def __init__(self, path: Path):
        self.pause_before_analysis_insert = False
        self.insert_reached = threading.Event()
        self.release_insert = threading.Event()
        super().__init__(path)

    @contextlib.contextmanager
    def session(self):
        with super().session() as connection:
            yield _PausingConnection(connection, self)


class AnalysisAtomicityTests(unittest.TestCase):
    def test_hash_check_holds_a_sqlite_write_transaction_until_insert(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            watched = root / "watched"
            watched.mkdir()
            (watched / "record.txt").write_text(EIS_PAYLOAD, encoding="utf-8")
            database = _PausingDatabase(root / "state" / "atomic.sqlite3")
            scanner = make_scanner(database, watched)
            self.assertEqual(scanner.scan()["imported"], 1)
            run = database.list_runs()[0]
            database.pause_before_analysis_insert = True
            errors: list[BaseException] = []

            def save() -> None:
                try:
                    database.save_analysis(
                        run_id=run["id"],
                        analysis_type="eis_resistance",
                        schema_version=1,
                        algorithm_id="test.algorithm",
                        algorithm_version="1",
                        source_sha256=run["sha256"],
                        parser_id=run["parser_id"],
                        parser_version=APP.PARSER_VERSION,
                        parameters={},
                        result={"value": 1},
                    )
                except BaseException as exc:
                    errors.append(exc)

            saver = threading.Thread(target=save)
            saver.start()
            self.assertTrue(database.insert_reached.wait(timeout=3))
            competing = sqlite3.connect(database.path, timeout=0)
            try:
                with self.assertRaises(sqlite3.OperationalError) as caught:
                    competing.execute(
                        "UPDATE runs SET sha256 = ? WHERE id = ?",
                        ("f" * 64, run["id"]),
                    )
                self.assertIn("locked", str(caught.exception).lower())
            finally:
                competing.close()
                database.release_insert.set()
                saver.join(timeout=3)

            self.assertFalse(saver.is_alive())
            self.assertEqual(errors, [])
            records = database.list_analyses(run["id"])
            self.assertIsNotNone(records)
            self.assertEqual(len(records), 1)
            self.assertFalse(records[0]["stale"])


if __name__ == "__main__":
    unittest.main()
