from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from echem_platform.start_stop_lanbts import (
    LanbtsConfigConflict,
    LanbtsMonitor,
    LanbtsRunConfigStore,
    LanbtsStateFile,
    _duration_seconds,
    _protocol,
    lanbts_probe_script,
    load_lanbts_machine_config,
)


def machine_config(root: Path) -> dict:
    return {
        "version": 1,
        "id": "lanbts-144",
        "name": "蓝博八通道",
        "hostname": "AGHID-P",
        "ip": "192.168.110.144",
        "user": "private-user",
        "identity_file": str(root / "private-key"),
        "known_hosts_file": str(root / "known-hosts"),
        "installation_root": "D:\\LANBTS",
        "data_root": "D:\\LANBTS\\Data",
        "system_root": "D:\\LANBTS\\LANBTSSystem",
        "device_id": "2426577833",
        "box_id": "1",
    }


def process(discharge: float, charge: float | None, minutes: float) -> list[dict]:
    rows = [
        {
            "stepnum": "1",
            "stepname": f"恒流放电     :{discharge:g} mA",
            "finishconditon1": f"步骤时间 ≥ {minutes:02g}:00",
        }
    ]
    if charge is not None:
        rows.append(
            {
                "stepnum": "2",
                "stepname": f"恒流充电     :{charge:g} mA",
                "finishconditon1": f"步骤时间 ≥ {minutes:02g}:00",
            }
        )
    return rows


