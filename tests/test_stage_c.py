from __future__ import annotations

import datetime as dt
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import app as APP
from echem_platform.configuration import load_config
from echem_platform.control import (
    REQUIRED_CONFIRMATIONS,
    ControlSafetyError,
    ProcessInfo,
    ProcessObservation,
    StageCManager,
    build_stage_c_preflight,
    stage_c_profile_sha256,
)


def ocp_protocol(*, duration_s: float = 60) -> dict:
    return {
        "schema_version": 1,
        "name": "Stage C OCP safety check",
        "sample_id": "PRIVATE-STAGE-C",
        "reference": "REVIEW-REQUIRED",
        "execution_mode": "single_macro",
        "steps": [
            {
                "id": "step-ocp",
                "name": "60 s OCP",
                "technique": "ocp",
                "enabled": True,
                "save_basename": "OCP_60S",
                "params": {
                    "duration_s": duration_s,
                    "sample_interval_s": 0.1,
                    "quiet_time_s": 0,
                    "upper_limit_v": 2,
                    "lower_limit_v": -2,
                },
            }
        ],
    }


class FakeAdapter:
    supported = True

    def __init__(self) -> None:
        self.processes: list[ProcessInfo] = []
        self.launch_calls: list[tuple[Path, Path, Path]] = []
        self.observations: dict[int, ProcessObservation] = {}
        self.next_pid = 4242

    def list_chi_processes(self) -> list[ProcessInfo]:
        return list(self.processes)

    def launch(self, executable: Path, working_directory: Path, macro_path: Path) -> int:
        self.launch_calls.append((executable, working_directory, macro_path))
        self.processes = [ProcessInfo(self.next_pid, "chi760e.exe")]
        self.observations[self.next_pid] = ProcessObservation(True, None)
        return self.next_pid

    def poll(self, pid: int) -> ProcessObservation:
        return self.observations[pid]


class StageCHarness:
    def __init__(self, root: Path, *, enabled: bool = True) -> None:
        self.root = root
        self.executable_dir = root / "chi"
        self.executable_dir.mkdir()
        self.executable = self.executable_dir / "chi760e.exe"
        self.executable.write_bytes(b"verified-chi-executable")
        self.control_root = root / "control"
        self.control_root.mkdir()
        self.run_root = root / "runs"
        self.database = APP.Database(root / "state.sqlite3")
        self.adapter = FakeAdapter()
        self.protocol = ocp_protocol()
        self.config = {
            "bind": "127.0.0.1",
            "instrument_control_enabled": enabled,
            "control_stage": "ocp_60s" if enabled else "off",
            "chi_executable": str(self.executable),
            "chi_executable_sha256": hashlib.sha256(
                self.executable.read_bytes()
            ).hexdigest(),
            "chi_working_directory": str(self.executable_dir),
            "control_root": str(self.control_root),
            "run_root": "D:/data/yx/Runs",
            "stage_c_ocp_profile_sha256": stage_c_profile_sha256(self.protocol),
            "arm_token_ttl_seconds": 300,
            "completion_grace_seconds": 5,
            "stable_age_seconds": 0,
        }

    def manager(self, **kwargs) -> StageCManager:
        return StageCManager(
            self.database,
            self.config,
            adapter=self.adapter,
            run_root_path=self.run_root,
            **kwargs,
        )


def confirmations() -> dict[str, bool]:
    return {key: True for key in REQUIRED_CONFIRMATIONS}


class StageCProtocolTests(unittest.TestCase):
    def test_profile_hash_excludes_sample_metadata_but_locks_parameters(self):
        first = ocp_protocol()
        second = ocp_protocol()
        second["sample_id"] = "ANOTHER-SAMPLE"
        second["steps"][0]["name"] = "Another display label"
        self.assertEqual(
            stage_c_profile_sha256(first),
            stage_c_profile_sha256(second),
        )
        second["steps"][0]["params"]["sample_interval_s"] = 1
        self.assertNotEqual(
            stage_c_profile_sha256(first),
            stage_c_profile_sha256(second),
        )

    def test_stage_c_rejects_non_60_second_or_multi_step_protocol(self):
        with self.assertRaises(ControlSafetyError) as raised:
            stage_c_profile_sha256(ocp_protocol(duration_s=61))
        self.assertEqual(raised.exception.code, "stage_c_duration")

        protocol = ocp_protocol()
        protocol["steps"].append(
            {
                **protocol["steps"][0],
                "id": "second-ocp",
                "save_basename": "OCP_SECOND",
            }
        )
        with self.assertRaises(ControlSafetyError) as raised:
            stage_c_profile_sha256(protocol)
        self.assertEqual(raised.exception.code, "stage_c_scope")


