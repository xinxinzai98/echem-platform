from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import unicodedata
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Mapping

from .start_stop_backup import operational_check_database


SCHEMA_VERSION = 5
AUTO_UPDATE_INTERVAL_MINUTES = (30, 60, 180, 360, 720, 1440)
_CHUNK_SIZE = 1024 * 1024
_DEFAULT_INLINE_BLOB_MAX_BYTES = 64 * 1024 * 1024
_MAX_LIVE_PREVIEW_JSON_BYTES = 8 * 1024 * 1024
_SQLITE_JOURNAL_MODES = frozenset({"WAL", "DELETE"})
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_.-]{1,160}$")
_DRIVE_PATH = re.compile(r"^[A-Za-z]:")
_INTERNAL_ARTIFACT_PATHS = frozenset(
    {".start-stop-artifacts.json", ".start-stop-manifest.json"}
)
ARTIFACT_MANIFEST_NAME = "artifact_manifest.json"
ARTIFACT_CHECKSUMS_NAME = "SHA256SUMS.txt"
ARTIFACT_SEAL_SCHEMA_VERSION = 1
OPERATIONAL_REQUIRED_TABLES = (
    "analysis_runs",
    "artifact_current",
    "artifact_entries",
    "artifact_generations",
    "artifact_selections",
    "audit_log",
    "auto_update_current",
    "auto_update_revisions",
    "auto_update_runtime",
    "candidate_current",
    "candidate_marks",
    "collection_batches",
    "collection_config_current",
    "collection_config_revisions",
    "collection_issues",
    "content_blobs",
    "dataset_snapshot_files",
    "dataset_snapshots",
    "job_events",
    "jobs",
    "live_preview_current",
    "live_preview_revisions",
    "live_preview_runtime",
    "material_config_current",
    "material_config_revisions",
    "source_current",
    "source_selections",
    "source_versions",
    "sources",
)


class AutoUpdateConfigConflict(ValueError):
    """Raised when an automatic-update settings revision is stale."""


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def _hash_path(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK_SIZE):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _sealed_artifact_files(root: Path) -> list[tuple[str, Path]]:
    if not root.is_dir() or root.is_symlink():
        raise ValueError("artifact output_dir must be a directory")
    rows: list[tuple[str, Path]] = []
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError(
                "artifact output contains a symbolic link: "
                + path.relative_to(root).as_posix()
            )
        if not path.is_file():
            continue
        relative = _relative_path(
            path.relative_to(root).as_posix(), label="artifact path"
        )
        if relative in _INTERNAL_ARTIFACT_PATHS:
            continue
        rows.append((relative, path))
    rows.sort(key=lambda item: item[0].casefold())
    _assert_no_path_conflicts([relative for relative, _ in rows])
    return rows


def seal_artifact_directory(
    output_dir: str | Path,
    *,
    snapshot_id: int,
    config_revision: int,
    kind: str,
    provenance: Mapping[str, Any],
) -> dict[str, Any]:
    """Write the canonical manifest/checksums after every payload is final."""
    root = Path(output_dir)
    manifest_path = root / ARTIFACT_MANIFEST_NAME
    checksums_path = root / ARTIFACT_CHECKSUMS_NAME
    manifest_path.unlink(missing_ok=True)
    checksums_path.unlink(missing_ok=True)

    payload_files = _sealed_artifact_files(root)
    payload_rows = []
    for relative, path in payload_files:
        sha256, size_bytes = _hash_path(path)
        payload_rows.append(
            {
                "path": relative,
                "sha256": sha256,
                "size_bytes": size_bytes,
            }
        )
    manifest = {
        "schema_version": ARTIFACT_SEAL_SCHEMA_VERSION,
        "snapshot_id": int(snapshot_id),
        "config_revision": int(config_revision),
        "kind": str(kind),
        "sealed_utc": _utc_now(),
        "provenance": dict(provenance),
        "files": payload_rows,
    }
    _atomic_write_text(
        manifest_path,
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )

    checksum_rows = []
    for relative, path in _sealed_artifact_files(root):
        if relative == ARTIFACT_CHECKSUMS_NAME:
            continue
        sha256, _ = _hash_path(path)
        checksum_rows.append(f"{sha256}  {relative}")
    _atomic_write_text(checksums_path, "\n".join(checksum_rows) + "\n")
    return validate_sealed_artifact_directory(root)


def validate_sealed_artifact_directory(
    output_dir: str | Path,
) -> dict[str, Any]:
    """Verify that the canonical manifest and SHA256SUMS cover one exact tree."""
    root = Path(output_dir)
    manifest_path = root / ARTIFACT_MANIFEST_NAME
    checksums_path = root / ARTIFACT_CHECKSUMS_NAME
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ValueError("artifact manifest is missing")
    if not checksums_path.is_file() or checksums_path.is_symlink():
        raise ValueError("artifact checksum list is missing")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("artifact manifest is invalid") from exc
    if not isinstance(manifest, dict):
        raise ValueError("artifact manifest must be an object")
    if manifest.get("schema_version") != ARTIFACT_SEAL_SCHEMA_VERSION:
        raise ValueError("artifact manifest schema is unsupported")
    manifest_files = manifest.get("files")
    if not isinstance(manifest_files, list):
        raise ValueError("artifact manifest files must be an array")

    actual = {
        relative: path for relative, path in _sealed_artifact_files(root)
    }
    expected_payload_paths = set(actual) - {
        ARTIFACT_MANIFEST_NAME,
        ARTIFACT_CHECKSUMS_NAME,
    }
    manifest_rows: dict[str, Mapping[str, Any]] = {}
    for raw in manifest_files:
        if not isinstance(raw, Mapping):
            raise ValueError("artifact manifest contains an invalid file row")
        relative = _relative_path(raw.get("path"), label="artifact manifest path")
        if relative in manifest_rows:
            raise ValueError(f"artifact manifest path is duplicated: {relative}")
        if relative in {ARTIFACT_MANIFEST_NAME, ARTIFACT_CHECKSUMS_NAME}:
            raise ValueError(f"artifact manifest contains a control file: {relative}")
        manifest_rows[relative] = raw
    if set(manifest_rows) != expected_payload_paths:
        missing = sorted(expected_payload_paths - set(manifest_rows))
        extra = sorted(set(manifest_rows) - expected_payload_paths)
        raise ValueError(
            "artifact manifest does not match output tree"
            f"; missing={missing[:5]}; extra={extra[:5]}"
        )
    for relative in sorted(manifest_rows, key=str.casefold):
        sha256, size_bytes = _hash_path(actual[relative])
        row = manifest_rows[relative]
        if row.get("sha256") != sha256 or row.get("size_bytes") != size_bytes:
            raise ValueError(f"artifact manifest mismatch: {relative}")

    checksum_rows: dict[str, str] = {}
    try:
        raw_checksum_rows = checksums_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError("artifact checksum list is invalid") from exc
    for raw in raw_checksum_rows:
        if not raw or "  " not in raw:
            raise ValueError("artifact checksum row is invalid")
        sha256, raw_relative = raw.split("  ", 1)
        relative = _relative_path(raw_relative, label="artifact checksum path")
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ValueError(f"artifact checksum is invalid: {relative}")
        if relative in checksum_rows:
            raise ValueError(f"artifact checksum path is duplicated: {relative}")
        if relative == ARTIFACT_CHECKSUMS_NAME:
            raise ValueError("artifact checksum list cannot checksum itself")
        checksum_rows[relative] = sha256
    expected_checksum_paths = set(actual) - {ARTIFACT_CHECKSUMS_NAME}
    if set(checksum_rows) != expected_checksum_paths:
        missing = sorted(expected_checksum_paths - set(checksum_rows))
        extra = sorted(set(checksum_rows) - expected_checksum_paths)
        raise ValueError(
            "artifact checksum list does not match output tree"
            f"; missing={missing[:5]}; extra={extra[:5]}"
        )
    for relative in sorted(checksum_rows, key=str.casefold):
        sha256, _ = _hash_path(actual[relative])
        if checksum_rows[relative] != sha256:
            raise ValueError(f"artifact checksum mismatch: {relative}")
    return {
        "schema_version": ARTIFACT_SEAL_SCHEMA_VERSION,
        "file_count": len(actual),
        "payload_file_count": len(manifest_rows),
        "manifest_sha256": _hash_path(manifest_path)[0],
        "checksums_sha256": _hash_path(checksums_path)[0],
        "manifest": manifest,
    }


def _relative_path(value: Any, *, label: str = "path") -> str:
    raw = unicodedata.normalize("NFC", str(value or ""))
    if not raw or "\x00" in raw:
        raise ValueError(f"{label} is empty or contains NUL")
    if raw.startswith(("/", "\\", "//")) or _DRIVE_PATH.match(raw):
        raise ValueError(f"{label} must be relative")
    raw = raw.replace("\\", "/")
    parts = raw.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"{label} contains an unsafe component")
    result = PurePosixPath(*parts).as_posix()
    if result == "." or PurePosixPath(result).is_absolute():
        raise ValueError(f"{label} must be relative")
    return result


def _path_key(value: str) -> tuple[str, ...]:
    return tuple(part.casefold() for part in PurePosixPath(value).parts)


def _assert_no_path_conflicts(paths: list[str], *, reserved: tuple[str, ...] = ()) -> None:
    seen: dict[tuple[str, ...], str] = {}
    reserved_keys = {_path_key(_relative_path(item)) for item in reserved}
    for path in paths:
        normalized = _relative_path(path)
        key = _path_key(normalized)
        if key in reserved_keys:
            raise ValueError(f"reserved repository path: {normalized}")
        if key in seen:
            raise ValueError(f"conflicting repository paths: {seen[key]} / {normalized}")
        for index in range(1, len(key)):
            if key[:index] in seen:
                raise ValueError(f"file/directory path conflict: {seen[key[:index]]} / {normalized}")
        if any(existing[: len(key)] == key for existing in seen if len(existing) > len(key)):
            raise ValueError(f"file/directory path conflict: {normalized}")
        seen[key] = normalized


def _candidate_from_file(path: Path, logical_path: str) -> tuple[bool, str]:
    """Match the analysis script's cheap, content-based candidate gate."""
    stem = PurePosixPath(logical_path.replace("\\", "/")).stem.casefold()
    try:
        with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
            lines = [handle.readline() for _ in range(3)]
    except OSError:
        return False, ""
    if any("ID_GalSquareWave" in line for line in lines):
        return True, "gal_square_wave"
    if stem == "adt" and any("ID_ScriptMethod" in line for line in lines):
        return True, "adt_script_method"
    return False, ""


def _source_locator(value: Any) -> str:
    """Normalize a remote identifier without treating it as a local path."""
    raw = unicodedata.normalize("NFC", str(value or ""))
    if not raw or "\x00" in raw:
        raise ValueError("remote_path is empty or contains NUL")
    # Windows paths are valid source identities.  They are never joined to a
    # local filesystem path; only repository_path is materialized.
    return raw.replace("/", "\\").casefold()


def _modified_ns(value: Any) -> int:
    raw = str(value or "").strip()
    if not raw:
        return 0
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return int(parsed.timestamp() * 1_000_000_000)
    except (OverflowError, ValueError):
        return 0


