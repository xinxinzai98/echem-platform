from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from echem_platform.start_stop_workstations import (
    WorkstationMonitor,
    WorkstationStateFile,
    infer_task,
    workstation_root_probe_script,
    workstation_signal_script,
)


MACHINE_ID = "01_测试室1_AGHID-G_192.168.110.153"
ROOT_LABEL = "桌面_工艺优化"
MATERIAL_FOLDER = "260820/NMP-N-恒-20mA-30min-脉-80mA-30s-10s-90min"
FILE_PATH = f"{MATERIAL_FOLDER}/qiting_2026_08_23_08_00_00.txt"
MATERIAL_KEY = f"{MACHINE_ID}/{ROOT_LABEL}/{MATERIAL_FOLDER}"


def signal_payload() -> dict:
    return {
        "ok": True,
        "computer_name": "AGHID-G",
        "checked_at_utc": "2026-08-23T00:20:00Z",
        "serial_probe_ok": True,
        "serial_ports": [
            {"device_id": "COM3", "name": "STMicroelectronics Virtual COM Port", "status": "OK"},
            {"device_id": "COM5", "name": "STMicroelectronics Virtual COM Port", "status": "OK"},
        ],
        "processes": [
            {"process_name": "CorrTest.CSStudio", "pid": 1001, "responding": True},
            {"process_name": "CorrTest.CSStudio", "pid": 1002, "responding": True},
        ],
    }


def root_payload(size: int, ticks: int, *, relative_path: str = FILE_PATH) -> dict:
    return {
        "ok": True,
        "checked_at_utc": "2026-08-23T00:20:00Z",
        "scan_mode": "discovery" if size == 100 else "quick",
        "root": {"label": ROOT_LABEL, "exists": True, "scan_ok": True, "observed_files": 1},
        "files": [
            {
                "root_label": ROOT_LABEL,
                "relative_path": relative_path,
                "file_name": Path(relative_path).name,
                "extension": ".txt",
                "size_bytes": size,
                "last_write_ticks": ticks,
                "last_write_utc": "2026-08-23T00:19:50Z",
            }
        ],
    }


class FakeTransport:
    payloads: list[dict | Exception] = []
    scripts: list[str] = []

    def __init__(self, *, known_hosts_file=None):
        self.known_hosts_file = known_hosts_file

    def _run_payload(self, machine, script, *, timeout):
        self.__class__.scripts.append(script)
        if not self.__class__.payloads:
            raise AssertionError("missing fake payload")
        payload = self.__class__.payloads.pop(0)
        if isinstance(payload, Exception):
            raise payload
        return payload


class WorkstationMonitorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state_path = self.root / "shared" / "workstation-monitor.json"
        self.config = {
            "version": 1,
            "collection_config_revision": 4,
            "known_hosts_file": str(self.root / "known_hosts"),
            "extensions": [".txt"],
            "machines": [
                {
                    "id": MACHINE_ID,
                    "name": "测试室1",
                    "hostname": "AGHID-G",
                    "ip": "192.168.110.153",
                    "user": "private-user",
                    "identity_file": str(self.root / "private-key"),
                    "roots": [
                        {
                            "label": ROOT_LABEL,
                            "remote_path": "C:\\Users\\aghid\\Desktop\\工艺优化",
                            "exclude_directories": [],
                        }
                    ],
                }
            ],
        }
        self.materials = {
            "materials": [
                {
                    "key": MATERIAL_KEY,
                    "plot_name": "NMP-N（恒流 20 mA 30 min，脉冲 80 mA 30 s/10 s 90 min）",
                    "auto_name": "NMP-N",
                    "favorite": True,
                }
            ]
        }
        FakeTransport.payloads = []
        FakeTransport.scripts = []
        self.monitor = WorkstationMonitor(
            self.state_path,
            active_probe=True,
            config_provider=lambda: self.config,
            material_provider=lambda: self.materials,
            transport_factory=FakeTransport,
            poll_seconds=30,
            discovery_seconds=300,
        )

    def tearDown(self) -> None:
        self.monitor.close()
        self.temporary.cleanup()

    def test_two_samples_promote_growing_file_to_active_matched_material(self) -> None:
        FakeTransport.payloads = [
            signal_payload(),
            root_payload(100, 1000),
            signal_payload(),
            root_payload(160, 1100),
        ]

        first = self.monitor._probe_once()
        second = self.monitor._probe_once()

        first_activity = first["machines"][0]["material_activities"][0]
        second_activity = second["machines"][0]["material_activities"][0]
        self.assertEqual(first_activity["activity_status"], "recent")
        self.assertEqual(second_activity["activity_status"], "active")
        self.assertEqual(second_activity["material_match"], "matched")
        self.assertEqual(
            second_activity["display_name"],
            "NMP-N（恒流 20 mA 30 min，脉冲 80 mA 30 s/10 s 90 min）",
        )
        self.assertTrue(second_activity["favorite"])
        self.assertIn("恒流 20 mA", second_activity["task"]["task_label"])
        self.assertIn("脉冲 80 mA", second_activity["task"]["task_label"])
        self.assertFalse(second_activity["task"]["phase_identified"])
        self.assertEqual(second["counts"]["stations_total"], 2)
        self.assertEqual(second["counts"]["stations_online"], 2)
        self.assertEqual(second["counts"]["stations_running"], 1)
        self.assertEqual(second["counts"]["stations_idle"], 1)
        self.assertEqual(second["counts"]["active_materials"], 1)
        self.assertEqual(second["machines"][0]["stations"][0]["status"], "running")
        self.assertEqual(second["machines"][0]["stations"][1]["status"], "idle")
        self.assertEqual(
            second["machines"][0]["stations"][0]["assigned_activity"]["display_name"],
            second_activity["display_name"],
        )
        self.assertFalse(
            second["machines"][0]["stations"][0]["physical_station_mapping_confirmed"]
        )
        self.assertEqual(
            second["machines"][0]["stations"][0]["material_assignment"],
            "machine_level_only",
        )

        serialized = json.dumps(second, ensure_ascii=False)
        for secret in ("private-user", "private-key", "C:\\Users\\aghid"):
            self.assertNotIn(secret, serialized)

    def test_shared_reader_returns_same_sanitized_snapshot_without_probing(self) -> None:
        FakeTransport.payloads = [signal_payload(), root_payload(100, 1000)]
        written = self.monitor._probe_once()
        reader = WorkstationMonitor(self.state_path, active_probe=False)

        readback = reader.snapshot()

        self.assertEqual(readback["generated_at_utc"], written["generated_at_utc"])
        self.assertEqual(readback["machines"][0]["hostname"], "AGHID-G")
        self.assertEqual(readback["scope"]["monitoring_mode"], "server_cached_read_only")
        self.assertFalse(readback["scope"]["file_contents_read"])

    def test_unmatched_folder_is_explicitly_marked_for_confirmation(self) -> None:
        path = "260823/NewCatalyst-恒-50mA-30min/启停.txt"
        FakeTransport.payloads = [
            signal_payload(),
            root_payload(100, 1000, relative_path=path),
        ]

        payload = self.monitor._probe_once()
        activity = payload["machines"][0]["material_activities"][0]

        self.assertEqual(activity["material_match"], "unmatched")
        self.assertEqual(activity["display_name"], "NewCatalyst")
        self.assertEqual(activity["confidence"], "folder_inference")

    def test_300_30_5min_suffix_is_a_distinct_start_stop_work_step(self) -> None:
        task = infer_task("徕阳x3-300-30-5min", "启停.txt")

        self.assertEqual(task["inferred_material_name"], "徕阳x3")
        self.assertEqual(
            task["work_step_key"],
            "start_stop|jc=-300|jr=30|tc=5min|tr=5min",
        )
        self.assertIn("−300 ↔ +30 mA·cm⁻²", task["task_label"])
        self.assertIn("5 + 5 min", task["task_label"])
        self.assertEqual(task["cathodic_duration_s"], 300.0)
        self.assertEqual(task["recovery_duration_s"], 300.0)

        process_task = infer_task(
            "NMP-N-恒-20mA-30min-脉-80mA-30s-10s-60min-300-30-5min",
            "qiting.txt",
        )
        self.assertEqual(process_task["inferred_material_name"], "NMP-N")
        self.assertIn("脉冲 80 mA", process_task["task_label"])
        self.assertTrue(process_task["task_label"].endswith("5 + 5 min"))

    def test_one_failed_root_is_partial_without_making_the_pc_offline(self) -> None:
        FakeTransport.payloads = [signal_payload(), ValueError("root failed")]

        payload = self.monitor._probe_once()

        self.assertEqual(payload["status"], "partial")
        self.assertEqual(payload["counts"]["machines_reachable"], 1)
        self.assertEqual(payload["counts"]["roots_failed"], 1)
        self.assertTrue(payload["machines"][0]["reachable"])
        self.assertEqual(payload["machines"][0]["status"], "attention")

    def test_probe_script_keeps_the_read_only_boundary(self) -> None:
        signal = workstation_signal_script()
        root = workstation_root_probe_script(
            root={
                "label": ROOT_LABEL,
                "remote_path": "C:\\Users\\aghid\\Desktop\\工艺优化",
                "exclude_directories": [],
            },
            extensions=[".txt"],
            candidates=[],
            discovery=True,
        )

        self.assertIn("Get-Process", signal)
        self.assertIn("Win32_SerialPort", signal)
        self.assertIn("Get-ChildItem", root)
        for forbidden in (
            "Get-Content",
            "Set-Content",
            "Remove-Item",
            "SerialPort]::new",
            "Win32_Process",
            "CommandLine",
        ):
            self.assertNotIn(forbidden, signal + root)

    def test_task_parser_keeps_phase_unknown_without_file_content(self) -> None:
        task = infer_task(
            "NMP-N-恒-20mA-30min-脉-80mA-30s-10s-90min",
            "qiting_2026_08_23.txt",
        )
        self.assertEqual(task["test_type"], "start_stop")
        self.assertEqual(task["inferred_material_name"], "NMP-N")
        self.assertFalse(task["phase_identified"])

    def test_state_reader_rejects_symlink(self) -> None:
        target = self.root / "target.json"
        target.write_text(json.dumps({"schema_version": 1, "machines": []}), encoding="utf-8")
        link = self.root / "link.json"
        link.symlink_to(target)
        self.assertIsNone(WorkstationStateFile(link).read())


if __name__ == "__main__":
    unittest.main()
