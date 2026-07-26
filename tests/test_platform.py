from __future__ import annotations

import importlib.util
import inspect
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

from echem_platform.control import default_dry_run_draft
from echem_platform.fixture_validation import ManifestError, validate_fixture_manifest
from echem_platform.parsers import decimate_points


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

    def test_real_chi_and_corrtest_text_exports_are_auto_detected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            chi_path = root / "S3EIS.txt"
            chi_path.write_text(
                "\n".join(
                    [
                        "A.C. Impedance",
                        "Instrument Model: CHI760E",
                        "",
                        'Freq/Hz, Z\'/ohm, Z"/ohm, Z/ohm, Phase/deg',
                        "1.0e5, 1.20, 0.40, 1.26, 18.4",
                        "1.0e4, 1.30, -0.10, 1.30, -4.4",
                    ]
                ),
                encoding="utf-8",
            )
            corrtest_path = root / "her-eis.txt"
            corrtest_path.write_text(
                "\n".join(
                    [
                        "CSStudioFile,ID_EISVSFRQ,fixture-header",
                        "Freq(Hz)\tAmpl(mV)\tZ'(Ohm.cm²)\tZ''(Ohm.cm²)\tPhase",
                        "+1.0E+05\t10\t+1.21E+00\t+6.47E-01\t28.1",
                        "+1.0E+04\t10\t+1.18E+00\t-2.10E-02\t-1.0",
                    ]
                ),
                encoding="utf-8",
            )

            chi = APP.parse_curve(chi_path, chi_path.read_bytes(), 2000)
            corrtest = APP.parse_curve(
                corrtest_path,
                corrtest_path.read_bytes(),
                2000,
            )

        self.assertEqual((chi.status, chi.instrument, chi.technique), ("parsed", "CHI", "EIS"))
        self.assertEqual((chi.x_name, chi.y_name), ("Z'/ohm", 'Z"/ohm'))
        self.assertEqual(
            (corrtest.status, corrtest.instrument, corrtest.technique),
            ("parsed", "CorrTest", "EIS"),
        )
        self.assertEqual(
            (corrtest.x_name, corrtest.y_name),
            ("Z'(Ohm.cm²)", "Z''(Ohm.cm²)"),
        )

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
        result = decimate_points(points, 12)
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

    def test_file_tree_groups_mixed_workstation_exports_by_relative_folder(self):
        chi_folder = self.source / "20260725" / "CHI batch"
        corrtest_folder = self.source / "20260725" / "CorrTest batch"
        chi_folder.mkdir(parents=True)
        corrtest_folder.mkdir(parents=True)
        (chi_folder / "S2CV.txt").write_bytes(
            (ROOT / "demo_data" / "chi_cv_demo.txt").read_bytes()
        )
        (corrtest_folder / "her-eis.z60").write_bytes(
            (ROOT / "demo_data" / "corrtest_eis_demo.z60").read_bytes()
        )
        database, scanner = self.make_scanner()
        scanner.scan()
        runtime = APP.RuntimeState(
            database,
            scanner,
            APP.load_config(ROOT / "config.json"),
        )

        tree = runtime.file_tree()
        payload = json.dumps(tree, ensure_ascii=False)
        files = tree["roots"][0]["files"]

        self.assertEqual(tree["total"], 2)
        self.assertFalse(tree["truncated"])
        self.assertEqual(
            {item["instrument"] for item in files},
            {"CHI", "CorrTest"},
        )
        self.assertEqual(
            {item["relative_path"] for item in files},
            {
                "20260725/CHI batch/S2CV.txt",
                "20260725/CorrTest batch/her-eis.z60",
            },
        )
        self.assertNotIn(str(self.source), payload)
        self.assertNotIn("source_path", payload)


class ProtocolDraftDatabaseTests(unittest.TestCase):
    def test_draft_is_persisted_with_revision_without_requiring_valid_protocol(self):
        with tempfile.TemporaryDirectory() as temporary:
            database_path = Path(temporary) / "state.sqlite3"
            database = APP.Database(database_path)
            draft = database.save_protocol_draft(
                "draft-main",
                {"schema_version": 1, "name": "", "steps": []},
                "D:/EchemPlatform/Runs/RUN-DRAFT-001",
                "D:/EchemPlatform/Runs",
            )
            self.assertEqual(draft["revision"], 1)
            self.assertEqual(draft["protocol"]["steps"], [])

            reopened = APP.Database(database_path)
            updated = reopened.save_protocol_draft(
                "draft-main",
                {"schema_version": 1, "name": "恢复测试", "steps": []},
                "D:/EchemPlatform/Runs/RUN-DRAFT-002",
                "D:/EchemPlatform/Runs",
            )
            self.assertEqual(updated["revision"], 2)
            self.assertEqual(
                reopened.get_protocol_draft("draft-main")["protocol"]["name"],
                "恢复测试",
            )
            self.assertEqual(len(reopened.list_protocol_drafts()), 1)


class ProtocolApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database_path = self.root / "state.sqlite3"
        self.database = APP.Database(self.database_path)

    def tearDown(self):
        self.temp.cleanup()

    def test_dry_run_remains_offline_and_control_routes_are_config_gated(self):
        capabilities = APP.dry_run_capabilities()
        self.assertEqual(capabilities["stage"], "web_dry_run")
        self.assertFalse(capabilities["instrument_control_enabled"])
        self.assertFalse(capabilities["launch_available"])
        self.assertFalse(capabilities["serial_access"])
        handler_source = inspect.getsource(APP.create_handler)
        self.assertIn('path == "/api/control/capabilities"', handler_source)
        self.assertIn('path == "/api/control/runs"', handler_source)
        self.assertIn('runtime.config["instrument_control_enabled"]', handler_source)
        self.assertNotIn("/start", handler_source)

    def test_draft_save_list_and_restore(self):
        draft = default_dry_run_draft()
        saved = self.database.save_protocol_draft(
            draft["id"],
            draft["protocol"],
            draft["output_folder"],
            draft["allowed_run_root"],
        )
        self.assertEqual(saved["revision"], 1)
        self.assertEqual(saved["protocol"]["name"], draft["protocol"]["name"])
        listed = self.database.list_protocol_drafts()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["id"], "draft-main")
        restored = self.database.get_protocol_draft("draft-main")
        self.assertEqual(restored["output_folder"], draft["output_folder"])

    def test_validate_and_compile_preview_never_write_or_start(self):
        draft = default_dry_run_draft()
        request_payload = {
            "protocol": draft["protocol"],
            "output_folder": draft["output_folder"],
            "allowed_run_root": draft["allowed_run_root"],
        }
        validation = APP.build_dry_run(
            request_payload,
            include_macro_preview=False,
        )
        self.assertEqual(validation["mode"], "validation")
        self.assertNotIn("macro_preview", validation)

        preview = APP.build_dry_run(
            request_payload,
            include_macro_preview=True,
        )
        self.assertEqual(preview["mode"], "compile_preview")
        self.assertTrue(preview["macro_preview"].startswith("folder:"))
        self.assertFalse(preview["macro_written"])
        self.assertFalse(preview["instrument_started"])
        self.assertFalse(preview["launch_available"])
        self.assertFalse(preview["serial_access"])
        self.assertFalse(preview["network_control"])

    def test_validation_errors_are_structured(self):
        draft = default_dry_run_draft()
        draft["protocol"]["steps"][0]["params"]["unknown_parameter"] = 1
        with self.assertRaises(APP.ProtocolValidationError) as raised:
            APP.build_dry_run(
                {
                    "protocol": draft["protocol"],
                    "output_folder": draft["output_folder"],
                    "allowed_run_root": draft["allowed_run_root"],
                },
                include_macro_preview=False,
            )
        payload = raised.exception.to_dict()
        self.assertEqual(payload["error"], "protocol_validation_failed")
        self.assertTrue(
            any(issue["code"] == "unknown_field" for issue in payload["issues"])
        )

    def test_workspace_modules_are_served_with_restrictive_headers(self):
        body = (ROOT / "static" / "protocol.html").read_text(encoding="utf-8")
        handler_source = inspect.getsource(APP.create_handler)
        self.assertIn("工步设置", body)
        self.assertIn('"/steps"', handler_source)
        self.assertIn('"/analysis"', handler_source)
        self.assertIn('"/environment"', handler_source)
        self.assertIn('"/api/files/tree"', handler_source)
        self.assertIn("Content-Security-Policy", handler_source)
        self.assertIn("X-Content-Type-Options", handler_source)

    def test_stage_c_capability_does_not_unlock_dry_run_page(self):
        runtime = APP.RuntimeState(
            self.database,
            APP.Scanner(
                database=self.database,
                roots=[],
                extensions=[".txt"],
                max_file_bytes=1024 * 1024,
                max_points=2000,
                stable_age_seconds=0,
            ),
            APP.load_config(ROOT / "config.json"),
        )
        runtime.config["instrument_control_enabled"] = True

        capabilities = runtime.control_capabilities()

        self.assertTrue(capabilities["instrument_control_enabled"])
        self.assertFalse(
            capabilities["dry_run"]["instrument_control_enabled"]
        )
        self.assertFalse(capabilities["dry_run"]["launch_available"])
        self.assertIn("default_draft", capabilities["dry_run"])


