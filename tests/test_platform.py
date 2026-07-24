from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("echem_app", ROOT / "app.py")
assert SPEC and SPEC.loader
APP = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = APP
SPEC.loader.exec_module(APP)


class ParserTests(unittest.TestCase):
    def test_parser_implementation_is_split_from_main_app(self):
        self.assertEqual(APP.parse_curve.__module__, "echem_platform.parsers.text")

    def test_chi_cv_text_is_parsed(self):
        path = ROOT / "demo_data" / "chi_cv_demo.txt"
        curve = APP.parse_curve(path, path.read_bytes(), 2000)
        self.assertEqual(curve.status, "parsed")
        self.assertEqual(curve.instrument, "CHI")
        self.assertEqual(curve.technique, "CV")
        self.assertEqual(curve.parser_id, "chi.delimited_text")
        self.assertEqual(curve.x_name, "Potential/V")
        self.assertEqual(curve.y_name, "Current/A")
        self.assertEqual(curve.point_count, 29)

    def test_corrtest_eis_uses_nyquist_axes(self):
        path = ROOT / "demo_data" / "corrtest_eis_demo.z60"
        curve = APP.parse_curve(path, path.read_bytes(), 2000)
        self.assertEqual(curve.status, "parsed")
        self.assertEqual(curve.instrument, "CorrTest")
        self.assertEqual(curve.technique, "EIS")
        self.assertEqual(curve.parser_id, "corrtest.z60_text")
        self.assertEqual(curve.x_name, "Zreal(ohm)")
        self.assertEqual(curve.y_name, "Zimag(ohm)")
        self.assertEqual(curve.point_count, 15)

    def test_binary_is_metadata_only(self):
        path = ROOT / "demo_data" / "chi_ocpt_demo.bin"
        curve = APP.parse_curve(path, path.read_bytes(), 2000)
        self.assertEqual(curve.status, "metadata_only")
        self.assertEqual(curve.instrument, "CHI")
        self.assertEqual(curve.parser_id, "chi.binary.metadata")
        self.assertEqual(curve.point_count, 0)

    def test_galstatic_defaults_to_potential_over_time(self):
        path = ROOT / "demo_data" / "corrtest_galstatic_demo.cor"
        curve = APP.parse_curve(path, path.read_bytes(), 2000)
        self.assertEqual(curve.technique, "CP/GCD")
        self.assertEqual(curve.x_name, "T(s)")
        self.assertEqual(curve.y_name, "E(V)")

    def test_decimation_preserves_endpoints(self):
        points = [[float(index), float(index * index)] for index in range(100)]
        result = APP.decimate_points(points, 12)
        self.assertEqual(len(result), 12)
        self.assertEqual(result[0], points[0])
        self.assertEqual(result[-1], points[-1])


class ScannerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database_path = self.root / "state" / "test.sqlite3"

    def tearDown(self):
        self.temp.cleanup()

    def make_scanner(self):
        database = APP.Database(self.database_path)
        scanner = APP.Scanner(
            database=database,
            roots=[self.source],
            extensions=[".txt", ".cor", ".z60", ".bin"],
            max_file_bytes=1024 * 1024,
            max_points=2000,
            stable_age_seconds=0,
        )
        return database, scanner

    def test_scan_does_not_modify_source_file(self):
        path = self.source / "chi_cv_test.txt"
        payload = (ROOT / "demo_data" / "chi_cv_demo.txt").read_bytes()
        path.write_bytes(payload)
        source_stat = path.stat()
        database, scanner = self.make_scanner()

        first = scanner.scan()
        after_stat = path.stat()
        second = scanner.scan()

        self.assertEqual(first["imported"], 1)
        self.assertEqual(second["unchanged"], 1)
        self.assertEqual(path.read_bytes(), payload)
        self.assertEqual(after_stat.st_size, source_stat.st_size)
        self.assertEqual(after_stat.st_mtime_ns, source_stat.st_mtime_ns)
        run = database.list_runs()[0]
        self.assertTrue(run["source_available"])
        self.assertEqual(run["sha256"], APP.hashlib.sha256(payload).hexdigest())
        self.assertEqual(run["parser_id"], "chi.delimited_text")

    def test_missing_source_is_reported_without_deleting_cached_record(self):
        path = self.source / "chi_cv_test.txt"
        path.write_bytes((ROOT / "demo_data" / "chi_cv_demo.txt").read_bytes())
        database, scanner = self.make_scanner()
        scanner.scan()
        run_id = database.list_runs()[0]["id"]
        database.update_metadata(run_id, {"sample_id": "NiMo-cache-test"})

        path.unlink()

        listed = database.list_runs()[0]
        detail = database.get_run(run_id)
        counts = database.status_counts()
        self.assertFalse(listed["source_available"])
        self.assertIsNotNone(detail)
        self.assertFalse(detail["source_available"])
        self.assertGreater(detail["point_count"], 0)
        self.assertEqual(detail["sample_id"], "NiMo-cache-test")
        self.assertEqual(counts["total"], 1)
        self.assertEqual(counts["available_sources"], 0)
        self.assertEqual(counts["unavailable_sources"], 1)

    def test_metadata_update_only_changes_platform_database(self):
        path = self.source / "corrtest_demo.cor"
        payload = (ROOT / "demo_data" / "corrtest_galstatic_demo.cor").read_bytes()
        path.write_bytes(payload)
        database, scanner = self.make_scanner()
        scanner.scan()
        run_id = database.list_runs()[0]["id"]

        updated = database.update_metadata(
            run_id,
            {
                "sample_id": "NiMo-001",
                "material": "NiMo/NF",
                "electrolyte": "1 M KOH",
                "area_cm2": 1.0,
                "tags": "HER, demo",
                "notes": "read-only source test",
            },
        )

        self.assertEqual(updated["sample_id"], "NiMo-001")
        self.assertEqual(path.read_bytes(), payload)
        self.assertTrue(
            any(item["action"] == "metadata_updated" for item in database.recent_audit())
        )

    def test_unstable_file_is_deferred(self):
        path = self.source / "live.txt"
        path.write_text("X,Y\n1,2\n", encoding="utf-8")
        database = APP.Database(self.database_path)
        scanner = APP.Scanner(
            database=database,
            roots=[self.source],
            extensions=[".txt"],
            max_file_bytes=1024 * 1024,
            max_points=2000,
            stable_age_seconds=120,
        )
        result = scanner.scan()
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(database.status_counts()["total"], 0)


class SafetyTests(unittest.TestCase):
    def test_non_loopback_bind_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "bind": "0.0.0.0",
                        "watch_roots": [],
                        "extensions": [".txt"],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                APP.load_config(config_path)

    def test_relative_watch_root_is_resolved_from_config_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            config = {"watch_roots": ["data"]}
            self.assertEqual(APP.resolve_watch_roots(config, base), [(base / "data").resolve()])

    def test_untracked_local_config_overrides_public_base(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            config_path = base / "config.json"
            local_path = base / "config.local.json"
            config_path.write_text(
                json.dumps(
                    {
                        "bind": "127.0.0.1",
                        "port": 8787,
                        "watch_roots": ["demo_data"],
                        "extensions": [".txt"],
                    }
                ),
                encoding="utf-8",
            )
            local_path.write_text(
                json.dumps(
                    {
                        "port": 8877,
                        "watch_roots": ["private_data"],
                    }
                ),
                encoding="utf-8",
            )

            config = APP.load_config(config_path)

            self.assertEqual(config["port"], 8877)
            self.assertEqual(config["watch_roots"], ["private_data"])
            self.assertTrue(config["local_override_active"])
            self.assertEqual(
                config["config_sources"],
                [str(config_path.resolve()), str(local_path.resolve())],
            )

    def test_local_config_cannot_enable_network_bind(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            config_path = base / "config.json"
            local_path = base / "config.local.json"
            config_path.write_text(
                json.dumps(
                    {
                        "bind": "127.0.0.1",
                        "watch_roots": [],
                        "extensions": [".txt"],
                    }
                ),
                encoding="utf-8",
            )
            local_path.write_text(
                json.dumps({"bind": "0.0.0.0"}),
                encoding="utf-8",
            )

            with self.assertRaises(ValueError):
                APP.load_config(config_path)


if __name__ == "__main__":
    unittest.main()
