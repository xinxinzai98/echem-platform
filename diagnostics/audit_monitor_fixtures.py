"""Synthetic, normalized monitor states. All probes use unit-test fake transports."""
import copy
import datetime as dt

from tests.test_start_stop_workstations import WorkstationMonitorTests
from tests.test_start_stop_lanbts import LanbtsMonitorTests


def now_text():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class SnapshotFixture:
    active_probe = False
    config_store = None

    def __init__(self, payload):
        self.payload = payload

    def snapshot(self, **_kwargs):
        payload = copy.deepcopy(self.payload)
        payload["generated_at_utc"] = now_text()
        payload["cache_age_seconds"] = 0
        return payload


def fixtures():
    workstation_test = WorkstationMonitorTests()
    workstation_test.setUp()
    try:
        workstation_test.test_two_samples_promote_growing_file_to_active_matched_material()
        workstations = workstation_test.monitor.snapshot()
    finally:
        workstation_test.tearDown()
    sample = workstations["machines"][0]
    workstations["machines"] = []
    for index in range(3):
        machine = copy.deepcopy(sample)
        machine.update(id=f"audit-pc-{index+1}", name=f"审计样例电脑 {index+1}", hostname=f"AUDIT-{index+1}", ip=f"192.0.2.{index+1}")
        for slot, station in enumerate(machine["stations"]):
            station["id"] = f"audit-pc-{index+1}-slot-{slot+1}"
            activity = station.get("assigned_activity")
            if activity:
                activity["display_name"] = f"审计样例材料 {index+1} · NiMo 恒流与脉冲复合工艺"
                activity["last_write_utc"] = now_text()
        if index == 2:
            machine.update(reachable=False, status="offline", status_label="离线", material_activities=[])
            for station in machine["stations"]:
                station.update(status="offline", online=False, assigned_activity=None, status_label="审计样例：电脑不可达")
        workstations["machines"].append(machine)
    workstations.update(status="partial", message="合成审计样例，不代表真实仪器状态。")
    workstations["counts"].update(machines_total=3, machines_reachable=2, stations_total=6,
        stations_online=4, stations_running=2, stations_idle=2, active_materials=2)

    lanbts_test = LanbtsMonitorTests()
    lanbts_test.setUp()
    try:
        lanbts_test.test_states_distinguish_empty_discharge_charge_and_completed()
        lanbts = lanbts_test.monitor.snapshot()
    finally:
        lanbts_test.tearDown()
    for channel in lanbts["channels"]:
        if channel.get("run_id"):
            channel["display_name"] = f'审计样例蓝博材料 {channel["channel"]}'
            channel["data_timestamp_local"] = dt.datetime.now().isoformat(timespec="seconds")
    error = copy.deepcopy(lanbts["channels"][1])
    error.update(channel=8, status="attention", status_label="读取异常", state_label="读取异常",
        display_name="审计样例：旧数据保留", data_timestamp_local=(dt.datetime.now()-dt.timedelta(minutes=5)).isoformat(timespec="seconds"),
        warnings=["审计样例：最新读取失败，当前数值来自之前的缓存。"])
    lanbts["channels"][7] = error
    lanbts["counts"]["channels_idle"] = 3
    lanbts.update(status="partial", message="合成审计样例，包含放电、充电、完成、空置与读取异常。")

    connectivity = {"available":True, "can_check":False, "total":3,"checked":3,"reachable":2,
        "machines":[{"id":machine["id"],"name":machine["name"],"hostname":machine["hostname"],"ip":machine["ip"],
            "status":"reachable" if index<2 else "unreachable", "checked_at_utc":now_text(),
            "message":"合成审计样例"} for index,machine in enumerate(workstations["machines"])]}

    def live_preview():
        points=[]
        for cycle in range(1000):
            for offset,potential in ((0,-0.8),(29.9,-0.8),(30,-0.2),(59.9,-0.2)):
                points.append([cycle*60+offset,potential-cycle*0.00001])
        return {"available":True,"enabled":True,"last_status":"completed","message":"合成审计预览，不连接仪器。",
            "preview":{"generated_at_utc":now_text(),"items":[{"display_name":"审计样例：高频启停的尾段走势", "machine_name":"审计电脑 1", "hostname":"AUDIT-1",
                "points":points, "complete_row_count":len(points), "phase":"恢复段", "last_potential_v":points[-1][1],"last_current_a_cm2":0.03,
                "time_end_s":points[-1][0],"file_name":"审计样例_启停.txt","captured_at_utc":now_text(),
                "task":{"task_label":"−300 / +30 mA·cm⁻²，30 s / 30 s"}}]}}
    return SnapshotFixture(workstations), SnapshotFixture(lanbts), SnapshotFixture(connectivity), live_preview
