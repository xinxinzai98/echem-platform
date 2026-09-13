from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from echem_platform.configuration import load_config
from echem_platform.control import (
    CHI_MACRO_HEADER,
    MacroValidationError,
    ProtocolValidationError,
    build_dry_run,
    compile_protocol,
    default_dry_run_draft,
    dry_run_capabilities,
    inspect_macro,
    normalize_protocol,
)


ROOT = Path(__file__).resolve().parents[1]
RUN_ROOT = "D:/EchemPlatform/Runs"
RUN_FOLDER = "D:/EchemPlatform/Runs/RUN-TEST-001"


def valid_protocol() -> dict:
    return {
        "schema_version": 1,
        "name": "离线编译验证",
        "sample_id": "PRIVATE-SAMPLE-NAME",
        "reference": "TEST-REFERENCE",
        "execution_mode": "single_macro",
        "steps": [
            {
                "id": "step-cv",
                "name": "CV test",
                "technique": "cv",
                "enabled": True,
                "save_basename": "CV_ACT",
                "params": {
                    "initial_v": 0.1,
                    "high_v": 0.1,
                    "low_v": -0.1,
                    "direction": "n",
                    "scan_rate_v_s": 0.01,
                    "cycles": 2,
                    "sample_interval_v": 0.001,
                    "quiet_time_s": 1,
                    "auto_sensitivity": True,
                },
            },
            {
                "id": "step-ocp",
                "name": "OCP test",
                "technique": "ocp",
                "enabled": True,
                "save_basename": "OCP_TEST",
                "params": {
                    "duration_s": 60,
                    "sample_interval_s": 1,
                    "quiet_time_s": 2,
                },
            },
            {
                "id": "step-lsv",
                "name": "LSV test",
                "technique": "lsv",
                "enabled": True,
                "save_basename": "LSV_TEST",
                "params": {
                    "initial_v": 0,
                    "final_v": -0.1,
                    "scan_rate_v_s": 0.001,
                    "sample_interval_v": 0.001,
                    "quiet_time_s": 0,
                    "auto_sensitivity": False,
                    "sensitivity_a_v": 0.1,
                },
            },
            {
                "id": "step-eis",
                "name": "EIS test",
                "technique": "eis",
                "enabled": True,
                "save_basename": "EIS_TEST",
                "params": {
                    "bias_mode": "ocp",
                    "high_frequency_hz": 1000,
                    "low_frequency_hz": 1,
                    "amplitude_v": 0.005,
                    "quiet_time_s": 1,
                },
            },
        ],
    }


class ProtocolValidationTests(unittest.TestCase):
    def test_cv_cycles_are_converted_only_for_clear_endpoint_path(self):
        normalized = normalize_protocol(valid_protocol())
        cv = normalized.steps[0]
        self.assertEqual(cv.params["cycles"], 2)
        self.assertEqual(cv.params["segments"], 4)
        self.assertAlmostEqual(cv.expected_seconds, 81.0)

    def test_ambiguous_cv_cycle_conversion_requires_explicit_segments(self):
        protocol = valid_protocol()
        protocol["steps"][0]["params"]["initial_v"] = 0
        with self.assertRaises(ProtocolValidationError) as raised:
            normalize_protocol(protocol)
        self.assertTrue(
            any(
                issue.code == "ambiguous_cycle_conversion"
                for issue in raised.exception.issues
            )
        )

    def test_duplicate_output_names_are_rejected_case_insensitively(self):
        protocol = valid_protocol()
        protocol["steps"][1]["save_basename"] = "cv_act"
        with self.assertRaises(ProtocolValidationError) as raised:
            normalize_protocol(protocol)
        self.assertTrue(any(issue.code == "duplicate" for issue in raised.exception.issues))

    def test_all_disabled_steps_are_rejected(self):
        protocol = valid_protocol()
        for step in protocol["steps"]:
            step["enabled"] = False
        with self.assertRaises(ProtocolValidationError) as raised:
            normalize_protocol(protocol)
        self.assertTrue(
            any(issue.code == "no_active_steps" for issue in raised.exception.issues)
        )

    def test_unverified_eis_points_per_decade_field_is_rejected(self):
        protocol = valid_protocol()
        protocol["steps"][3]["params"]["points_per_decade"] = 12
        with self.assertRaises(ProtocolValidationError) as raised:
            normalize_protocol(protocol)
        self.assertTrue(
            any(
                issue.path.endswith("points_per_decade")
                and issue.code == "unknown_field"
                for issue in raised.exception.issues
            )
        )

    def test_user_metadata_is_preserved_in_snapshot_but_not_required_to_be_ascii(self):
        normalized = normalize_protocol(valid_protocol())
        self.assertEqual(normalized.name, "离线编译验证")
        self.assertEqual(normalized.metadata["sample_id"], "PRIVATE-SAMPLE-NAME")


