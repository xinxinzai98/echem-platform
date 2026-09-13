from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"


class StartStopChartInteractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.script = (STATIC / "start-stop.js").read_text(encoding="utf-8")
        cls.styles = (STATIC / "start-stop.css").read_text(encoding="utf-8")

    def test_drag_selection_zoom_is_kept_with_existing_navigation_modes(self) -> None:
        for marker in (
            'setChartInteractionMode("zoom")',
            'setChartInteractionMode("pan")',
            "updateZoomSelection",
            "finishChartGesture",
            "chartSuppressClickUntil",
            'overlay.addEventListener("wheel"',
        ):
            self.assertIn(marker, self.script)

    def test_custom_context_menu_replaces_native_menu_and_restores_full_range(self) -> None:
        self.assertIn('elements.chartFrame.addEventListener("contextmenu"', self.script)
        self.assertIn("event.preventDefault()", self.script)
        self.assertIn('autoScale.id = "chartAutoScale"', self.script)
        self.assertIn('"自动缩放"', self.script)
        self.assertIn('"恢复完整数据范围"', self.script)
        self.assertIn("resetChartZoom();", self.script)
        self.assertIn("closeChartContextMenu", self.script)

    def test_context_menu_supports_escape_outside_click_and_keyboard_focus(self) -> None:
        self.assertIn('event.key === "Escape"', self.script)
        self.assertIn('event.target.closest(".chart-context-menu")', self.script)
        self.assertIn('document.addEventListener("pointerdown"', self.script)
        self.assertIn('["ArrowDown", "ArrowUp", "Home", "End"]', self.script)
        self.assertIn('role", "menu"', self.script)
        self.assertIn('role", "menuitem"', self.script)

    def test_context_menu_is_constrained_overlay_and_hidden_when_closed(self) -> None:
        self.assertIn(".chart-context-menu {", self.styles)
        self.assertIn(".chart-context-menu[hidden]", self.styles)
        self.assertIn("position: absolute", self.styles)
        self.assertIn("z-index: 24", self.styles)

    def test_live_preview_curves_reuse_the_interactive_comparison_chart(self) -> None:
        for marker in (
            "installLivePreviewWorkspace",
            "mergeLiveComparisonChart",
            "formal_series_id",
            "replace_from_segment_index",
            "continuation_cycle_offset",
            "cathodic_last1s_median_raw_v",
            "current_a_cm2",
            "scheduleLiveComparisonPoll",
            'request("/api/start-stop/live-preview")',
        ):
            self.assertIn(marker, self.script)
        self.assertNotIn('LIVE_PREVIEW_WORK_STEP_KEY = "__live_preview__"', self.script)
        self.assertNotIn('variant: "live"', self.script)
        self.assertIn(".live-comparison-banner {", self.styles)
        self.assertIn("body.live-comparison-mode", self.styles)


if __name__ == "__main__":
    unittest.main()