class StartStopDatabase:
    """Content-addressed repository for start-stop source data and artifacts."""

    def __init__(
        self,
        path: str | Path,
        *,
        busy_timeout_ms: int = 15_000,
        journal_mode: str | None = None,
        inline_blob_max_bytes: int = _DEFAULT_INLINE_BLOB_MAX_BYTES,
    ):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.busy_timeout_ms = max(1_000, min(int(busy_timeout_ms), 300_000))
        requested_journal_mode = str(
            journal_mode
            if journal_mode is not None
            else os.environ.get("START_STOP_SQLITE_JOURNAL_MODE", "WAL")
        ).strip().upper()
        if requested_journal_mode not in _SQLITE_JOURNAL_MODES:
            raise ValueError("start-stop SQLite journal mode must be WAL or DELETE")
        self.journal_mode = requested_journal_mode
        self.inline_blob_max_bytes = max(
            0,
            min(int(inline_blob_max_bytes), 256 * 1024 * 1024),
        )
        self._lock = threading.RLock()
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA mmap_size=0")
        connection.execute(
            "PRAGMA synchronous=FULL"
            if self.journal_mode == "DELETE"
            else "PRAGMA synchronous=NORMAL"
        )
        return connection

    @contextlib.contextmanager
    def session(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._lock, self.session() as connection:
            actual_journal_mode = str(
                connection.execute(
                    f"PRAGMA journal_mode={self.journal_mode}"
                ).fetchone()[0]
            ).upper()
            if actual_journal_mode != self.journal_mode:
                raise RuntimeError(
                    "start-stop SQLite journal mode could not be applied"
                )
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"start-stop database schema {version} is newer than supported {SCHEMA_VERSION}"
                )
            if version == SCHEMA_VERSION:
                self._validate_live_preview_v5(connection)
                return
            if version == 1:
                self._migrate_collection_config_v2(connection)
                version = 2
            if version == 2:
                self._migrate_auto_update_v3(connection)
                version = 3
            if version == 3:
                self._migrate_job_provenance_v4(connection)
                version = 4
            if version == 4:
                self._migrate_live_preview_v5(connection)
                return
            try:
                schema = (
                    """
                    CREATE TABLE content_blobs(
                        id INTEGER PRIMARY KEY,
                        sha256 TEXT NOT NULL CHECK(length(sha256)=64),
                        size_bytes INTEGER NOT NULL CHECK(size_bytes>=0),
                        content BLOB NOT NULL,
                        created_utc TEXT NOT NULL,
                        UNIQUE(sha256,size_bytes),
                        CHECK(length(content)=size_bytes)
                    );
                    CREATE TABLE collection_batches(
                        id TEXT PRIMARY KEY,
                        status TEXT NOT NULL,
                        machine_count INTEGER NOT NULL DEFAULT 0,
                        metadata_json TEXT NOT NULL DEFAULT '{}',
                        totals_json TEXT NOT NULL DEFAULT '{}',
                        started_utc TEXT NOT NULL,
                        finished_utc TEXT NOT NULL DEFAULT ''
                    );
                    CREATE TABLE collection_issues(
                        id INTEGER PRIMARY KEY,
                        batch_id TEXT NOT NULL REFERENCES collection_batches(id),
                        severity TEXT NOT NULL,
                        code TEXT NOT NULL,
                        message TEXT NOT NULL,
                        machine_id TEXT NOT NULL DEFAULT '',
                        root_label TEXT NOT NULL DEFAULT '',
                        remote_path TEXT NOT NULL DEFAULT '',
                        detail_json TEXT NOT NULL DEFAULT '{}',
                        created_utc TEXT NOT NULL
                    );
                    CREATE TABLE sources(
                        id INTEGER PRIMARY KEY,
                        machine_id TEXT NOT NULL,
                        root_label TEXT NOT NULL,
                        remote_path TEXT NOT NULL,
                        created_utc TEXT NOT NULL,
                        UNIQUE(machine_id,root_label,remote_path)
                    );
                    CREATE TABLE source_versions(
                        id INTEGER PRIMARY KEY,
                        source_id INTEGER NOT NULL REFERENCES sources(id),
                        version_number INTEGER NOT NULL CHECK(version_number>=1),
                        blob_id INTEGER NOT NULL REFERENCES content_blobs(id),
                        repository_path TEXT NOT NULL,
                        size_bytes INTEGER NOT NULL CHECK(size_bytes>=0),
                        last_write_ticks INTEGER NOT NULL DEFAULT 0,
                        source_modified_utc TEXT NOT NULL DEFAULT '',
                        modified_ns INTEGER NOT NULL DEFAULT 0,
                        is_candidate INTEGER NOT NULL CHECK(is_candidate IN (0,1)),
                        candidate_kind TEXT NOT NULL DEFAULT '',
                        metadata_json TEXT NOT NULL DEFAULT '{}',
                        batch_id TEXT REFERENCES collection_batches(id),
                        created_utc TEXT NOT NULL,
                        UNIQUE(source_id,version_number)
                    );
                    CREATE TABLE source_selections(
                        id INTEGER PRIMARY KEY,
                        source_id INTEGER NOT NULL REFERENCES sources(id),
                        source_version_id INTEGER NOT NULL REFERENCES source_versions(id),
                        batch_id TEXT REFERENCES collection_batches(id),
                        reason TEXT NOT NULL,
                        selected_utc TEXT NOT NULL
                    );
                    CREATE TABLE source_current(
                        source_id INTEGER PRIMARY KEY REFERENCES sources(id),
                        selection_id INTEGER NOT NULL REFERENCES source_selections(id)
                    );
                    CREATE TABLE candidate_marks(
                        id INTEGER PRIMARY KEY,
                        source_version_id INTEGER NOT NULL REFERENCES source_versions(id),
                        is_candidate INTEGER NOT NULL CHECK(is_candidate IN (0,1)),
                        candidate_kind TEXT NOT NULL DEFAULT '',
                        reason TEXT NOT NULL DEFAULT '',
                        created_utc TEXT NOT NULL
                    );
                    CREATE TABLE candidate_current(
                        source_version_id INTEGER PRIMARY KEY REFERENCES source_versions(id),
                        mark_id INTEGER NOT NULL REFERENCES candidate_marks(id)
                    );
                    CREATE TABLE dataset_snapshots(
                        id INTEGER PRIMARY KEY,
                        dataset_fingerprint TEXT NOT NULL UNIQUE,
                        candidate_only INTEGER NOT NULL CHECK(candidate_only IN (0,1)),
                        batch_id TEXT REFERENCES collection_batches(id),
                        metadata_json TEXT NOT NULL DEFAULT '{}',
                        manifest_json TEXT NOT NULL,
                        file_count INTEGER NOT NULL,
                        total_bytes INTEGER NOT NULL,
                        created_utc TEXT NOT NULL
                    );
                    CREATE TABLE dataset_snapshot_files(
                        snapshot_id INTEGER NOT NULL REFERENCES dataset_snapshots(id),
                        ordinal INTEGER NOT NULL,
                        source_version_id INTEGER NOT NULL REFERENCES source_versions(id),
                        repository_path TEXT NOT NULL,
                        sha256 TEXT NOT NULL,
                        size_bytes INTEGER NOT NULL,
                        modified_ns INTEGER NOT NULL DEFAULT 0,
                        source_modified_utc TEXT NOT NULL DEFAULT '',
                        PRIMARY KEY(snapshot_id,ordinal),
                        UNIQUE(snapshot_id,repository_path)
                    );
                    CREATE TABLE artifact_generations(
                        id INTEGER PRIMARY KEY,
                        kind TEXT NOT NULL,
                        snapshot_id INTEGER NOT NULL REFERENCES dataset_snapshots(id),
                        config_revision INTEGER NOT NULL,
                        manifest_json TEXT NOT NULL,
                        manifest_sha256 TEXT NOT NULL,
                        file_count INTEGER NOT NULL,
                        total_bytes INTEGER NOT NULL,
                        created_utc TEXT NOT NULL
                    );
                    CREATE TABLE artifact_entries(
                        generation_id INTEGER NOT NULL REFERENCES artifact_generations(id),
                        ordinal INTEGER NOT NULL,
                        blob_id INTEGER NOT NULL REFERENCES content_blobs(id),
                        relative_path TEXT NOT NULL,
                        sha256 TEXT NOT NULL,
                        size_bytes INTEGER NOT NULL,
                        modified_ns INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY(generation_id,ordinal),
                        UNIQUE(generation_id,relative_path)
                    );
                    CREATE TABLE artifact_selections(
                        id INTEGER PRIMARY KEY,
                        kind TEXT NOT NULL,
                        generation_id INTEGER NOT NULL REFERENCES artifact_generations(id),
                        selected_utc TEXT NOT NULL
                    );
                    CREATE TABLE artifact_current(
                        kind TEXT PRIMARY KEY,
                        selection_id INTEGER NOT NULL REFERENCES artifact_selections(id)
                    );
                    CREATE TABLE material_config_revisions(
                        revision INTEGER PRIMARY KEY CHECK(revision>=1),
                        dataset_fingerprint TEXT NOT NULL,
                        materials_json TEXT NOT NULL,
                        created_utc TEXT NOT NULL
                    );
                    CREATE TABLE material_config_current(
                        id INTEGER PRIMARY KEY CHECK(id=1),
                        revision INTEGER NOT NULL REFERENCES material_config_revisions(revision)
                    );
                    CREATE TABLE audit_log(
                        id INTEGER PRIMARY KEY,
                        created_utc TEXT NOT NULL,
                        action TEXT NOT NULL,
                        target TEXT NOT NULL,
                        detail_json TEXT NOT NULL
                    );
                    CREATE INDEX idx_source_versions_source ON source_versions(source_id,id DESC);
                    CREATE INDEX idx_source_versions_batch ON source_versions(batch_id);
                    CREATE INDEX idx_snapshot_files_snapshot ON dataset_snapshot_files(snapshot_id,ordinal);
                    CREATE INDEX idx_artifacts_kind ON artifact_generations(kind,id DESC);
                    CREATE INDEX idx_issues_batch ON collection_issues(batch_id,id);
                    """
                )
                immutable_tables = (
                    "content_blobs", "sources", "source_versions", "source_selections",
                    "candidate_marks", "collection_issues", "dataset_snapshots",
                    "dataset_snapshot_files", "artifact_generations", "artifact_entries",
                    "artifact_selections", "material_config_revisions", "audit_log",
                )
                triggers = "".join(
                    f"""
                        CREATE TRIGGER {table}_immutable_update BEFORE UPDATE ON {table}
                        BEGIN SELECT RAISE(ABORT,'{table} is immutable'); END;
                        CREATE TRIGGER {table}_immutable_delete BEFORE DELETE ON {table}
                        BEGIN SELECT RAISE(ABORT,'{table} is immutable'); END;
                        """
                    for table in immutable_tables
                )
                connection.executescript(
                    "BEGIN IMMEDIATE;\n"
                    + schema
                    + triggers
                    + "PRAGMA user_version=1;\nCOMMIT;"
                )
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise
            self._migrate_collection_config_v2(connection)
            self._migrate_auto_update_v3(connection)
            self._migrate_job_provenance_v4(connection)
            self._migrate_live_preview_v5(connection)

    @staticmethod
    def _migrate_collection_config_v2(connection: sqlite3.Connection) -> None:
        """Add versioned experiment-machine root settings to an existing v1 DB."""
        try:
            connection.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE collection_config_revisions(
                    revision INTEGER PRIMARY KEY CHECK(revision>=1),
                    machines_json TEXT NOT NULL,
                    created_utc TEXT NOT NULL
                );
                CREATE TABLE collection_config_current(
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    revision INTEGER NOT NULL REFERENCES collection_config_revisions(revision)
                );
                CREATE TRIGGER collection_config_revisions_immutable_update
                BEFORE UPDATE ON collection_config_revisions
                BEGIN SELECT RAISE(ABORT,'collection_config_revisions is immutable'); END;
                CREATE TRIGGER collection_config_revisions_immutable_delete
                BEFORE DELETE ON collection_config_revisions
                BEGIN SELECT RAISE(ABORT,'collection_config_revisions is immutable'); END;
                PRAGMA user_version=2;
                COMMIT;
                """
            )
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise

    @staticmethod
    def _migrate_auto_update_v3(connection: sqlite3.Connection) -> None:
        """Add versioned, disabled-by-default automatic update settings."""
        allowed = ",".join(str(value) for value in AUTO_UPDATE_INTERVAL_MINUTES)
        try:
            connection.executescript(
                f"""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS auto_update_revisions(
                    revision INTEGER PRIMARY KEY CHECK(revision>=1),
                    enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
                    interval_minutes INTEGER NOT NULL
                        CHECK(interval_minutes IN ({allowed})),
                    created_utc TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS auto_update_current(
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    revision INTEGER NOT NULL REFERENCES auto_update_revisions(revision)
                );
                CREATE TABLE IF NOT EXISTS auto_update_runtime(
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    next_run_utc TEXT NOT NULL DEFAULT '',
                    last_attempt_utc TEXT NOT NULL DEFAULT '',
                    last_started_utc TEXT NOT NULL DEFAULT '',
                    last_finished_utc TEXT NOT NULL DEFAULT '',
                    last_status TEXT NOT NULL DEFAULT 'never_run'
                        CHECK(last_status IN (
                            'never_run','running','completed',
                            'completed_with_warnings','failed','busy',
                            'interrupted_by_restart'
                        )),
                    active_job_id TEXT NOT NULL DEFAULT ''
                );
                INSERT OR IGNORE INTO auto_update_runtime(id) VALUES(1);
                CREATE TRIGGER IF NOT EXISTS auto_update_revisions_immutable_update
                BEFORE UPDATE ON auto_update_revisions
                BEGIN SELECT RAISE(ABORT,'auto_update_revisions is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS auto_update_revisions_immutable_delete
                BEFORE DELETE ON auto_update_revisions
                BEGIN SELECT RAISE(ABORT,'auto_update_revisions is immutable'); END;
                PRAGMA user_version=3;
                COMMIT;
                """
            )
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise

    @staticmethod
    def _validate_job_provenance_v4(connection: sqlite3.Connection) -> None:
        """Fail closed when the durable task/provenance schema is incomplete."""
        required_columns = {
            "jobs": {
                "id", "action", "requested_via", "retry_of_job_id", "status",
                "stage", "failure_class", "failure_code", "message",
                "progress_json", "result_json",
                "collection_batch_id", "snapshot_id", "config_revision",
                "artifact_generation_id", "worker_instance_id", "created_utc",
                "started_utc", "completed_utc", "updated_utc",
            },
            "job_events": {
                "id", "job_id", "event_type", "level", "status", "stage",
                "percent", "message", "detail_json", "created_utc",
            },
            "analysis_runs": {
                "id", "job_id", "snapshot_id", "config_revision",
                "artifact_generation_id", "dataset_fingerprint",
                "artifact_manifest_sha256", "analysis_script_sha256",
                "material_config_sha256", "analysis_summary_sha256",
                "rules_json", "runtime_json", "result_summary_json", "created_utc",
            },
        }
        existing_tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('jobs','job_events','analysis_runs')"
            )
        }
        complete = existing_tables == set(required_columns)
        if complete:
            for table, expected in required_columns.items():
                observed = {
                    str(row[1])
                    for row in connection.execute(f"PRAGMA table_info({table})")
                }
                if not expected.issubset(observed):
                    complete = False
                    break

        required_objects = {
            "idx_jobs_single_active": ("index", "jobs"),
            "idx_jobs_created": ("index", "jobs"),
            "idx_job_events_job": ("index", "job_events"),
            "job_events_immutable_update": ("trigger", "job_events"),
            "job_events_immutable_delete": ("trigger", "job_events"),
            "analysis_runs_immutable_update": ("trigger", "analysis_runs"),
            "analysis_runs_immutable_delete": ("trigger", "analysis_runs"),
        }
        observed_objects = {
            str(row[0]): (str(row[1]), str(row[2]), str(row[3] or ""))
            for row in connection.execute(
                """SELECT name,type,tbl_name,sql FROM sqlite_master
                   WHERE name IN (?,?,?,?,?,?,?)""",
                tuple(sorted(required_objects)),
            )
        }
        if complete:
            complete = all(
                name in observed_objects
                and observed_objects[name][:2] == expected
                for name, expected in required_objects.items()
            )

        if complete:
            for name, operation in (
                ("job_events_immutable_update", "UPDATE"),
                ("job_events_immutable_delete", "DELETE"),
                ("analysis_runs_immutable_update", "UPDATE"),
                ("analysis_runs_immutable_delete", "DELETE"),
            ):
                sql = " ".join(observed_objects[name][2].upper().split())
                if f"BEFORE {operation} ON" not in sql or "RAISE(ABORT" not in sql:
                    complete = False
                    break

        if complete:
            jobs_indexes = {
                str(row[1]): (int(row[2]), int(row[4]))
                for row in connection.execute("PRAGMA index_list(jobs)")
            }
            event_indexes = {
                str(row[1])
                for row in connection.execute("PRAGMA index_list(job_events)")
            }
            single_active_sql = " ".join(
                observed_objects["idx_jobs_single_active"][2].upper().split()
            )
            jobs_created_columns = tuple(
                str(row[2])
                for row in connection.execute("PRAGMA index_info(idx_jobs_created)")
            )
            event_columns = tuple(
                str(row[2])
                for row in connection.execute("PRAGMA index_info(idx_job_events_job)")
            )
            complete = (
                jobs_indexes.get("idx_jobs_single_active") == (1, 1)
                and "idx_jobs_created" in jobs_indexes
                and "idx_job_events_job" in event_indexes
                and "ON JOBS((1))" in single_active_sql
                and "WHERE STATUS IN ('QUEUED','RUNNING')" in single_active_sql
                and jobs_created_columns == ("created_utc", "id")
                and event_columns == ("job_id", "id")
            )

        if not complete:
            raise RuntimeError(
                "start-stop schema v4 objects are present but incomplete"
            )

    @staticmethod
    def _migrate_job_provenance_v4(connection: sqlite3.Connection) -> None:
        """Add durable task state, immutable task events, and sealed analyses."""
        existing_tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('jobs','job_events','analysis_runs')"
            )
        }
        if existing_tables:
            StartStopDatabase._validate_job_provenance_v4(connection)
            connection.execute("PRAGMA user_version=4")
            return
        try:
            connection.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE jobs(
                    id TEXT PRIMARY KEY,
                    action TEXT NOT NULL
                        CHECK(action IN ('scan','prepare_upload','render')),
                    requested_via TEXT NOT NULL
                        CHECK(requested_via IN ('manual','automatic','upload')),
                    retry_of_job_id TEXT REFERENCES jobs(id),
                    status TEXT NOT NULL CHECK(status IN (
                        'queued','running','completed','completed_with_warnings',
                        'failed','interrupted'
                    )),
                    stage TEXT NOT NULL,
                    failure_class TEXT NOT NULL DEFAULT 'none'
                        CHECK(failure_class IN ('none','partial','fatal','interrupted')),
                    failure_code TEXT NOT NULL DEFAULT '',
                    message TEXT NOT NULL DEFAULT '',
                    progress_json TEXT NOT NULL DEFAULT '{}',
                    result_json TEXT NOT NULL DEFAULT '{}',
                    collection_batch_id TEXT NOT NULL DEFAULT '',
                    snapshot_id INTEGER REFERENCES dataset_snapshots(id),
                    config_revision INTEGER
                        CHECK(config_revision IS NULL OR config_revision>=0),
                    artifact_generation_id INTEGER REFERENCES artifact_generations(id),
                    worker_instance_id TEXT NOT NULL DEFAULT '',
                    created_utc TEXT NOT NULL,
                    started_utc TEXT NOT NULL DEFAULT '',
                    completed_utc TEXT NOT NULL DEFAULT '',
                    updated_utc TEXT NOT NULL
                );
                CREATE UNIQUE INDEX idx_jobs_single_active ON jobs((1))
                    WHERE status IN ('queued','running');
                CREATE INDEX idx_jobs_created ON jobs(created_utc DESC,id DESC);

                CREATE TABLE job_events(
                    id INTEGER PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES jobs(id),
                    event_type TEXT NOT NULL,
                    level TEXT NOT NULL CHECK(level IN ('info','warning','error')),
                    status TEXT NOT NULL DEFAULT '',
                    stage TEXT NOT NULL DEFAULT '',
                    percent REAL CHECK(percent IS NULL OR (percent>=0 AND percent<=100)),
                    message TEXT NOT NULL DEFAULT '',
                    detail_json TEXT NOT NULL DEFAULT '{}',
                    created_utc TEXT NOT NULL
                );
                CREATE INDEX idx_job_events_job ON job_events(job_id,id);
                CREATE TRIGGER job_events_immutable_update
                BEFORE UPDATE ON job_events
                BEGIN SELECT RAISE(ABORT,'job_events is immutable'); END;
                CREATE TRIGGER job_events_immutable_delete
                BEFORE DELETE ON job_events
                BEGIN SELECT RAISE(ABORT,'job_events is immutable'); END;

                CREATE TABLE analysis_runs(
                    id INTEGER PRIMARY KEY,
                    job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id),
                    snapshot_id INTEGER NOT NULL REFERENCES dataset_snapshots(id),
                    config_revision INTEGER NOT NULL
                        REFERENCES material_config_revisions(revision),
                    artifact_generation_id INTEGER NOT NULL UNIQUE
                        REFERENCES artifact_generations(id),
                    dataset_fingerprint TEXT NOT NULL,
                    artifact_manifest_sha256 TEXT NOT NULL
                        CHECK(length(artifact_manifest_sha256)=64),
                    analysis_script_sha256 TEXT NOT NULL
                        CHECK(length(analysis_script_sha256)=64),
                    material_config_sha256 TEXT NOT NULL
                        CHECK(length(material_config_sha256)=64),
                    analysis_summary_sha256 TEXT NOT NULL
                        CHECK(length(analysis_summary_sha256)=64),
                    rules_json TEXT NOT NULL,
                    runtime_json TEXT NOT NULL,
                    result_summary_json TEXT NOT NULL DEFAULT '{}',
                    created_utc TEXT NOT NULL
                );
                CREATE TRIGGER analysis_runs_immutable_update
                BEFORE UPDATE ON analysis_runs
                BEGIN SELECT RAISE(ABORT,'analysis_runs is immutable'); END;
                CREATE TRIGGER analysis_runs_immutable_delete
                BEFORE DELETE ON analysis_runs
                BEGIN SELECT RAISE(ABORT,'analysis_runs is immutable'); END;
                PRAGMA user_version=4;
                COMMIT;
                """
            )
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        StartStopDatabase._validate_job_provenance_v4(connection)

    @staticmethod
    def _validate_live_preview_v5(connection: sqlite3.Connection) -> None:
        """Fail closed when the live-preview settings/cache schema is incomplete."""
        StartStopDatabase._validate_job_provenance_v4(connection)
        required_columns = {
            "live_preview_revisions": {
                "revision", "enabled", "interval_minutes", "created_utc",
            },
            "live_preview_current": {"id", "revision"},
            "live_preview_runtime": {
                "id", "next_run_utc", "last_attempt_utc", "last_started_utc",
                "last_finished_utc", "last_status", "phase", "message",
                "items_completed", "items_total", "preview_json",
            },
        }
        for table, expected in required_columns.items():
            observed = {
                str(row[1])
                for row in connection.execute(f"PRAGMA table_info({table})")
            }
            if not expected.issubset(observed):
                raise RuntimeError(
                    "start-stop schema v5 live preview objects are incomplete"
                )
        triggers = {
            str(row[0]): str(row[1] or "")
            for row in connection.execute(
                """SELECT name,sql FROM sqlite_master
                   WHERE type='trigger' AND name IN (?,?)""",
                (
                    "live_preview_revisions_immutable_update",
                    "live_preview_revisions_immutable_delete",
                ),
            )
        }
        for name, operation in (
            ("live_preview_revisions_immutable_update", "UPDATE"),
            ("live_preview_revisions_immutable_delete", "DELETE"),
        ):
            sql = " ".join(triggers.get(name, "").upper().split())
            if f"BEFORE {operation} ON" not in sql or "RAISE(ABORT" not in sql:
                raise RuntimeError(
                    "start-stop schema v5 live preview objects are incomplete"
                )

    @staticmethod
    def _migrate_live_preview_v5(connection: sqlite3.Connection) -> None:
        """Add disabled-by-default five-minute activity preview settings."""
        StartStopDatabase._validate_job_provenance_v4(connection)
        try:
            connection.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS live_preview_revisions(
                    revision INTEGER PRIMARY KEY CHECK(revision>=1),
                    enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
                    interval_minutes INTEGER NOT NULL DEFAULT 5
                        CHECK(interval_minutes=5),
                    created_utc TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS live_preview_current(
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    revision INTEGER NOT NULL
                        REFERENCES live_preview_revisions(revision)
                );
                CREATE TABLE IF NOT EXISTS live_preview_runtime(
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    next_run_utc TEXT NOT NULL DEFAULT '',
                    last_attempt_utc TEXT NOT NULL DEFAULT '',
                    last_started_utc TEXT NOT NULL DEFAULT '',
                    last_finished_utc TEXT NOT NULL DEFAULT '',
                    last_status TEXT NOT NULL DEFAULT 'never_run'
                        CHECK(last_status IN (
                            'never_run','running','completed',
                            'completed_with_warnings','failed'
                        )),
                    phase TEXT NOT NULL DEFAULT 'idle',
                    message TEXT NOT NULL DEFAULT '尚未生成实时预览',
                    items_completed INTEGER NOT NULL DEFAULT 0
                        CHECK(items_completed>=0),
                    items_total INTEGER NOT NULL DEFAULT 0
                        CHECK(items_total>=0),
                    preview_json TEXT NOT NULL DEFAULT '{"items":[]}'
                );
                INSERT OR IGNORE INTO live_preview_runtime(id) VALUES(1);
                CREATE TRIGGER IF NOT EXISTS live_preview_revisions_immutable_update
                BEFORE UPDATE ON live_preview_revisions
                BEGIN SELECT RAISE(ABORT,'live_preview_revisions is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS live_preview_revisions_immutable_delete
                BEFORE DELETE ON live_preview_revisions
                BEGIN SELECT RAISE(ABORT,'live_preview_revisions is immutable'); END;
                PRAGMA user_version=5;
                COMMIT;
                """
            )
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        StartStopDatabase._validate_live_preview_v5(connection)

    @staticmethod
    def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    @staticmethod
    def _job_from_row(
        connection: sqlite3.Connection,
        row: sqlite3.Row | None,
    ) -> dict[str, Any] | None:
        if row is None:
            return None
        payload = dict(row)
        for stored, public in (
            ("progress_json", "progress"),
            ("result_json", "result"),
        ):
            try:
                value = json.loads(str(payload.pop(stored)))
            except (TypeError, ValueError, json.JSONDecodeError):
                value = {}
            payload[public] = value if isinstance(value, dict) else {}
        cursor = connection.execute(
            "SELECT COALESCE(MAX(id),0) FROM job_events WHERE job_id=?",
            (str(payload["id"]),),
        ).fetchone()
        payload["event_cursor"] = int(cursor[0]) if cursor is not None else 0
        payload["can_retry"] = str(payload.get("status") or "") in {
            "failed",
            "interrupted",
        }
        return payload

    @staticmethod
    def _insert_job_event(
        connection: sqlite3.Connection,
        *,
        job_id: str,
        event_type: str,
        level: str,
        status: str,
        stage: str,
        message: str,
        percent: float | int | None = None,
        detail: Mapping[str, Any] | None = None,
        created_utc: str | None = None,
    ) -> int:
        normalized_percent = None
        if percent is not None:
            normalized_percent = max(0.0, min(100.0, float(percent)))
        cursor = connection.execute(
            """INSERT INTO job_events(
                   job_id,event_type,level,status,stage,percent,message,
                   detail_json,created_utc
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                job_id,
                str(event_type)[:64],
                level,
                str(status)[:64],
                str(stage)[:128],
                normalized_percent,
                str(message)[:800],
                _json(dict(detail or {})),
                str(created_utc or _utc_now()),
            ),
        )
        return int(cursor.lastrowid)

    def create_job(
        self,
        *,
        job_id: str,
        action: str,
        requested_via: str = "manual",
        message: str = "等待执行",
        progress: Mapping[str, Any] | None = None,
        result: Mapping[str, Any] | None = None,
        collection_batch_id: str = "",
        retry_of_job_id: str | None = None,
    ) -> dict[str, Any]:
        job_id = str(job_id or "").strip()
        action = str(action or "").strip()
        requested_via = str(requested_via or "manual").strip()
        collection_batch_id = str(collection_batch_id or "").strip()
        if not _SAFE_TOKEN.fullmatch(job_id):
            raise ValueError("invalid job id")
        if action not in {"scan", "prepare_upload", "render"}:
            raise ValueError("invalid job action")
        if requested_via not in {"manual", "automatic", "upload"}:
            raise ValueError("invalid job origin")
        if collection_batch_id and not _SAFE_TOKEN.fullmatch(collection_batch_id):
            raise ValueError("invalid collection batch id")
        if retry_of_job_id is not None and not _SAFE_TOKEN.fullmatch(
            str(retry_of_job_id)
        ):
            raise ValueError("invalid retry job id")
        now = _utc_now()
        with self._lock, self.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """INSERT INTO jobs(
                           id,action,requested_via,retry_of_job_id,status,stage,
                           message,progress_json,result_json,collection_batch_id,
                           created_utc,updated_utc
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        job_id,
                        action,
                        requested_via,
                        retry_of_job_id,
                        "queued",
                        "queued",
                        str(message)[:800],
                        _json(dict(progress or {})),
                        _json(dict(result or {})),
                        collection_batch_id,
                        now,
                        now,
                    ),
                )
                self._insert_job_event(
                    connection,
                    job_id=job_id,
                    event_type="created",
                    level="info",
                    status="queued",
                    stage="queued",
                    message=str(message),
                    created_utc=now,
                )
                row = connection.execute(
                    "SELECT * FROM jobs WHERE id=?", (job_id,)
                ).fetchone()
                result_payload = self._job_from_row(connection, row)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        assert result_payload is not None
        return result_payload

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self.session() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE id=?", (str(job_id),)
            ).fetchone()
            return self._job_from_row(connection, row)

    def latest_job(self) -> dict[str, Any] | None:
        with self.session() as connection:
            row = connection.execute(
                "SELECT * FROM jobs ORDER BY created_utc DESC,rowid DESC LIMIT 1"
            ).fetchone()
            return self._job_from_row(connection, row)

    def claim_job(
        self,
        job_id: str,
        *,
        worker_instance_id: str,
        started_utc: str | None = None,
    ) -> dict[str, Any]:
        worker = str(worker_instance_id or "").strip()
        if not _SAFE_TOKEN.fullmatch(worker):
            raise ValueError("invalid job worker id")
        now = str(started_utc or _utc_now())
        with self._lock, self.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    """UPDATE jobs SET status='running',worker_instance_id=?,
                           started_utc=?,updated_utc=?
                       WHERE id=? AND status='queued'""",
                    (worker, now, now, str(job_id)),
                )
                if cursor.rowcount != 1:
                    row = connection.execute(
                        "SELECT * FROM jobs WHERE id=?", (str(job_id),)
                    ).fetchone()
                    if row is None:
                        raise KeyError(job_id)
                    raise RuntimeError("job is not queued")
                self._insert_job_event(
                    connection,
                    job_id=str(job_id),
                    event_type="started",
                    level="info",
                    status="running",
                    stage="queued",
                    message="任务已开始执行",
                    created_utc=now,
                )
                row = connection.execute(
                    "SELECT * FROM jobs WHERE id=?", (str(job_id),)
                ).fetchone()
                result = self._job_from_row(connection, row)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        assert result is not None
        return result

    def update_job(
        self,
        job_id: str,
        changes: Mapping[str, Any],
    ) -> dict[str, Any]:
        allowed = {
            "status",
            "stage",
            "failure_class",
            "failure_code",
            "message",
            "progress",
            "result",
            "collection_batch_id",
            "snapshot_id",
            "config_revision",
            "artifact_generation_id",
            "started_utc",
            "completed_utc",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError("unsupported job fields: " + ", ".join(sorted(unknown)))
        terminal = {"completed", "completed_with_warnings", "failed", "interrupted"}
        transitions = {
            "queued": {"queued", "running", "failed", "interrupted"},
            "running": {"running", *terminal},
        }
        now = _utc_now()
        with self._lock, self.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM jobs WHERE id=?", (str(job_id),)
                ).fetchone()
                if row is None:
                    raise KeyError(job_id)
                current = dict(row)
                current_status = str(current["status"])
                next_status = str(changes.get("status", current_status))
                if next_status not in {
                    "queued", "running", "completed", "completed_with_warnings",
                    "failed", "interrupted",
                }:
                    raise ValueError("invalid job status")
                if current_status in terminal:
                    if next_status != current_status:
                        raise RuntimeError("terminal job status is immutable")
                elif next_status not in transitions[current_status]:
                    raise RuntimeError("invalid job status transition")
                next_stage = str(changes.get("stage", current["stage"]))[:128]
                if not next_stage or not _SAFE_TOKEN.fullmatch(next_stage):
                    raise ValueError("invalid job stage")
                failure_class = str(
                    changes.get("failure_class", current["failure_class"])
                )
                if next_status == "completed_with_warnings" and failure_class == "none":
                    failure_class = "partial"
                elif next_status == "failed" and failure_class == "none":
                    failure_class = "fatal"
                elif next_status == "interrupted":
                    failure_class = "interrupted"
                if failure_class not in {"none", "partial", "fatal", "interrupted"}:
                    raise ValueError("invalid job failure class")
                failure_code = str(
                    changes.get("failure_code", current["failure_code"])
                )[:160]
                if failure_code and not _SAFE_TOKEN.fullmatch(failure_code):
                    raise ValueError("invalid job failure code")
                progress = changes.get("progress")
                result = changes.get("result")
                progress_json = (
                    _json(dict(progress))
                    if isinstance(progress, Mapping)
                    else str(current["progress_json"])
                )
                result_json = (
                    _json(dict(result))
                    if isinstance(result, Mapping)
                    else str(current["result_json"])
                )
                completed_utc = str(
                    changes.get("completed_utc", current["completed_utc"])
                )
                if next_status in terminal and not completed_utc:
                    completed_utc = now
                started_utc = str(changes.get("started_utc", current["started_utc"]))
                if next_status == "running" and not started_utc:
                    started_utc = now
                collection_batch_id = str(
                    changes.get("collection_batch_id", current["collection_batch_id"])
                    or ""
                )
                if collection_batch_id and not _SAFE_TOKEN.fullmatch(collection_batch_id):
                    raise ValueError("invalid collection batch id")
                snapshot_id = changes.get("snapshot_id", current["snapshot_id"])
                config_revision = changes.get(
                    "config_revision", current["config_revision"]
                )
                generation_id = changes.get(
                    "artifact_generation_id", current["artifact_generation_id"]
                )
                connection.execute(
                    """UPDATE jobs SET status=?,stage=?,failure_class=?,failure_code=?,
                           message=?,progress_json=?,result_json=?,collection_batch_id=?,
                           snapshot_id=?,config_revision=?,artifact_generation_id=?,
                           started_utc=?,completed_utc=?,updated_utc=? WHERE id=?""",
                    (
                        next_status,
                        next_stage,
                        failure_class,
                        failure_code,
                        str(changes.get("message", current["message"]))[:800],
                        progress_json,
                        result_json,
                        collection_batch_id,
                        None if snapshot_id in {None, ""} else int(snapshot_id),
                        None if config_revision in {None, ""} else int(config_revision),
                        None if generation_id in {None, ""} else int(generation_id),
                        started_utc,
                        completed_utc,
                        now,
                        str(job_id),
                    ),
                )
                event_required = (
                    next_status != current_status
                    or next_stage != str(current["stage"])
                    or failure_class != str(current["failure_class"])
                    or failure_code != str(current["failure_code"])
                )
                if event_required:
                    if next_status in {"failed", "interrupted"}:
                        event_type, level = next_status, "error"
                    elif next_status in {"completed", "completed_with_warnings"}:
                        event_type = "completed"
                        level = (
                            "warning"
                            if next_status == "completed_with_warnings"
                            else "info"
                        )
                    else:
                        event_type, level = "stage", "info"
                    self._insert_job_event(
                        connection,
                        job_id=str(job_id),
                        event_type=event_type,
                        level=level,
                        status=next_status,
                        stage=next_stage,
                        message=str(changes.get("message", current["message"])),
                        created_utc=now,
                    )
                next_row = connection.execute(
                    "SELECT * FROM jobs WHERE id=?", (str(job_id),)
                ).fetchone()
                payload = self._job_from_row(connection, next_row)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        assert payload is not None
        return payload

    def update_job_progress(
        self,
        job_id: str,
        progress: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(progress, Mapping):
            raise ValueError("job progress must be an object")
        now = _utc_now()
        with self._lock, self.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM jobs WHERE id=?", (str(job_id),)
                ).fetchone()
                if row is None:
                    raise KeyError(job_id)
                if str(row["status"]) not in {"queued", "running"}:
                    connection.rollback()
                    return self._job_from_row(connection, row) or {}
                try:
                    previous = json.loads(str(row["progress_json"]))
                except (TypeError, ValueError, json.JSONDecodeError):
                    previous = {}
                previous_phase = str(previous.get("phase") or "") if isinstance(previous, dict) else ""
                next_progress = dict(progress)
                connection.execute(
                    "UPDATE jobs SET progress_json=?,updated_utc=? WHERE id=?",
                    (_json(next_progress), now, str(job_id)),
                )
                next_phase = str(next_progress.get("phase") or "")
                if next_phase and next_phase != previous_phase:
                    self._insert_job_event(
                        connection,
                        job_id=str(job_id),
                        event_type="progress",
                        level="info",
                        status=str(row["status"]),
                        stage=str(row["stage"]),
                        message=str(next_progress.get("phase_label") or next_phase),
                        percent=next_progress.get("percent"),
                        created_utc=now,
                    )
                next_row = connection.execute(
                    "SELECT * FROM jobs WHERE id=?", (str(job_id),)
                ).fetchone()
                payload = self._job_from_row(connection, next_row)
                connection.commit()
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise
        assert payload is not None
        return payload

    def finish_job(
        self,
        job_id: str,
        *,
        status: str,
        stage: str,
        message: str,
        result: Mapping[str, Any] | None = None,
        progress: Mapping[str, Any] | None = None,
        failure_class: str = "none",
        failure_code: str = "",
        completed_utc: str | None = None,
    ) -> dict[str, Any]:
        changes: dict[str, Any] = {
            "status": status,
            "stage": stage,
            "message": message,
            "failure_class": failure_class,
            "failure_code": failure_code,
            "completed_utc": completed_utc or _utc_now(),
        }
        if result is not None:
            changes["result"] = result
        if progress is not None:
            changes["progress"] = progress
        return self.update_job(job_id, changes)

    def recover_interrupted_jobs(
        self,
        *,
        worker_instance_id: str,
        now_utc: str | None = None,
    ) -> list[dict[str, Any]]:
        worker = str(worker_instance_id or "").strip()
        if not _SAFE_TOKEN.fullmatch(worker):
            raise ValueError("invalid recovery worker id")
        now = str(now_utc or _utc_now())
        recovered: list[dict[str, Any]] = []
        with self._lock, self.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                rows = connection.execute(
                    "SELECT * FROM jobs WHERE status IN ('queued','running') ORDER BY created_utc,id"
                ).fetchall()
                for row in rows:
                    generation_id = row["artifact_generation_id"]
                    published = False
                    if generation_id is not None:
                        published = connection.execute(
                            """SELECT 1 FROM artifact_current ac
                               JOIN artifact_selections ase ON ase.id=ac.selection_id
                               JOIN artifact_generations ag ON ag.id=ase.generation_id
                               WHERE ac.kind=? AND ag.id=?""",
                            (str(row["action"]), int(generation_id)),
                        ).fetchone() is not None
                    if published:
                        status = (
                            "completed_with_warnings"
                            if str(row["failure_class"]) == "partial"
                            else "completed"
                        )
                        failure_class = str(row["failure_class"])
                        failure_code = str(row["failure_code"])
                        stage = "completed"
                        message = "任务产物已发布，服务重启后已确认完成。"
                        event_type = "recovered_after_publish"
                        level = "warning" if status == "completed_with_warnings" else "info"
                    else:
                        status = "interrupted"
                        failure_class = "interrupted"
                        failure_code = "process_restart"
                        stage = str(row["stage"])
                        message = "任务因服务重启中断，可重新执行。"
                        event_type = "interrupted"
                        level = "error"
                    connection.execute(
                        """UPDATE jobs SET status=?,stage=?,failure_class=?,
                               failure_code=?,message=?,completed_utc=?,updated_utc=?
                           WHERE id=?""",
                        (
                            status,
                            stage,
                            failure_class,
                            failure_code,
                            message,
                            now,
                            now,
                            str(row["id"]),
                        ),
                    )
                    self._insert_job_event(
                        connection,
                        job_id=str(row["id"]),
                        event_type=event_type,
                        level=level,
                        status=status,
                        stage=stage,
                        message=message,
                        detail={"recovered_by": worker},
                        created_utc=now,
                    )
                    next_row = connection.execute(
                        "SELECT * FROM jobs WHERE id=?", (str(row["id"]),)
                    ).fetchone()
                    payload = self._job_from_row(connection, next_row)
                    if payload is not None:
                        recovered.append(payload)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return recovered

    def current_analysis_run(self, kind: str = "render") -> dict[str, Any]:
        normalized_kind = str(kind or "render").strip()
        if not _SAFE_TOKEN.fullmatch(normalized_kind):
            raise ValueError("invalid artifact kind")
        with self.session() as connection:
            row = connection.execute(
                """SELECT ag.id AS generation_id,ag.snapshot_id,
                          ag.config_revision,ag.created_utc AS generation_created_utc,
                          ar.*
                   FROM artifact_current ac
                   JOIN artifact_selections ase ON ase.id=ac.selection_id
                   JOIN artifact_generations ag ON ag.id=ase.generation_id
                   LEFT JOIN analysis_runs ar ON ar.artifact_generation_id=ag.id
                   WHERE ac.kind=?""",
                (normalized_kind,),
            ).fetchone()
        if row is None:
            return {"state": "none"}
        payload = dict(row)
        if payload.get("id") is None:
            return {
                "state": "legacy_unverified",
                "artifact_generation_id": int(payload["generation_id"]),
                "snapshot_id": int(payload["snapshot_id"]),
                "config_revision": int(payload["config_revision"]),
                "created_utc": str(payload["generation_created_utc"]),
            }
        result = {
            "state": "sealed",
            "analysis_run_id": int(payload["id"]),
            "job_id": str(payload["job_id"]),
            "snapshot_id": int(payload["snapshot_id"]),
            "config_revision": int(payload["config_revision"]),
            "artifact_generation_id": int(payload["artifact_generation_id"]),
            "dataset_fingerprint": str(payload["dataset_fingerprint"]),
            "artifact_manifest_sha256": str(payload["artifact_manifest_sha256"]),
            "analysis_script_sha256": str(payload["analysis_script_sha256"]),
            "material_config_sha256": str(payload["material_config_sha256"]),
            "analysis_summary_sha256": str(payload["analysis_summary_sha256"]),
            "created_utc": str(payload["created_utc"]),
        }
        for stored, public in (
            ("rules_json", "rules"),
            ("runtime_json", "runtime"),
        ):
            try:
                value = json.loads(str(payload[stored]))
            except (TypeError, ValueError, json.JSONDecodeError):
                value = {}
            result[public] = value if isinstance(value, dict) else {}
        return result

    def audit(self, action: str, target: str, detail: Any) -> int:
        detail_json = _json(detail)
        with self._lock, self.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "INSERT INTO audit_log(created_utc,action,target,detail_json) VALUES(?,?,?,?)",
                (_utc_now(), str(action)[:160], str(target)[:500], detail_json),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def begin_collection_batch(
        self,
        machine_count: int = 0,
        metadata: Mapping[str, Any] | None = None,
        batch_id: str | None = None,
    ) -> str:
        batch_id = str(batch_id or uuid.uuid4()).strip()
        if not _SAFE_TOKEN.fullmatch(batch_id):
            raise ValueError("invalid collection batch id")
        now = _utc_now()
        with self._lock, self.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO collection_batches(id,status,machine_count,metadata_json,started_utc) VALUES(?,?,?,?,?)",
                (batch_id, "running", max(0, int(machine_count)), _json(dict(metadata or {})), now),
            )
            connection.commit()
        return batch_id

    def finish_collection_batch(
        self, batch_id: str, status: str = "completed", totals: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        status = str(status or "").strip()
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", status):
            raise ValueError("invalid collection status")
        with self._lock, self.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE collection_batches SET status=?,totals_json=?,finished_utc=? WHERE id=?",
                (status, _json(dict(totals or {})), _utc_now(), batch_id),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise KeyError(f"unknown collection batch: {batch_id}")
            connection.commit()
            row = connection.execute("SELECT * FROM collection_batches WHERE id=?", (batch_id,)).fetchone()
        result = dict(row)
        result["metadata"] = json.loads(result.pop("metadata_json"))
        result["totals"] = json.loads(result.pop("totals_json"))
        return result

    def fail_collection_batch_if_running(
        self,
        batch_id: str,
        *,
        reason: str,
        returncode: int | None = None,
        parent_job_id: str = "",
    ) -> dict[str, Any] | None:
        """Atomically close one collector batch left running by a dead child.

        The caller must already know the exact batch id.  Completed batches and
        unknown ids are deliberately left untouched so recovery cannot guess at
        another process's work.
        """
        batch_id = str(batch_id or "").strip()
        parent_job_id = str(parent_job_id or "").strip()
        if not _SAFE_TOKEN.fullmatch(batch_id):
            raise ValueError("invalid collection batch id")
        if parent_job_id and not _SAFE_TOKEN.fullmatch(parent_job_id):
            raise ValueError("invalid parent job id")
        if returncode is not None and isinstance(returncode, bool):
            raise ValueError("invalid collector return code")
        exit_code = int(returncode) if returncode is not None else None
        safe_reason = str(reason or "采集子进程异常退出")[:2000]
        now = _utc_now()
        with self._lock, self.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT id,status FROM collection_batches WHERE id=?",
                (batch_id,),
            ).fetchone()
            if row is None or str(row["status"]) != "running":
                connection.rollback()
                return None
            partial = connection.execute(
                """SELECT COUNT(*) AS file_count,
                          COALESCE(SUM(size_bytes),0) AS total_bytes
                   FROM source_versions WHERE batch_id=?""",
                (batch_id,),
            ).fetchone()
            cursor = connection.execute(
                """UPDATE collection_batches
                   SET status='failed',finished_utc=?
                   WHERE id=? AND status='running'""",
                (now, batch_id),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return None
            detail = {
                "scope": "batch",
                "parent_job_id": parent_job_id,
                "returncode": exit_code,
                "partial_file_count": int(partial["file_count"]),
                "partial_bytes": int(partial["total_bytes"]),
            }
            connection.execute(
                """INSERT INTO collection_issues(
                       batch_id,severity,code,message,machine_id,root_label,
                       remote_path,detail_json,created_utc
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    batch_id,
                    "error",
                    "collector_process_aborted",
                    safe_reason,
                    "",
                    "",
                    "",
                    _json(detail),
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO audit_log(created_utc,action,target,detail_json) VALUES(?,?,?,?)",
                (now, "collection_batch.recovered", batch_id, _json(detail)),
            )
            connection.commit()
        return {
            "batch_id": batch_id,
            "status": "failed",
            "finished_utc": now,
            **detail,
        }

    def record_collection_issue(
        self,
        batch_id: str,
        severity: str = "error",
        code: str = "",
        message: str = "",
        machine_id: str = "",
        root_label: str = "",
        remote_path: str = "",
        detail: Mapping[str, Any] | None = None,
    ) -> int:
        with self._lock, self.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """INSERT INTO collection_issues(
                    batch_id,severity,code,message,machine_id,root_label,remote_path,detail_json,created_utc
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    batch_id, str(severity)[:32], str(code)[:160], str(message)[:4000],
                    str(machine_id)[:300], str(root_label)[:500], str(remote_path)[:2000],
                    _json(dict(detail or {})), _utc_now(),
                ),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def _source_identity(self, machine_id: Any, root_label: Any, remote_path: Any) -> tuple[str, str, str]:
        return (
            _relative_path(machine_id, label="machine_id"),
            _relative_path(root_label, label="root_label"),
            _source_locator(remote_path),
        )

    def needs_download(
        self, machine_id: str, root_label: str, remote_path: str, size: int, ticks: int
    ) -> bool:
        machine_id, root_label, remote_path = self._source_identity(machine_id, root_label, remote_path)
        with self.session() as connection:
            row = connection.execute(
                """SELECT sv.size_bytes,sv.last_write_ticks
                FROM sources s JOIN source_current sc ON sc.source_id=s.id
                JOIN source_selections ss ON ss.id=sc.selection_id
                JOIN source_versions sv ON sv.id=ss.source_version_id
                WHERE s.machine_id=? AND s.root_label=? AND s.remote_path=?""",
                (machine_id, root_label, remote_path),
            ).fetchone()
        return row is None or int(row["size_bytes"]) != int(size) or int(row["last_write_ticks"]) != int(ticks)

    def _ensure_blob_from_path(
        self, connection: sqlite3.Connection, path: Path, sha256: str, size_bytes: int
    ) -> int:
        row = connection.execute(
            "SELECT id FROM content_blobs WHERE sha256=? AND size_bytes=?", (sha256, size_bytes)
        ).fetchone()
        if row:
            return int(row["id"])
        if size_bytes <= self.inline_blob_max_bytes:
            content = path.read_bytes()
            if len(content) != size_bytes or hashlib.sha256(content).hexdigest() != sha256:
                raise OSError("staged file changed while ingesting")
            cursor = connection.execute(
                "INSERT INTO content_blobs(sha256,size_bytes,content,created_utc) VALUES(?,?,?,?)",
                (sha256, size_bytes, content, _utc_now()),
            )
            return int(cursor.lastrowid)
        cursor = connection.execute(
            "INSERT INTO content_blobs(sha256,size_bytes,content,created_utc) VALUES(?,?,zeroblob(?),?)",
            (sha256, size_bytes, size_bytes, _utc_now()),
        )
        blob_id = int(cursor.lastrowid)
        if size_bytes:
            digest = hashlib.sha256()
            copied = 0
            with path.open("rb") as source, connection.blobopen(
                "content_blobs", "content", blob_id, readonly=False
            ) as target:
                while True:
                    chunk = source.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    digest.update(chunk)
                    target.write(chunk)
                    copied += len(chunk)
            if copied != size_bytes or digest.hexdigest() != sha256:
                raise OSError("staged file changed while ingesting")
        return blob_id

    @staticmethod
    def _hash_file(path: Path) -> tuple[str, int]:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(_CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
        return digest.hexdigest(), size

    def ingest_staged_file(
        self, path: str | Path, metadata: Mapping[str, Any], batch_id: str | None = None
    ) -> dict[str, Any]:
        staged = Path(path)
        if staged.is_symlink() or not staged.is_file():
            raise ValueError("staged path must be a regular file")
        meta = dict(metadata)
        machine_id, root_label, remote_path = self._source_identity(
            meta.get("machine_id"), meta.get("root_label"), meta.get("remote_path")
        )
        relative = meta.get("repository_path")
        if not relative:
            remote_relative = meta.get("remote_relative_path") or remote_path
            relative = "/".join((machine_id, root_label, _relative_path(remote_relative, label="remote_relative_path")))
        repository_path = _relative_path(relative, label="repository_path")
        sha256, size_bytes = self._hash_file(staged)
        expected_sha = str(meta.get("sha256") or "").strip().casefold()
        if expected_sha:
            if re.fullmatch(r"[0-9a-f]{64}", expected_sha) is None:
                raise ValueError("collection metadata contains an invalid SHA-256")
            if expected_sha != sha256:
                raise OSError("staged file SHA-256 differs from collection metadata")
        expected_size = meta.get("size_bytes", meta.get("size"))
        if expected_size not in (None, "") and int(expected_size) != size_bytes:
            raise OSError("staged file size differs from collection metadata")
        ticks = int(meta.get("last_write_ticks", meta.get("ticks", 0)) or 0)
        modified_utc = str(meta.get("last_write_utc") or meta.get("source_modified_utc") or "")
        modified_ns = int(meta.get("modified_ns", 0) or 0) or _modified_ns(modified_utc)
        detected_candidate, detected_kind = _candidate_from_file(staged, repository_path)
        is_candidate = bool(meta.get("is_candidate", detected_candidate))
        candidate_kind = str(meta.get("candidate_kind") or (detected_kind if is_candidate else ""))[:80]
        now = _utc_now()
        with self._lock, self.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if batch_id is not None and connection.execute(
                    "SELECT 1 FROM collection_batches WHERE id=?", (batch_id,)
                ).fetchone() is None:
                    raise KeyError(f"unknown collection batch: {batch_id}")
                blob_id = self._ensure_blob_from_path(connection, staged, sha256, size_bytes)
                source = connection.execute(
                    "SELECT id FROM sources WHERE machine_id=? AND root_label=? AND remote_path=?",
                    (machine_id, root_label, remote_path),
                ).fetchone()
                if source is None:
                    source_id = int(connection.execute(
                        "INSERT INTO sources(machine_id,root_label,remote_path,created_utc) VALUES(?,?,?,?)",
                        (machine_id, root_label, remote_path, now),
                    ).lastrowid)
                else:
                    source_id = int(source["id"])
                current = connection.execute(
                    """SELECT sv.* FROM source_current sc
                    JOIN source_selections ss ON ss.id=sc.selection_id
                    JOIN source_versions sv ON sv.id=ss.source_version_id
                    WHERE sc.source_id=?""", (source_id,),
                ).fetchone()
                unchanged = current is not None and int(current["blob_id"]) == blob_id and int(current["size_bytes"]) == size_bytes and int(current["last_write_ticks"]) == ticks and current["repository_path"] == repository_path
                if unchanged:
                    connection.commit()
                    return {
                        "changed": False, "source_id": source_id, "version_id": int(current["id"]),
                        "sha256": sha256, "size_bytes": size_bytes,
                        "repository_path": repository_path, "is_candidate": bool(current["is_candidate"]),
                    }
                version_number = 1 + int(connection.execute(
                    "SELECT COALESCE(MAX(version_number),0) FROM source_versions WHERE source_id=?", (source_id,)
                ).fetchone()[0])
                version_id = int(connection.execute(
                    """INSERT INTO source_versions(
                        source_id,version_number,blob_id,repository_path,size_bytes,last_write_ticks,
                        source_modified_utc,modified_ns,is_candidate,candidate_kind,metadata_json,batch_id,created_utc
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (source_id, version_number, blob_id, repository_path, size_bytes, ticks,
                     modified_utc, modified_ns, int(is_candidate), candidate_kind, _json(meta), batch_id, now),
                ).lastrowid)
                selection_id = int(connection.execute(
                    "INSERT INTO source_selections(source_id,source_version_id,batch_id,reason,selected_utc) VALUES(?,?,?,?,?)",
                    (source_id, version_id, batch_id, "ingest", now),
                ).lastrowid)
                connection.execute(
                    "INSERT INTO source_current(source_id,selection_id) VALUES(?,?) ON CONFLICT(source_id) DO UPDATE SET selection_id=excluded.selection_id",
                    (source_id, selection_id),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return {
            "changed": True, "source_id": source_id, "version_id": version_id,
            "version_number": version_number, "sha256": sha256, "size_bytes": size_bytes,
            "repository_path": repository_path, "is_candidate": is_candidate,
            "candidate_kind": candidate_kind,
        }

    def import_source_path(
        self, path: str | Path, metadata: Mapping[str, Any], batch_id: str | None = None
    ) -> dict[str, Any]:
        imported = dict(metadata)
        logical = imported.get("logical_path") or imported.get("repository_path")
        if logical:
            logical = _relative_path(logical, label="logical_path")
            imported.setdefault("repository_path", logical)
            imported.setdefault("remote_relative_path", logical)
            imported.setdefault("remote_path", logical)
        imported.setdefault("machine_id", "local-import")
        imported.setdefault("root_label", "local-data")
        return self.ingest_staged_file(path, imported, batch_id)

    def mark_candidate(
        self, source_version_id: int, is_candidate: bool, candidate_kind: str = "", reason: str = ""
    ) -> dict[str, Any]:
        with self._lock, self.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM source_versions WHERE id=?", (source_version_id,)).fetchone() is None:
                connection.rollback()
                raise KeyError(source_version_id)
            mark_id = int(connection.execute(
                "INSERT INTO candidate_marks(source_version_id,is_candidate,candidate_kind,reason,created_utc) VALUES(?,?,?,?,?)",
                (int(source_version_id), int(bool(is_candidate)), str(candidate_kind)[:80], str(reason)[:500], _utc_now()),
            ).lastrowid)
            connection.execute(
                "INSERT INTO candidate_current(source_version_id,mark_id) VALUES(?,?) ON CONFLICT(source_version_id) DO UPDATE SET mark_id=excluded.mark_id",
                (int(source_version_id), mark_id),
            )
            connection.commit()
        return {"mark_id": mark_id, "source_version_id": int(source_version_id), "is_candidate": bool(is_candidate), "candidate_kind": str(candidate_kind)}

    def _current_source_rows(self, connection: sqlite3.Connection) -> list[sqlite3.Row]:
        return connection.execute(
            """SELECT sv.*,b.sha256,
                COALESCE(cm.is_candidate,sv.is_candidate) AS effective_candidate,
                COALESCE(cm.candidate_kind,sv.candidate_kind) AS effective_candidate_kind,
                s.machine_id,s.root_label,s.remote_path
            FROM source_current sc
            JOIN source_selections ss ON ss.id=sc.selection_id
            JOIN source_versions sv ON sv.id=ss.source_version_id
            JOIN sources s ON s.id=sv.source_id
            JOIN content_blobs b ON b.id=sv.blob_id
            LEFT JOIN candidate_current cc ON cc.source_version_id=sv.id
            LEFT JOIN candidate_marks cm ON cm.id=cc.mark_id
            ORDER BY sv.repository_path COLLATE NOCASE,sv.repository_path"""
        ).fetchall()

    def freeze_snapshot(
        self, candidate_only: bool = True, batch_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._lock, self.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                rows = self._current_source_rows(connection)
                if candidate_only:
                    rows = [row for row in rows if bool(row["effective_candidate"])]
                if not rows:
                    raise ValueError("no current start-stop candidate files to freeze")
                paths = [str(row["repository_path"]) for row in rows]
                _assert_no_path_conflicts(paths, reserved=(".start-stop-manifest.json",))
                files = [
                    {
                        "path": str(row["repository_path"]), "sha256": str(row["sha256"]),
                        "size_bytes": int(row["size_bytes"]), "modified_ns": int(row["modified_ns"]),
                        "source_modified_utc": str(row["source_modified_utc"]),
                        "source_version_id": int(row["id"]), "machine_id": str(row["machine_id"]),
                        "root_label": str(row["root_label"]), "remote_path": str(row["remote_path"]),
                        "candidate_kind": str(row["effective_candidate_kind"]),
                    }
                    for row in rows
                ]
                fingerprint = hashlib.sha256(_json(files).encode("utf-8")).hexdigest()
                existing = connection.execute(
                    "SELECT id FROM dataset_snapshots WHERE dataset_fingerprint=?", (fingerprint,)
                ).fetchone()
                if existing:
                    connection.rollback()
                    result = self._snapshot_by_id(int(existing["id"]))
                    assert result is not None
                    return result
                now = _utc_now()
                cursor = connection.execute(
                    """INSERT INTO dataset_snapshots(
                        dataset_fingerprint,candidate_only,batch_id,metadata_json,manifest_json,
                        file_count,total_bytes,created_utc
                    ) VALUES(?,?,?,?,?,?,?,?)""",
                    (fingerprint, int(bool(candidate_only)), batch_id, _json(dict(metadata or {})),
                     _json(files), len(files), sum(item["size_bytes"] for item in files), now),
                )
                snapshot_id = int(cursor.lastrowid)
                connection.executemany(
                    """INSERT INTO dataset_snapshot_files(
                        snapshot_id,ordinal,source_version_id,repository_path,sha256,size_bytes,
                        modified_ns,source_modified_utc
                    ) VALUES(?,?,?,?,?,?,?,?)""",
                    [(snapshot_id, i, item["source_version_id"], item["path"], item["sha256"],
                      item["size_bytes"], item["modified_ns"], item["source_modified_utc"])
                     for i, item in enumerate(files)],
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        result = self._snapshot_by_id(snapshot_id)
        assert result is not None
        return result

    def _snapshot_by_id(self, snapshot_id: int) -> dict[str, Any] | None:
        with self.session() as connection:
            row = connection.execute("SELECT * FROM dataset_snapshots WHERE id=?", (int(snapshot_id),)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["candidate_only"] = bool(result["candidate_only"])
        result["metadata"] = json.loads(result.pop("metadata_json"))
        result["files"] = json.loads(result.pop("manifest_json"))
        return result

    def latest_snapshot(self) -> dict[str, Any] | None:
        with self.session() as connection:
            row = connection.execute("SELECT id FROM dataset_snapshots ORDER BY id DESC LIMIT 1").fetchone()
        return self._snapshot_by_id(int(row["id"])) if row else None

    def _write_blob(self, connection: sqlite3.Connection, blob_id: int, target: Path, expected_sha: str, expected_size: int) -> None:
        digest = hashlib.sha256()
        copied = 0
        with connection.blobopen("content_blobs", "content", int(blob_id), readonly=True) as source, target.open("xb") as output:
            while True:
                chunk = source.read(_CHUNK_SIZE)
                if not chunk:
                    break
                output.write(chunk)
                digest.update(chunk)
                copied += len(chunk)
            output.flush()
            os.fsync(output.fileno())
        if copied != int(expected_size) or digest.hexdigest() != expected_sha:
            raise sqlite3.DatabaseError(f"blob verification failed for {target.name}")

    @contextlib.contextmanager
    def _staged_directory(self, target: Path) -> Iterator[Path]:
        target = Path(target)
        parent = target.parent.resolve()
        parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if target.is_symlink() or not target.is_dir() or any(target.iterdir()):
                raise FileExistsError(f"target directory must not exist or must be empty: {target}")
            target.rmdir()
        stage = Path(tempfile.mkdtemp(prefix=f".{target.name}.staging-", dir=parent))
        try:
            yield stage
            os.replace(stage, target)
        except BaseException:
            shutil.rmtree(stage, ignore_errors=True)
            raise

    def materialize_snapshot(self, snapshot_id: int, source_root: str | Path) -> dict[str, Any]:
        snapshot = self._snapshot_by_id(int(snapshot_id))
        if snapshot is None:
            raise KeyError(snapshot_id)
        files = list(snapshot["files"])
        _assert_no_path_conflicts([item["path"] for item in files], reserved=(".start-stop-manifest.json",))
        target = Path(source_root)
        with self.session() as connection, self._staged_directory(target) as stage:
            for item in files:
                relative = _relative_path(item["path"])
                output = stage.joinpath(*PurePosixPath(relative).parts)
                output.parent.mkdir(parents=True, exist_ok=True)
                row = connection.execute(
                    """SELECT sv.blob_id FROM source_versions sv
                    WHERE sv.id=? AND sv.size_bytes=?""",
                    (int(item["source_version_id"]), int(item["size_bytes"])),
                ).fetchone()
                if row is None:
                    raise sqlite3.DatabaseError("snapshot source version is missing")
                self._write_blob(connection, int(row["blob_id"]), output, item["sha256"], int(item["size_bytes"]))
                if int(item.get("modified_ns") or 0) > 0:
                    os.utime(output, ns=(int(item["modified_ns"]), int(item["modified_ns"])))
            manifest = {
                "schema_version": SCHEMA_VERSION, "snapshot_id": int(snapshot_id),
                "dataset_fingerprint": snapshot["dataset_fingerprint"], "created_utc": snapshot["created_utc"],
                "file_count": snapshot["file_count"], "total_bytes": snapshot["total_bytes"], "files": files,
            }
            (stage / ".start-stop-manifest.json").write_text(_json(manifest), encoding="utf-8")
        return {
            "root": str(target), "manifest_path": str(target / ".start-stop-manifest.json"),
            "snapshot_id": int(snapshot_id), "dataset_fingerprint": snapshot["dataset_fingerprint"],
            "file_count": snapshot["file_count"], "total_bytes": snapshot["total_bytes"], "files": files,
        }

    def publish_artifacts(
        self,
        output_dir: str | Path,
        snapshot_id: int,
        config_revision: int,
        kind: str,
        *,
        job_id: str = "",
        analysis_run: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        root = Path(output_dir)
        if not root.is_dir() or root.is_symlink():
            raise ValueError("artifact output_dir must be a directory")
        seal_files = {
            ARTIFACT_MANIFEST_NAME: (root / ARTIFACT_MANIFEST_NAME).exists(),
            ARTIFACT_CHECKSUMS_NAME: (root / ARTIFACT_CHECKSUMS_NAME).exists(),
        }
        sealed: dict[str, Any] | None = None
        if seal_files[ARTIFACT_MANIFEST_NAME]:
            if not seal_files[ARTIFACT_CHECKSUMS_NAME]:
                raise ValueError("artifact seal is incomplete")
            sealed = validate_sealed_artifact_directory(root)
        kind = str(kind or "analysis").strip()
        if not _SAFE_TOKEN.fullmatch(kind):
            raise ValueError("invalid artifact kind")
        job_id = str(job_id or "").strip()
        if job_id and not _SAFE_TOKEN.fullmatch(job_id):
            raise ValueError("invalid job id")
        if analysis_run is not None and not job_id:
            raise ValueError("analysis run requires a job id")
        if analysis_run is not None and kind != "render":
            raise ValueError("analysis runs are only valid for render artifacts")
        if sealed is not None:
            manifest = sealed["manifest"]
            if int(manifest.get("snapshot_id", -1)) != int(snapshot_id):
                raise ValueError("artifact seal snapshot does not match publication")
            if int(manifest.get("config_revision", -1)) != int(config_revision):
                raise ValueError("artifact seal config revision does not match publication")
            if str(manifest.get("kind") or "") != kind:
                raise ValueError("artifact seal kind does not match publication")
        paths = sorted(
            (
                item
                for item in root.rglob("*")
                if item.is_file()
                and not item.is_symlink()
                and item.relative_to(root).as_posix()
                not in _INTERNAL_ARTIFACT_PATHS
            ),
            key=lambda p: p.relative_to(root).as_posix().casefold(),
        )
        relatives = [_relative_path(item.relative_to(root).as_posix(), label="artifact path") for item in paths]
        _assert_no_path_conflicts(relatives)
        payloads = []
        for path, relative in zip(paths, relatives):
            sha, size = self._hash_file(path)
            payloads.append((relative, path, path.stat().st_mtime_ns, sha, size))
        with self._lock, self.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                snapshot_row = connection.execute(
                    "SELECT id,dataset_fingerprint FROM dataset_snapshots WHERE id=?",
                    (int(snapshot_id),),
                ).fetchone()
                if snapshot_row is None:
                    raise KeyError(snapshot_id)
                entries = []
                for relative, path, modified_ns, sha, size in payloads:
                    blob_id = self._ensure_blob_from_path(connection, path, sha, size)
                    entries.append({"path": relative, "sha256": sha, "size_bytes": size, "modified_ns": int(modified_ns), "blob_id": blob_id})
                public_manifest = [{k: v for k, v in item.items() if k != "blob_id"} for item in entries]
                manifest_sha = hashlib.sha256(_json(public_manifest).encode("utf-8")).hexdigest()
                now = _utc_now()
                generation_id = int(connection.execute(
                    """INSERT INTO artifact_generations(
                        kind,snapshot_id,config_revision,manifest_json,manifest_sha256,file_count,total_bytes,created_utc
                    ) VALUES(?,?,?,?,?,?,?,?)""",
                    (kind, int(snapshot_id), int(config_revision), _json(public_manifest), manifest_sha,
                     len(entries), sum(item["size_bytes"] for item in entries), now),
                ).lastrowid)
                connection.executemany(
                    """INSERT INTO artifact_entries(
                        generation_id,ordinal,blob_id,relative_path,sha256,size_bytes,modified_ns
                    ) VALUES(?,?,?,?,?,?,?)""",
                    [(generation_id, i, item["blob_id"], item["path"], item["sha256"], item["size_bytes"], item["modified_ns"])
                     for i, item in enumerate(entries)],
                )
                analysis_run_id: int | None = None
                if job_id:
                    job_row = connection.execute(
                        "SELECT action,status FROM jobs WHERE id=?", (job_id,)
                    ).fetchone()
                    if job_row is None:
                        raise KeyError(job_id)
                    if str(job_row["action"]) != kind:
                        raise ValueError("job action does not match artifact kind")
                    if str(job_row["status"]) != "running":
                        raise RuntimeError("artifact job is not running")
                    connection.execute(
                        """UPDATE jobs SET snapshot_id=?,config_revision=?,
                               artifact_generation_id=?,updated_utc=? WHERE id=?""",
                        (
                            int(snapshot_id),
                            int(config_revision),
                            generation_id,
                            now,
                            job_id,
                        ),
                    )
                if analysis_run is not None:
                    if sealed is None:
                        raise ValueError("analysis run requires a sealed artifact")

                    def required_sha(name: str) -> str:
                        value = str(analysis_run.get(name) or "").strip().lower()
                        if not re.fullmatch(r"[0-9a-f]{64}", value):
                            raise ValueError(f"invalid analysis run hash: {name}")
                        return value

                    artifact_manifest_sha256 = required_sha(
                        "artifact_manifest_sha256"
                    )
                    analysis_script_sha256 = required_sha(
                        "analysis_script_sha256"
                    )
                    material_config_sha256 = required_sha(
                        "material_config_sha256"
                    )
                    analysis_summary_sha256 = required_sha(
                        "analysis_summary_sha256"
                    )
                    if artifact_manifest_sha256 != str(
                        sealed.get("manifest_sha256") or ""
                    ):
                        raise ValueError("analysis run artifact manifest hash mismatch")
                    provenance = sealed["manifest"].get("provenance")
                    provenance = provenance if isinstance(provenance, Mapping) else {}
                    script_provenance = provenance.get("analysis_script")
                    script_provenance = (
                        script_provenance
                        if isinstance(script_provenance, Mapping)
                        else {}
                    )
                    if analysis_script_sha256 != str(
                        script_provenance.get("sha256") or ""
                    ):
                        raise ValueError("analysis run script hash mismatch")
                    summary_entry = next(
                        (
                            item
                            for item in public_manifest
                            if item["path"] == "analysis_summary.json"
                        ),
                        None,
                    )
                    if (
                        summary_entry is None
                        or analysis_summary_sha256 != summary_entry["sha256"]
                    ):
                        raise ValueError("analysis run summary hash mismatch")
                    dataset_fingerprint = str(
                        analysis_run.get("dataset_fingerprint") or ""
                    )
                    if dataset_fingerprint != str(snapshot_row["dataset_fingerprint"]):
                        raise ValueError("analysis run dataset fingerprint mismatch")
                    if connection.execute(
                        "SELECT 1 FROM material_config_revisions WHERE revision=?",
                        (int(config_revision),),
                    ).fetchone() is None:
                        raise KeyError(config_revision)
                    rules = analysis_run.get("rules")
                    runtime = analysis_run.get("runtime")
                    result_summary = analysis_run.get("result_summary")
                    if not isinstance(rules, Mapping):
                        raise ValueError("analysis run rules must be an object")
                    if not isinstance(runtime, Mapping):
                        raise ValueError("analysis run runtime must be an object")
                    if not isinstance(result_summary, Mapping):
                        raise ValueError("analysis run result summary must be an object")
                    analysis_run_id = int(
                        connection.execute(
                            """INSERT INTO analysis_runs(
                                   job_id,snapshot_id,config_revision,
                                   artifact_generation_id,dataset_fingerprint,
                                   artifact_manifest_sha256,analysis_script_sha256,
                                   material_config_sha256,analysis_summary_sha256,
                                   rules_json,runtime_json,result_summary_json,created_utc
                               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (
                                job_id,
                                int(snapshot_id),
                                int(config_revision),
                                generation_id,
                                dataset_fingerprint,
                                artifact_manifest_sha256,
                                analysis_script_sha256,
                                material_config_sha256,
                                analysis_summary_sha256,
                                _json(dict(rules)),
                                _json(dict(runtime)),
                                _json(dict(result_summary)),
                                now,
                            ),
                        ).lastrowid
                    )
                selection_id = int(connection.execute(
                    "INSERT INTO artifact_selections(kind,generation_id,selected_utc) VALUES(?,?,?)",
                    (kind, generation_id, now),
                ).lastrowid)
                connection.execute(
                    "INSERT INTO artifact_current(kind,selection_id) VALUES(?,?) ON CONFLICT(kind) DO UPDATE SET selection_id=excluded.selection_id",
                    (kind, selection_id),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return {
            "id": generation_id, "generation_id": generation_id, "kind": kind,
            "snapshot_id": int(snapshot_id), "config_revision": int(config_revision),
            "manifest_sha256": manifest_sha, "file_count": len(entries),
            "total_bytes": sum(item["size_bytes"] for item in entries), "created_utc": now,
            "files": public_manifest,
            "analysis_run_id": analysis_run_id,
        }

    def publish_sealed_artifacts(
        self,
        output_dir: str | Path,
        snapshot_id: int,
        config_revision: int,
        kind: str,
        *,
        job_id: str = "",
        analysis_run: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Publish only a complete, self-verified canonical artifact package."""
        root = Path(output_dir)
        sealed = validate_sealed_artifact_directory(root)
        manifest = sealed["manifest"]
        normalized_kind = str(kind or "analysis").strip()
        if int(manifest.get("snapshot_id", -1)) != int(snapshot_id):
            raise ValueError("artifact seal snapshot does not match publication")
        if int(manifest.get("config_revision", -1)) != int(config_revision):
            raise ValueError("artifact seal config revision does not match publication")
        if str(manifest.get("kind") or "") != normalized_kind:
            raise ValueError("artifact seal kind does not match publication")
        return self.publish_artifacts(
            root,
            snapshot_id,
            config_revision,
            normalized_kind,
            job_id=job_id,
            analysis_run=analysis_run,
        )

    def restore_current_artifact_files(
        self,
        target: str | Path,
        relative_paths: list[str] | tuple[str, ...],
        kind: str | None = None,
    ) -> dict[str, Any] | None:
        """Restore only explicitly named inputs from the current generation."""
        requested = [
            _relative_path(item, label="artifact seed path")
            for item in relative_paths
        ]
        if len(set(requested)) != len(requested):
            raise ValueError("artifact seed paths must be unique")
        _assert_no_path_conflicts(requested, reserved=tuple(_INTERNAL_ARTIFACT_PATHS))
        with self.session() as connection:
            if kind:
                row = connection.execute(
                    """SELECT ag.* FROM artifact_current ac
                    JOIN artifact_selections ase ON ase.id=ac.selection_id
                    JOIN artifact_generations ag ON ag.id=ase.generation_id WHERE ac.kind=?""",
                    (kind,),
                ).fetchone()
            else:
                row = connection.execute(
                    """SELECT ag.* FROM artifact_current ac
                    JOIN artifact_selections ase ON ase.id=ac.selection_id
                    JOIN artifact_generations ag ON ag.id=ase.generation_id
                    ORDER BY ase.id DESC LIMIT 1"""
                ).fetchone()
            if row is None:
                return None
            generation = dict(row)
            available = {
                str(entry["relative_path"]): entry
                for entry in connection.execute(
                    "SELECT * FROM artifact_entries WHERE generation_id=?",
                    (generation["id"],),
                ).fetchall()
            }
            selected = [available[path] for path in requested if path in available]
            target_path = Path(target)
            with self._staged_directory(target_path) as stage:
                for entry in selected:
                    relative = _relative_path(entry["relative_path"])
                    output = stage.joinpath(*PurePosixPath(relative).parts)
                    output.parent.mkdir(parents=True, exist_ok=True)
                    self._write_blob(
                        connection,
                        int(entry["blob_id"]),
                        output,
                        entry["sha256"],
                        int(entry["size_bytes"]),
                    )
                    if int(entry["modified_ns"]) > 0:
                        os.utime(
                            output,
                            ns=(int(entry["modified_ns"]), int(entry["modified_ns"])),
                        )
        restored = [str(entry["relative_path"]) for entry in selected]
        return {
            "root": str(target_path),
            "generation_id": generation["id"],
            "kind": generation["kind"],
            "requested": requested,
            "restored": restored,
            "missing": [path for path in requested if path not in available],
        }

    def restore_current_artifacts(self, target: str | Path, kind: str | None = None) -> dict[str, Any] | None:
        with self.session() as connection:
            if kind:
                row = connection.execute(
                    """SELECT ag.* FROM artifact_current ac
                    JOIN artifact_selections ase ON ase.id=ac.selection_id
                    JOIN artifact_generations ag ON ag.id=ase.generation_id WHERE ac.kind=?""", (kind,),
                ).fetchone()
            else:
                row = connection.execute(
                    """SELECT ag.* FROM artifact_current ac
                    JOIN artifact_selections ase ON ase.id=ac.selection_id
                    JOIN artifact_generations ag ON ag.id=ase.generation_id ORDER BY ase.id DESC LIMIT 1"""
                ).fetchone()
            if row is None:
                return None
            generation = dict(row)
            entries = connection.execute(
                "SELECT * FROM artifact_entries WHERE generation_id=? ORDER BY ordinal", (generation["id"],)
            ).fetchall()
            _assert_no_path_conflicts([entry["relative_path"] for entry in entries], reserved=(".start-stop-artifacts.json",))
            target_path = Path(target)
            with self._staged_directory(target_path) as stage:
                for entry in entries:
                    relative = _relative_path(entry["relative_path"])
                    output = stage.joinpath(*PurePosixPath(relative).parts)
                    output.parent.mkdir(parents=True, exist_ok=True)
                    self._write_blob(connection, int(entry["blob_id"]), output, entry["sha256"], int(entry["size_bytes"]))
                    if int(entry["modified_ns"]) > 0:
                        os.utime(output, ns=(int(entry["modified_ns"]), int(entry["modified_ns"])))
                manifest = {"generation_id": generation["id"], "kind": generation["kind"], "snapshot_id": generation["snapshot_id"], "config_revision": generation["config_revision"], "manifest_sha256": generation["manifest_sha256"], "files": json.loads(generation["manifest_json"])}
                (stage / ".start-stop-artifacts.json").write_text(_json(manifest), encoding="utf-8")
        return {"root": str(target_path), **manifest, "file_count": generation["file_count"], "total_bytes": generation["total_bytes"]}

    def get_collection_config(self) -> dict[str, Any]:
        """Return the current user-controlled remote roots, if configured."""
        with self.session() as connection:
            row = connection.execute(
                """SELECT r.* FROM collection_config_current c
                JOIN collection_config_revisions r ON r.revision=c.revision
                WHERE c.id=1"""
            ).fetchone()
        if row is None:
            return {"revision": 0, "updated_utc": "", "machines": []}
        machines = json.loads(row["machines_json"])
        if not isinstance(machines, list):
            raise RuntimeError("stored collection configuration is invalid")
        return {
            "revision": int(row["revision"]),
            "updated_utc": str(row["created_utc"]),
            "machines": machines,
        }

    def save_collection_config(
        self,
        *,
        expected_revision: int,
        machines: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Atomically publish one already-validated remote-root revision."""
        if not isinstance(machines, list):
            raise ValueError("machines must be a list")
        payload = json.loads(_json(machines))
        with self._lock, self.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current = connection.execute(
                    "SELECT revision FROM collection_config_current WHERE id=1"
                ).fetchone()
                revision = int(current["revision"]) if current else 0
                if revision != int(expected_revision):
                    raise ValueError(
                        "搜索位置配置已在另一页中更新，请刷新后重试。"
                    )
                revision += 1
                now = _utc_now()
                connection.execute(
                    "INSERT INTO collection_config_revisions(revision,machines_json,created_utc) VALUES(?,?,?)",
                    (revision, _json(payload), now),
                )
                connection.execute(
                    """INSERT INTO collection_config_current(id,revision) VALUES(1,?)
                    ON CONFLICT(id) DO UPDATE SET revision=excluded.revision""",
                    (revision,),
                )
                connection.execute(
                    "INSERT INTO audit_log(created_utc,action,target,detail_json) VALUES(?,?,?,?)",
                    (
                        now,
                        "start_stop_collection_config.save",
                        str(revision),
                        _json(
                            {
                                "revision": revision,
                                "machine_count": len(payload),
                                "path_count": sum(
                                    len(item.get("paths", []))
                                    for item in payload
                                    if isinstance(item, dict)
                                ),
                            }
                        ),
                    ),
                )
                connection.commit()
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise
        return self.get_collection_config()

    @staticmethod
    def _auto_update_state_from_connection(
        connection: sqlite3.Connection,
    ) -> dict[str, Any]:
        config = connection.execute(
            """SELECT r.* FROM auto_update_current c
            JOIN auto_update_revisions r ON r.revision=c.revision
            WHERE c.id=1"""
        ).fetchone()
        runtime = connection.execute(
            "SELECT * FROM auto_update_runtime WHERE id=1"
        ).fetchone()
        if runtime is None:
            raise RuntimeError("automatic update runtime row is missing")
        return {
            "revision": int(config["revision"]) if config is not None else 0,
            "enabled": bool(config["enabled"]) if config is not None else False,
            "interval_minutes": (
                int(config["interval_minutes"]) if config is not None else 60
            ),
            "updated_utc": str(config["created_utc"]) if config is not None else "",
            "next_run_utc": str(runtime["next_run_utc"]),
            "last_attempt_utc": str(runtime["last_attempt_utc"]),
            "last_started_utc": str(runtime["last_started_utc"]),
            "last_finished_utc": str(runtime["last_finished_utc"]),
            "last_status": str(runtime["last_status"]),
            "active_job_id": str(runtime["active_job_id"]),
        }

    def _get_auto_update_state(self) -> dict[str, Any]:
        """Return scheduler state, including its internal active job id."""
        with self.session() as connection:
            return self._auto_update_state_from_connection(connection)

    def get_auto_update_config(self) -> dict[str, Any]:
        """Return only the safe settings and scheduling status used by the UI."""
        state = self._get_auto_update_state()
        state.pop("active_job_id", None)
        state["last_completed_utc"] = state["last_finished_utc"]
        state["allowed_interval_minutes"] = list(AUTO_UPDATE_INTERVAL_MINUTES)
        return state

    def save_auto_update_config(
        self,
        *,
        expected_revision: int,
        enabled: bool,
        interval_minutes: int,
        now_utc: str | None = None,
    ) -> dict[str, Any]:
        """Publish one settings revision and schedule its first future run."""
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
            raise ValueError("expected_revision must be an integer")
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean")
        if isinstance(interval_minutes, bool) or not isinstance(interval_minutes, int):
            raise ValueError("interval_minutes must be an integer")
        if interval_minutes not in AUTO_UPDATE_INTERVAL_MINUTES:
            raise ValueError("interval_minutes is not an allowed automatic update interval")
        now = str(now_utc or _utc_now())
        try:
            parsed_now = dt.datetime.fromisoformat(now.replace("Z", "+00:00"))
            if parsed_now.tzinfo is None:
                raise ValueError
            parsed_now = parsed_now.astimezone(dt.timezone.utc)
        except (TypeError, ValueError) as exc:
            raise ValueError("now_utc must be a timezone-aware ISO timestamp") from exc
        now = parsed_now.isoformat(timespec="seconds")
        next_run = (
            (parsed_now + dt.timedelta(minutes=interval_minutes)).isoformat(
                timespec="seconds"
            )
            if enabled
            else ""
        )
        with self._lock, self.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current = connection.execute(
                    "SELECT revision FROM auto_update_current WHERE id=1"
                ).fetchone()
                revision = int(current["revision"]) if current else 0
                if revision != expected_revision:
                    raise AutoUpdateConfigConflict(
                        "自动更新设置已在另一页中修改，请刷新后重试。"
                    )
                revision += 1
                connection.execute(
                    """INSERT INTO auto_update_revisions(
                        revision,enabled,interval_minutes,created_utc
                    ) VALUES(?,?,?,?)""",
                    (revision, int(enabled), interval_minutes, now),
                )
                connection.execute(
                    """INSERT INTO auto_update_current(id,revision) VALUES(1,?)
                    ON CONFLICT(id) DO UPDATE SET revision=excluded.revision""",
                    (revision,),
                )
                connection.execute(
                    "UPDATE auto_update_runtime SET next_run_utc=? WHERE id=1",
                    (next_run,),
                )
                connection.execute(
                    """INSERT INTO audit_log(
                        created_utc,action,target,detail_json
                    ) VALUES(?,?,?,?)""",
                    (
                        now,
                        "start_stop_auto_update.save",
                        str(revision),
                        _json(
                            {
                                "revision": revision,
                                "enabled": enabled,
                                "interval_minutes": interval_minutes,
                            }
                        ),
                    ),
                )
                connection.commit()
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise
        return self.get_auto_update_config()

    def ensure_auto_update_schedule(self, *, now_utc: str) -> dict[str, Any]:
        """Repair only a missing schedule; never turn startup into an immediate run."""
        now = dt.datetime.fromisoformat(str(now_utc).replace("Z", "+00:00"))
        if now.tzinfo is None:
            raise ValueError("now_utc must include a timezone")
        now = now.astimezone(dt.timezone.utc)
        with self._lock, self.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = self._auto_update_state_from_connection(connection)
            next_run = state["next_run_utc"]
            if state["enabled"] and not next_run and not state["active_job_id"]:
                next_run = (now + dt.timedelta(minutes=state["interval_minutes"])).isoformat(
                    timespec="seconds"
                )
                connection.execute(
                    "UPDATE auto_update_runtime SET next_run_utc=? WHERE id=1",
                    (next_run,),
                )
            elif not state["enabled"] and next_run:
                connection.execute(
                    "UPDATE auto_update_runtime SET next_run_utc='' WHERE id=1"
                )
            connection.commit()
        return self._get_auto_update_state()

    def record_auto_update_started(
        self,
        *,
        job_id: str,
        now_utc: str,
    ) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", str(job_id or "")):
            raise ValueError("automatic update job id is invalid")
        now = dt.datetime.fromisoformat(str(now_utc).replace("Z", "+00:00"))
        if now.tzinfo is None:
            raise ValueError("now_utc must include a timezone")
        now = now.astimezone(dt.timezone.utc)
        now_text = now.isoformat(timespec="seconds")
        with self._lock, self.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = self._auto_update_state_from_connection(connection)
            next_run = (
                (now + dt.timedelta(minutes=state["interval_minutes"])).isoformat(
                    timespec="seconds"
                )
                if state["enabled"]
                else ""
            )
            connection.execute(
                """UPDATE auto_update_runtime SET
                    next_run_utc=?,last_attempt_utc=?,last_started_utc=?,
                    last_status='running',active_job_id=? WHERE id=1""",
                (next_run, now_text, now_text, str(job_id)),
            )
            connection.commit()
        return self._get_auto_update_state()

    def record_auto_update_busy(
        self,
        *,
        now_utc: str,
        retry_minutes: int = 5,
    ) -> dict[str, Any]:
        now = dt.datetime.fromisoformat(str(now_utc).replace("Z", "+00:00"))
        if now.tzinfo is None:
            raise ValueError("now_utc must include a timezone")
        now = now.astimezone(dt.timezone.utc)
        now_text = now.isoformat(timespec="seconds")
        with self._lock, self.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = self._auto_update_state_from_connection(connection)
            next_run = (
                (now + dt.timedelta(minutes=max(1, int(retry_minutes)))).isoformat(
                    timespec="seconds"
                )
                if state["enabled"]
                else ""
            )
            connection.execute(
                """UPDATE auto_update_runtime SET next_run_utc=?,
                    last_attempt_utc=?,last_status='busy',active_job_id=''
                    WHERE id=1""",
                (next_run, now_text),
            )
            connection.commit()
        return self._get_auto_update_state()

    def record_auto_update_launch_failure(
        self,
        *,
        now_utc: str,
    ) -> dict[str, Any]:
        now = dt.datetime.fromisoformat(str(now_utc).replace("Z", "+00:00"))
        if now.tzinfo is None:
            raise ValueError("now_utc must include a timezone")
        now = now.astimezone(dt.timezone.utc)
        now_text = now.isoformat(timespec="seconds")
        with self._lock, self.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = self._auto_update_state_from_connection(connection)
            next_run = (
                (now + dt.timedelta(minutes=state["interval_minutes"])).isoformat(
                    timespec="seconds"
                )
                if state["enabled"]
                else ""
            )
            connection.execute(
                """UPDATE auto_update_runtime SET next_run_utc=?,
                    last_attempt_utc=?,last_finished_utc=?,last_status='failed',
                    active_job_id='' WHERE id=1""",
                (next_run, now_text, now_text),
            )
            connection.commit()
        return self._get_auto_update_state()

    def record_auto_update_finished(
        self,
        *,
        job_id: str,
        status: str,
        now_utc: str,
    ) -> dict[str, Any]:
        normalized_status = str(status or "").strip()
        if normalized_status not in {"completed", "completed_with_warnings", "failed"}:
            raise ValueError("automatic update terminal status is invalid")
        now = dt.datetime.fromisoformat(str(now_utc).replace("Z", "+00:00"))
        if now.tzinfo is None:
            raise ValueError("now_utc must include a timezone")
        now = now.astimezone(dt.timezone.utc)
        now_text = now.isoformat(timespec="seconds")
        with self._lock, self.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = self._auto_update_state_from_connection(connection)
            if state["active_job_id"] != str(job_id):
                connection.rollback()
                return state
            next_run = state["next_run_utc"] if state["enabled"] else ""
            if state["enabled"]:
                try:
                    due = dt.datetime.fromisoformat(next_run.replace("Z", "+00:00"))
                    if due.tzinfo is None:
                        raise ValueError
                    due = due.astimezone(dt.timezone.utc)
                except (TypeError, ValueError):
                    due = now
                if due <= now:
                    next_run = (
                        now + dt.timedelta(minutes=state["interval_minutes"])
                    ).isoformat(timespec="seconds")
            connection.execute(
                """UPDATE auto_update_runtime SET next_run_utc=?,
                    last_finished_utc=?,last_status=?,active_job_id='' WHERE id=1""",
                (next_run, now_text, normalized_status),
            )
            connection.commit()
        return self._get_auto_update_state()

    def record_auto_update_interrupted(
        self,
        *,
        now_utc: str,
        retry_minutes: int = 5,
    ) -> dict[str, Any]:
        now = dt.datetime.fromisoformat(str(now_utc).replace("Z", "+00:00"))
        if now.tzinfo is None:
            raise ValueError("now_utc must include a timezone")
        now = now.astimezone(dt.timezone.utc)
        now_text = now.isoformat(timespec="seconds")
        with self._lock, self.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = self._auto_update_state_from_connection(connection)
            next_run = (
                (now + dt.timedelta(minutes=max(1, int(retry_minutes)))).isoformat(
                    timespec="seconds"
                )
                if state["enabled"]
                else ""
            )
            connection.execute(
                """UPDATE auto_update_runtime SET next_run_utc=?,
                    last_finished_utc=?,last_status='interrupted_by_restart',
                    active_job_id=''
                    WHERE id=1""",
                (next_run, now_text),
            )
            connection.commit()
        return self._get_auto_update_state()

    @staticmethod
    def _live_preview_state_from_connection(
        connection: sqlite3.Connection,
    ) -> dict[str, Any]:
        config = connection.execute(
            """SELECT r.* FROM live_preview_current c
            JOIN live_preview_revisions r ON r.revision=c.revision
            WHERE c.id=1"""
        ).fetchone()
        runtime = connection.execute(
            "SELECT * FROM live_preview_runtime WHERE id=1"
        ).fetchone()
        if runtime is None:
            raise RuntimeError("live preview runtime row is missing")
        try:
            preview = json.loads(str(runtime["preview_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            preview = {}
        if not isinstance(preview, dict):
            preview = {}
        items = preview.get("items")
        if not isinstance(items, list):
            preview["items"] = []
        return {
            "revision": int(config["revision"]) if config is not None else 0,
            "enabled": bool(config["enabled"]) if config is not None else False,
            "interval_minutes": 5,
            "updated_utc": str(config["created_utc"]) if config is not None else "",
            "next_run_utc": str(runtime["next_run_utc"]),
            "last_attempt_utc": str(runtime["last_attempt_utc"]),
            "last_started_utc": str(runtime["last_started_utc"]),
            "last_finished_utc": str(runtime["last_finished_utc"]),
            "last_status": str(runtime["last_status"]),
            "phase": str(runtime["phase"]),
            "message": str(runtime["message"]),
            "items_completed": int(runtime["items_completed"]),
            "items_total": int(runtime["items_total"]),
            "preview": preview,
        }

    def _get_live_preview_state(self) -> dict[str, Any]:
        with self.session() as connection:
            return self._live_preview_state_from_connection(connection)

    def get_live_preview_config(self) -> dict[str, Any]:
        state = self._get_live_preview_state()
        state["last_completed_utc"] = state["last_finished_utc"]
        state["allowed_interval_minutes"] = [5]
        return state

    def save_live_preview_config(
        self,
        *,
        expected_revision: int,
        enabled: bool,
        now_utc: str | None = None,
    ) -> dict[str, Any]:
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
            raise ValueError("expected_revision must be an integer")
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean")
        now = str(now_utc or _utc_now())
        try:
            parsed_now = dt.datetime.fromisoformat(now.replace("Z", "+00:00"))
            if parsed_now.tzinfo is None:
                raise ValueError
            parsed_now = parsed_now.astimezone(dt.timezone.utc)
        except (TypeError, ValueError) as exc:
            raise ValueError("now_utc must be a timezone-aware ISO timestamp") from exc
        now_text = parsed_now.isoformat(timespec="seconds")
        with self._lock, self.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current = connection.execute(
                    "SELECT revision FROM live_preview_current WHERE id=1"
                ).fetchone()
                revision = int(current["revision"]) if current else 0
                if revision != expected_revision:
                    raise AutoUpdateConfigConflict(
                        "实时数据预览设置已在另一页中修改，请刷新后重试。"
                    )
                revision += 1
                connection.execute(
                    """INSERT INTO live_preview_revisions(
                        revision,enabled,interval_minutes,created_utc
                    ) VALUES(?,?,5,?)""",
                    (revision, int(enabled), now_text),
                )
                connection.execute(
                    """INSERT INTO live_preview_current(id,revision) VALUES(1,?)
                    ON CONFLICT(id) DO UPDATE SET revision=excluded.revision""",
                    (revision,),
                )
                connection.execute(
                    """UPDATE live_preview_runtime SET
                        next_run_utc=?,phase=?,message=? WHERE id=1""",
                    (
                        now_text if enabled else "",
                        "waiting" if enabled else "disabled",
                        "已开启，正在等待首次实时预览"
                        if enabled
                        else "实时数据预览已关闭",
                    ),
                )
                connection.execute(
                    """INSERT INTO audit_log(
                        created_utc,action,target,detail_json
                    ) VALUES(?,?,?,?)""",
                    (
                        now_text,
                        "start_stop_live_preview.save",
                        str(revision),
                        _json({"revision": revision, "enabled": enabled}),
                    ),
                )
                connection.commit()
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise
        return self.get_live_preview_config()

    def ensure_live_preview_schedule(self, *, now_utc: str) -> dict[str, Any]:
        now = dt.datetime.fromisoformat(str(now_utc).replace("Z", "+00:00"))
        if now.tzinfo is None:
            raise ValueError("now_utc must include a timezone")
        now_text = now.astimezone(dt.timezone.utc).isoformat(timespec="seconds")
        with self._lock, self.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = self._live_preview_state_from_connection(connection)
            if state["enabled"] and not state["next_run_utc"]:
                connection.execute(
                    "UPDATE live_preview_runtime SET next_run_utc=? WHERE id=1",
                    (now_text,),
                )
            elif not state["enabled"] and state["next_run_utc"]:
                connection.execute(
                    "UPDATE live_preview_runtime SET next_run_utc='' WHERE id=1"
                )
            connection.commit()
        return self._get_live_preview_state()

    def record_live_preview_started(self, *, now_utc: str) -> dict[str, Any]:
        now = dt.datetime.fromisoformat(str(now_utc).replace("Z", "+00:00"))
        if now.tzinfo is None:
            raise ValueError("now_utc must include a timezone")
        now = now.astimezone(dt.timezone.utc)
        now_text = now.isoformat(timespec="seconds")
        with self._lock, self.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = self._live_preview_state_from_connection(connection)
            next_run = (
                (now + dt.timedelta(minutes=5)).isoformat(timespec="seconds")
                if state["enabled"]
                else ""
            )
            connection.execute(
                """UPDATE live_preview_runtime SET
                    next_run_utc=?,last_attempt_utc=?,last_started_utc=?,
                    last_status='running',phase='recognizing',
                    message='正在识别活动启停文件',items_completed=0,items_total=0
                    WHERE id=1""",
                (next_run, now_text, now_text),
            )
            connection.commit()
        return self._get_live_preview_state()

    def record_live_preview_progress(
        self,
        *,
        phase: str,
        message: str,
        items_completed: int,
        items_total: int,
    ) -> dict[str, Any]:
        phase_text = re.sub(r"[^a-z0-9_-]+", "_", str(phase).casefold())[:64]
        message_text = str(message or "")[:500]
        completed = max(0, int(items_completed))
        total = max(completed, int(items_total))
        with self._lock, self.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """UPDATE live_preview_runtime SET phase=?,message=?,
                    items_completed=?,items_total=?
                    WHERE id=1 AND last_status='running'""",
                (phase_text or "running", message_text, completed, total),
            )
            connection.commit()
        return self._get_live_preview_state()

    def record_live_preview_finished(
        self,
        *,
        status: str,
        payload: Mapping[str, Any],
        message: str,
        now_utc: str,
    ) -> dict[str, Any]:
        normalized_status = str(status or "").strip()
        if normalized_status not in {"completed", "completed_with_warnings"}:
            raise ValueError("live preview terminal status is invalid")
        if not isinstance(payload, Mapping):
            raise ValueError("live preview payload must be an object")
        preview_json = _json(dict(payload))
        if len(preview_json.encode("utf-8")) > _MAX_LIVE_PREVIEW_JSON_BYTES:
            raise ValueError("live preview payload is too large")
        now = dt.datetime.fromisoformat(str(now_utc).replace("Z", "+00:00"))
        if now.tzinfo is None:
            raise ValueError("now_utc must include a timezone")
        now = now.astimezone(dt.timezone.utc)
        now_text = now.isoformat(timespec="seconds")
        with self._lock, self.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = self._live_preview_state_from_connection(connection)
            next_run = state["next_run_utc"] if state["enabled"] else ""
            if state["enabled"]:
                try:
                    due = dt.datetime.fromisoformat(next_run.replace("Z", "+00:00"))
                    if due.tzinfo is None:
                        raise ValueError
                    due = due.astimezone(dt.timezone.utc)
                except (TypeError, ValueError):
                    due = now
                if due <= now:
                    next_run = (
                        now + dt.timedelta(minutes=5)
                    ).isoformat(timespec="seconds")
            item_count = len(payload.get("items", [])) if isinstance(payload.get("items"), list) else 0
            connection.execute(
                """UPDATE live_preview_runtime SET next_run_utc=?,
                    last_finished_utc=?,last_status=?,phase='completed',message=?,
                    items_completed=?,items_total=?,preview_json=? WHERE id=1""",
                (
                    next_run,
                    now_text,
                    normalized_status,
                    str(message or "")[:500],
                    item_count,
                    item_count,
                    preview_json,
                ),
            )
            connection.commit()
        return self._get_live_preview_state()

    def record_live_preview_failed(
        self,
        *,
        message: str,
        now_utc: str,
    ) -> dict[str, Any]:
        now = dt.datetime.fromisoformat(str(now_utc).replace("Z", "+00:00"))
        if now.tzinfo is None:
            raise ValueError("now_utc must include a timezone")
        now = now.astimezone(dt.timezone.utc)
        now_text = now.isoformat(timespec="seconds")
        with self._lock, self.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = self._live_preview_state_from_connection(connection)
            next_run = (
                (now + dt.timedelta(minutes=5)).isoformat(timespec="seconds")
                if state["enabled"]
                else ""
            )
            connection.execute(
                """UPDATE live_preview_runtime SET next_run_utc=?,
                    last_finished_utc=?,last_status='failed',phase='failed',
                    message=?,items_completed=0,items_total=0 WHERE id=1""",
                (next_run, now_text, str(message or "")[:500]),
            )
            connection.commit()
        return self._get_live_preview_state()

    def get_start_stop_config(self) -> dict[str, Any]:
        with self.session() as connection:
            row = connection.execute(
                """SELECT r.* FROM material_config_current c
                JOIN material_config_revisions r ON r.revision=c.revision WHERE c.id=1"""
            ).fetchone()
        if row is None:
            return {"dataset_fingerprint": "", "revision": 0, "updated_utc": "", "materials": []}
        materials = json.loads(row["materials_json"])
        for item in materials:
            item.setdefault("updated_utc", row["created_utc"])
            if "include_in_summary_atlas" in item:
                item["include_in_summary_atlas"] = bool(item["include_in_summary_atlas"])
            item["favorite"] = bool(item.get("favorite", False))
        return {"dataset_fingerprint": row["dataset_fingerprint"], "revision": int(row["revision"]), "updated_utc": row["created_utc"], "materials": materials}

    def save_start_stop_config(
        self, *, dataset_fingerprint: str, expected_revision: int,
        materials: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if not isinstance(materials, list):
            raise ValueError("materials must be a list")
        normalized = []
        seen = set()
        for raw in materials:
            if not isinstance(raw, Mapping):
                raise ValueError("each material must be an object")
            item = dict(raw)
            key = unicodedata.normalize("NFC", str(item.get("material_key") or item.get("key") or ""))
            if not key or key in seen:
                raise ValueError("material keys must be non-empty and unique")
            seen.add(key)
            item["material_key"] = key
            item.pop("key", None)
            item["plot_name"] = str(item.get("plot_name") or "")
            item["include_in_summary_atlas"] = bool(item.get("include_in_summary_atlas"))
            item["favorite"] = bool(item.get("favorite", False))
            item["notes"] = str(item.get("notes") or "")
            item["source_fingerprint"] = str(item.get("source_fingerprint") or item.get("fingerprint") or "")
            item.pop("updated_utc", None)
            normalized.append(item)
        with self._lock, self.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute("SELECT revision FROM material_config_current WHERE id=1").fetchone()
            revision = int(current["revision"]) if current else 0
            if revision != int(expected_revision):
                connection.rollback()
                raise ValueError("材料配置已在另一页中更新，请刷新后重试。")
            revision += 1
            now = _utc_now()
            connection.execute(
                "INSERT INTO material_config_revisions(revision,dataset_fingerprint,materials_json,created_utc) VALUES(?,?,?,?)",
                (revision, str(dataset_fingerprint), _json(normalized), now),
            )
            connection.execute(
                "INSERT INTO material_config_current(id,revision) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision",
                (revision,),
            )
            connection.execute(
                "INSERT INTO audit_log(created_utc,action,target,detail_json) VALUES(?,?,?,?)",
                (now, "start_stop_config.save", str(dataset_fingerprint), _json({"revision": revision, "material_count": len(normalized)})),
            )
            connection.commit()
        return self.get_start_stop_config()

    def import_start_stop_config(
        self, config: Mapping[str, Any], *, overwrite: bool = False
    ) -> dict[str, Any]:
        revision = max(1, int(config.get("revision", 1)))
        materials = list(config.get("materials") or [])
        fingerprint = str(config.get("dataset_fingerprint") or "")
        created = str(config.get("updated_utc") or _utc_now())
        with self._lock, self.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute("SELECT revision FROM material_config_current WHERE id=1").fetchone()
            if current and not overwrite:
                connection.rollback()
                raise ValueError("material configuration already exists")
            existing = connection.execute("SELECT 1 FROM material_config_revisions WHERE revision=?", (revision,)).fetchone()
            if existing and int(current["revision"]) != revision:
                connection.rollback()
                raise ValueError("configuration revision already exists")
            if not existing:
                connection.execute(
                    "INSERT INTO material_config_revisions(revision,dataset_fingerprint,materials_json,created_utc) VALUES(?,?,?,?)",
                    (revision, fingerprint, _json(materials), created),
                )
            connection.execute(
                "INSERT INTO material_config_current(id,revision) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision",
                (revision,),
            )
            connection.execute(
                "INSERT INTO audit_log(created_utc,action,target,detail_json) VALUES(?,?,?,?)",
                (_utc_now(), "start_stop_config.import", fingerprint, _json({"revision": revision, "material_count": len(materials)})),
            )
            connection.commit()
        return self.get_start_stop_config()

    def repository_status(self) -> dict[str, Any]:
        with self.session() as connection:
            scalar = lambda sql: connection.execute(sql).fetchone()[0]
            stored_blob_count = int(scalar("SELECT COUNT(*) FROM content_blobs"))
            stored_blob_bytes = int(scalar("SELECT COALESCE(SUM(size_bytes),0) FROM content_blobs"))
            raw_blob_row = connection.execute(
                """SELECT COUNT(*),COALESCE(SUM(size_bytes),0) FROM content_blobs
                WHERE id IN (SELECT DISTINCT blob_id FROM source_versions)"""
            ).fetchone()
            blob_count = int(raw_blob_row[0])
            blob_bytes = int(raw_blob_row[1])
            source_count = int(scalar("SELECT COUNT(*) FROM sources"))
            version_count = int(scalar("SELECT COUNT(*) FROM source_versions"))
            current_count = int(scalar("SELECT COUNT(*) FROM source_current"))
            current_bytes = int(scalar("SELECT COALESCE(SUM(sv.size_bytes),0) FROM source_current sc JOIN source_selections ss ON ss.id=sc.selection_id JOIN source_versions sv ON sv.id=ss.source_version_id"))
            candidate_count = int(scalar("SELECT COUNT(*) FROM source_current sc JOIN source_selections ss ON ss.id=sc.selection_id JOIN source_versions sv ON sv.id=ss.source_version_id LEFT JOIN candidate_current cc ON cc.source_version_id=sv.id LEFT JOIN candidate_marks cm ON cm.id=cc.mark_id WHERE COALESCE(cm.is_candidate,sv.is_candidate)=1"))
            batch_count = int(scalar("SELECT COUNT(*) FROM collection_batches"))
            issue_count = int(scalar("SELECT COUNT(*) FROM collection_issues"))
            snapshot_count = int(scalar("SELECT COUNT(*) FROM dataset_snapshots"))
            generation_count = int(scalar("SELECT COUNT(*) FROM artifact_generations"))
            audit_count = int(scalar("SELECT COUNT(*) FROM audit_log"))
            latest_batch = self._row_dict(connection.execute("SELECT * FROM collection_batches ORDER BY started_utc DESC,id DESC LIMIT 1").fetchone())
            latest_snapshot_row = self._row_dict(connection.execute("SELECT id,dataset_fingerprint,file_count,total_bytes,created_utc FROM dataset_snapshots ORDER BY id DESC LIMIT 1").fetchone())
            current_artifacts = [dict(row) for row in connection.execute("""SELECT ac.kind,ag.id AS generation_id,ag.snapshot_id,ag.config_revision,ag.file_count,ag.total_bytes,ag.created_utc FROM artifact_current ac JOIN artifact_selections ase ON ase.id=ac.selection_id JOIN artifact_generations ag ON ag.id=ase.generation_id ORDER BY ac.kind""").fetchall()]
            config = self.get_start_stop_config()
            time_range = connection.execute("SELECT MIN(NULLIF(source_modified_utc,'')),MAX(NULLIF(source_modified_utc,'')) FROM source_versions").fetchone()
            pragmas = {
                "journal_mode": str(connection.execute("PRAGMA journal_mode").fetchone()[0]),
                "synchronous": int(connection.execute("PRAGMA synchronous").fetchone()[0]),
                "foreign_keys": bool(connection.execute("PRAGMA foreign_keys").fetchone()[0]),
                "busy_timeout_ms": int(connection.execute("PRAGMA busy_timeout").fetchone()[0]),
            }
        if latest_batch:
            latest_batch["metadata"] = json.loads(latest_batch.pop("metadata_json"))
            latest_batch["totals"] = json.loads(latest_batch.pop("totals_json"))
        latest_collection_utc = (latest_batch or {}).get("finished_utc") or (latest_batch or {}).get("started_utc") or ""
        result = {
            "schema_version": SCHEMA_VERSION, "database_path": str(self.path),
            "database_size_bytes": self.path.stat().st_size if self.path.exists() else 0,
            **pragmas,
            "blobs": {"count": blob_count, "total_bytes": blob_bytes, "stored_count": stored_blob_count, "stored_bytes": stored_blob_bytes},
            "sources": {"count": source_count, "version_count": version_count, "current_count": current_count, "current_bytes": current_bytes, "candidate_count": candidate_count},
            "collections": {"batch_count": batch_count, "issue_count": issue_count, "latest": latest_batch},
            "snapshots": {"count": snapshot_count, "latest": latest_snapshot_row},
            "artifacts": {"generation_count": generation_count, "current": current_artifacts},
            "config": {"revision": config["revision"], "dataset_fingerprint": config["dataset_fingerprint"], "updated_utc": config["updated_utc"]},
            "audit_count": audit_count, "source_first_modified_utc": time_range[0] or "",
            "source_last_modified_utc": time_range[1] or "", "latest_collection_utc": latest_collection_utc,
            "source_count": source_count, "current_source_count": current_count,
            "current_file_count": current_count, "current_bytes": current_bytes,
            "blob_count": blob_count, "blob_bytes": blob_bytes, "snapshot_count": snapshot_count,
            "stored_blob_count": stored_blob_count, "stored_blob_bytes": stored_blob_bytes,
            "artifact_generation_count": generation_count,
        }
        return result

    def operational_check(self) -> dict[str, Any]:
        """Fast routine-operation gate; never copies or hashes BLOB content."""

        return operational_check_database(
            self.path,
            expected_schema_version=SCHEMA_VERSION,
            required_tables=OPERATIONAL_REQUIRED_TABLES,
        )

    def integrity_check(self, verify_blobs: bool = True) -> dict[str, Any]:
        errors: list[str] = []
        checked_blobs = 0
        with self.session() as connection:
            for row in connection.execute("PRAGMA integrity_check").fetchall():
                if str(row[0]).lower() != "ok":
                    errors.append(f"sqlite: {row[0]}")
            for row in connection.execute("PRAGMA foreign_key_check").fetchall():
                errors.append(f"foreign key: {tuple(row)}")
            if verify_blobs:
                for row in connection.execute("SELECT id,sha256,size_bytes FROM content_blobs ORDER BY id"):
                    digest = hashlib.sha256()
                    size = 0
                    with connection.blobopen("content_blobs", "content", int(row["id"]), readonly=True) as blob:
                        while True:
                            chunk = blob.read(_CHUNK_SIZE)
                            if not chunk:
                                break
                            digest.update(chunk)
                            size += len(chunk)
                    checked_blobs += 1
                    if size != int(row["size_bytes"]) or digest.hexdigest() != row["sha256"]:
                        errors.append(f"blob {row['id']} hash/size mismatch")
            for row in connection.execute("""SELECT sc.source_id,ss.source_id AS selection_source,sv.source_id AS version_source FROM source_current sc JOIN source_selections ss ON ss.id=sc.selection_id JOIN source_versions sv ON sv.id=ss.source_version_id WHERE sc.source_id<>ss.source_id OR ss.source_id<>sv.source_id"""):
                errors.append(f"source current pointer mismatch: {row['source_id']}")
            for row in connection.execute("SELECT id,manifest_json,manifest_sha256 FROM artifact_generations"):
                actual = hashlib.sha256(str(row["manifest_json"]).encode("utf-8")).hexdigest()
                if actual != row["manifest_sha256"]:
                    errors.append(f"artifact generation {row['id']} manifest mismatch")
        return {"ok": not errors, "schema_version": SCHEMA_VERSION, "checked_blobs": checked_blobs, "errors": errors}


__all__ = [
    "OPERATIONAL_REQUIRED_TABLES",
    "SCHEMA_VERSION",
    "StartStopDatabase",
]
