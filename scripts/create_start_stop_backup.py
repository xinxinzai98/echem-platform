#!/usr/bin/env python3
"""Create or inspect Start-stop Studio SQLite backups.

The command deliberately emits JSON only.  Its default summaries contain file
names and integrity metadata, but no absolute path to the live database.  A
human running the command locally can opt in to path details with
``--show-local-paths``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from echem_platform.start_stop_backup import (  # noqa: E402
    DEFAULT_SAFETY_MARGIN_BYTES,
    DEFAULT_SAFETY_MARGIN_RATIO,
    InsufficientStorageError,
    StartStopBackupError,
    UnsafeBackupPathError,
    create_backup,
    operational_check_database,
    plan_backup_retention,
    read_backup_status,
)
from echem_platform.start_stop_database import (  # noqa: E402
    OPERATIONAL_REQUIRED_TABLES,
    SCHEMA_VERSION as DATABASE_SCHEMA_VERSION,
)


SCHEMA_VERSION = 1
EXIT_SUCCESS = 0
EXIT_VERIFICATION_FAILED = 1
EXIT_USAGE = 2
EXIT_INSUFFICIENT_STORAGE = 3
EXIT_OPERATION_FAILED = 4


def _non_negative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def _non_negative_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create a verified online backup of the Start-stop Studio SQLite "
            "database, verify existing backups, or print a deletion-free "
            "retention plan."
        )
    )
    parser.add_argument(
        "--database",
        type=Path,
        required=True,
        help="live Start-stop Studio SQLite database",
    )
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=None,
        help="dedicated backup directory",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check-live",
        action="store_true",
        help=(
            "run the fast read-only operational metadata/FK check; never "
            "copies or hashes BLOB content"
        ),
    )
    mode.add_argument(
        "--verify-only",
        action="store_true",
        help="verify every published backup without creating a backup",
    )
    mode.add_argument(
        "--retention-plan",
        "--retention-plan-only",
        dest="retention_plan",
        action="store_true",
        help="print a conservative retention plan; never deletes files",
    )
    parser.add_argument(
        "--reason",
        choices=("manual", "scheduled", "schema_migration", "database_repair"),
        default="manual",
        help="why a full backup is being created",
    )
    parser.add_argument(
        "--backup-name",
        help=(
            "optional safe file name; by default a unique UTC-stamped name is "
            "generated"
        ),
    )
    parser.add_argument(
        "--estimated-output-bytes",
        type=_non_negative_int,
        default=0,
        help="additional output expected on the backup filesystem",
    )
    parser.add_argument(
        "--safety-margin-bytes",
        type=_non_negative_int,
        default=DEFAULT_SAFETY_MARGIN_BYTES,
    )
    parser.add_argument(
        "--safety-margin-ratio",
        type=_non_negative_float,
        default=DEFAULT_SAFETY_MARGIN_RATIO,
    )
    parser.add_argument("--keep-latest", type=_non_negative_int, default=7)
    parser.add_argument("--keep-daily-days", type=_non_negative_int, default=30)
    parser.add_argument("--keep-weekly-weeks", type=_non_negative_int, default=12)
    parser.add_argument(
        "--show-local-paths",
        "--local-details",
        dest="show_local_paths",
        action="store_true",
        help="opt in to absolute local paths in the JSON result",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="write compact JSON",
    )
    args = parser.parse_args(argv)
    if not args.check_live and args.backup_dir is None:
        parser.error("--backup-dir is required unless --check-live is used")
    if args.backup_name and (
        args.check_live or args.verify_only or args.retention_plan
    ):
        parser.error("--backup-name is available only when creating a backup")
    if args.reason != "manual" and (
        args.check_live or args.verify_only or args.retention_plan
    ):
        parser.error("--reason is available only when creating a backup")
    return args


def _resolved(path: Path) -> str:
    return str(path.expanduser().resolve(strict=False))


def _local_paths(
    args: argparse.Namespace,
    *,
    backup_path: str | Path | None = None,
    manifest_path: str | Path | None = None,
) -> dict[str, str]:
    paths = {
        "database": _resolved(args.database),
    }
    if args.backup_dir is not None:
        paths["backup_dir"] = _resolved(args.backup_dir)
    if backup_path:
        paths["backup"] = _resolved(Path(backup_path))
    if manifest_path:
        paths["manifest"] = _resolved(Path(manifest_path))
    return paths


def _redact_text(value: Any, args: argparse.Namespace) -> str:
    text = str(value)
    if args.show_local_paths:
        return text
    replacements = (
        (_resolved(args.database), "<database>"),
        (str(args.database.expanduser()), "<database>"),
        *(
            (
                (_resolved(args.backup_dir), "<backup-dir>"),
                (str(args.backup_dir.expanduser()), "<backup-dir>"),
            )
            if args.backup_dir is not None
            else ()
        ),
    )
    for secret, replacement in replacements:
        if secret:
            text = text.replace(secret, replacement)
    return text


def _safe_preflight(value: Mapping[str, Any]) -> dict[str, Any]:
    fields = (
        "ok",
        "database_file_bytes",
        "database_logical_bytes",
        "database_wal_bytes",
        "estimated_backup_bytes",
        "estimated_output_bytes",
        "safety_margin_bytes",
        "safety_margin_ratio",
        "required_bytes",
        "available_bytes",
        "shortfall_bytes",
    )
    return {field: value.get(field) for field in fields if field in value}


def _safe_backup_entry(
    entry: Mapping[str, Any], args: argparse.Namespace
) -> dict[str, Any]:
    backup_raw = str(entry.get("backup_path") or "")
    manifest_raw = str(entry.get("manifest_path") or "")
    result: dict[str, Any] = {
        "database_file": Path(backup_raw).name if backup_raw else "",
        "manifest_file": Path(manifest_raw).name if manifest_raw else "",
        "valid": bool(entry.get("valid")),
        "created_utc": str(entry.get("created_utc") or ""),
        "size_bytes": int(entry.get("size_bytes") or 0),
        "hash_verified": bool(entry.get("hash_verified", False)),
        "quick_check_verified": bool(entry.get("quick_check_verified", False)),
        "errors": [
            _redact_text(error, args) for error in (entry.get("errors") or [])
        ],
    }
    sha256 = str(entry.get("sha256") or "")
    if sha256:
        result["sha256"] = sha256
    if args.show_local_paths:
        result["local_paths"] = {
            "backup": _resolved(Path(backup_raw)) if backup_raw else "",
            "manifest": _resolved(Path(manifest_raw)) if manifest_raw else "",
        }
    return result


def _safe_retention_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    backup_raw = str(entry.get("backup_path") or "")
    manifest_raw = str(entry.get("manifest_path") or "")
    result: dict[str, Any] = {
        "database_file": str(entry.get("database_file") or Path(backup_raw).name),
        "manifest_file": Path(manifest_raw).name if manifest_raw else "",
    }
    for field in (
        "created_utc",
        "size_bytes",
        "reasons",
        "reason",
        "requires_verification",
    ):
        if field in entry:
            result[field] = entry[field]
    return result


def _safe_retention_plan(
    plan: Mapping[str, Any], args: argparse.Namespace
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "mode": "plan_only",
        "files_deleted": 0,
        "policy": dict(plan.get("policy") or {}),
        "keep": [_safe_retention_entry(item) for item in plan.get("keep", [])],
        "delete_candidates": [
            _safe_retention_entry(item)
            for item in plan.get("delete_candidates", [])
        ],
        "protected": [
            _safe_retention_entry(item) for item in plan.get("protected", [])
        ],
        "potential_reclaim_bytes": int(plan.get("potential_reclaim_bytes") or 0),
    }
    if args.show_local_paths:
        result["local_paths"] = {"backup_dir": _resolved(args.backup_dir)}
    return result


def _base_result(command: str, *, ok: bool, exit_code: int) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "command": command,
        "ok": ok,
        "exit_code": exit_code,
    }


def _retention_plan(args: argparse.Namespace) -> dict[str, Any]:
    return plan_backup_retention(
        args.backup_dir,
        keep_latest=args.keep_latest,
        keep_daily_days=args.keep_daily_days,
        keep_weekly_weeks=args.keep_weekly_weeks,
    )


def _run_create(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    assert args.backup_dir is not None
    created = create_backup(
        args.database,
        args.backup_dir,
        backup_name=args.backup_name,
        estimated_output_bytes=args.estimated_output_bytes,
        safety_margin_bytes=args.safety_margin_bytes,
        safety_margin_ratio=args.safety_margin_ratio,
        metadata={
            "created_by": "create_start_stop_backup.py",
            "reason": args.reason,
        },
    )
    backup_path = Path(created["backup_path"])
    manifest_path = Path(created["manifest_path"])
    result = _base_result("create", ok=True, exit_code=EXIT_SUCCESS)
    result["backup"] = {
        "database_file": backup_path.name,
        "manifest_file": manifest_path.name,
        "created_utc": created["created_utc"],
        "sha256": created["sha256"],
        "size_bytes": created["size_bytes"],
        "quick_check": created["quick_check"],
    }
    result["preflight"] = _safe_preflight(created["preflight"])
    if args.show_local_paths:
        result["local_paths"] = _local_paths(
            args,
            backup_path=backup_path,
            manifest_path=manifest_path,
        )
    return result, EXIT_SUCCESS


def _run_verify(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    assert args.backup_dir is not None
    status = read_backup_status(
        args.backup_dir,
        verify_latest=False,
        verify_all=True,
    )
    valid = (
        int(status.get("backup_count") or 0) > 0
        and int(status.get("invalid_count") or 0) == 0
        and int(status.get("orphan_database_count") or 0) == 0
    )
    exit_code = EXIT_SUCCESS if valid else EXIT_VERIFICATION_FAILED
    result = _base_result("verify", ok=valid, exit_code=exit_code)
    result.update(
        {
            "available": bool(status.get("available")),
            "backup_count": int(status.get("backup_count") or 0),
            "invalid_count": int(status.get("invalid_count") or 0),
            "orphan_database_count": int(
                status.get("orphan_database_count") or 0
            ),
            "orphan_databases": list(status.get("orphan_databases") or []),
            "backups": [
                _safe_backup_entry(entry, args)
                for entry in status.get("backups", [])
            ],
        }
    )
    result["latest"] = (
        _safe_backup_entry(status["latest"], args)
        if status.get("latest")
        else None
    )
    if args.show_local_paths:
        result["local_paths"] = _local_paths(args)
    return result, exit_code


def _run_retention(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    assert args.backup_dir is not None
    plan = _retention_plan(args)
    result = _base_result("retention_plan", ok=True, exit_code=EXIT_SUCCESS)
    result["retention_plan"] = _safe_retention_plan(plan, args)
    return result, EXIT_SUCCESS


def _run_check_live(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    checked = operational_check_database(
        args.database,
        expected_schema_version=DATABASE_SCHEMA_VERSION,
        required_tables=OPERATIONAL_REQUIRED_TABLES,
    )
    ok = checked.get("ok") is True
    exit_code = EXIT_SUCCESS if ok else EXIT_VERIFICATION_FAILED
    result = _base_result("check_live", ok=ok, exit_code=exit_code)
    result["check"] = checked
    if args.show_local_paths:
        result["local_paths"] = _local_paths(args)
    return result, exit_code


def _error_result(
    args: argparse.Namespace,
    *,
    command: str,
    error_code: str,
    message: str,
    exit_code: int,
    details: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], int]:
    result = _base_result(command, ok=False, exit_code=exit_code)
    result["error"] = {
        "code": error_code,
        "message": _redact_text(message, args),
    }
    if details:
        result["error"]["details"] = dict(details)
    if args.show_local_paths:
        result["local_paths"] = _local_paths(args)
    return result, exit_code


def _execute(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    command = (
        "check_live"
        if args.check_live
        else "verify"
        if args.verify_only
        else "retention_plan"
        if args.retention_plan
        else "create"
    )
    try:
        if args.check_live:
            return _run_check_live(args)
        if args.verify_only:
            return _run_verify(args)
        if args.retention_plan:
            return _run_retention(args)
        return _run_create(args)
    except InsufficientStorageError as exc:
        return _error_result(
            args,
            command=command,
            error_code="insufficient_storage",
            message="insufficient free space for a safe backup",
            exit_code=EXIT_INSUFFICIENT_STORAGE,
            details={"preflight": _safe_preflight(exc.preflight)},
        )
    except (UnsafeBackupPathError, ValueError, FileNotFoundError, NotADirectoryError) as exc:
        return _error_result(
            args,
            command=command,
            error_code="invalid_input",
            message=str(exc),
            exit_code=EXIT_USAGE,
        )
    except (StartStopBackupError, OSError) as exc:
        return _error_result(
            args,
            command=command,
            error_code="operation_failed",
            message=str(exc),
            exit_code=EXIT_OPERATION_FAILED,
        )
    except Exception as exc:  # pragma: no cover - final JSON safety boundary
        message = str(exc) if args.show_local_paths else "unexpected backup failure"
        return _error_result(
            args,
            command=command,
            error_code="unexpected_failure",
            message=message,
            exit_code=EXIT_OPERATION_FAILED,
        )


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    result, exit_code = _execute(args)
    print(
        json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
            indent=None if args.compact else 2,
        )
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
