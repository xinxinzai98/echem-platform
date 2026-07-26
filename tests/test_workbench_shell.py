from __future__ import annotations

import unittest
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"


class PageStructureParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []
        self.links: list[dict[str, str | None]] = []
        self.settings_links: list[dict[str, str | None]] = []
        self.stylesheets: list[str] = []
        self.scripts: list[str] = []
        self.sidebar_count = 0

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        values = dict(attrs)
        if values.get("id"):
            self.ids.append(str(values["id"]))
        classes = set(str(values.get("class") or "").split())
        if "workbench-sidebar" in classes:
            self.sidebar_count += 1
        if tag == "a" and "workbench-nav-item" in classes:
            self.links.append(values)
        if tag == "a" and "workbench-settings-button" in classes:
            self.settings_links.append(values)
        if tag == "link" and values.get("rel") == "stylesheet":
            self.stylesheets.append(str(values.get("href") or ""))
        if tag == "script" and values.get("src"):
            self.scripts.append(str(values.get("src") or ""))


class WorkbenchShellTests(unittest.TestCase):
    pages = {
        "index.html": "/",
        "protocol.html": "/steps",
        "analysis.html": "/analysis",
        "monitor.html": None,
    }

    def parse_page(self, name: str) -> PageStructureParser:
        parser = PageStructureParser()
        parser.feed((STATIC / name).read_text(encoding="utf-8"))
        return parser

    def test_every_workspace_page_uses_the_shared_sidebar(self) -> None:
        for name, active_path in self.pages.items():
            with self.subTest(page=name):
                parser = self.parse_page(name)
                self.assertEqual(parser.sidebar_count, 1)
                self.assertIn("/static/workbench.css", parser.stylesheets)
                self.assertEqual(
                    [link.get("href") for link in parser.links],
                    ["/", "/steps", "/analysis"],
                )
                active = [
                    link for link in parser.links
                    if link.get("aria-current") == "page"
                ]
                if active_path:
                    self.assertEqual(len(active), 1)
                    self.assertEqual(active[0].get("href"), active_path)
                else:
                    self.assertEqual(active, [])
                self.assertEqual(
                    [link.get("href") for link in parser.settings_links],
                    ["/environment"],
                )

    def test_environment_page_is_active_only_in_the_corner_settings_dock(self) -> None:
        parser = self.parse_page("monitor.html")
        self.assertEqual(len(parser.settings_links), 1)
        self.assertEqual(
            parser.settings_links[0].get("aria-current"),
            "page",
        )
        self.assertIn("/static/icons/gear.svg", (
            STATIC / "monitor.html"
        ).read_text(encoding="utf-8"))

    def test_dashboard_contains_only_the_three_requested_information_regions(self) -> None:
        dashboard = (STATIC / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="instrumentActivity"', dashboard)
        self.assertIn('class="system-metric-grid"', dashboard)
        self.assertIn('id="recentActivity"', dashboard)
        self.assertNotIn('id="curveChart"', dashboard)
        self.assertNotIn('id="metadataForm"', dashboard)
        self.assertNotIn('id="runList"', dashboard)

    def test_data_analysis_is_a_separate_module(self) -> None:
        parser = self.parse_page("analysis.html")
        self.assertIn('curveChart', parser.ids)
        self.assertIn('fileTree', parser.ids)
        self.assertIn('treeCount', parser.ids)
        self.assertNotIn('metadataForm', parser.ids)
        self.assertNotIn('auditList', parser.ids)
        self.assertNotIn('totalCount', parser.ids)
        self.assertNotIn('parsedCount', parser.ids)
        self.assertNotIn('integrityCount', parser.ids)
        self.assertNotIn('runList', parser.ids)
        self.assertNotIn('watchRoots', parser.ids)
        self.assertIn("/static/analysis.js", parser.scripts)

    def test_analysis_exposes_method_specific_eis_and_cv_workflows(self) -> None:
        parser = self.parse_page("analysis.html")
        page = (STATIC / "analysis.html").read_text(encoding="utf-8")
        script = (STATIC / "analysis.js").read_text(encoding="utf-8")
        for element_id in (
            "methodAnalysisPanel",
            "eisFields",
            "cvFields",
            "cvSolution",
            "cvSolutionCustom",
            "cvPh",
            "cvReference",
            "cvReferenceCustom",
            "cvReferenceOffset",
            "cvCompensation",
            "cvResistance",
            "cvScanBranch",
            "previewAnalysis",
            "saveAnalysis",
        ):
            self.assertIn(element_id, parser.ids)
        self.assertIn("所有条件会随结果保存为参数快照", page)
        self.assertIn("不确定（不允许离线补偿）", page)
        self.assertIn("/analyses/preview", script)
        self.assertIn("/api/runs/${runId}/analyses", script)
        self.assertIn("analysisRequestId", script)
        self.assertIn("analysisHistoryRequestId", script)
        self.assertIn("setAnalysisBusy(true)", script)
        self.assertIn("renderDetailLoading", script)
        self.assertIn("renderDetailFailure", script)
        self.assertIn("eis_resistance", script)
        self.assertIn("cv_overpotential", script)
        self.assertIn('<select id="cvSolution"', page)
        self.assertIn('data-ph="14.00"', page)
        self.assertIn('data-ph="13.00"', page)
        self.assertIn('data-offset="0.098"', page)
        self.assertIn('data-offset="0.197"', page)
        self.assertIn("25 ℃名义值", page)
        self.assertIn("normalizeSolutionLabel", script)
        self.assertIn("syncSolutionPreset", script)
        self.assertIn("selectedSolutionValue", script)
        self.assertIn("selectedReferenceValue", script)
        self.assertIn("syncReferenceOffset", script)
        self.assertIn("cvReferenceOffset.readOnly = !custom", script)
        self.assertNotIn("metadataRequestId", script)
        self.assertNotIn("/api/audit", script)
        self.assertNotIn("/metadata", script)

    def test_analysis_page_omits_deferred_summary_and_context_regions(self) -> None:
        page = (STATIC / "analysis.html").read_text(encoding="utf-8")
        script = (STATIC / "analysis.js").read_text(encoding="utf-8")
        for text in (
            'class="hero-grid"',
            'id="metadataForm"',
            'id="auditList"',
            "样品信息",
            "最近活动",
            "搜索文件夹、文件或样品",
        ):
            self.assertNotIn(text, page)
        self.assertNotIn("file-sample", script)
        self.assertNotIn("loadStatus", script)
        self.assertNotIn("loadAudit", script)

    def test_data_folders_are_managed_from_environment_settings(self) -> None:
        analysis = (STATIC / "analysis.html").read_text(encoding="utf-8")
        monitor = (STATIC / "monitor.html").read_text(encoding="utf-8")
        script = (STATIC / "analysis.js").read_text(encoding="utf-8")
        self.assertNotIn("监控目录", analysis)
        self.assertNotIn('id="instrumentFilter"', analysis)
        self.assertIn('id="dataFolderList"', monitor)
        self.assertIn('id="dataFolderSummary"', monitor)
        self.assertIn("/api/files/tree", script)
        self.assertIn("parser-badge", script)

    def test_workspace_pages_do_not_duplicate_element_ids(self) -> None:
        for name in self.pages:
            with self.subTest(page=name):
                ids = self.parse_page(name).ids
                self.assertEqual(len(ids), len(set(ids)))

    def test_sidebar_is_desktop_sticky_and_content_uses_a_two_column_shell(self) -> None:
        css = (STATIC / "workbench.css").read_text(encoding="utf-8")
        self.assertIn("grid-template-columns: var(--workbench-sidebar)", css)
        self.assertIn("position: sticky", css)
        self.assertIn("height: 100vh", css)
        self.assertIn('aria-current="page"', (
            STATIC / "index.html"
        ).read_text(encoding="utf-8"))

    def test_environment_settings_reflects_the_control_lock(self) -> None:
        monitor = (STATIC / "monitor.html").read_text(encoding="utf-8")
        script = (STATIC / "monitor.js").read_text(encoding="utf-8")
        self.assertIn('id="sidebarControlState"', monitor)
        self.assertIn("elements.sidebarControlState.textContent", script)


if __name__ == "__main__":
    unittest.main()
