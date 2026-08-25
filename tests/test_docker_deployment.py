from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DockerDeploymentTests(unittest.TestCase):
    def test_dev7p2_uses_risk_tiered_backup_policy_and_fast_gate(self):
        hotfix = (ROOT / "Dockerfile.v060dev7p2-hotfix").read_text(
            encoding="utf-8"
        )
        environment = (
            ROOT / "deployments" / "start-stop-target-v060dev7p2.env"
        ).read_text(encoding="utf-8")
        installer = (
            ROOT / "scripts" / "install-start-stop-v060dev7p2.sh"
        ).read_text(encoding="utf-8")
        windows_wrapper = (ROOT / "scripts" / "docker-compose.ps1").read_text(
            encoding="utf-8"
        )
        scheduled_runner = (
            ROOT / "scripts" / "run_start_stop_backup_scheduler.py"
        ).read_text(encoding="utf-8")
        scheduled_configurator = (
            ROOT / "scripts" / "configure-start-stop-backup-task.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn("FROM start-stop-analysis:0.6.0-dev.7.1-windows", hotfix)
        self.assertIn("ARG APP_VERSION=0.6.0-dev.7.2", hotfix)
        for filename in (
            "start_stop_service.py",
            "echem_platform/start_stop.py",
            "echem_platform/start_stop_backup.py",
            "echem_platform/start_stop_database.py",
            "scripts/create_start_stop_backup.py",
            "scripts/run_start_stop_backup_scheduler.py",
            "scripts/write_start_stop_backup_status.py",
            "static/start-stop-config.html",
            "static/start-stop-config.js",
        ):
            self.assertIn(filename, hotfix)
            self.assertIn(filename, installer)
        self.assertIn("!Dockerfile.v060dev7p2-hotfix", (ROOT / ".dockerignore").read_text(encoding="utf-8"))
        self.assertIn("ECHEM_IMAGE_VERSION=0.6.0-dev.7.2", environment)
        self.assertIn("ECHEM_IMAGE_TAG=0.6.0-dev.7.2-windows", environment)
        self.assertIn("ECHEM_BACKUP_CPU_LIMIT=1.0", environment)
        self.assertIn("ECHEM_BACKUP_STATUS_VOLUME_NAME=", environment)
        self.assertIn("ECHEM_BACKUP_SCHEDULE_ENABLED=1", environment)
        self.assertIn("backup=not-required-code-only", installer)
        self.assertIn("predeploy_check=required", installer)
        self.assertNotIn("backup-before-v060dev7p2", installer)
        self.assertIn('$ComposeArguments[0] -eq "check"', windows_wrapper)
        self.assertIn("--check-live", windows_wrapper)
        self.assertIn("start-stop-analysis-full-backup", windows_wrapper)
        self.assertIn("--cpus", windows_wrapper)
        self.assertIn('"reason": "scheduled"', scheduled_runner)
        self.assertIn("schedule_slot", scheduled_runner)
        self.assertIn("not _already_attempted", scheduled_runner)
        self.assertNotIn("schtasks.exe", scheduled_configurator)
        self.assertIn("backup-scheduler", scheduled_configurator)
        self.assertIn("catch_up_missed_runs = $false", scheduled_configurator)

    def test_image_is_a_pinned_non_root_start_stop_service(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        requirements = (ROOT / "requirements.docker.txt").read_text(
            encoding="utf-8"
        )

        self.assertIn("python:3.12-slim-bookworm", dockerfile)
        self.assertIn("openssh-client", dockerfile)
        self.assertIn("fonts-noto-cjk", dockerfile)
        self.assertIn("USER ${APP_UID}:${APP_GID}", dockerfile)
        self.assertIn("exec python3 /app/start_stop_service.py", dockerfile)
        self.assertIn("umask 077", dockerfile)
        self.assertIn("org.opencontainers.image.version", dockerfile)
        self.assertIn("org.opencontainers.image.revision", dockerfile)
        self.assertIn("io.start-stop-analysis.git-state", dockerfile)
        self.assertIn("echem_platform/start_stop*.py", dockerfile)
        self.assertIn("docker/start_stop_analysis/*.py", dockerfile)
        self.assertIn("scripts/create_start_stop_backup.py", dockerfile)
        self.assertIn("scripts/write_start_stop_backup_status.py", dockerfile)
        self.assertIn("static/styles.css", dockerfile)
        self.assertIn("static/workbench.css", dockerfile)
        self.assertIn("static/start-stop-shell.js", dockerfile)
        self.assertIn("static/start-stop-cv-eis.html", dockerfile)
        self.assertIn("static/start-stop-cv-eis.js", dockerfile)
        self.assertIn("static/start-stop-cv-eis.css", dockerfile)
        self.assertIn("static/start-stop-materials.html", dockerfile)
        self.assertIn("static/start-stop-materials.js", dockerfile)
        self.assertIn("static/start-stop-workstations.html", dockerfile)
        self.assertIn("static/start-stop-workstations.js", dockerfile)
        self.assertIn("static/start-stop-workstations.css", dockerfile)
        self.assertIn("static/icons/gear.svg", dockerfile)
        self.assertNotIn("COPY --chown=${APP_UID}:${APP_GID} app.py", dockerfile)
        self.assertNotIn(" app.py config.json ", dockerfile)
        self.assertNotIn("demo_data", dockerfile)
        self.assertNotIn("echem_platform ./echem_platform", dockerfile)
        self.assertNotIn("@oai/artifact-tool", dockerfile + requirements)
        for dependency in ("matplotlib", "numpy", "openpyxl", "pandas"):
            self.assertRegex(requirements, rf"(?m)^{dependency}==")

    def test_compose_uses_its_own_database_and_published_cache(self):
        compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
        common, services = compose.split("services:\n", 1)
        local, lan = services.split("  lan-readonly:\n", 1)

        self.assertIn("name: start-stop-analysis", compose)
        self.assertIn(
            "image: ${ECHEM_IMAGE_REPOSITORY:-start-stop-analysis}:"
            "${ECHEM_IMAGE_TAG:-0.6.0-dev.7.2}",
            compose,
        )
        self.assertIn("APP_VERSION: ${ECHEM_IMAGE_VERSION:-0.6.0-dev.7.2}", common)
        self.assertIn("VCS_REF: ${ECHEM_GIT_SHA:-unknown}", common)
        self.assertIn("BUILD_STATE: ${ECHEM_GIT_STATE:-unknown}", common)
        self.assertIn("container_name: start-stop-analysis-local", local)
        self.assertIn("container_name: start-stop-analysis-lan-readonly", lan)
        self.assertNotIn("container_name: echem-platform", compose)
        self.assertIn("read_only: true", common)
        self.assertIn("cap_drop:\n    - ALL", common)
        self.assertIn("no-new-privileges:true", common)
        self.assertIn("stop_grace_period: ${ECHEM_STOP_GRACE_PERIOD:-90s}", common)
        self.assertIn("driver: json-file", common)
        self.assertIn("max-size: ${ECHEM_LOG_MAX_SIZE:-10m}", common)
        self.assertIn("max-file: ${ECHEM_LOG_MAX_FILES:-5}", common)
        self.assertIn('PYTHONFAULTHANDLER: "1"', common)
        self.assertIn(
            "START_STOP_SQLITE_JOURNAL_MODE: ${ECHEM_SQLITE_JOURNAL_MODE:-DELETE}",
            common,
        )
        self.assertIn("START_STOP_BACKUP_SCHEDULE_ENABLED", common)
        self.assertIn("START_STOP_BACKUP_SCHEDULE_LABEL", common)
        self.assertNotIn("START_STOP_SOURCE_ROOT:", common)
        self.assertNotIn("START_STOP_OUTPUT_DIR:", common)

        self.assertIn("127.0.0.1:${ECHEM_LOCAL_PORT:-18787}:8787", local)
        self.assertIn("cpus: ${ECHEM_LOCAL_CPU_LIMIT:-4.0}", local)
        self.assertIn("mem_limit: ${ECHEM_LOCAL_MEMORY_LIMIT:-8g}", local)
        self.assertIn("pids_limit: ${ECHEM_LOCAL_PIDS_LIMIT:-512}", local)
        self.assertIn("/app/state/database/start-stop.sqlite3", local)
        self.assertIn("./state/docker/database:/app/state/database", local)
        self.assertIn(
            "${ECHEM_PUBLISHED_DIR:-./state/docker/published}:"
            "/app/state/published-cache",
            local,
        )
        self.assertIn("/app/state/published-cache/current", local)
        self.assertIn(
            "/app/scratch:size=${ECHEM_LOCAL_SCRATCH_SIZE:-4g}", local
        )
        self.assertIn("source: ${ECHEM_BACKUP_DIR:-./state/docker/backups}", local)
        self.assertIn("target: /app/state/backups", local)
        self.assertIn("read_only: true", local)
        self.assertIn("create_host_path: false", local)
        self.assertIn("/app/start_stop_analysis/analyze_and_plot_start_stop.py", local)
        self.assertIn("/app/docker/collection-config.json", local)
        self.assertEqual(local.count("target: /Users/hive/.ssh/"), 4)
        self.assertEqual(local.count("${ECHEM_SSH_DIR:-/Users/hive/.ssh}/"), 4)
        self.assertEqual(local.count("create_host_path: false"), 5)

        self.assertIn("--lan-read-only", lan)
        self.assertIn("cpus: ${ECHEM_LAN_CPU_LIMIT:-1.0}", lan)
        self.assertIn("mem_limit: ${ECHEM_LAN_MEMORY_LIMIT:-1g}", lan)
        self.assertIn("pids_limit: ${ECHEM_LAN_PIDS_LIMIT:-128}", lan)
        self.assertIn("${ECHEM_LAN_PORT:-18788}:8787", lan)
        self.assertIn("${ECHEM_LAN_BIND:-0.0.0.0}", lan)
        self.assertIn(
            "${ECHEM_PUBLISHED_DIR:-./state/docker/published}:"
            "/app/state/published-cache:ro",
            lan,
        )
        self.assertIn("/app/state/published-cache/current", lan)
        self.assertIn("/app/docker/collection-config.json", lan)
        self.assertIn("--cv-eis-database", lan)
        self.assertIn("/app/state/database-readonly/start-stop.sqlite3", lan)
        self.assertIn("./state/docker/database:/app/state/database-readonly:ro", lan)
        self.assertNotIn("./state/docker/database:/app/state/database:rw", lan)
        self.assertNotIn("/app/state/backups", lan)
        self.assertNotIn("/.ssh/", lan)
        self.assertNotIn("START_STOP_SOURCE_ROOT", lan)

        self.assertNotIn("/Users/hive/Desktop/20260729", compose)
        self.assertNotIn("./state:/app/state", compose)
        self.assertIn("local_runtime", compose)
        self.assertIn("lan_runtime", compose)
        self.assertNotIn("internal: true", compose)
        self.assertEqual(
            compose.count("interval: ${ECHEM_HEALTHCHECK_INTERVAL:-60s}"), 2
        )

    def test_compose_wrapper_initializes_private_state_and_supports_versioned_tags(self):
        wrapper = (ROOT / "scripts" / "docker-compose.sh").read_text(
            encoding="utf-8"
        )
        environment = (ROOT / ".env.example").read_text(encoding="utf-8")

        self.assertIn("umask 077", wrapper)
        self.assertIn('"$project_dir/state/docker/backups"', wrapper)
        self.assertIn("chmod 700", wrapper)
        self.assertIn('chmod 600 "$database_file"', wrapper)
        self.assertIn("ECHEM_IMAGE_TAG_FROM_GIT", wrapper)
        self.assertIn("ECHEM_GIT_STATE=dirty", wrapper)
        self.assertIn('tag_suffix="-${ECHEM_GIT_STATE:-unknown}"', wrapper)
        self.assertIn("git -C", wrapper)
        self.assertIn("--network none", wrapper)
        self.assertIn("/app/scripts/create_start_stop_backup.py", wrapper)
        self.assertIn("/app/state/database:ro", wrapper)
        self.assertIn("/app/state/backups:rw", wrapper)
        self.assertIn("/app/scripts/create_start_stop_backup.py:ro", wrapper)
        self.assertNotIn('python_bin="$project_dir/.venv/bin/python"', wrapper)
        for variable in (
            "ECHEM_IMAGE_REPOSITORY",
            "ECHEM_IMAGE_VERSION",
            "ECHEM_LOCAL_MEMORY_LIMIT",
            "ECHEM_LOCAL_PIDS_LIMIT",
            "ECHEM_LOG_MAX_SIZE",
            "ECHEM_HEALTHCHECK_INTERVAL",
            "ECHEM_BACKUP_DIR",
            "ECHEM_BACKUP_CPU_LIMIT",
            "ECHEM_BACKUP_SCHEDULE_ENABLED",
            "ECHEM_PUBLISHED_DIR",
            "ECHEM_SSH_DIR",
        ):
            self.assertIn(f"{variable}=", environment)

    def test_windows_override_uses_native_database_volume_and_wrapper(self):
        override = (ROOT / "compose.windows.yaml").read_text(encoding="utf-8")
        environment = (ROOT / ".env.windows.example").read_text(encoding="utf-8")
        wrapper = (ROOT / "scripts" / "docker-compose.ps1").read_text(
            encoding="utf-8"
        )
        autostart = (
            ROOT / "scripts" / "configure-docker-desktop-autostart.ps1"
        ).read_text(encoding="utf-8")
        interactive_start = (
            ROOT / "scripts" / "start-docker-desktop-interactive.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn("type: volume", override)
        self.assertIn("source: start_stop_database", override)
        self.assertIn("target: /app/state/database", override)
        self.assertIn("target: /app/state/backup-status", override)
        self.assertIn("START_STOP_BACKUP_STATUS_FILE", override)
        self.assertIn("source: backup_status", override)
        self.assertIn("external: true", override)
        self.assertIn("backup-scheduler:", override)
        self.assertIn("start-stop-analysis-backup-scheduler", override)
        self.assertIn("run_start_stop_backup_scheduler.py", override)
        self.assertIn("network_mode: none", override)
        self.assertIn("target: /app/state/database-readonly", override)
        self.assertIn("read_only: true", override)
        self.assertIn("--cv-eis-database", override)
        self.assertIn("ECHEM_DATABASE_VOLUME_NAME", override)
        self.assertIn("- --lan-no-auth", override)
        self.assertNotIn("cp /run/start-stop-secrets/lan-basic-auth.txt", override)
        self.assertNotIn("chmod 600 /tmp/lan-basic-auth.txt", override)
        self.assertIn(
            "cp /Users/hive/.ssh/aghid_windows_codex_ed25519 "
            "/tmp/start-stop-ssh/",
            override,
        )

        self.assertIn('$ComposeArguments[0] -eq "backup"', wrapper)
        self.assertIn("ECHEM_DATABASE_VOLUME_NAME", wrapper)
        self.assertIn("ECHEM_BACKUP_DIR", wrapper)
        self.assertIn("ECHEM_BACKUP_STATUS_VOLUME_NAME", wrapper)
        self.assertIn('eq "backup-status"', wrapper)
        self.assertIn("write_start_stop_backup_status.py", wrapper)
        self.assertIn("volume create $BackupStatusVolume", wrapper)
        self.assertIn("type=volume,source=$DatabaseVolume,target=/app/state/database,readonly", wrapper)
        self.assertIn("type=bind,source=$BackupDir,target=/app/state/backups", wrapper)
        self.assertIn("--network", wrapper)
        self.assertIn("no-new-privileges:true", wrapper)
        self.assertIn("/app/scripts/create_start_stop_backup.py", wrapper)
        self.assertIn('throw "启停数据库备份失败', wrapper)
        self.assertIn(
            "chmod 600 /tmp/start-stop-ssh/aghid_windows_codex_ed25519",
            override,
        )
        self.assertIn(
            "sed 's#/Users/hive/.ssh/#/tmp/start-stop-ssh/#g'",
            override,
        )
        self.assertIn("- /tmp/collection-config.windows.json", override)
        self.assertNotIn("chmod 600 /Users/hive/.ssh/", override)
        self.assertIn("ECHEM_LAN_IP=192.168.110.225", environment)
        self.assertIn("ECHEM_IMAGE_VERSION=0.6.0-dev.7.2", environment)
        self.assertIn("ECHEM_IMAGE_TAG=0.6.0-dev.7.2-windows", environment)
        self.assertIn("ECHEM_BACKUP_CPU_LIMIT=1.0", environment)
        self.assertIn("ECHEM_BACKUP_STATUS_VOLUME_NAME=", environment)
        self.assertIn("ECHEM_BACKUP_SCHEDULE_ENABLED=1", environment)
        self.assertIn("ECHEM_BACKUP_SCHEDULE_ANCHOR=", environment)
        self.assertIn("ECHEM_SQLITE_JOURNAL_MODE=DELETE", environment)
        self.assertIn("ECHEM_SSH_DIR=C:/StartStopAnalysis/secrets/ssh", environment)
        self.assertIn("compose.windows.yaml", wrapper)
        self.assertIn('Join-Path $stateRoot "import"', wrapper)
        self.assertIn("[switch]$Disable", autostart)
        self.assertIn("$settings.AutoStart = $autoStart", autostart)
        self.assertIn('"Docker Desktop"', autostart)
        self.assertIn("Set-ItemProperty", autostart)
        self.assertIn("Remove-ItemProperty", autostart)
        self.assertIn("-LogonType Interactive", interactive_start)
        self.assertIn("Start-ScheduledTask", interactive_start)
        self.assertIn("-ExecutionTimeLimit (New-TimeSpan -Hours 1)", interactive_start)

    def test_collection_config_has_three_read_only_ssh_sources(self):
        config = json.loads(
            (ROOT / "docker" / "collection-config.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(config["known_hosts_file"], "/Users/hive/.ssh/known_hosts")
        self.assertEqual(config["settle_seconds"], 300)
        self.assertEqual(len(config["machines"]), 3)
        self.assertNotIn("destination_root", config)
        self.assertNotIn(".exp", config["extensions"])
        self.assertEqual(
            {machine["ip"] for machine in config["machines"]},
            {"192.168.110.153", "192.168.110.155", "192.168.110.164"},
        )
        self.assertEqual(
            {machine["name"] for machine in config["machines"]},
            {"测试室1", "测试室2", "制备室"},
        )
        for machine in config["machines"]:
            self.assertTrue(machine["identity_file"].startswith("/Users/hive/.ssh/"))
            self.assertTrue(machine["roots"])

    def test_vendored_analysis_uses_container_path_overrides(self):
        analyzer = (
            ROOT
            / "docker"
            / "start_stop_analysis"
            / "analyze_and_plot_start_stop.py"
        ).read_text(encoding="utf-8")
        helper = (
            ROOT
            / "docker"
            / "start_stop_analysis"
            / "material_config_workbook.py"
        )

        self.assertTrue(helper.is_file())
        self.assertIn('os.environ.get("START_STOP_PROJECT_ROOT"', analyzer)
        self.assertIn('os.environ.get("START_STOP_SOURCE_ROOT"', analyzer)
        self.assertIn('"START_STOP_OUTPUT_DIR"', analyzer)
        self.assertIn(
            'DEFAULT_PROJECT_ROOT = Path("/Users/hive/Desktop/20260729 反向电流测试")',
            analyzer,
        )

    def test_build_context_is_an_allowlist_without_platform_or_private_state(self):
        rules = (ROOT / ".dockerignore").read_text(encoding="utf-8")
        self.assertTrue(rules.startswith("**\n"))
        self.assertIn("!start_stop_service.py", rules)
        self.assertIn("!compose.windows.yaml", rules)
        self.assertIn("!echem_platform/start_stop*.py", rules)
        self.assertIn("!static/start-stop.js", rules)
        self.assertIn("!static/start-stop-shell.js", rules)
        self.assertIn("!static/start-stop-config.html", rules)
        self.assertIn("!static/start-stop-config.js", rules)
        self.assertIn("!static/start-stop-config.css", rules)
        self.assertIn("!static/start-stop-cv-eis.html", rules)
        self.assertIn("!static/start-stop-cv-eis.js", rules)
        self.assertIn("!static/start-stop-cv-eis.css", rules)
        self.assertIn("!static/start-stop-materials.html", rules)
        self.assertIn("!static/start-stop-materials.js", rules)
        self.assertIn("!static/start-stop-workstations.html", rules)
        self.assertIn("!static/start-stop-workstations.js", rules)
        self.assertIn("!static/start-stop-workstations.css", rules)
        self.assertIn("!static/styles.css", rules)
        self.assertIn("!static/workbench.css", rules)
        self.assertIn("!static/icons/gear.svg", rules)
        self.assertIn("!docker/start_stop_analysis/*.py", rules)
        self.assertIn("!scripts/create_start_stop_backup.py", rules)
        self.assertNotIn("!app.py", rules)
        self.assertNotIn("!config.json", rules)
        self.assertNotIn("!state/", rules)
        self.assertNotIn("!.ssh/", rules)
        self.assertNotIn("!demo_data/", rules)


if __name__ == "__main__":
    unittest.main()
