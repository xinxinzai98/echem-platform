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
        self.stylesheets: list[str] = []
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
        if tag == "link" and values.get("rel") == "stylesheet":
            self.stylesheets.append(str(values.get("href") or ""))


class WorkbenchShellTests(unittest.TestCase):
    pages = {
        "index.html": "/",
        "protocol.html": "/protocol",
        "monitor.html": "/monitor",
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
                    ["/", "/protocol", "/monitor"],
                )
                active = [
                    link for link in parser.links
                    if link.get("aria-current") == "page"
                ]
                self.assertEqual(len(active), 1)
                self.assertEqual(active[0].get("href"), active_path)

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

    def test_monitor_sidebar_reflects_the_control_lock(self) -> None:
        monitor = (STATIC / "monitor.html").read_text(encoding="utf-8")
        script = (STATIC / "monitor.js").read_text(encoding="utf-8")
        self.assertIn('id="sidebarControlState"', monitor)
        self.assertIn("elements.sidebarControlState.textContent", script)


if __name__ == "__main__":
    unittest.main()
