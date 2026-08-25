#!/bin/sh
set -eu

# State created by this wrapper is private by default.  The image applies the
# same umask before Python creates SQLite/WAL and generated files.
umask 077

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)

# Bind mounts must exist with the desktop user's ownership before Docker starts.
mkdir -p \
  "$project_dir/state/docker/database" \
  "$project_dir/state/docker/published" \
  "$project_dir/state/docker/backups" \
  "$project_dir/state/docker/secrets"

chmod 700 \
  "$project_dir/state/docker" \
  "$project_dir/state/docker/database" \
  "$project_dir/state/docker/published" \
  "$project_dir/state/docker/backups" \
  "$project_dir/state/docker/secrets"

# The wrapper deliberately never creates, edits or chmods LAN credentials.  A
# deployment must provide the owner-only username:password file explicitly;
# the service itself fails closed if its type or mode is unsafe.

# Tighten the live database and its SQLite sidecars without recursively walking
# the multi-gigabyte content-addressed repository on every Compose command.
for database_file in "$project_dir"/state/docker/database/*.sqlite3*; do
  if [ -f "$database_file" ]; then
    chmod 600 "$database_file"
  fi
done

# Embed the current Git revision in OCI image labels.  Existing image naming is
# unchanged unless ECHEM_IMAGE_TAG_FROM_GIT=1 is explicitly requested.
if [ -z "${ECHEM_GIT_SHA:-}" ] && command -v git >/dev/null 2>&1; then
  if git -C "$project_dir" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    ECHEM_GIT_SHA=$(git -C "$project_dir" rev-parse --short=12 HEAD)
    export ECHEM_GIT_SHA
  fi
fi

if [ -n "${ECHEM_GIT_SHA:-}" ] && [ -z "${ECHEM_GIT_STATE:-}" ]; then
  if [ -n "$(git -C "$project_dir" status --porcelain --untracked-files=normal)" ]; then
    ECHEM_GIT_STATE=dirty
  else
    ECHEM_GIT_STATE=clean
  fi
  export ECHEM_GIT_STATE
fi

if [ "${ECHEM_IMAGE_TAG_FROM_GIT:-0}" = "1" ] && [ -z "${ECHEM_IMAGE_TAG:-}" ]; then
  if [ -z "${ECHEM_GIT_SHA:-}" ]; then
    echo "无法生成版本化镜像标签：当前目录没有可用的 Git 提交。" >&2
    exit 1
  fi
  tag_suffix=""
  if [ "${ECHEM_GIT_STATE:-unknown}" != "clean" ]; then
    tag_suffix="-${ECHEM_GIT_STATE:-unknown}"
  fi
  ECHEM_IMAGE_TAG="${ECHEM_IMAGE_VERSION:-0.4.0-dev.10}-${ECHEM_GIT_SHA}${tag_suffix}"
  export ECHEM_IMAGE_TAG
fi

if command -v docker >/dev/null 2>&1; then
  docker_bin="$(command -v docker)"
elif [ -x "/Users/hive/Applications/Docker.app/Contents/Resources/bin/docker" ]; then
  docker_bin="/Users/hive/Applications/Docker.app/Contents/Resources/bin/docker"
elif [ -x "/Applications/Docker.app/Contents/Resources/bin/docker" ]; then
  docker_bin="/Applications/Docker.app/Contents/Resources/bin/docker"
else
  echo "未找到 Docker Desktop，请先启动 Docker。" >&2
  exit 1
fi

# Routine code deployments and scan/render tasks use a quick read-only
# metadata/FK gate.  It does not copy or hash the multi-gigabyte BLOB store.
if [ "${1:-}" = "check" ]; then
  exec "$docker_bin" run \
    --rm \
    --read-only \
    --network none \
    --cap-drop ALL \
    --security-opt no-new-privileges:true \
    --user 501:20 \
    --entrypoint python3 \
    -v "$project_dir/state/docker/database:/app/state/database:ro" \
    "${ECHEM_IMAGE_REPOSITORY:-start-stop-analysis}:${ECHEM_IMAGE_TAG:-0.6.0-dev.7.2}" \
    /app/scripts/create_start_stop_backup.py \
    --database /app/state/database/start-stop.sqlite3 \
    --check-live \
    --compact
fi

# Full verified backups are deliberately separate from normal operations.
# They run only for schema migration/database repair or from the low-frequency
# background schedule, while the web service remains online.
if [ "${1:-}" = "backup" ]; then
  shift
  backup_dir=${ECHEM_BACKUP_DIR:-$project_dir/state/docker/backups}
  case "$backup_dir" in
    /*) ;;
    *) backup_dir="$project_dir/${backup_dir#./}" ;;
  esac
  image_ref=$("$docker_bin" compose \
    --project-directory "$project_dir" \
    -f "$project_dir/compose.yaml" \
    config --images | sed -n '1p')
  if [ -z "$image_ref" ]; then
    echo "无法确定启停分析 Docker 镜像。" >&2
    exit 1
  fi
  exec "$docker_bin" run \
    --rm \
    --name start-stop-analysis-full-backup \
    --read-only \
    --network none \
    --cpus "${ECHEM_BACKUP_CPU_LIMIT:-1.0}" \
    --cap-drop ALL \
    --security-opt no-new-privileges:true \
    --user 501:20 \
    --entrypoint python3 \
    -v "$project_dir/state/docker/database:/app/state/database:ro" \
    -v "$backup_dir:/app/state/backups:rw" \
    -v "$project_dir/scripts/create_start_stop_backup.py:/app/scripts/create_start_stop_backup.py:ro" \
    "$image_ref" \
    /app/scripts/create_start_stop_backup.py \
    --database /app/state/database/start-stop.sqlite3 \
    --backup-dir /app/state/backups \
    "$@"
fi

exec "$docker_bin" compose \
  --project-directory "$project_dir" \
  -f "$project_dir/compose.yaml" \
  "$@"
