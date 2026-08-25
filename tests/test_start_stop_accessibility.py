from __future__ import annotations

import unittest
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"


class AttributeParser(HTMLParser):
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


def parse_page(filename: str) -> AttributeParser:
    parser = AttributeParser()
    parser.feed((STATIC / filename).read_text(encoding="utf-8"))
    return parser


def relative_luminance(hex_color: str) -> float:
    values = [int(hex_color[index:index + 2], 16) / 255 for index in (0, 2, 4)]
    linear = [
        value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
        for value in values
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


class StartStopAccessibilityTests(unittest.TestCase):
    def test_pdf_download_menu_has_menu_semantics_and_escape_focus_restore(self) -> None:
        page = parse_page("start-stop-materials.html")
        config = parse_page("start-stop-config.html")
        button = page.by_id["materialsExportPdf"]
        menu = page.by_id["materialsExportMenu"]
        script = (STATIC / "start-stop-materials.js").read_text(encoding="utf-8")
        self.assertEqual(button.get("aria-haspopup"), "menu")
        self.assertEqual(button.get("aria-controls"), "materialsExportMenu")
        self.assertEqual(menu.get("role"), "menu")
        self.assertNotIn("exportMenuButton", config.by_id)
        self.assertIn('event.key === "Escape"', script)
        self.assertIn("closeMaterialsExportMenu({ restoreFocus: true })", script)
        self.assertIn("materialsElements.materialsExportPdf.focus({ preventScroll: true })", script)
        self.assertIn("export_pdf: true", script)

    def test_material_lists_do_not_announce_every_rebuilt_row(self) -> None:
        analysis = parse_page("start-stop.html")
        cv_eis = parse_page("start-stop-cv-eis.html")
        materials = parse_page("start-stop-materials.html")
        self.assertNotIn("aria-live", analysis.by_id["materialList"])
        self.assertNotIn("aria-live", cv_eis.by_id["cvEisMaterialList"])
        self.assertNotIn("aria-live", materials.by_id["materialsList"])
        self.assertEqual(analysis.by_id["materialFilterSummary"].get("role"), "status")
        self.assertEqual(
            analysis.by_id["startStopStepFilter"].get("aria-describedby"),
            "startStopStepSummary",
        )
        self.assertEqual(analysis.by_id["startStopStepSummary"].get("role"), "status")
        self.assertEqual(materials.by_id["visibleMaterialsCount"].get("role"), "status")

    def test_start_stop_work_step_filter_is_a_hard_comparison_boundary(self) -> None:
        script = (STATIC / "start-stop.js").read_text(encoding="utf-8")
        for marker in (
            'item.work_step_key === state.activeWorkStepKey',
            'series.work_step_key !== state.activeWorkStepKey',
            'payload.work_step_key !== state.activeWorkStepKey',
            'state.highlightedSeries.clear()',
        ):
            self.assertIn(marker, script)

    def test_cv_eis_chart_has_keyboard_and_context_menu_recovery(self) -> None:
        page = parse_page("start-stop-cv-eis.html")
        script = (STATIC / "start-stop-cv-eis.js").read_text(encoding="utf-8")
        self.assertEqual(page.by_id["cvEisChartFrame"].get("tabindex"), "0")
        self.assertEqual(page.by_id["cvEisContextMenu"].get("role"), "menu")
        self.assertEqual(page.by_id["cvEisContextAutoScale"].get("role"), "menuitem")
        self.assertIn('event.key === "Escape"', script)
        self.assertIn("resetCvEisZoom", script)

    def test_material_validation_reveals_and_focuses_the_exact_invalid_field(self) -> None:
        script = (STATIC / "start-stop-materials.js").read_text(encoding="utf-8")
        for marker in (
            "function showMaterialsValidationError(validation)",
            'materialsElements.materialsSearch.value = ""',
            'materialsElements.materialsFilter.value = "all"',
            'field.setAttribute("aria-invalid", "true")',
            'field.setAttribute("aria-describedby", "materialsNotice")',
            'row.scrollIntoView({ behavior: "smooth", block: "center" })',
            'field.focus({ preventScroll: true })',
        ):
            self.assertIn(marker, script)

    def test_full_names_and_source_order_have_expand_and_copy_paths(self) -> None:
        analysis_script = (STATIC / "start-stop.js").read_text(encoding="utf-8")
        materials_script = (STATIC / "start-stop-materials.js").read_text(encoding="utf-8")
        shared_css = (STATIC / "start-stop.css").read_text(encoding="utf-8")
        for marker in (
            'element("details", "material-source-details")',
            '"\u67e5\u770b\u5b8c\u6574\u6765\u6e90\u4e0e\u63a5\u7eed"',
        ):
            self.assertIn(marker, analysis_script)
        self.assertIn('materialsElement("details", "config-source-details")', materials_script)
        self.assertIn('materialsElement("button", "config-copy-source", "\u590d\u5236\u6765\u6e90")', materials_script)
        self.assertIn("overflow-wrap: anywhere", shared_css)

    def test_readable_secondary_gray_exceeds_wcag_aa_on_white(self) -> None:
        foreground = relative_luminance("52606a")
        background = relative_luminance("ffffff")
        contrast = (background + 0.05) / (foreground + 0.05)
        self.assertGreaterEqual(contrast, 4.5)
        self.assertIn("color: #52606a", (STATIC / "start-stop.css").read_text(encoding="utf-8"))
        self.assertIn("color: #52606a", (STATIC / "start-stop-config.css").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