class FixtureValidationTests(unittest.TestCase):
    def test_missing_manifest_error_does_not_disclose_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "sensitive-project-name.json"
            with self.assertRaises(ManifestError) as raised:
                validate_fixture_manifest(missing)
            self.assertNotIn(str(missing), str(raised.exception))

    def test_private_manifest_validates_read_only_without_disclosing_paths_or_names(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            chi_dir = root / "chi"
            corrtest_dir = root / "corrtest"
            chi_dir.mkdir()
            corrtest_dir.mkdir()
            chi_path = chi_dir / "private-cv-export.txt"
            corrtest_path = corrtest_dir / "private-eis-export.z60"
            chi_payload = (ROOT / "demo_data" / "chi_cv_demo.txt").read_bytes()
            corrtest_payload = (ROOT / "demo_data" / "corrtest_eis_demo.z60").read_bytes()
            chi_path.write_bytes(chi_payload)
            corrtest_path.write_bytes(corrtest_payload)
            before = {
                path: (path.stat().st_size, path.stat().st_mtime_ns, path.read_bytes())
                for path in (chi_path, corrtest_path)
            }
            manifest_path = root / "manifest.local.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "max_points_per_curve": 2000,
                        "cases": [
                            {
                                "id": "chi-cv-01",
                                "file": "chi/private-cv-export.txt",
                                "expected": {
                                    "status": "parsed",
                                    "instrument": "CHI",
                                    "technique": "CV",
                                    "parser_id": "chi.delimited_text",
                                    "point_count": 29,
                                    "x_name": "Potential/V",
                                    "y_name": "Current/A",
                                },
                            },
                            {
                                "id": "corrtest-eis-01",
                                "file": "corrtest/private-eis-export.z60",
                                "expected": {
                                    "status": "parsed",
                                    "instrument": "CorrTest",
                                    "technique": "EIS",
                                    "parser_id": "corrtest.z60_text",
                                    "point_count": 15,
                                    "x_name": "Zreal(ohm)",
                                    "y_name": "Zimag(ohm)",
                                },
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            report = validate_fixture_manifest(manifest_path)

            self.assertTrue(report["passed"])
            self.assertTrue(report["read_only"])
            self.assertEqual(report["summary"], {"total": 2, "passed": 2, "failed": 0})
            self.assertTrue(all(case["source_unchanged"] for case in report["cases"]))
            serialized = json.dumps(report, ensure_ascii=False)
            self.assertNotIn(str(root), serialized)
            self.assertNotIn("private-cv-export.txt", serialized)
            self.assertNotIn("private-eis-export.z60", serialized)
            self.assertNotIn("DEMO-NiMo-01", serialized)
            for path, snapshot in before.items():
                self.assertEqual(
                    (path.stat().st_size, path.stat().st_mtime_ns, path.read_bytes()),
                    snapshot,
                )

    def test_private_manifest_rejects_parent_directory_escape(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private_root = root / "private"
            private_root.mkdir()
            manifest_path = private_root / "manifest.local.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "cases": [
                            {
                                "id": "unsafe-01",
                                "file": "../outside.txt",
                                "expected": {
                                    "status": "parsed",
                                    "instrument": "CHI",
                                    "technique": "CV",
                                    "parser_id": "chi.delimited_text",
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(ManifestError):
                validate_fixture_manifest(manifest_path)

    def test_private_manifest_reports_assertion_mismatch_without_source_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture_path = root / "cv-export.txt"
            fixture_path.write_bytes((ROOT / "demo_data" / "chi_cv_demo.txt").read_bytes())
            manifest_path = root / "manifest.local.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "cases": [
                            {
                                "id": "chi-cv-mismatch",
                                "file": "cv-export.txt",
                                "expected": {
                                    "status": "parsed",
                                    "instrument": "CHI",
                                    "technique": "CV",
                                    "parser_id": "chi.delimited_text",
                                    "point_count": 999,
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            report = validate_fixture_manifest(manifest_path)

            self.assertFalse(report["passed"])
            self.assertEqual(report["summary"]["failed"], 1)
            self.assertIn("point_count 不符合清单预期。", report["cases"][0]["errors"])
            self.assertNotIn(str(root), json.dumps(report, ensure_ascii=False))


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
