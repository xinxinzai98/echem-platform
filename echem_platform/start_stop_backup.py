from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping


BACKUP_MANIFEST_SCHEMA_VERSION = 1
SCHEDULED_BACKUP_STATUS_SCHEMA_VERSION = 1
SCHEDULED_BACKUP_STATUS_NAME = "scheduled-backup-status.json"
DEFAULT_SAFETY_MARGIN_BYTES = 1024 * 1024 * 1024
DEFAULT_SAFETY_MARGIN_RATIO = 0.25
TASK_EPHEMERAL_MARGIN_BYTES = 256 * 1024 * 1024
_HASH_CHUNK_SIZE = 1024 * 1024
_BACKUP_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}\.sqlite3$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class StartStopBackupError(RuntimeError):
    """Base error for a backup operation that did not publish a backup."""


class UnsafeBackupPathError(ValueError):
    """Raised when a backup target could escape its explicit backup directory."""


class InsufficientStorageError(StartStopBackupError):
    """Raised when a storage preflight cannot reserve enough free space."""

    def __init__(self, preflight: Mapping[str, Any]):
        self.preflight = dict(preflight)
        super().__init__(
            "insufficient free space for start-stop backup "
            f"(required={self.preflight['required_bytes']}, "
            f"available={self.preflight['available_bytes']})"
        )


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _utc_iso(value: dt.datetime | None = None) -> str:
    current = value or _utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=dt.timezone.utc)
    return current.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def _utc_datetime(value: dt.datetime | None = None) -> dt.datetime:
    current = value or _utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=dt.timezone.utc)
    return current.astimezone(dt.timezone.utc)


def _parse_utc(value: Any) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        # Directory fsync is not supported on every platform/filesystem.
        pass
    finally:
        os.close(descriptor)


def _existing_directory(path: Path) -> Path:
    candidate = path
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            raise FileNotFoundError(path)
        candidate = parent
    if not candidate.is_dir():
        raise NotADirectoryError(candidate)
    return candidate


def _resolve_database_path(database_path: str | Path) -> Path:
    raw = Path(database_path).expanduser()
    try:
        path = raw.resolve(strict=True)
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"start-stop database does not exist: {raw}") from exc
    if not path.is_file():
        raise ValueError(f"start-stop database is not a regular file: {path}")
    return path


def _resolve_backup_root(backup_dir: str | Path, *, create: bool) -> Path:
    raw = Path(backup_dir).expanduser()
    if raw.exists() and not raw.is_dir():
        raise NotADirectoryError(raw)
    existed = raw.exists()
    if create:
        raw.mkdir(parents=True, exist_ok=True, mode=0o700)
    elif not existed:
        _existing_directory(raw)
    root = raw.resolve(strict=create or existed)
    if create and not existed:
        # mkdir honours umask; explicitly make a newly-created backup leaf private.
        root.chmod(0o700)
    return root


def _safe_backup_name(value: str | None, *, created_at: dt.datetime | None) -> str:
    if value is None:
        moment = _utc_datetime(created_at)
        stamp = moment.strftime("%Y%m%dT%H%M%S%fZ")
        value = f"start-stop-{stamp}-{uuid.uuid4().hex[:8]}.sqlite3"
    raw = str(value)
    if "\x00" in raw or Path(raw).name != raw or raw in {".", ".."}:
        raise UnsafeBackupPathError("backup_name must be a single file name")
    if not raw.endswith(".sqlite3"):
        raw += ".sqlite3"
    if not _BACKUP_NAME.fullmatch(raw):
        raise UnsafeBackupPathError(
            "backup_name may contain only ASCII letters, numbers, dot, dash and underscore"
        )
    return raw


def _target_paths(
    database_path: Path,
    backup_root: Path,
    backup_name: str,
) -> tuple[Path, Path]:
    target = backup_root / backup_name
    manifest = backup_root / f"{backup_name}.manifest.json"
    if target.parent != backup_root or manifest.parent != backup_root:
        raise UnsafeBackupPathError("backup target escaped the backup directory")
    if target.is_symlink() or manifest.is_symlink():
        raise UnsafeBackupPathError("backup targets must not be symbolic links")
    if target.resolve(strict=False) == database_path:
        raise UnsafeBackupPathError("backup target must not be the live database")
    if target.exists() or manifest.exists():
        raise FileExistsError(f"backup target already exists: {target}")
    return target, manifest


def _sqlite_uri(path: Path, *, immutable: bool = False) -> str:
    query = "mode=ro"
    if immutable:
        query += "&immutable=1"
    return f"{path.resolve().as_uri()}?{query}"