class MacroCompilerTests(unittest.TestCase):
    def setUp(self):
        self.compiled = compile_protocol(
            valid_protocol(),
            output_folder=RUN_FOLDER,
            allowed_run_root=RUN_ROOT,
        )

    def test_compiler_generates_verified_binary_without_user_metadata(self):
        self.assertEqual(self.compiled.payload[:5], CHI_MACRO_HEADER)
        self.assertEqual(self.compiled.inspection.run_count, 4)
        self.assertEqual(
            self.compiled.inspection.techniques,
            ("cv", "ocpt", "lsv", "imp"),
        )
        self.assertIn("cl=4", self.compiled.body)
        self.assertIn("save:CV_ACT\ntsave:CV_ACT", self.compiled.body)
        self.assertNotIn("fileoverride", self.compiled.body.lower())
        self.assertNotIn("cellon", self.compiled.body.lower())
        self.assertNotIn("PRIVATE-SAMPLE-NAME", self.compiled.body)
        self.assertNotIn("离线编译验证", self.compiled.body)
        self.assertEqual(
            hashlib.sha256(self.compiled.payload).hexdigest(),
            self.compiled.macro_sha256,
        )
        self.assertTrue(
            any("Points/Decade" in warning for warning in self.compiled.report()["warnings"])
        )
        self.assertFalse(self.compiled.report()["instrument_started"])
        self.assertFalse(self.compiled.report()["instrument_control_enabled"])

    def test_compilation_is_deterministic(self):
        second = compile_protocol(
            valid_protocol(),
            output_folder=RUN_FOLDER,
            allowed_run_root=RUN_ROOT,
        )
        self.assertEqual(second.payload, self.compiled.payload)
        self.assertEqual(second.protocol_sha256, self.compiled.protocol_sha256)
        self.assertEqual(second.macro_sha256, self.compiled.macro_sha256)

    def test_binary_snapshot_is_stable(self):
        self.assertEqual(len(self.compiled.payload), 481)
        self.assertEqual(
            self.compiled.macro_sha256,
            "692616bf8367e6c843d20c611b0b5908c3985756133889aed79b3397c1c9eede",
        )

    def test_static_inspection_rejects_wrong_header(self):
        tampered = b"\x00" + self.compiled.payload[1:]
        with self.assertRaises(MacroValidationError) as raised:
            inspect_macro(tampered, RUN_ROOT)
        self.assertEqual(raised.exception.code, "macro_header")

    def test_static_inspection_rejects_ascii_control_bytes(self):
        tampered = self.compiled.payload.replace(b"\n\n", b"\n\x0b\n", 1)
        with self.assertRaises(MacroValidationError) as raised:
            inspect_macro(tampered, RUN_ROOT)
        self.assertEqual(raised.exception.code, "macro_encoding")

    def test_static_inspection_rejects_fileoverride(self):
        body = self.compiled.body.replace("abortov", "fileoverride", 1)
        with self.assertRaises(MacroValidationError) as raised:
            inspect_macro(CHI_MACRO_HEADER + body.encode("ascii"), RUN_ROOT)
        self.assertEqual(raised.exception.code, "forbidden_command")

    def test_static_inspection_rejects_unknown_command(self):
        body = self.compiled.body.replace("abortov", "mystery", 1)
        with self.assertRaises(MacroValidationError) as raised:
            inspect_macro(CHI_MACRO_HEADER + body.encode("ascii"), RUN_ROOT)
        self.assertEqual(raised.exception.code, "unknown_command")

    def test_static_inspection_rejects_out_of_range_parameter(self):
        body = self.compiled.body.replace("amp=0.005", "amp=-0.005", 1)
        with self.assertRaises(MacroValidationError) as raised:
            inspect_macro(CHI_MACRO_HEADER + body.encode("ascii"), RUN_ROOT)
        self.assertEqual(raised.exception.code, "numeric_range")

    def test_static_inspection_rejects_missing_save_pair(self):
        body = self.compiled.body.replace("\nsave:CV_ACT\n", "\n", 1)
        with self.assertRaises(MacroValidationError) as raised:
            inspect_macro(CHI_MACRO_HEADER + body.encode("ascii"), RUN_ROOT)
        self.assertEqual(raised.exception.code, "save_sequence")

    def test_static_inspection_rejects_missing_eis_impft(self):
        body = self.compiled.body.replace("\nimpft\n", "\n", 1)
        with self.assertRaises(MacroValidationError) as raised:
            inspect_macro(CHI_MACRO_HEADER + body.encode("ascii"), RUN_ROOT)
        self.assertEqual(raised.exception.code, "eis_commands")

    def test_output_folder_must_be_new_child_of_allowed_root(self):
        with self.assertRaises(MacroValidationError) as raised:
            compile_protocol(
                valid_protocol(),
                output_folder="D:/EchemPlatform/Elsewhere/RUN-001",
                allowed_run_root=RUN_ROOT,
            )
        self.assertEqual(raised.exception.code, "run_root_escape")

    def test_output_folder_rejects_spaces_and_non_ascii(self):
        for folder in (
            "D:/EchemPlatform/Runs/RUN WITH SPACE",
            "D:/EchemPlatform/Runs/实验001",
        ):
            with self.subTest(folder=folder):
                with self.assertRaises(MacroValidationError):
                    compile_protocol(
                        valid_protocol(),
                        output_folder=folder,
                        allowed_run_root=RUN_ROOT,
                    )

    def test_output_folder_rejects_traversal_and_windows_reserved_names(self):
        cases = (
            ("D:/EchemPlatform/Runs/../Elsewhere", "windows_path_component"),
            ("D:/EchemPlatform/Runs/CON", "windows_reserved_name"),
        )
        for folder, expected_code in cases:
            with self.subTest(folder=folder):
                with self.assertRaises(MacroValidationError) as raised:
                    compile_protocol(
                        valid_protocol(),
                        output_folder=folder,
                        allowed_run_root=RUN_ROOT,
                    )
                self.assertEqual(raised.exception.code, expected_code)


