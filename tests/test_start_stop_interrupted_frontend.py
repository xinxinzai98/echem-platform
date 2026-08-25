from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"


class StartStopInterruptedFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.page = (STATIC / "start-stop-config.html").read_text(encoding="utf-8")
        cls.config_script = (STATIC / "start-stop-config.js").read_text(
            encoding="utf-8"
        )
        cls.materials_script = (STATIC / "start-stop-materials.js").read_text(
            encoding="utf-8"
        )

    def test_interrupted_jobs_have_explicit_desktop_messages(self) -> None:
        for marker in (
            'interrupted: job.message || "服务重启导致任务中断，可重新执行"',
            'interrupted: "已中断，可重新执行"',
            'payload.job?.status === "interrupted"',
            "可使用页面上方按钮重新执行",
        ):
            self.assertIn(marker, self.config_script)
        self.assertIn('status.job?.status === "interrupted"', self.materials_script)
        self.assertIn("可回到运行配置页重新执行", self.materials_script)

    def test_live_progress_regions_are_not_nested_in_aria_busy_panel(self) -> None:
        panel_tag = self.page.split('id="jobProgressPanel"', 1)[1].split(">", 1)[0]
        self.assertNotIn("aria-busy", panel_tag)
        self.assertNotIn(
            'jobProgressPanel.setAttribute("aria-busy"',
            self.config_script,
        )
        self.assertIn('id="jobProgressState"', self.page)
        self.assertIn('id="jobProgressAnnouncement"', self.page)


if __name__ == "__main__":
    unittest.main()