class StageCPreflightTests(unittest.TestCase):
    def test_preflight_is_read_only_and_blocks_existing_chi_instances(self):
        with tempfile.TemporaryDirectory() as temporary:
            harness = StageCHarness(Path(temporary))
            harness.adapter.processes = [
                ProcessInfo(101, "chi760e.exe"),
                ProcessInfo(202, "chi760e.exe"),
            ]
            self.assertFalse(harness.run_root.exists())

            report = build_stage_c_preflight(
                harness.config,
                adapter=harness.adapter,
                active_run_count=0,
                run_root_path=harness.run_root,
            )

            self.assertFalse(report["ready"])
            self.assertEqual(report["chi_process_count"], 2)
            self.assertEqual(report["chi_pids"], [101, 202])
            self.assertIn("single_instance_clear", report["blocked_checks"])
            self.assertFalse(report["writes_performed"])
            self.assertFalse(report["instrument_started"])
            self.assertFalse(harness.run_root.exists())

    def test_disabled_control_blocks_run_creation_before_any_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            harness = StageCHarness(Path(temporary), enabled=False)
            manager = harness.manager()
            with self.assertRaises(ControlSafetyError) as raised:
                manager.create_run(harness.protocol)
            self.assertEqual(raised.exception.code, "control_disabled")
            self.assertEqual(raised.exception.status, 404)
            self.assertFalse(harness.run_root.exists())
            self.assertEqual(harness.adapter.launch_calls, [])


