import shutil
import subprocess
import unittest
from pathlib import Path


class StartStopJavascriptBehaviorTests(unittest.TestCase):
    def test_monitor_status_and_tail_window(self):
        self.run_behavior("start_stop_monitor_behavior.cjs")

    def test_shared_plot_interaction_geometry(self):
        self.run_behavior("start_stop_plot_interaction_behavior.cjs")

    def test_shared_client_requests_and_visibility(self):
        self.run_behavior("start_stop_client_behavior.cjs")

    def test_real_chart_loader_and_default_selection(self):
        self.run_behavior("start_stop_loader_behavior.cjs")

    def run_behavior(self, name):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js required for JavaScript behavior regression")
        root = Path(__file__).resolve().parents[1]
        completed = subprocess.run(
            [node, str(root / "tests/js" / name)],
            cwd=root, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("PASS:", completed.stdout)