def probe_payload(*, data_file: str = "1_NMP_chanxian_COM3_1_2_20260831094808.bts") -> dict:
    return {
        "ok": True,
        "checked_at_utc": "2026-08-31T07:44:20Z",
        "computer_name": "AGHID-P",
        "software_running": True,
        "software_version": "3.16.6.230",
        "files": {"total": 21, "total_bytes": 60_655_976, "active": 2, "inactive": 19},
        "channels": [
            {
                "channel": 2,
                "state": "ConstElectricityDischarge",
                "last_state": "ConstElectricityCharge",
                "voltage_v": -1.56168628,
                "current_ma": -300.0,
                "target_ma": 300.0,
                "step_no": 1,
                "loop_count": 36,
                "elapsed_s": 21_152.09,
                "step_elapsed_s": 52.0,
                "test_start_local": "2026-08-31T09:48:08.1500000",
                "data_timestamp_local": "2026-08-31T15:44:13.2400000",
                "data_path": f"D:\\LANBTS\\Data\\xy\\{data_file}",
                "data_file": data_file,
                "data_size_bytes": 3_558_392,
                "data_last_write_utc": "2026-08-31T07:44:13Z",
                "plan_file": "300-30-5min.plan",
                "processes": process(300, 30, 5),
            },
            {
                "channel": 6,
                "state": "ConstElectricityDischarge",
                "last_state": "ConstElectricityDischarge",
                "voltage_v": -1.74581528,
                "current_ma": -499.9752,
                "target_ma": 500.0,
                "step_no": 1,
                "loop_count": 1,
                "elapsed_s": 16_403.13,
                "step_elapsed_s": 16_403.13,
                "test_start_local": "2026-08-31T11:10:51.3600000",
                "data_timestamp_local": "2026-08-31T15:44:17.4900000",
                "data_path": "D:\\LANBTS\\Data\\xy\\1_NMP_long_COM3_1_6_20260831111051.bts",
                "data_file": "1_NMP_long_COM3_1_6_20260831111051.bts",
                "data_size_bytes": 2_251_896,
                "data_last_write_utc": "2026-08-31T07:44:17Z",
                "plan_file": "500.plan",
                "processes": [
                    {
                        "stepnum": "1",
                        "stepname": "恒流放电     :500 mA",
                        "finishconditon1": "步骤时间≥28.16:00:00",
                    }
                ],
            },
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
            raise AssertionError("missing fake LANBTS payload")
        payload = self.__class__.payloads.pop(0)
        if isinstance(payload, Exception):
            raise payload
        return payload


class LanbtsMonitorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state_path = self.root / "shared" / "lanbts-monitor.json"
        self.config_path = self.root / "state" / "lanbts-channel-config.json"
        self.store = LanbtsRunConfigStore(self.config_path)
        FakeTransport.payloads = []
        FakeTransport.scripts = []
        self.monitor = LanbtsMonitor(
            self.state_path,
            active_probe=True,
            machine_config=machine_config(self.root),
            config_store=self.store,
            transport_factory=FakeTransport,
            poll_seconds=30,
        )

    def tearDown(self) -> None:
        self.monitor.close()
        self.temporary.cleanup()

    def full_rows(self, snapshot: dict, *, channel_two_area=None, name="") -> list[dict]:
        rows = []
        for item in snapshot["channels"]:
            rows.append(
                {
                    "channel": item["channel"],
                    "run_id": item["run_id"],
                    "material_name": name if item["channel"] == 2 else "",
                    "electrode_area_cm2": (
                        channel_two_area if item["channel"] == 2 else None
                    ),
                    "notes": "Hg/HgO 待确认" if item["channel"] == 2 else "",
                }
            )
        return rows

    def test_probe_builds_eight_cards_and_classifies_protocols(self) -> None:
        FakeTransport.payloads = [probe_payload()]

        payload = self.monitor._probe_once()

        self.assertEqual(len(payload["channels"]), 8)
        self.assertEqual(payload["counts"]["channels_running"], 2)
        self.assertEqual(payload["counts"]["channels_discharging"], 2)
        self.assertEqual(payload["counts"]["channels_charging"], 0)
        self.assertEqual(payload["counts"]["channels_completed"], 0)
        self.assertEqual(payload["counts"]["channels_idle"], 6)
        by_channel = {item["channel"]: item for item in payload["channels"]}
        self.assertEqual(by_channel[1]["status"], "idle")
        self.assertEqual(by_channel[1]["status_label"], "空置")
        self.assertEqual(by_channel[2]["status"], "discharging")
        self.assertEqual(by_channel[2]["status_label"], "放电")
        self.assertEqual(by_channel[2]["protocol"]["kind"], "bipolar")
        self.assertEqual(by_channel[2]["protocol"]["category"], "反向启停")
        self.assertIn("−300 mA", by_channel[2]["protocol"]["label"])
        self.assertEqual(by_channel[6]["protocol"]["kind"], "constant_current")
        self.assertEqual(by_channel[6]["protocol"]["label"], "−500 mA · 28 d 16 h")
        self.assertIsNone(by_channel[2]["current_density_ma_cm2"])
        self.assertIn("参照未确认", by_channel[2]["voltage_label"])
        self.assertEqual(payload["files"]["total"], 21)
        serialized = json.dumps(payload, ensure_ascii=False)
        for secret in ("private-user", "private-key", "D:\\LANBTS"):
            self.assertNotIn(secret, serialized)

    def test_states_distinguish_empty_discharge_charge_and_completed(self) -> None:
        payload = probe_payload()
        charge = dict(payload["channels"][0])
        charge.update(
            {
                "channel": 4,
                "state": "ConstElectricityCharge",
                "current_ma": 30.0,
                "target_ma": 30.0,
                "data_path": "D:\\LANBTS\\Data\\xy\\1_NMP_charge_COM3_1_4_20260901090000.bts",
                "data_file": "1_NMP_charge_COM3_1_4_20260901090000.bts",
                "test_start_local": "2026-09-01T09:00:00",
            }
        )
        completed = dict(payload["channels"][0])
        completed.update(
            {
                "channel": 5,
                "state": "Finish",
                "current_ma": 0.0,
                "target_ma": 0.0,
                "data_path": "D:\\LANBTS\\Data\\xy\\1_NMP_done_COM3_1_5_20260901080000.bts",
                "data_file": "1_NMP_done_COM3_1_5_20260901080000.bts",
                "test_start_local": "2026-09-01T08:00:00",
            }
        )
        payload["channels"].extend([charge, completed])
        FakeTransport.payloads = [payload]

        result = self.monitor._probe_once()

        by_channel = {item["channel"]: item for item in result["channels"]}
        self.assertEqual(by_channel[1]["status_label"], "空置")
        self.assertEqual(by_channel[2]["status_label"], "放电")
        self.assertEqual(by_channel[4]["status_label"], "充电")
        self.assertEqual(by_channel[5]["status_label"], "测试完成")
        self.assertEqual(by_channel[5]["state_label"], "测试完成")
        self.assertEqual(result["counts"]["channels_discharging"], 2)
        self.assertEqual(result["counts"]["channels_charging"], 1)
        self.assertEqual(result["counts"]["channels_completed"], 1)
        self.assertEqual(result["counts"]["channels_idle"], 4)
        self.assertEqual(result["counts"]["channels_running"], 3)

    def test_area_is_saved_per_run_and_new_run_does_not_inherit_it(self) -> None:
        FakeTransport.payloads = [probe_payload()]
        first = self.monitor._probe_once()

        saved = self.monitor.save_config(
            expected_revision=0,
            channels=self.full_rows(first, channel_two_area=2.0, name="NiMoP-A"),
        )

        self.assertEqual(saved["revision"], 1)
        configured = {item["channel"]: item for item in self.monitor.snapshot()["channels"]}
        self.assertEqual(configured[2]["display_name"], "NiMoP-A")
        self.assertAlmostEqual(configured[2]["current_density_ma_cm2"], -150.0)
        self.assertTrue(configured[2]["configuration"]["complete"])

        FakeTransport.payloads = [
            probe_payload(data_file="1_NMP_newrun_COM3_1_2_20260831170000.bts")
        ]
        second = self.monitor._probe_once()
        new_channel = {item["channel"]: item for item in second["channels"]}[2]
        self.assertNotEqual(new_channel["run_id"], configured[2]["run_id"])
        self.assertEqual(new_channel["configuration"]["material_name"], "")
        self.assertIsNone(new_channel["configuration"]["electrode_area_cm2"])
        self.assertIsNone(new_channel["current_density_ma_cm2"])

        with self.assertRaises(LanbtsConfigConflict):
            self.monitor.save_config(
                expected_revision=1,
                channels=self.full_rows(first, channel_two_area=3.0, name="旧运行"),
            )

    def test_shared_reader_reuses_sanitized_cache_without_ssh(self) -> None:
        FakeTransport.payloads = [probe_payload()]
        written = self.monitor._probe_once()
        reader = LanbtsMonitor(self.state_path, active_probe=False)

        readback = reader.snapshot()

        self.assertEqual(readback["generated_at_utc"], written["generated_at_utc"])
        self.assertEqual(readback["device"]["hostname"], "AGHID-P")
        self.assertFalse(readback["scope"]["instrument_control_performed"])
        self.assertFalse(readback["scope"]["instrument_data_modified"])

    def test_probe_script_limits_writes_to_support_cache(self) -> None:
        script = lanbts_probe_script(machine_config(self.root))

        self.assertIn("LoadBatteryStatusData", script)
        self.assertNotIn("GetProcessData", script)
        self.assertNotIn("GetAllRecordData", script)
        self.assertNotIn("DdaServices]::Initialize", script)
        self.assertIn("[Environment]::Exit(0)", script)
        self.assertIn("start-stop-lanbts-readonly-v1", script)
        self.assertIn("StartStopLanbtsReadOnlyV1", script)
        self.assertIn("Copy-Item -LiteralPath $coreSource", script)
        self.assertIn("Get-ChildItem -LiteralPath $dataRoot", script)
        for forbidden in (
            "Stop-Process",
            "Start-Process",
            "Set-Content",
            "Add-Content",
            "Remove-Item",
            "Move-Item",
            "GetAllRecordData",
            "GetRecordData",
        ):
            self.assertNotIn(forbidden, script)

    def test_lightweight_poll_reuses_cached_protocol_for_same_run(self) -> None:
        FakeTransport.payloads = [probe_payload()]
        first = self.monitor._probe_once()
        next_payload = probe_payload()
        for row in next_payload["channels"]:
            row["processes"] = []
        FakeTransport.payloads = [next_payload]

        second = self.monitor._probe_once()

        first_channel = {item["channel"]: item for item in first["channels"]}[2]
        second_channel = {item["channel"]: item for item in second["channels"]}[2]
        self.assertEqual(first_channel["run_id"], second_channel["run_id"])
        self.assertEqual(second_channel["protocol"], first_channel["protocol"])
        self.assertFalse(second["scope"]["bts_embedded_process_read"])


class LanbtsConfigTests(unittest.TestCase):
    def test_fixed_config_and_duration_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "lanbts.json"
            path.write_text(
                json.dumps(machine_config(root), ensure_ascii=False),
                encoding="utf-8",
            )

            loaded = load_lanbts_machine_config(path)

        self.assertEqual(loaded["ip"], "192.168.110.144")
        self.assertEqual(_duration_seconds("步骤时间 ≥ 05:00"), 300.0)
        self.assertEqual(_duration_seconds("步骤时间≥28.16:00:00"), 2_476_800.0)
        protocol = _protocol(process(500, None, 10))
        self.assertEqual(protocol["kind"], "constant_current")

    def test_state_reader_rejects_wrong_channel_count(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "channels": [{"channel": 1}],
                    }
                ),
                encoding="utf-8",
            )

            self.assertIsNone(LanbtsStateFile(path).read())


if __name__ == "__main__":
    unittest.main()
