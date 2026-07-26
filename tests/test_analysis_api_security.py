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
