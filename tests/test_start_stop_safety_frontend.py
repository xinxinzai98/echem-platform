from __future__ import annotations

import unittest
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"


class IdParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.by_id: dict[str, dict[str, str | None]] = {}

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        values = dict(attrs)
        if values.get("id"):
            self.by_id[str(values["id"])] = values


class StartStopSafetyFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.page = (STATIC / "start-stop-config.html").read_text(encoding="utf-8")
        cls.script = (STATIC / "start-stop-config.js").read_text(encoding="utf-8")
        cls.css = (STATIC / "start-stop-config.css").read_text(encoding="utf-8")
        cls.parser = IdParser()
        cls.parser.feed(cls.page)

    def test_configuration_page_has_read_only_safety_and_version_region(self) -> None:
        expected_ids = {
            "safetyVersionTitle",
            "safetyOverallState",
            "safetyStatusGrid",
            "safetyStorageCard",
            "safetyStorageState",
            "safetyStoragePrimary",
            "safetyStorageDetail",
            "safetyStorageWarning",
            "safetyBackupCard",
            "safetyBackupState",
            "safetyBackupPrimary",
            "safetyBackupDetail",
            "safetyBackupWarning",
            "safetyProvenanceCard",
            "safetyProvenanceState",
            "safetyProvenancePrimary",
            "safetyProvenanceDetail",
            "safetyAnalysisRun",
            "safetySnapshot",
            "safetyConfigRevision",
            "safetyArtifactGeneration",
            "safetyManifestHash",
            "safetyScriptHash",
            "safetyImageReference",
        }
        self.assertEqual(expected_ids - self.parser.by_id.keys(), set())
        self.assertEqual(self.parser.by_id["safetyOverallState"].get("role"), "status")
        self.assertEqual(self.parser.by_id["safetyOverallState"].get("aria-live"), "polite")
        self.assertEqual(self.parser.by_id["safetyOverallState"].get("aria-atomic"), "true")
        self.assertNotIn("aria-live", self.parser.by_id["safetyStatusGrid"])
        safety_markup = self.page.split('class="panel safety-version-panel"', 1)[1].split(
            'class="panel collection-config-panel"', 1
        )[0]
        self.assertNotIn("<button", safety_markup)
        self.assertNotIn("deleteBackup", safety_markup)
        self.assertNotIn("backupDelete", safety_markup)
        self.assertIn("本区域只显示状态", safety_markup)

    def test_missing_backend_fields_fall_back_without_throwing(self) -> None:
        for marker in (
            "function renderSafetyStatus(safety = null)",
            "renderStorageSafety(safety?.storage)",
            "renderBackupSafety(safety?.backup)",
            "renderProvenanceSafety(safety?.provenance)",
            "renderSafetyStatus(payload?.safety)",
            'renderSafetyStatus();',
            'return "尚未启用";',
        ):
            self.assertIn(marker, self.script)
        safety_script = self.script.split("function safetyHasValue", 1)[1].split(
            "function autoUpdateResultText", 1
        )[0]
        self.assertNotIn("backup_path", safety_script)
        self.assertNotIn("database_path", safety_script)
        self.assertNotIn("backup_dir", safety_script)
        self.assertNotIn("innerHTML", safety_script)

    def test_storage_backup_and_provenance_risks_are_explicit(self) -> None:
        for marker in (
            'storage.preflight_ok === false',
            'shortfall_bytes',
            '当前空间不足以安全完成下一次任务',
            '备份与数据库位于同一磁盘',
            '只能作为快速回滚点',
            '当前清单与文件大小一致',
            '创建时完成 SHA-256 与 SQLite 检查',
            '实际恢复前请再执行完整哈希与 SQLite 核验',
            'backup.destination_preflight_ok === false',
            'backup.policy_mode === "risk_tiered"',
            '普通更新、下载和绘图不做全量备份',
            '数据库迁移或修复前强制全量备份',
            '低频后台执行全量备份',
            '后台全量备份正在运行',
            '最近一次后台全量备份失败',
            'backupCount > 0 && backup.latest_valid === false',
            'waitingForFirstBackup',
            'backupCount < 1',
            'backup.destination_preflight_ok === true',
            'label = "等待首次备份"',
            '备份目标剩余空间不足',
            'storage.blocked_roles',
            '任务临时空间',
            '网页图集缓存',
            'provenanceState === "sealed"',
            'provenanceState === "legacy"',
            'artifact_manifest_sha256',
            'analysis_script_sha256',
            '.slice(0, 12)',
        ):
            self.assertIn(marker, self.script)
        self.assertNotIn("最新备份校验通过", self.script)

    def test_safety_styles_do_not_add_narrow_screen_overrides(self) -> None:
        self.assertIn(".safety-status-grid", self.css)
        self.assertIn("repeat(auto-fit, minmax(290px, 1fr))", self.css)
        narrow_rules = self.css.split("@media (max-width: 1180px)", 1)[1]
        self.assertNotIn(".safety-", narrow_rules)


if __name__ == "__main__":
    unittest.main()
