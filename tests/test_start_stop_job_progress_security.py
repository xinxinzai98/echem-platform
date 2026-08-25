from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "echem_start_stop_job_progress_security_tests",
    ROOT / "start_stop_service.py",
)
assert SPEC and SPEC.loader
SERVICE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SERVICE
SPEC.loader.exec_module(SERVICE)


class StartStopJobProgressSecurityTests(unittest.TestCase):
    def status(self, progress, *, lan_read_only: bool = False):
        return SERVICE.public_status(
            {
                "available": True,
                "execution": {"ready": True},
                "job": {
                    "id": "abc123",
                    "action": "scan",
                    "status": "running",
                    "stage": "collecting_remote",
                    "message": "正在检查实验电脑",
                    "result": {},
                    "progress": progress,
                },
            },
            lan_read_only=lan_read_only,
        )

    def test_local_and_lan_status_expose_only_the_documented_progress_schema(self):
        progress = {
            "mode": "determinate",
            "percent": 42.5,
            "completed": 17,
            "total": 40,
            "unit": "files",
            "phase_index": 3,
            "phase_count": 6,
            "phase_label": "正在下载文件",
            "current_item": r"C:\Users\lab\Desktop\NiMoP\启停.txt",
            "detail": "已完成 17 个文件",
            "identity": "/Users/server/.ssh/id_ed25519",
            "debug": {"traceback": "private"},
            "machines": [
                {
                    "machine_id": "AGHID-G",
                    "name": "AGHID-G",
                    "status": "downloading",
                    "completed": 7,
                    "total": 12,
                    "message": "正在下载数据",
                    "identity": "/run/secrets/ssh-key",
                    "remote_path": r"D:\data\NiMoP\启停.txt",
                    "debug": {"stderr": "private"},
                }
            ],
        }
        expected = {
            "mode": "determinate",
            "percent": 42.5,
            "completed": 17,
            "total": 40,
            "unit": "files",
            "phase_index": 3,
            "phase_count": 6,
            "phase_label": "正在下载文件",
            "current_item": "启停.txt",
            "detail": "已完成 17 个文件",
            "machines": [
                {
                    "machine_id": "AGHID-G",
                    "name": "AGHID-G",
                    "status": "downloading",
                    "completed": 7,
                    "total": 12,
                    "message": "正在下载数据",
                }
            ],
        }

        local = self.status(progress)["job"]["progress"]
        lan = self.status(progress, lan_read_only=True)["job"]["progress"]

        self.assertEqual(local, expected)
        self.assertEqual(lan, expected)
        serialized = json.dumps({"local": local, "lan": lan}, ensure_ascii=False)
        for private in ("C:\\Users", "/Users/server", "/run/secrets", "D:\\data", "identity", "traceback", "stderr"):
            self.assertNotIn(private, serialized)

    def test_current_item_is_reduced_to_a_safe_basename_or_dropped(self):
        windows = self.status(
            {"current_item": r"\\AGHID-G\D$\data\sample\启停2.txt"}
        )["job"]["progress"]
        unix = self.status(
            {"current_item": "/private/server/data/sample/ADT.txt"}
        )["job"]["progress"]
        identity = self.status(
            {"current_item": "IdentityFile=/Users/server/.ssh/id_ed25519"}
        )["job"]["progress"]

        self.assertEqual(windows["current_item"], "启停2.txt")
        self.assertEqual(unix["current_item"], "ADT.txt")
        self.assertNotIn("current_item", identity)

    def test_progress_types_are_whitelisted_and_numbers_are_bounded(self):
        progress = self.status(
            {
                "mode": "script",
                "percent": 130.25,
                "completed": 99,
                "total": 3,
                "unit": {"name": "files"},
                "phase_index": 99,
                "phase_count": 4,
                "phase_label": ["downloading"],
                "detail": "Traceback: /private/server/app.py",
                "machines": [
                    {
                        "id": "machine-1",
                        "name": "AGHID-G",
                        "status": "downloading",
                        "completed": 20,
                        "total": 2,
                        "message": "正常进行",
                    },
                    {
                        "machine_id": "../../private",
                        "name": "/private/server/machine",
                        "status": "shell",
                        "completed": True,
                        "total": "10",
                        "message": {"stderr": "private"},
                        "extra": {"identity": "private"},
                    },
                    "not-an-object",
                ],
            }
        )["job"]["progress"]

        self.assertEqual(progress["percent"], 100)
        self.assertEqual(progress["completed"], 3)
        self.assertEqual(progress["total"], 3)
        self.assertEqual(progress["phase_index"], 4)
        self.assertEqual(progress["phase_count"], 4)
        self.assertNotIn("mode", progress)
        self.assertNotIn("unit", progress)
        self.assertNotIn("phase_label", progress)
        self.assertNotIn("detail", progress)
        self.assertEqual(
            progress["machines"],
            [
                {
                    "machine_id": "machine-1",
                    "name": "AGHID-G",
                    "status": "downloading",
                    "completed": 2,
                    "total": 2,
                    "message": "正常进行",
                }
            ],
        )

    def test_machine_failures_never_expose_internal_error_details(self):
        progress = self.status(
            {
                "machines": [
                    {
                        "machine_id": "AGHID-H",
                        "name": "AGHID-H",
                        "status": "failed",
                        "message": "Traceback /private/server/collector.py identity=/run/key",
                    },
                    {
                        "machine_id": "A-9",
                        "name": "A-9",
                        "status": "unreachable",
                        "message": r"ssh failed at C:\Users\lab\.ssh\id_ed25519",
                    },
                ]
            },
            lan_read_only=True,
        )["job"]["progress"]

        self.assertEqual(
            [machine["message"] for machine in progress["machines"]],
            [
                "该实验机当前未完成，请在服务器本机查看详情。",
                "该实验机当前未完成，请在服务器本机查看详情。",
            ],
        )
        serialized = json.dumps(progress, ensure_ascii=False)
        self.assertNotIn("Traceback", serialized)
        self.assertNotIn("/private", serialized)
        self.assertNotIn("C:\\Users", serialized)
        self.assertNotIn("identity", serialized)

    def test_downloaded_and_ingested_result_counts_are_public_safe_integers(self):
        payload = {
            "available": True,
            "execution": {"ready": True},
            "job": {
                "id": "abc123",
                "action": "scan",
                "status": "completed",
                "stage": "completed",
                "message": "任务已完成",
                "result": {
                    "collection_downloaded": 12,
                    "collection_ingested": 9.0,
                    "output_dir": "/private/server/start-stop-jobs",
                    "remote_path": r"D:\data\sample\启停.txt",
                    "identity": "/Users/server/.ssh/id_ed25519",
                },
            },
        }

        local = SERVICE.public_status(payload, lan_read_only=False)["job"]["result"]
        lan = SERVICE.public_status(payload, lan_read_only=True)["job"]["result"]

        self.assertEqual(
            local,
            {"collection_downloaded": 12, "collection_ingested": 9},
        )
        self.assertEqual(lan, local)
        serialized = json.dumps({"local": local, "lan": lan}, ensure_ascii=False)
        for private in ("/private", "D:\\data", "/Users/server", "identity"):
            self.assertNotIn(private, serialized)

    def test_missing_or_non_object_progress_remains_backward_compatible(self):
        payload = {
            "available": True,
            "execution": {"ready": True},
            "job": {"status": "idle", "stage": "idle", "result": {}},
        }
        missing = SERVICE.public_status(payload, lan_read_only=False)
        invalid = self.status([{"percent": 50}])

        self.assertNotIn("progress", missing["job"])
        self.assertNotIn("progress", invalid["job"])

    def test_render_progress_allows_only_named_count_units(self):
        pages = self.status({"unit": "pages", "completed": 12, "total": 28})[
            "job"
        ]["progress"]
        materials = self.status(
            {"unit": "materials", "completed": 7, "total": 32}
        )["job"]["progress"]
        unknown = self.status({"unit": "commands", "completed": 1, "total": 2})[
            "job"
        ]["progress"]

        self.assertEqual(pages["unit"], "pages")
        self.assertEqual(materials["unit"], "materials")
        self.assertNotIn("unit", unknown)

    def test_historical_duplicate_failure_becomes_safe_actionable_notice(self):
        payload = {
            "available": True,
            "execution": {"ready": True},
            "job": {
                "id": "duplicate-old-job",
                "action": "prepare_upload",
                "status": "failed",
                "stage": "refreshing_material_table",
                "failure_class": "fatal",
                "failure_code": "job_failed",
                "message": (
                    "- 01_machine/private/30s：绘图名称“NiMoP-30s-30s”"
                    "与 01_machine/private/10s 重复；子进程退出码 1"
                ),
                "result": {"uploaded_files": 42},
            },
        }

        for lan_read_only in (False, True):
            with self.subTest(lan_read_only=lan_read_only):
                job = SERVICE.public_status(
                    payload, lan_read_only=lan_read_only
                )["job"]
                self.assertEqual(job["failure_code"], "duplicate_material_name")
                self.assertEqual(
                    job["message"],
                    (
                        "检测到重复的绘图名称：“NiMoP-30s-30s”。"
                        "新数据已保留，请到“材料库”修改完整绘图名称后重试更新。"
                    ),
                )
                self.assertEqual(job["result"]["uploaded_files"], 42)
                serialized = json.dumps(job, ensure_ascii=False)
                self.assertNotIn("01_machine/private", serialized)
                self.assertNotIn("子进程退出码", serialized)


if __name__ == "__main__":
    unittest.main()
