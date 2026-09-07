"""Newly discovered repository materials require explicit inclusion."""
import unittest
from types import SimpleNamespace

from echem_platform.start_stop_materials import MaterialLibraryMixin


class MaterialSelectionTests(unittest.TestCase):
    def workspace(self, *, repository=True, revision=1):
        snapshot = {"dataset_fingerprint": "dataset-new", "materials": [
            {"key": key, "auto_name": key, "fingerprint": "new-" + key}
            for key in ("existing", "excluded", "new")
        ]}
        config = {"revision": revision, "materials": [
            {"material_key": key, "plot_name": "用户名称 " + key,
             "include_in_summary_atlas": include, "favorite": True,
             "source_fingerprint": "old-" + key}
            for key, include in (("existing", True), ("excluded", False))
        ] if revision else []}
        readback = {"dataset_fingerprint": "dataset-new", "materials": [
            {"key": row["key"], "fingerprint": row["fingerprint"],
             "include_in_summary_atlas": True}
            for row in snapshot["materials"]
        ]}
        workspace = MaterialLibraryMixin()
        workspace.repository_mode = repository
        workspace.source_database = SimpleNamespace(
            current_material_catalog=lambda: {"snapshot": snapshot, "readback": readback},
            get_start_stop_config=lambda: config,
        )
        workspace._snapshot = lambda: snapshot
        workspace.READBACK_NAME = "readback"
        workspace._read_json = lambda *args, **kwargs: readback
        return workspace, config

    def test_new_material_not_selected_even_when_generated_workbook_selects_it(self):
        workspace, _ = self.workspace()
        rows = {row["key"]: row for row in workspace.materials()["materials"]}
        self.assertFalse(rows["new"]["include_in_summary_atlas"])
        self.assertEqual(rows["new"]["status"], "新增")
        self.assertTrue(rows["existing"]["include_in_summary_atlas"])
        self.assertTrue(rows["existing"]["favorite"])
        self.assertEqual(rows["existing"]["status"], "数据已更新")
        self.assertFalse(rows["excluded"]["include_in_summary_atlas"])

    def test_new_material_can_be_explicitly_selected(self):
        workspace, config = self.workspace()
        config["materials"].append({"material_key": "new", "plot_name": "已确认材料",
            "include_in_summary_atlas": True, "source_fingerprint": "new-new"})
        rows = {row["key"]: row for row in workspace.materials()["materials"]}
        self.assertTrue(rows["new"]["include_in_summary_atlas"])
        self.assertEqual(rows["new"]["status"], "未变化")

    def test_first_use_preserves_bootstrap_workbook_selection(self):
        workspace, _ = self.workspace(revision=0)
        self.assertEqual(workspace.materials()["counts"]["selected"], 3)

    def test_legacy_workbook_workflow_keeps_explicit_workbook_selection(self):
        workspace, _ = self.workspace(repository=False)
        self.assertEqual(workspace.materials()["counts"]["selected"], 2)