class StageCRunLifecycleTests(unittest.TestCase):
    def test_snapshot_is_written_once_without_launching_instrument(self):
        with tempfile.TemporaryDirectory() as temporary:
            harness = StageCHarness(Path(temporary))
            manager = harness.manager()

            run = manager.create_run(harness.protocol)

            run_directory = Path(run["run_directory"])
            self.assertEqual(run["status"], "prepared")
            self.assertEqual(harness.adapter.launch_calls, [])
            self.assertEqual(
                {path.name for path in run_directory.iterdir()},
                {
                    "events.jsonl",
                    "manifest.json",
                    "protocol.json",
                    "protocol.mcr",
                    "protocol.normalized.json",
                },
            )
            self.assertTrue((run_directory / "protocol.mcr").read_bytes().startswith(
                bytes((0x43, 0x02, 0x00, 0x00, 0x0A))
            ))
            manifest = json.loads(
                (run_directory / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertFalse(manifest["instrument_started"])
            self.assertEqual(manifest["expected_outputs"], ["OCP_60S.bin", "OCP_60S.txt"])

    def test_tampered_snapshot_cannot_be_armed(self):
        with tempfile.TemporaryDirectory() as temporary:
            harness = StageCHarness(Path(temporary))
            manager = harness.manager()
            run = manager.create_run(harness.protocol)
            macro_path = Path(run["macro_path"])
            macro_path.write_bytes(macro_path.read_bytes() + b"tampered")

            with self.assertRaises(ControlSafetyError) as raised:
                manager.arm(run["run_id"], confirmations(), run["run_id"])

            self.assertEqual(raised.exception.code, "snapshot_integrity")
            self.assertEqual(harness.adapter.launch_calls, [])

    def test_start_rechecks_processes_and_consumes_one_time_arm(self):
        with tempfile.TemporaryDirectory() as temporary:
            harness = StageCHarness(Path(temporary))
            manager = harness.manager()
            run = manager.create_run(harness.protocol)
            armed = manager.arm(run["run_id"], confirmations(), run["run_id"])
            harness.adapter.processes = [ProcessInfo(999, "chi760e.exe")]

            with self.assertRaises(ControlSafetyError) as raised:
                manager.start(run["run_id"], armed["arm_token"])

            self.assertEqual(raised.exception.code, "preflight_blocked")
            self.assertEqual(harness.adapter.launch_calls, [])
            stored = harness.database.get_automation_run(
                run["run_id"],
                include_secret=True,
            )
            self.assertEqual(stored["status"], "prepared")
            self.assertEqual(stored["arm_token_sha256"], "")

    def test_database_global_lock_blocks_a_second_platform_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            harness = StageCHarness(Path(temporary))
            manager = harness.manager()
            run = manager.create_run(harness.protocol)
            armed = manager.arm(run["run_id"], confirmations(), run["run_id"])
            self.assertTrue(
                harness.database.acquire_automation_control_lock(
                    "RUN-OTHER-PROCESS",
                    "other-owner",
                )
            )

            with self.assertRaises(ControlSafetyError) as raised:
                manager.start(run["run_id"], armed["arm_token"])

            self.assertEqual(raised.exception.code, "preflight_blocked")
            self.assertIn(
                "global_lock_clear",
                manager.preflight()["blocked_checks"],
            )
            self.assertEqual(harness.adapter.launch_calls, [])

    def test_success_requires_zero_exit_stable_pair_parse_and_unchanged_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            harness = StageCHarness(Path(temporary))

            def importer(binary_path: Path, text_path: Path) -> dict:
                self.assertEqual(binary_path.read_bytes(), b"binary-output")
                self.assertIn(b"Time/s", text_path.read_bytes())
                return {
                    "binary_run_id": 11,
                    "text_run_id": 12,
                    "parse_status": "parsed",
                }

            manager = harness.manager(output_importer=importer)
            run = manager.create_run(harness.protocol)
            armed = manager.arm(run["run_id"], confirmations(), run["run_id"])
            running = manager.start(run["run_id"], armed["arm_token"])
            self.assertEqual(running["status"], "running")
            self.assertEqual(len(harness.adapter.launch_calls), 1)

            run_directory = Path(run["run_directory"])
            (run_directory / "OCP_60S.bin").write_bytes(b"binary-output")
            (run_directory / "OCP_60S.txt").write_bytes(
                b"Time/s,Potential/V\n0,-0.7\n60,-0.71\n"
            )
            harness.adapter.processes = []
            harness.adapter.observations[4242] = ProcessObservation(False, 0)

            manager.poll_all()
            manager.poll_all()

            completed = harness.database.get_automation_run(run["run_id"])
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(completed["chi_exit_code"], 0)
            self.assertTrue(completed["completion_confirmed"])
            step = completed["steps"][0]
            self.assertEqual(step["data_status"], "parsed")
            self.assertEqual(step["binary_run_id"], 11)
            self.assertEqual(step["text_run_id"], 12)
            self.assertTrue(step["source_unchanged"])
            self.assertTrue(step["binary_sha256"])
            self.assertTrue(step["text_sha256"])
            self.assertIsNone(harness.database.automation_control_lock())

    def test_platform_restart_invalidates_arm_token(self):
        with tempfile.TemporaryDirectory() as temporary:
            harness = StageCHarness(Path(temporary))
            first = harness.manager()
            run = first.create_run(harness.protocol)
            first.arm(run["run_id"], confirmations(), run["run_id"])

            harness.manager()

            stored = harness.database.get_automation_run(
                run["run_id"],
                include_secret=True,
            )
            self.assertEqual(stored["status"], "prepared")
            self.assertEqual(stored["arm_token_sha256"], "")
            event_types = [
                event["event_type"]
                for event in harness.database.automation_events(run["run_id"])
            ]
            self.assertIn("arm_invalidated_restart", event_types)


class StageCConfigurationTests(unittest.TestCase):
    def test_only_untracked_local_override_can_enable_control(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = root / "config.json"
            local = root / "config.local.json"
            base.write_text(
                json.dumps(
                    {
                        "bind": "127.0.0.1",
                        "instrument_control_enabled": False,
                        "control_stage": "off",
                        "watch_roots": [],
                        "extensions": [".txt"],
                    }
                ),
                encoding="utf-8",
            )
            local.write_text(
                json.dumps(
                    {
                        "instrument_control_enabled": True,
                        "control_stage": "ocp_60s",
                        "chi_executable": "C:\\chi\\chi760e.exe",
                        "chi_executable_sha256": "a" * 64,
                        "chi_working_directory": "C:\\chi",
                        "control_root": "D:\\control",
                        "run_root": "D:\\runs",
                        "stage_c_ocp_profile_sha256": "b" * 64,
                    }
                ),
                encoding="utf-8",
            )

            config = load_config(base, local_path=local)

            self.assertTrue(config["instrument_control_enabled"])
            self.assertTrue(config["local_override_active"])
            self.assertEqual(config["control_stage"], "ocp_60s")


if __name__ == "__main__":
    unittest.main()
