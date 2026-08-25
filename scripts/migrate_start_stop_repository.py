#!/usr/bin/env python3
"""Import the legacy start/stop file tree into ``StartStopDatabase``.

The migration is deliberately source-read-only.  It validates every legacy
file against its manifest (size and SHA-256) before opening the destination
database, imports the unmanaged Beijing TXT files as ``local_import`` sources,
and copies the existing material configuration without changing its revision.
The old files and old SQLite database are never modified or removed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Sequence


EXPECTED_CURRENT_FILES = 907
EXPECTED_CURRENT_BYTES = 2_363_196_315
EXPECTED_CURRENT_REMOTE_FILES = 823
EXPECTED_CURRENT_LOCAL_FILES = 84
COPY_STATUSES = {"copied", "versioned", "unchanged_content"}
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
UTC = dt.timezone.utc


class MigrationError(RuntimeError):
    """The legacy source is unsafe, inconsistent, or cannot be migrated."""


@dataclass(frozen=True)
class SourceEntry:
    path: Path
    logical_path: str
    size_bytes: int
    sha256: str
    modified_utc: str
    source_kind: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class MigrationPlan:
    remote_entries: tuple[SourceEntry, ...]
    local_entries: tuple[SourceEntry, ...]

    @property
    def entries(self) -> tuple[SourceEntry, ...]:
        return self.remote_entries + self.local_entries

    @property
    def file_count(self) -> int:
        return len(self.entries)

    @property
    def total_bytes(self) -> int:
        return sum(entry.size_bytes for entry in self.entries)

    @property
    def unique_blobs(self) -> int:
        return len({entry.sha256 for entry in self.entries})


def utc_now() -> str:
    return dt.datetime.now(UTC).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _logical_path(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MigrationError(f"{field} must be a non-empty string")
    if "\x00" in value or "\\" in value:
        raise MigrationError(f"{field} must use a safe POSIX relative path: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise MigrationError(f"{field} is not a safe relative path: {value!r}")
    if path.parts and re.fullmatch(r"[A-Za-z]:", path.parts[0]):
        raise MigrationError(f"{field} must not contain a drive prefix: {value!r}")
    return path.as_posix()


def _contained_file(root: Path, relative: str, *, field: str) -> Path:
    logical = _logical_path(relative, field=field)
    root_resolved = root.resolve(strict=True)
    candidate = (root_resolved / Path(*PurePosixPath(logical).parts)).resolve(strict=True)
    try:
        candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise MigrationError(f"{field} escapes the source root: {relative!r}") from exc
    if not candidate.is_file() or candidate.is_symlink():
        raise MigrationError(f"legacy source is not a regular file: {candidate}")
    return candidate


def _required_text(record: dict[str, Any], field: str, line_number: int) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value:
        raise MigrationError(f"manifest line {line_number} has invalid {field}")
    return value


def _required_integer(record: dict[str, Any], field: str, line_number: int) -> int:
    value = record.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MigrationError(f"manifest line {line_number} has invalid {field}")
    return value


def read_remote_manifest(source_root: Path, manifest_path: Path) -> tuple[SourceEntry, ...]:
    if not manifest_path.is_file():
        raise MigrationError(f"legacy collection manifest does not exist: {manifest_path}")
    entries: list[SourceEntry] = []
    local_paths: set[str] = set()
    source_keys: set[tuple[str, str, str]] = set()
    with manifest_path.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            if not raw_line.strip():
                continue
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise MigrationError(
                    f"manifest line {line_number} is not valid JSON: {exc}"
                ) from exc
            if not isinstance(record, dict):
                raise MigrationError(f"manifest line {line_number} is not an object")
            status = _required_text(record, "status", line_number)
            if status not in COPY_STATUSES:
                raise MigrationError(
                    f"manifest line {line_number} has unsupported status {status!r}"
                )
            machine_id = _required_text(record, "machine_id", line_number)
            hostname = _required_text(record, "hostname", line_number)
            ip = _required_text(record, "ip", line_number)
            root_label = _required_text(record, "root_label", line_number)
            remote_root = _required_text(record, "remote_root", line_number)
            remote_path = _required_text(record, "remote_path", line_number)
            relative_windows = _required_text(
                record, "remote_relative_path", line_number
            )
            relative_parts = [
                part for part in relative_windows.replace("\\", "/").split("/") if part
            ]
            if not relative_parts or any(part in {".", ".."} for part in relative_parts):
                raise MigrationError(
                    f"manifest line {line_number} has unsafe remote_relative_path"
                )
            logical_path = _logical_path(
                "/".join((machine_id, root_label, *relative_parts)),
                field=f"manifest line {line_number} logical path",
            )
            local_relative = _logical_path(
                _required_text(record, "local_path", line_number),
                field=f"manifest line {line_number} local_path",
            )
            if "_采集记录" in PurePosixPath(local_relative).parts:
                raise MigrationError(
                    f"manifest line {line_number} points into _采集记录"
                )
            size = _required_integer(record, "remote_size", line_number)
            expected_sha = _required_text(record, "sha256", line_number).casefold()
            if SHA256_PATTERN.fullmatch(expected_sha) is None:
                raise MigrationError(f"manifest line {line_number} has invalid sha256")
            last_write_ticks = _required_integer(
                record, "remote_last_write_ticks", line_number
            )
            last_write_utc = _required_text(
                record, "remote_last_write_utc", line_number
            )
            collected_at_utc = _required_text(
                record, "collected_at_utc", line_number
            )
            source_key = (machine_id, root_label, remote_path.casefold())
            if local_relative in local_paths:
                raise MigrationError(f"duplicate local_path in manifest: {local_relative}")
            if source_key in source_keys:
                raise MigrationError(
                    "duplicate remote source in manifest: "
                    f"{machine_id}/{root_label}/{remote_path}"
                )
            local_paths.add(local_relative)
            source_keys.add(source_key)
            path = _contained_file(
                source_root, local_relative, field=f"manifest line {line_number} local_path"
            )
            actual_size = path.stat().st_size
            if actual_size != size:
                raise MigrationError(
                    f"size mismatch for {local_relative}: manifest={size}, actual={actual_size}"
                )
            actual_sha = sha256_file(path)
            if actual_sha != expected_sha:
                raise MigrationError(
                    f"SHA-256 mismatch for {local_relative}: "
                    f"manifest={expected_sha}, actual={actual_sha}"
                )
            metadata = {
                "machine_id": machine_id,
                "hostname": hostname,
                "ip": ip,
                "root_label": root_label,
                "remote_root": remote_root,
                "remote_path": remote_path,
                "remote_relative_path": relative_windows,
                "repository_path": logical_path,
                "size": size,
                "size_bytes": size,
                "sha256": expected_sha,
                "last_write_ticks": last_write_ticks,
                "last_write_utc": last_write_utc,
                "modified_ns": path.stat().st_mtime_ns,
                "collected_at_utc": collected_at_utc,
            }
            entries.append(
                SourceEntry(
                    path=path,
                    logical_path=logical_path,
                    size_bytes=size,
                    sha256=expected_sha,
                    modified_utc=last_write_utc,
                    source_kind="remote",
                    metadata=metadata,
                )
            )
    return tuple(entries)


def read_local_imports(source_root: Path, beijing_root: Path) -> tuple[SourceEntry, ...]:
    source_resolved = source_root.resolve(strict=True)
    beijing_resolved = beijing_root.resolve(strict=True)
    try:
        beijing_resolved.relative_to(source_resolved)
    except ValueError as exc:
        raise MigrationError("Beijing import root must be inside the source root") from exc
    entries: list[SourceEntry] = []
    for path in sorted(beijing_resolved.rglob("*"), key=lambda item: item.as_posix()):
        if not path.is_file() or path.is_symlink():
            continue
        relative_to_beijing = path.relative_to(beijing_resolved)
        if path.name == ".DS_Store" or "_采集记录" in relative_to_beijing.parts:
            continue
        if path.suffix.casefold() != ".txt":
            continue
        logical_path = _logical_path(
            path.relative_to(source_resolved).as_posix(), field="local import path"
        )
        stat = path.stat()
        modified_utc = (
            dt.datetime.fromtimestamp(stat.st_mtime, UTC)
            .isoformat()
            .replace("+00:00", "Z")
        )
        digest = sha256_file(path)
        metadata = {
            "source_kind": "local_import",
            "logical_path": logical_path,
            "size": int(stat.st_size),
            "sha256": digest,
            "last_write_utc": modified_utc,
            "modified_ns": int(stat.st_mtime_ns),
            "collected_at_utc": utc_now(),
        }
        entries.append(
            SourceEntry(
                path=path.resolve(strict=True),
                logical_path=logical_path,
                size_bytes=int(stat.st_size),
                sha256=digest,
                modified_utc=modified_utc,
                source_kind="local_import",
                metadata=metadata,
            )
        )
    return tuple(entries)


def build_plan(
    source_root: Path,
    manifest_path: Path,
    beijing_root: Path,
    *,
    expect_current_baseline: bool,
) -> MigrationPlan:
    remote_entries = read_remote_manifest(source_root, manifest_path)
    local_entries = read_local_imports(source_root, beijing_root)
    plan = MigrationPlan(remote_entries, local_entries)
    if expect_current_baseline:
        observed = (
            plan.file_count,
            plan.total_bytes,
            len(plan.remote_entries),
            len(plan.local_entries),
        )
        expected = (
            EXPECTED_CURRENT_FILES,
            EXPECTED_CURRENT_BYTES,
            EXPECTED_CURRENT_REMOTE_FILES,
            EXPECTED_CURRENT_LOCAL_FILES,
        )
        if observed != expected:
            raise MigrationError(
                "current baseline mismatch: "
                f"files={observed[0]}/{expected[0]}, "
                f"bytes={observed[1]}/{expected[1]}, "
                f"remote={observed[2]}/{expected[2]}, "
                f"local={observed[3]}/{expected[3]}"
            )
    return plan


def _read_legacy_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise MigrationError(f"legacy configuration database does not exist: {path}")
    try:
        # mode=ro remains source-read-only while still honoring a live WAL;
        # immutable=1 would risk missing the latest committed configuration.
        source_uri = path.resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(source_uri, uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            state_row = connection.execute(
                "SELECT dataset_fingerprint, revision, updated_utc "
                "FROM start_stop_config_state WHERE id = 1"
            ).fetchone()
            material_rows = connection.execute(
                "SELECT material_key, plot_name, include_in_summary_atlas, notes, "
                "source_fingerprint, updated_utc "
                "FROM start_stop_material_config ORDER BY material_key"
            ).fetchall()
    except sqlite3.Error as exc:
        raise MigrationError(f"cannot read legacy material configuration: {exc}") from exc
    state = dict(state_row) if state_row is not None else None
    materials = [
        {
            **dict(row),
            "include_in_summary_atlas": bool(row["include_in_summary_atlas"]),
        }
        for row in material_rows
    ]
    if state is None and not materials:
        return {
            "dataset_fingerprint": "",
            "revision": 0,
            "updated_utc": "",
            "materials": [],
        }
    if state is None:
        raise MigrationError("legacy material configuration has rows but no state record")
    return {
        "dataset_fingerprint": state["dataset_fingerprint"],
        "revision": int(state["revision"]),
        "updated_utc": state["updated_utc"],
        "materials": materials,
    }


def _config_identity(config: dict[str, Any]) -> dict[str, Any]:
    materials = []
    for raw in config.get("materials") or []:
        row = dict(raw)
        row["include_in_summary_atlas"] = bool(row["include_in_summary_atlas"])
        row["favorite"] = bool(row.get("favorite", False))
        materials.append(row)
    materials.sort(key=lambda row: row["material_key"])
    return {
        "dataset_fingerprint": str(config.get("dataset_fingerprint") or ""),
        "revision": int(config.get("revision") or 0),
        "updated_utc": str(config.get("updated_utc") or ""),
        "materials": materials,
    }


def migrate_legacy_config(database: Any, legacy: Path) -> dict[str, Any]:
    legacy_config = _read_legacy_config(legacy)
    if not legacy_config["materials"] and legacy_config["revision"] == 0:
        return {"status": "empty", "materials": 0, "revision": 0}
    try:
        current = _config_identity(database.get_start_stop_config())
    except Exception as exc:
        raise MigrationError(f"cannot inspect destination material configuration: {exc}") from exc
    expected = _config_identity(legacy_config)
    if current["revision"] or current["materials"]:
        if current == expected:
            return {
                "status": "unchanged",
                "materials": len(expected["materials"]),
                "revision": expected["revision"],
                "dataset_fingerprint": expected["dataset_fingerprint"],
            }
        raise MigrationError(
            "destination material configuration is not empty and differs from legacy"
        )
    try:
        imported = database.import_start_stop_config(legacy_config, overwrite=False)
    except Exception as exc:
        raise MigrationError(f"cannot migrate material configuration: {exc}") from exc
    if _config_identity(imported) != expected:
        raise MigrationError("destination material configuration readback differs from legacy")
    return {
        "status": "imported",
        "materials": len(expected["materials"]),
        "revision": expected["revision"],
        "dataset_fingerprint": expected["dataset_fingerprint"],
    }


def _finish_batch(database: Any, batch_id: Any, status: str, totals: dict[str, Any]) -> Any:
    method = getattr(database, "finish_collection_batch", None)
    if method is None:
        return None
    return method(batch_id, status=status, totals=totals)


def _verify_ingest_result(result: Any, entry: SourceEntry) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise MigrationError(f"database returned no ingest receipt for {entry.logical_path}")
    if str(result.get("sha256") or "").casefold() != entry.sha256:
        raise MigrationError(f"database SHA-256 readback differs for {entry.logical_path}")
    if int(result.get("size_bytes", -1)) != entry.size_bytes:
        raise MigrationError(f"database size readback differs for {entry.logical_path}")
    repository_path = result.get("repository_path")
    if repository_path not in {None, entry.logical_path}:
        raise MigrationError(f"database logical path readback differs for {entry.logical_path}")
    return result


def _database_integrity(database: Any, database_path: Path) -> dict[str, Any]:
    try:
        with closing(sqlite3.connect(database_path.resolve())) as connection:
            integrity_rows = [
                row[0] for row in connection.execute("PRAGMA integrity_check").fetchall()
            ]
            foreign_key_rows = connection.execute("PRAGMA foreign_key_check").fetchall()
    except sqlite3.Error as exc:
        raise MigrationError(f"destination integrity check failed: {exc}") from exc
    result: dict[str, Any] = {
        "integrity_check": integrity_rows,
        "foreign_key_violations": len(foreign_key_rows),
        "ok": integrity_rows == ["ok"] and not foreign_key_rows,
    }
    repository_check = getattr(database, "integrity_check", None)
    if repository_check is not None:
        detail = repository_check(verify_blobs=True)
        result["repository_check"] = detail
        result["ok"] = bool(result["ok"] and detail.get("ok"))
    if not result["ok"]:
        raise MigrationError(f"destination database integrity is invalid: {result}")
    return result


def import_plan(
    database: Any,
    database_path: Path,
    plan: MigrationPlan,
    *,
    legacy_database: Path,
    legacy_artifacts: Path | None = None,
) -> dict[str, Any]:
    config_result = migrate_legacy_config(database, legacy_database)
    begin = getattr(database, "begin_collection_batch", None)
    if begin is None:
        raise MigrationError("StartStopDatabase does not implement begin_collection_batch")
    batch_id = begin(
        machine_count=len({entry.metadata["machine_id"] for entry in plan.remote_entries}),
        metadata={
            "mode": "legacy_migration",
            "remote_files": len(plan.remote_entries),
            "local_files": len(plan.local_entries),
        },
    )
    totals = {
        "inventoried": plan.file_count,
        "stable": plan.file_count,
        "downloaded": 0,
        "ingested": 0,
        "unchanged_content": 0,
        "errors": 0,
        "total_bytes": plan.total_bytes,
    }
    batch_finished = False
    try:
        for entry in plan.remote_entries:
            result = _verify_ingest_result(
                database.ingest_staged_file(
                    entry.path, dict(entry.metadata), batch_id
                ),
                entry,
            )
            totals["ingested"] += 1
            if isinstance(result, dict) and (
                result.get("changed") is False
                or result.get("status")
                in {"unchanged", "unchanged_content", "deduplicated"}
            ):
                totals["unchanged_content"] += 1
        for entry in plan.local_entries:
            result = _verify_ingest_result(
                database.import_source_path(
                    entry.path, dict(entry.metadata), batch_id
                ),
                entry,
            )
            totals["ingested"] += 1
            if isinstance(result, dict) and (
                result.get("changed") is False
                or result.get("status")
                in {"unchanged", "unchanged_content", "deduplicated"}
            ):
                totals["unchanged_content"] += 1
        finish_result = _finish_batch(database, batch_id, "completed", totals)
        batch_finished = True
    except Exception:
        totals["errors"] += 1
        if not batch_finished:
            try:
                _finish_batch(database, batch_id, "failed", totals)
            except Exception:
                pass
        raise

    snapshot = database.freeze_snapshot(candidate_only=True)
    if not isinstance(snapshot, dict) or snapshot.get("id") is None:
        if isinstance(finish_result, dict) and finish_result.get("snapshot_id") is not None:
            snapshot = {"id": finish_result["snapshot_id"]}
        else:
            raise MigrationError("StartStopDatabase did not return a frozen snapshot")

    artifact_result = None
    if legacy_artifacts is not None:
        if not legacy_artifacts.is_dir():
            raise MigrationError(f"legacy artifact directory does not exist: {legacy_artifacts}")
        artifact_result = database.publish_artifacts(
            legacy_artifacts,
            snapshot["id"],
            int(config_result.get("revision", 0)),
            "legacy",
        )

    status = database.repository_status()
    integrity = _database_integrity(database, database_path)
    return {
        "status": "completed",
        "counts": {
            "remote_files": len(plan.remote_entries),
            "local_import_files": len(plan.local_entries),
            "total_files": plan.file_count,
        },
        "bytes": plan.total_bytes,
        "unique_blobs": int(status.get("blob_count", plan.unique_blobs)),
        "source_unique_blobs": plan.unique_blobs,
        "snapshot": snapshot,
        "configuration": config_result,
        "artifacts": artifact_result,
        "repository": status,
        "integrity": integrity,
    }


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    repository_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(
        description="Validate and import the legacy start/stop repository into SQLite"
    )
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument(
        "--manifest",
        type=Path,
        help="defaults to SOURCE_ROOT/_采集记录/文件清单.jsonl",
    )
    parser.add_argument(
        "--beijing-root",
        type=Path,
        help="defaults to SOURCE_ROOT/北京数据列",
    )
    parser.add_argument(
        "--legacy-database",
        type=Path,
        default=repository_root / "state" / "echem-platform.sqlite3",
    )
    parser.add_argument("--legacy-artifacts", type=Path)
    parser.add_argument("--result-json", type=Path)
    parser.add_argument("--expect-current-baseline", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    source_root = args.source_root.resolve()
    manifest = (
        args.manifest.resolve()
        if args.manifest is not None
        else source_root / "_采集记录" / "文件清单.jsonl"
    )
    beijing_root = (
        args.beijing_root.resolve()
        if args.beijing_root is not None
        else source_root / "北京数据列"
    )
    try:
        # This full source verification intentionally occurs before the destination
        # database is opened, so a stale/tampered legacy tree causes no writes.
        plan = build_plan(
            source_root,
            manifest,
            beijing_root,
            expect_current_baseline=args.expect_current_baseline,
        )
        repository_root = str(Path(__file__).resolve().parent.parent)
        if repository_root not in sys.path:
            sys.path.insert(0, repository_root)
        from echem_platform.start_stop_database import StartStopDatabase

        database_path = args.database.resolve()
        database_path.parent.mkdir(parents=True, exist_ok=True)
        database = StartStopDatabase(database_path)
        summary = import_plan(
            database,
            database_path,
            plan,
            legacy_database=args.legacy_database.resolve(),
            legacy_artifacts=(
                args.legacy_artifacts.resolve()
                if args.legacy_artifacts is not None
                else None
            ),
        )
        if args.result_json is not None:
            _write_json_atomic(args.result_json.resolve(), summary)
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"migration failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