def _sqlite_details(path: Path, *, immutable: bool = False) -> dict[str, int]:
    try:
        connection = sqlite3.connect(
            _sqlite_uri(path, immutable=immutable),
            uri=True,
            timeout=15,
            isolation_level=None,
        )
        try:
            connection.execute("PRAGMA query_only=ON")
            page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
            page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
            user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise StartStopBackupError(f"cannot read SQLite database metadata: {exc}") from exc
    return {
        "page_size": page_size,
        "page_count": page_count,
        "logical_bytes": page_size * page_count,
        "user_version": user_version,
    }


def operational_check_database(
    database_path: str | Path,
    *,
    expected_schema_version: int | None = None,
    required_tables: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Run a fast, read-only operational check without copying or hashing BLOBs.

    This is the routine-operation gate.  It validates that SQLite opens, the
    expected schema is active, required tables exist, foreign keys are
    consistent, and BLOB size metadata agrees with SQLite's stored lengths.
    It deliberately does not replace a full backup or a full integrity/hash
    verification before a schema migration or database repair.
    """

    source = _resolve_database_path(database_path)
    expected = (
        None
        if expected_schema_version is None
        else int(expected_schema_version)
    )
    if expected is not None and expected < 0:
        raise ValueError("expected_schema_version must be non-negative")
    normalized_tables = tuple(sorted({str(name) for name in required_tables}))
    if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) for name in normalized_tables):
        raise ValueError("required_tables contains an unsafe table name")

    started = dt.datetime.now(dt.timezone.utc)
    errors: list[str] = []
    foreign_key_errors = 0
    blob_metadata_errors = 0
    pointer_errors = 0
    try:
        connection = sqlite3.connect(
            _sqlite_uri(source),
            uri=True,
            timeout=15,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA busy_timeout=15000")
            schema_version = int(
                connection.execute("PRAGMA user_version").fetchone()[0]
            )
            if expected is not None and schema_version != expected:
                errors.append(
                    f"schema version is {schema_version}; expected {expected}"
                )
            observed_tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_schema WHERE type='table'"
                )
            }
            missing_tables = sorted(set(normalized_tables) - observed_tables)
            if missing_tables:
                errors.append("missing required tables: " + ", ".join(missing_tables))

            foreign_rows = connection.execute("PRAGMA foreign_key_check").fetchmany(101)
            foreign_key_errors = len(foreign_rows)
            if foreign_key_errors:
                errors.append(
                    "foreign-key violations detected"
                    + (" (more than 100)" if foreign_key_errors > 100 else "")
                )

            if "content_blobs" in observed_tables:
                blob_metadata_errors = int(
                    connection.execute(
                        """SELECT COUNT(*) FROM content_blobs
                           WHERE size_bytes<>length(content)
                              OR length(sha256)<>64"""
                    ).fetchone()[0]
                )
                if blob_metadata_errors:
                    errors.append("BLOB size/hash metadata mismatches detected")

            pointer_tables = {
                "source_current",
                "source_selections",
                "source_versions",
            }
            if pointer_tables.issubset(observed_tables):
                pointer_errors = int(
                    connection.execute(
                        """SELECT COUNT(*)
                           FROM source_current sc
                           JOIN source_selections ss ON ss.id=sc.selection_id
                           JOIN source_versions sv ON sv.id=ss.source_version_id
                           WHERE sc.source_id<>ss.source_id
                              OR ss.source_id<>sv.source_id"""
                    ).fetchone()[0]
                )
                if pointer_errors:
                    errors.append("current-source pointer mismatches detected")
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise StartStopBackupError(
            f"SQLite operational check failed to run: {exc}"
        ) from exc

    elapsed_ms = max(
        0,
        round(
            (
                dt.datetime.now(dt.timezone.utc) - started
            ).total_seconds()
            * 1000
        ),
    )
    return {
        "ok": not errors,
        "mode": "operational_metadata",
        "schema_version": schema_version,
        "expected_schema_version": expected,
        "required_table_count": len(normalized_tables),
        "foreign_key_errors": foreign_key_errors,
        "blob_metadata_errors": blob_metadata_errors,
        "pointer_errors": pointer_errors,
        "elapsed_ms": elapsed_ms,
        "checked_utc": _utc_iso(),
        "errors": errors,
    }


def _read_scheduled_backup_status(
    root: Path,
    status_file: str | Path | None = None,
) -> dict[str, Any]:
    path = (
        Path(status_file)
        if status_file is not None
        else root / SCHEDULED_BACKUP_STATUS_NAME
    )
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 16 * 1024:
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        return {}
    state = str(payload.get("state") or "")
    if state not in {"never_run", "running", "completed", "failed", "skipped"}:
        return {}
    result: dict[str, Any] = {"scheduled_state": state}
    for source, target in (
        ("started_utc", "scheduled_started_utc"),
        ("completed_utc", "scheduled_completed_utc"),
        ("due_utc", "scheduled_due_utc"),
        ("next_due_utc", "scheduled_next_due_utc"),
    ):
        value = payload.get(source)
        if isinstance(value, str) and _parse_utc(value) is not None:
            result[target] = value
    exit_code = payload.get("exit_code")
    if isinstance(exit_code, int) and not isinstance(exit_code, bool):
        result["scheduled_exit_code"] = max(0, min(exit_code, 255))
    return result


def storage_preflight(
    database_path: str | Path,
    backup_dir: str | Path,
    *,
    estimated_output_bytes: int = 0,
    safety_margin_bytes: int = DEFAULT_SAFETY_MARGIN_BYTES,
    safety_margin_ratio: float = DEFAULT_SAFETY_MARGIN_RATIO,
    available_bytes_override: int | None = None,
) -> dict[str, Any]:
    """Estimate whether a backup plus planned output fits on the target filesystem.

    The check is read-only.  It deliberately counts the larger of the live
    database file and SQLite's logical page size, then adds any live WAL bytes,
    planned analysis output, and an explicit safety margin.
    """

    source = _resolve_database_path(database_path)
    destination = _resolve_backup_root(backup_dir, create=False)
    output_bytes = int(estimated_output_bytes)
    fixed_margin = int(safety_margin_bytes)
    ratio = float(safety_margin_ratio)
    if output_bytes < 0 or fixed_margin < 0 or ratio < 0:
        raise ValueError("storage estimates and safety margins must be non-negative")
    details = _sqlite_details(source)
    database_file_bytes = source.stat().st_size
    wal_path = Path(f"{source}-wal")
    wal_bytes = wal_path.stat().st_size if wal_path.is_file() else 0
    estimated_backup_bytes = max(database_file_bytes, details["logical_bytes"])
    workload_bytes = estimated_backup_bytes + wal_bytes + output_bytes
    effective_margin = max(fixed_margin, math.ceil(workload_bytes * ratio))
    required_bytes = workload_bytes + effective_margin
    available_bytes = (
        shutil.disk_usage(_existing_directory(destination)).free
        if available_bytes_override is None
        else int(available_bytes_override)
    )
    if available_bytes < 0:
        raise ValueError("available_bytes_override must be non-negative")
    shortfall = max(0, required_bytes - available_bytes)
    return {
        "ok": shortfall == 0,
        "database_file_bytes": database_file_bytes,
        "database_logical_bytes": details["logical_bytes"],
        "database_wal_bytes": wal_bytes,
        "estimated_backup_bytes": estimated_backup_bytes,
        "estimated_output_bytes": output_bytes,
        "safety_margin_bytes": effective_margin,
        "safety_margin_ratio": ratio,
        "required_bytes": required_bytes,
        "available_bytes": available_bytes,
        "shortfall_bytes": shortfall,
        "destination": str(destination),
    }


def _volume_usage(path: Path) -> tuple[int, Any]:
    existing = _existing_directory(path)
    return int(os.stat(existing).st_dev), shutil.disk_usage(existing)


def _task_storage_preflight(
    database_path: Path,
    *,
    scratch_dir: Path,
    cache_dir: Path,
    estimated_snapshot_bytes: int,
    estimated_output_bytes: int,
) -> dict[str, Any]:
    """Check every filesystem that receives bytes during one analysis task."""

    snapshot_bytes = max(0, int(estimated_snapshot_bytes))
    output_bytes = max(0, int(estimated_output_bytes))
    details = _sqlite_details(database_path)
    database_bytes = max(
        int(database_path.stat().st_size),
        int(details["logical_bytes"]),
    )
    wal_path = Path(f"{database_path}-wal")
    wal_bytes = wal_path.stat().st_size if wal_path.is_file() else 0
    components = (
        {
            "role": "database",
            "path": database_path.parent,
            "workload_bytes": database_bytes + wal_bytes + output_bytes,
            "minimum_margin_bytes": DEFAULT_SAFETY_MARGIN_BYTES,
        },
        {
            "role": "scratch",
            "path": scratch_dir,
            "workload_bytes": snapshot_bytes + output_bytes,
            "minimum_margin_bytes": TASK_EPHEMERAL_MARGIN_BYTES,
        },
        {
            "role": "cache",
            "path": cache_dir,
            "workload_bytes": output_bytes,
            "minimum_margin_bytes": TASK_EPHEMERAL_MARGIN_BYTES,
        },
    )
    volumes: dict[int, dict[str, Any]] = {}
    for component in components:
        device, usage = _volume_usage(Path(component["path"]))
        volume = volumes.setdefault(
            device,
            {
                "roles": [],
                "workload_bytes": 0,
                "minimum_margin_bytes": 0,
                "free_bytes": int(usage.free),
            },
        )
        volume["roles"].append(component["role"])
        volume["workload_bytes"] += int(component["workload_bytes"])
        volume["minimum_margin_bytes"] = max(
            int(volume["minimum_margin_bytes"]),
            int(component["minimum_margin_bytes"]),
        )
        volume["free_bytes"] = min(int(volume["free_bytes"]), int(usage.free))

    blocked_roles: list[str] = []
    required_total = 0
    shortfall_total = 0
    for volume in volumes.values():
        workload = int(volume["workload_bytes"])
        margin = max(
            int(volume["minimum_margin_bytes"]),
            math.ceil(workload * DEFAULT_SAFETY_MARGIN_RATIO),
        )
        required = workload + margin
        shortfall = max(0, required - int(volume["free_bytes"]))
        required_total += required
        shortfall_total += shortfall
        if shortfall:
            blocked_roles.extend(str(role) for role in volume["roles"])
    return {
        "ok": shortfall_total == 0,
        "required_bytes": required_total,
        "shortfall_bytes": shortfall_total,
        "volume_count": len(volumes),
        "blocked_roles": sorted(set(blocked_roles)),
    }


def _quick_check(path: Path) -> list[str]:
    try:
        connection = sqlite3.connect(
            _sqlite_uri(path, immutable=True),
            uri=True,
            timeout=15,
            isolation_level=None,
        )
        try:
            rows = [str(row[0]) for row in connection.execute("PRAGMA quick_check")]
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise StartStopBackupError(f"SQLite quick_check failed to run: {exc}") from exc
    if rows != ["ok"]:
        raise StartStopBackupError(f"SQLite quick_check did not pass: {rows!r}")
    return rows


def _copy_sqlite_database(
    source_path: Path,
    destination_path: Path,
    *,
    pages: int,
    sleep: float,
    progress: Callable[[int, int, int], None] | None,
) -> None:
    source: sqlite3.Connection | None = None
    destination: sqlite3.Connection | None = None
    try:
        source = sqlite3.connect(
            _sqlite_uri(source_path),
            uri=True,
            timeout=30,
            isolation_level=None,
        )
        source.execute("PRAGMA query_only=ON")
        destination = sqlite3.connect(destination_path, timeout=30)
        source.backup(
            destination,
            pages=max(1, int(pages)),
            progress=progress,
            sleep=max(0.0, float(sleep)),
        )
        destination.commit()
        destination.execute("PRAGMA journal_mode=DELETE")
        destination.commit()
    except sqlite3.Error as exc:
        raise StartStopBackupError(f"SQLite online backup failed: {exc}") from exc
    finally:
        if destination is not None:
            destination.close()
        if source is not None:
            source.close()


def create_backup(
    database_path: str | Path,
    backup_dir: str | Path,
    *,
    backup_name: str | None = None,
    estimated_output_bytes: int = 0,
    safety_margin_bytes: int = DEFAULT_SAFETY_MARGIN_BYTES,
    safety_margin_ratio: float = DEFAULT_SAFETY_MARGIN_RATIO,
    available_bytes_override: int | None = None,
    metadata: Mapping[str, Any] | None = None,
    created_at: dt.datetime | None = None,
    pages: int = 1024,
    sleep: float = 0.01,
    progress: Callable[[int, int, int], None] | None = None,
) -> dict[str, Any]:
    """Create and atomically publish a verified SQLite online backup.

    Existing backup names are never overwritten.  Both temporary files live in
    the destination directory, so ``os.replace`` cannot cross filesystems.
    """

    source = _resolve_database_path(database_path)
    # Validate metadata before performing an expensive backup.
    metadata_value = dict(metadata or {})
    try:
        json.dumps(metadata_value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ValueError("backup metadata must be JSON serializable") from exc

    preflight = storage_preflight(
        source,
        backup_dir,
        estimated_output_bytes=estimated_output_bytes,
        safety_margin_bytes=safety_margin_bytes,
        safety_margin_ratio=safety_margin_ratio,
        available_bytes_override=available_bytes_override,
    )
    if not preflight["ok"]:
        raise InsufficientStorageError(preflight)

    root = _resolve_backup_root(backup_dir, create=True)
    name = _safe_backup_name(backup_name, created_at=created_at)
    target, manifest_path = _target_paths(source, root, name)
    lock_path = root / f".{name}.lock"
    try:
        lock_descriptor = os.open(
            lock_path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
    except FileExistsError as exc:
        raise StartStopBackupError(f"backup target is already being created: {name}") from exc
    try:
        os.write(lock_descriptor, f"pid={os.getpid()}\n".encode("ascii"))
    finally:
        os.close(lock_descriptor)

    temp_database: Path | None = None
    temp_manifest: Path | None = None
    published_database = False
    try:
        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{name}.", suffix=".tmp", dir=root
        )
        os.close(descriptor)
        temp_database = Path(temp_name)
        temp_database.unlink()
        _copy_sqlite_database(
            source,
            temp_database,
            pages=pages,
            sleep=sleep,
            progress=progress,
        )
        temp_database.chmod(0o600)
        quick_check = _quick_check(temp_database)
        sha256 = _sha256_file(temp_database)
        size_bytes = temp_database.stat().st_size
        copied_details = _sqlite_details(temp_database, immutable=True)
        source_details = _sqlite_details(source)
        manifest = {
            "schema_version": BACKUP_MANIFEST_SCHEMA_VERSION,
            "kind": "start_stop_sqlite_backup",
            "created_utc": _utc_iso(created_at),
            "database_file": name,
            "sha256": sha256,
            "size_bytes": size_bytes,
            "quick_check": quick_check,
            "sqlite": {
                "page_size": copied_details["page_size"],
                "page_count": copied_details["page_count"],
                "user_version": copied_details["user_version"],
            },
            "source": {
                "database_file": source.name,
                "user_version": source_details["user_version"],
            },
            "metadata": metadata_value,
        }
        descriptor, manifest_name = tempfile.mkstemp(
            prefix=f".{name}.manifest.", suffix=".tmp", dir=root
        )
        temp_manifest = Path(manifest_name)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_canonical_json(manifest))
            handle.flush()
            os.fsync(handle.fileno())
        temp_manifest.chmod(0o600)
        _fsync_file(temp_database)

        # Recheck after the potentially long copy while holding our name lock.
        _target_paths(source, root, name)
        os.replace(temp_database, target)
        temp_database = None
        published_database = True
        os.replace(temp_manifest, manifest_path)
        temp_manifest = None
        _fsync_directory(root)
        return {
            "ok": True,
            "backup_path": str(target),
            "manifest_path": str(manifest_path),
            "created_utc": manifest["created_utc"],
            "sha256": sha256,
            "size_bytes": size_bytes,
            "quick_check": "ok",
            "preflight": preflight,
            "manifest": manifest,
        }
    except BaseException:
        # The name did not exist before this operation; never leave an
        # unmanifested backup looking successful after a publication failure.
        if published_database and target.exists() and not manifest_path.exists():
            target.unlink()
        raise
    finally:
        for temporary in (temp_database, temp_manifest):
            if temporary is not None:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def _manifest_path_for_backup(backup_path: Path) -> Path:
    return backup_path.with_name(f"{backup_path.name}.manifest.json")


def verify_backup(
    backup_path: str | Path,
    *,
    manifest_path: str | Path | None = None,
    verify_hash: bool = True,
    run_quick_check: bool = True,
) -> dict[str, Any]:
    """Verify a backup without modifying it; validation errors are returned."""

    backup = Path(backup_path)
    manifest_file = (
        Path(manifest_path) if manifest_path is not None else _manifest_path_for_backup(backup)
    )
    errors: list[str] = []
    manifest: dict[str, Any] = {}
    hash_verified = False
    quick_check_verified = False
    if backup.is_symlink() or manifest_file.is_symlink():
        errors.append("symbolic links are not accepted as backup artifacts")
    if not backup.is_file():
        errors.append("backup database is missing")
    if not manifest_file.is_file():
        errors.append("backup manifest is missing")
    if manifest_file.is_file() and not manifest_file.is_symlink():
        try:
            raw = json.loads(manifest_file.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("manifest root is not an object")
            manifest = raw
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            errors.append(f"backup manifest is unreadable: {exc}")
    if manifest:
        if manifest.get("schema_version") != BACKUP_MANIFEST_SCHEMA_VERSION:
            errors.append("backup manifest schema is unsupported")
        if manifest.get("kind") != "start_stop_sqlite_backup":
            errors.append("backup manifest kind is invalid")
        if manifest.get("database_file") != backup.name:
            errors.append("backup manifest names a different database file")
        expected_sha = str(manifest.get("sha256") or "")
        if not _SHA256.fullmatch(expected_sha):
            errors.append("backup manifest SHA-256 is invalid")
        expected_size = manifest.get("size_bytes")
        if not isinstance(expected_size, int) or expected_size < 0:
            errors.append("backup manifest size is invalid")
        elif backup.is_file() and backup.stat().st_size != expected_size:
            errors.append("backup size does not match the manifest")
        if verify_hash and backup.is_file() and _SHA256.fullmatch(expected_sha):
            hash_verified = True
            if _sha256_file(backup) != expected_sha:
                errors.append("backup SHA-256 does not match the manifest")
    if run_quick_check and backup.is_file() and not backup.is_symlink():
        try:
            _quick_check(backup)
            quick_check_verified = True
        except StartStopBackupError as exc:
            errors.append(str(exc))
    return {
        "valid": not errors,
        "backup_path": str(backup),
        "manifest_path": str(manifest_file),
        "created_utc": str(manifest.get("created_utc") or ""),
        "sha256": str(manifest.get("sha256") or ""),
        "size_bytes": backup.stat().st_size if backup.is_file() else 0,
        "hash_verified": hash_verified,
        "quick_check_verified": quick_check_verified,
        "errors": errors,
        "manifest": manifest,
    }


def _status_entry(manifest_path: Path, *, verify: bool) -> dict[str, Any]:
    suffix = ".manifest.json"
    database_name = manifest_path.name[: -len(suffix)]
    if not _BACKUP_NAME.fullmatch(database_name):
        return {
            "valid": False,
            "backup_path": "",
            "manifest_path": str(manifest_path),
            "created_utc": "",
            "size_bytes": 0,
            "errors": ["unsafe backup manifest file name"],
        }
    backup_path = manifest_path.parent / database_name
    return verify_backup(
        backup_path,
        manifest_path=manifest_path,
        verify_hash=verify,
        run_quick_check=verify,
    )


def read_backup_status(
    backup_dir: str | Path,
    *,
    verify_latest: bool = True,
    verify_all: bool = False,
) -> dict[str, Any]:
    """Read backup inventory and verify at least the newest backup by default."""

    root = _resolve_backup_root(backup_dir, create=False)
    manifest_paths = sorted(root.glob("*.sqlite3.manifest.json"))
    lightweight = [_status_entry(path, verify=False) for path in manifest_paths]
    lightweight.sort(
        key=lambda item: (_parse_utc(item.get("created_utc")) or dt.datetime.min.replace(tzinfo=dt.timezone.utc), item["manifest_path"]),
        reverse=True,
    )
    backups: list[dict[str, Any]] = []
    for index, entry in enumerate(lightweight):
        should_verify = verify_all or (verify_latest and index == 0)
        if should_verify:
            entry = _status_entry(Path(entry["manifest_path"]), verify=True)
        backups.append(entry)
    known = {
        Path(item["backup_path"]).name
        for item in backups
        if item.get("backup_path")
    }
    orphan_databases = sorted(
        path.name
        for path in root.glob("*.sqlite3")
        if path.name not in known
    )
    invalid_count = sum(1 for item in backups if not item.get("valid"))
    latest = backups[0] if backups else None
    return {
        "available": bool(backups),
        "backup_dir": str(root),
        "backup_count": len(backups),
        "invalid_count": invalid_count,
        "orphan_database_count": len(orphan_databases),
        "orphan_databases": orphan_databases,
        "latest": latest,
        "backups": backups,
    }


def build_safety_status(
    database_path: str | Path,
    backup_dir: str | Path,
    *,
    estimated_output_bytes: int = 0,
    scratch_dir: str | Path | None = None,
    cache_dir: str | Path | None = None,
    estimated_snapshot_bytes: int = 0,
    low_space_ratio: float = 0.15,
    backup_policy_mode: str = "risk_tiered",
    scheduled_background_enabled: bool = False,
    scheduled_background_label: str = "",
    scheduled_status_file: str | Path | None = None,
) -> dict[str, Any]:
    """Return a path-free status payload suitable for the local web UI.

    This intentionally performs only lightweight manifest validation.  Full
    backup hashing remains an explicit backup/verification operation so the
    frequently-polled status endpoint never rereads a multi-gigabyte file.
    """

    ratio = float(low_space_ratio)
    if not 0.0 <= ratio < 1.0:
        raise ValueError("low_space_ratio must be in the range [0, 1)")
    source = _resolve_database_path(database_path)
    root = _resolve_backup_root(backup_dir, create=False)
    usage = shutil.disk_usage(source.parent)
    task_preflight = _task_storage_preflight(
        source,
        scratch_dir=(
            Path(scratch_dir).resolve()
            if scratch_dir is not None
            else source.parent
        ),
        cache_dir=(
            Path(cache_dir).resolve()
            if cache_dir is not None
            else source.parent
        ),
        estimated_snapshot_bytes=estimated_snapshot_bytes,
        estimated_output_bytes=estimated_output_bytes,
    )
    backup_preflight = storage_preflight(
        source,
        root,
        estimated_output_bytes=0,
    )
    inventory = read_backup_status(root, verify_latest=False)
    latest = inventory.get("latest")
    latest = latest if isinstance(latest, dict) else {}
    manifest = latest.get("manifest")
    manifest = manifest if isinstance(manifest, dict) else {}
    expected_sha = str(manifest.get("sha256") or "")
    manifest_quick_check = manifest.get("quick_check")
    same_filesystem = os.stat(source).st_dev == os.stat(root).st_dev
    used_percent = (
        round((usage.used / usage.total) * 100.0, 1) if usage.total else 0.0
    )
    latest_manifest_verified = bool(
        latest
        and latest.get("valid")
        and _SHA256.fullmatch(expected_sha)
        and manifest_quick_check == ["ok"]
    )
    policy_mode = str(backup_policy_mode or "").strip().lower()
    if policy_mode != "risk_tiered":
        policy_mode = "risk_tiered"
    schedule_label = " ".join(str(scheduled_background_label or "").split())
    if len(schedule_label) > 80:
        schedule_label = schedule_label[:80].rstrip()
    scheduled_status = _read_scheduled_backup_status(
        root,
        scheduled_status_file,
    )
    return {
        "storage": {
            "ok": bool(task_preflight["ok"]),
            "preflight_ok": bool(task_preflight["ok"]),
            "total_bytes": int(usage.total),
            "free_bytes": int(usage.free),
            "used_bytes": int(usage.used),
            "used_percent": used_percent,
            "low_space": bool(usage.total and usage.free / usage.total < ratio),
            "low_space_ratio": ratio,
            "required_bytes": int(task_preflight["required_bytes"]),
            "shortfall_bytes": int(task_preflight["shortfall_bytes"]),
            "volume_count": int(task_preflight["volume_count"]),
            "blocked_roles": list(task_preflight["blocked_roles"]),
        },
        "backup": {
            "configured": True,
            "available": bool(inventory.get("available")),
            "backup_count": int(inventory.get("backup_count") or 0),
            "invalid_count": int(inventory.get("invalid_count") or 0),
            "orphan_database_count": int(
                inventory.get("orphan_database_count") or 0
            ),
            "latest_created_utc": str(latest.get("created_utc") or ""),
            "latest_valid": latest_manifest_verified,
            # The status endpoint deliberately avoids rereading a multi-GB
            # database.  Be explicit that this is a current manifest/name/size
            # check, while the SHA-256 and SQLite checks were recorded when the
            # backup was created (or by an explicit verify operation).
            "latest_verification_level": "manifest_and_size",
            "latest_created_with_full_verification": bool(
                _SHA256.fullmatch(expected_sha)
                and manifest_quick_check == ["ok"]
            ),
            "destination_preflight_ok": bool(backup_preflight["ok"]),
            "destination_required_bytes": int(
                backup_preflight["required_bytes"]
            ),
            "destination_shortfall_bytes": int(
                backup_preflight["shortfall_bytes"]
            ),
            "latest_size_bytes": int(latest.get("size_bytes") or 0),
            "same_filesystem": same_filesystem,
            "off_disk": not same_filesystem,
            "policy_mode": policy_mode,
            "routine_full_backup_required": False,
            "schema_change_full_backup_required": True,
            "database_repair_full_backup_required": True,
            "scheduled_background_enabled": bool(
                scheduled_background_enabled
            ),
            "scheduled_background_label": schedule_label,
            **scheduled_status,
        },
    }


def plan_backup_retention(
    backup_dir: str | Path,
    *,
    keep_latest: int = 7,
    keep_daily_days: int = 30,
    keep_weekly_weeks: int = 12,
    protected_names: tuple[str, ...] = (),
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Return a conservative retention plan and never delete any file.

    Only well-formed database/manifest pairs can become deletion candidates.
    Unknown, orphaned, symlinked, malformed, future-dated, or explicitly
    protected artifacts are reported as protected for manual review.
    """

    for value, label in (
        (keep_latest, "keep_latest"),
        (keep_daily_days, "keep_daily_days"),
        (keep_weekly_weeks, "keep_weekly_weeks"),
    ):
        if int(value) < 0:
            raise ValueError(f"{label} must be non-negative")
    root = _resolve_backup_root(backup_dir, create=False)
    current = now or _utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=dt.timezone.utc)
    current = current.astimezone(dt.timezone.utc)
    protected_set = {_safe_backup_name(name, created_at=None) for name in protected_names}

    records: list[dict[str, Any]] = []
    protected: list[dict[str, Any]] = []
    recognized_paths: set[Path] = set()
    for manifest_path in root.glob("*.sqlite3.manifest.json"):
        entry = _status_entry(manifest_path, verify=False)
        backup_raw = str(entry.get("backup_path") or "")
        backup_path = Path(backup_raw) if backup_raw else None
        if backup_path is not None:
            recognized_paths.update({backup_path, manifest_path})
        created = _parse_utc(entry.get("created_utc"))
        metadata = entry.get("manifest", {}).get("metadata", {})
        marked = isinstance(metadata, dict) and bool(metadata.get("retain"))
        reason = ""
        if not entry.get("valid"):
            reason = "malformed or incomplete backup pair"
        elif backup_path is None or not backup_path.is_file():
            reason = "backup database is missing"
        elif backup_path.is_symlink() or manifest_path.is_symlink():
            reason = "symbolic link requires manual review"
        elif created is None:
            reason = "backup creation time is invalid"
        elif created > current + dt.timedelta(minutes=5):
            reason = "future-dated backup requires manual review"
        elif backup_path.name in protected_set or marked:
            reason = "explicitly protected"
        if reason:
            protected.append(
                {
                    "backup_path": backup_raw,
                    "manifest_path": str(manifest_path),
                    "reason": reason,
                }
            )
            continue
        records.append(
            {
                "backup_path": str(backup_path),
                "manifest_path": str(manifest_path),
                "database_file": backup_path.name,
                "created_utc": _utc_iso(created),
                "created": created,
                "size_bytes": backup_path.stat().st_size + manifest_path.stat().st_size,
            }
        )

    for path in root.iterdir():
        if path in recognized_paths or path.name.startswith("."):
            continue
        if path.is_file() and (
            path.name.endswith(".sqlite3") or path.name.endswith(".manifest.json")
        ):
            protected.append(
                {"backup_path": str(path), "manifest_path": "", "reason": "orphan artifact"}
            )

    records.sort(key=lambda item: (item["created"], item["database_file"]), reverse=True)
    keep_reasons: dict[str, set[str]] = {}

    def keep(record: Mapping[str, Any], reason: str) -> None:
        keep_reasons.setdefault(str(record["database_file"]), set()).add(reason)

    for record in records[: int(keep_latest)]:
        keep(record, "latest")

    daily_seen: set[dt.date] = set()
    weekly_seen: set[tuple[int, int]] = set()
    daily_cutoff = current - dt.timedelta(days=int(keep_daily_days))
    weekly_cutoff = current - dt.timedelta(weeks=int(keep_weekly_weeks))
    for record in records:
        created = record["created"]
        if created >= daily_cutoff and created.date() not in daily_seen:
            daily_seen.add(created.date())
            keep(record, "daily")
        iso = created.isocalendar()
        week = (iso.year, iso.week)
        if created >= weekly_cutoff and week not in weekly_seen:
            weekly_seen.add(week)
            keep(record, "weekly")

    kept: list[dict[str, Any]] = []
    deletion_candidates: list[dict[str, Any]] = []
    for record in records:
        public = {key: value for key, value in record.items() if key != "created"}
        reasons = sorted(keep_reasons.get(record["database_file"], set()))
        if reasons:
            kept.append(dict(public, reasons=reasons))
        else:
            deletion_candidates.append(
                dict(public, requires_verification=True)
            )
    return {
        "mode": "plan_only",
        "files_deleted": 0,
        "backup_dir": str(root),
        "policy": {
            "keep_latest": int(keep_latest),
            "keep_daily_days": int(keep_daily_days),
            "keep_weekly_weeks": int(keep_weekly_weeks),
        },
        "keep": kept,
        "delete_candidates": deletion_candidates,
        "protected": protected,
        "potential_reclaim_bytes": sum(
            int(item["size_bytes"]) for item in deletion_candidates
        ),
    }