class OfflineControlSafetyTests(unittest.TestCase):
    def test_configuration_cannot_enable_instrument_control(self):
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "bind": "127.0.0.1",
                        "instrument_control_enabled": True,
                        "watch_roots": [],
                        "extensions": [".txt"],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                load_config(config_path)

    def test_public_configs_keep_instrument_control_disabled(self):
        for filename in (
            "config.json",
            "config.local.example.json",
            "config.windows.template.json",
        ):
            with self.subTest(filename=filename):
                payload = json.loads((ROOT / filename).read_text(encoding="utf-8"))
                self.assertIs(payload["instrument_control_enabled"], False)

    def test_web_dry_run_capabilities_have_no_launch_surface(self):
        capabilities = dry_run_capabilities()
        self.assertEqual(capabilities["stage"], "web_dry_run")
        self.assertFalse(capabilities["instrument_control_enabled"])
        self.assertFalse(capabilities["instrument_started"])
        self.assertFalse(capabilities["launch_available"])
        self.assertFalse(capabilities["serial_access"])
        self.assertTrue(capabilities["draft_persistence"])

    def test_web_dry_run_compiles_in_memory_without_writing_or_starting(self):
        draft = default_dry_run_draft()
        protocol = valid_protocol()
        protocol["name"] = "网页预览测试"
        response = build_dry_run(
            {
                "protocol": protocol,
                "output_folder": RUN_FOLDER,
                "allowed_run_root": RUN_ROOT,
            },
            include_macro_preview=True,
        )
        self.assertEqual(response["status"], "valid")
        self.assertEqual(response["mode"], "compile_preview")
        self.assertIn("macro_preview", response)
        self.assertIn("folder: D:/EchemPlatform/Runs/RUN-TEST-001", response["macro_preview"])
        self.assertNotIn("网页预览测试", response["macro_preview"])
        self.assertFalse(response["macro_written"])
        self.assertFalse(response["instrument_started"])
        self.assertFalse(response["launch_available"])
        self.assertFalse(response["serial_access"])
        self.assertFalse(response["network_control"])
        self.assertEqual(len(response["output_files"]), 4)
        self.assertFalse(response["expected_seconds_complete"])
        self.assertEqual(draft["id"], "draft-main")

    def test_web_validation_response_omits_macro_body(self):
        draft = default_dry_run_draft()
        response = build_dry_run(
            {
                "protocol": draft["protocol"],
                "output_folder": draft["output_folder"],
                "allowed_run_root": draft["allowed_run_root"],
            },
            include_macro_preview=False,
        )
        self.assertEqual(response["mode"], "validation")
        self.assertNotIn("macro_preview", response)
        self.assertTrue(response["expected_seconds_complete"])

    def test_web_dry_run_request_rejects_unknown_fields(self):
        draft = default_dry_run_draft()
        with self.assertRaises(ValueError):
            build_dry_run(
                {
                    "protocol": draft["protocol"],
                    "output_folder": draft["output_folder"],
                    "allowed_run_root": draft["allowed_run_root"],
                    "start": True,
                },
                include_macro_preview=True,
            )

    def test_launcher_is_isolated_and_never_controls_serial_or_kills_chi(self):
        control_root = ROOT / "echem_platform" / "control"
        offline_files = (
            control_root / "dry_run.py",
            control_root / "macro_compiler.py",
            control_root / "validation.py",
        )
        forbidden = (
            "import subprocess",
            "from subprocess",
            "import serial",
            "from serial",
            "import socket",
            "from socket",
            "os.startfile",
            "chi760e.exe",
            "/runmacro",
        )
        combined = "\n".join(
            path.read_text(encoding="utf-8").lower() for path in offline_files
        )
        for token in forbidden:
            with self.subTest(token=token):
                self.assertNotIn(token, combined)
        stage_c = (control_root / "stage_c.py").read_text(encoding="utf-8").lower()
        self.assertIn("shell=false", stage_c)
        self.assertIn("/runmacro:", stage_c)
        for token in (
            "import serial",
            "from serial",
            "import socket",
            "from socket",
            ".kill(",
            ".terminate(",
            "stop-process",
            "shell=true",
        ):
            with self.subTest(stage_c_forbidden=token):
                self.assertNotIn(token, stage_c)

    def test_cli_writes_once_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            protocol_path = root / "protocol.json"
            macro_path = root / "protocol.mcr"
            protocol_path.write_text(
                json.dumps(valid_protocol(), ensure_ascii=False),
                encoding="utf-8",
            )
            command = [
                sys.executable,
                str(ROOT / "scripts" / "compile_chi_protocol.py"),
                str(protocol_path),
                "--output-folder",
                RUN_FOLDER,
                "--allowed-run-root",
                RUN_ROOT,
                "--macro-output",
                str(macro_path),
                "--compact",
            ]
            first = subprocess.run(
                command,
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            first_payload = macro_path.read_bytes()
            self.assertTrue(first_payload.startswith(CHI_MACRO_HEADER))
            report = json.loads(first.stdout)
            self.assertTrue(report["macro_written"])
            self.assertFalse(report["instrument_started"])

            second = subprocess.run(
                command,
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(second.returncode, 4)
            self.assertEqual(macro_path.read_bytes(), first_payload)
            self.assertIn("拒绝覆盖", second.stderr)


if __name__ == "__main__":
    unittest.main()
