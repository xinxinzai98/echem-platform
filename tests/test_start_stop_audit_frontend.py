import shutil
import subprocess
import unittest
from pathlib import Path


class StartStopAuditFrontendTests(unittest.TestCase):
    def test_scientific_display_and_review_refresh(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js required for JavaScript behavior regression")
        root = Path(__file__).resolve().parents[1]
        completed = subprocess.run(
            [node, str(root / "tests/js/start_stop_audit_frontend_behavior.cjs")],
            cwd=root, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("PASS:", completed.stdout)
