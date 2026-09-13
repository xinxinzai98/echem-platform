from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class WindowsDesktopLauncherTests(unittest.TestCase):
    def read(self, name: str) -> str:
        return (ROOT / name).read_text(encoding="utf-8")

    def test_launcher_is_versioned_path_relative_and_uses_staging_port(self):
        script = self.read("start-stage-c-desktop.ps1")
        self.assertIn("$target = $PSScriptRoot", script)
        self.assertIn("[int]$Port = 8788", script)
        self.assertNotIn("D:\\EchemPlatform", script)
        self.assertNotIn("3a178e9", script)
        self.assertIn("Start-Process -FilePath $python", script)
        self.assertIn("'--no-watch'", script)

    def test_launcher_requires_the_current_explorer_session(self):
        script = self.read("start-stage-c-desktop.ps1")
        self.assertIn("Get-Process -Id $PID", script)
        self.assertIn("Get-Process -Name 'explorer'", script)
        self.assertIn("$launcherSession -le 0", script)
        self.assertIn("$launcherSession -notin $explorerSessions", script)
        self.assertIn("/api/control/preflight", script)
        self.assertIn("'interactive_desktop_session'", script)

    def test_normal_launcher_refuses_enabled_control_and_instrument_commands(self):
        script = self.read("start-stage-c-desktop.ps1")
        self.assertIn("[switch]$AllowInstrumentControl", script)
        self.assertIn(
            "$status.instrument_control -and -not $AllowInstrumentControl",
            script,
        )
        lowered = script.lower()
        self.assertNotIn("chi760e.exe", lowered)
        self.assertNotIn("/runmacro", lowered)
        self.assertNotIn("com3", lowered)
        self.assertNotIn("com4", lowered)

    def test_launcher_opens_the_workbench_overview(self):
        script = self.read("start-stage-c-desktop.ps1")
        self.assertIn('Start-Process "$uri/"', script)
        self.assertIn('url = "$uri/"', script)
        self.assertNotIn('Start-Process "$uri/monitor"', script)

    def test_stop_script_targets_only_verified_platform_and_blocks_active_runs(self):
        script = self.read("stop-stage-c-desktop.ps1")
        self.assertIn("$owner.ExecutablePath -ine $python", script)
        self.assertIn("$owner.CommandLine -notlike \"*$app*\"", script)
        self.assertIn("/api/control/runs?limit=50", script)
        self.assertIn("'starting', 'running', 'stop_requested'", script)
        self.assertIn("Stop-Process -Id $ownerId -Force", script)
        lowered = script.lower()
        self.assertNotIn("chi760e.exe", lowered)
        self.assertNotIn("/runmacro", lowered)

    def test_cmd_wrappers_only_call_sibling_powershell_scripts(self):
        start = self.read("start-stage-c-desktop.cmd")
        stop = self.read("stop-stage-c-desktop.cmd")
        self.assertIn('"%~dp0start-stage-c-desktop.ps1"', start)
        self.assertIn('"%~dp0stop-stage-c-desktop.ps1"', stop)
        self.assertNotIn("D:\\", start)
        self.assertNotIn("D:\\", stop)


if __name__ == "__main__":
    unittest.main()
