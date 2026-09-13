#!/bin/sh
set -eu

source_root="${1:-/source}"
target_root="${2:-/target}"

files="
Dockerfile
Dockerfile.v060dev7p2-hotfix
compose.yaml
compose.windows.yaml
.dockerignore
.env.example
.env.windows.example
requirements.docker.txt
start_stop_service.py
echem_platform/start_stop.py
echem_platform/start_stop_backup.py
echem_platform/start_stop_database.py
scripts/create_start_stop_backup.py
scripts/run_start_stop_backup_scheduler.py
scripts/write_start_stop_backup_status.py
scripts/docker-compose.ps1
scripts/configure-start-stop-backup-task.ps1
static/start-stop-config.html
static/start-stop-config.js
docs/START_STOP_BACKUP_POLICY.md
"

for relative in $files
do
    if [ ! -f "$source_root/$relative" ]; then
        echo "missing staged file: $relative" >&2
        exit 2
    fi
done

environment="deployments/start-stop-target-v060dev7p2.env"
if [ ! -f "$source_root/$environment" ]; then
    echo "missing staged file: $environment" >&2
    exit 2
fi

# This release changes application code and operational policy only.  It does
# not change the SQLite schema or repair database content, so the risk-tiered
# policy requires a fast read-only pre-deploy check, not a 17+ GB full backup.
for relative in $files
do
    mkdir -p "$(dirname "$target_root/$relative")"
    cp "$source_root/$relative" "$target_root/$relative"
done

mkdir -p "$target_root/deployments" "$target_root/scripts"
cp "$source_root/scripts/install-start-stop-v060dev7p2.sh" "$target_root/scripts/"
cp "$source_root/$environment" "$target_root/$environment"
cp "$source_root/$environment" "$target_root/.env"

echo "installed=$target_root"
echo "backup=not-required-code-only"
echo "predeploy_check=required"
echo "full_backup_gate=schema-migration-or-database-repair"
