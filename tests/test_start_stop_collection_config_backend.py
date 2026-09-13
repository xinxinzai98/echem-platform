from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from echem_platform.start_stop import StartStopWorkspace
from echem_platform.start_stop_collection_config import CollectionConfigStore
from echem_platform.start_stop_database import StartStopDatabase


ROOT = Path(__file__).resolve().parents[1]


class CollectionConfigBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = StartStopDatabase(self.root / "start-stop.sqlite3")
        self.base_path = ROOT / "docker" / "collection-config.json"
        self.base = json.loads(self.base_path.read_text(encoding="utf-8"))
        self.store = CollectionConfigStore(self.base_path, self.database)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_effective_config_keeps_fixed_identity_and_base_exclusions(self) -> None:
        initial = self.store.snapshot()
        rows = []
        for machine in initial["machines"]:
            paths = [dict(item) for item in machine["paths"]]
            if machine["hostname"] == "AGHID-G":
                for item in paths:
                    if item["path"].casefold() == r"d:\data\yx".casefold():
                        item["label"] = "原始 yx 目录"
                paths.append(
                    {
                        "label": "用户新增位置",
                        "path": r"E:\electrochemistry\new",
                        "enabled": True,
                    }
                )
            rows.append({"id": machine["id"], "paths": paths})

        saved = self.store.save(
            expected_revision=initial["revision"], machines=rows
        )
        effective = self.store.effective_config()

        self.assertEqual(saved["revision"], 1)
        self.assertEqual(len(effective["machines"]), 3)
        fixed_by_id = {row["id"]: row for row in self.base["machines"]}
        for machine in effective["machines"]:
            fixed = fixed_by_id[machine["id"]]
            for field in ("hostname", "ip", "user", "identity_file"):
                self.assertEqual(machine[field], fixed[field])
        g_machine = next(
            row for row in effective["machines"] if row["hostname"] == "AGHID-G"
        )
        yx = next(
            row
            for row in g_machine["roots"]
            if row["remote_path"].casefold() == r"d:\data\yx".casefold()
        )
        custom = next(
            row
            for row in g_machine["roots"]
            if row["remote_path"].casefold()
            == r"e:\electrochemistry\new".casefold()
        )
        self.assertEqual(yx["exclude_directories"], ["Control_Programs"])
        self.assertEqual(custom["exclude_directories"], [])

        reopened = CollectionConfigStore(self.base_path, StartStopDatabase(self.database.path))
        self.assertEqual(reopened.snapshot()["revision"], 1)
        self.assertEqual(reopened.snapshot()["machines"], saved["machines"])

    def test_workspace_materializes_database_roots_only_into_job_scoped_file(self) -> None:
        initial = self.store.snapshot()
        machines = []
        for machine in initial["machines"]:
            paths = [dict(item) for item in machine["paths"]]
            paths[0]["label"] = "任务配置-" + machine["hostname"]
            machines.append({"id": machine["id"], "paths": paths})
        self.store.save(expected_revision=0, machines=machines)
        workspace = StartStopWorkspace(
            self.database,
            self.root / "published",
            collection_config=self.base_path,
            collection_config_provider=self.store,
            scratch_dir=self.root / "scratch",
            repository_mode=True,
        )
        job_file = self.root / "scratch" / "job" / "effective.json"

        workspace._write_effective_collection_config(job_file)

        payload = json.loads(job_file.read_text(encoding="utf-8"))
        self.assertEqual(len(payload["machines"]), 3)
        self.assertTrue(
            all(
                machine["roots"][0]["label"].startswith("任务配置-")
                for machine in payload["machines"]
            )
        )
        self.assertEqual(
            json.loads(self.base_path.read_text(encoding="utf-8")), self.base
        )


if __name__ == "__main__":
    unittest.main()
