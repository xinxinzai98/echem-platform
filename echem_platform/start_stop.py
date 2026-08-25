from __future__ import annotations

import copy
import csv
import datetime as dt
import hashlib
import json
import math
import os
import re
import signal
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import uuid
from collections import OrderedDict
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from .start_stop_backup import build_safety_status
from .start_stop_database import (
    ARTIFACT_SEAL_SCHEMA_VERSION,
    seal_artifact_directory,
    validate_sealed_artifact_directory,
)


class StartStopWorkspaceError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = int(status)


class _UnchangedRepositoryScan(Exception):
    def __init__(self, result: dict[str, Any], *, warning: bool) -> None:
        super().__init__("repository snapshot already published")
        self.result = dict(result)
        self.warning = bool(warning)


_DUPLICATE_PLOT_NAME_QUOTED = re.compile(
    r'绘图名称[“"](?P<name>[^”"\r\n]{1,180})[”"][^\r\n]{0,600}?重复'
)
_DUPLICATE_PLOT_NAME_PLAIN = re.compile(
    r"绘图名称重复[：:]\s*(?P<name>[^\r\n；;]{1,180})"
)


def duplicate_plot_name_notice(value: Any) -> str | None:
    """Convert a path-bearing analyzer collision into a safe UI notice."""
    if not isinstance(value, str):
        return None
    match = _DUPLICATE_PLOT_NAME_QUOTED.search(value)
    if match is None:
        match = _DUPLICATE_PLOT_NAME_PLAIN.search(value)
    if match is None:
        return None
    name = unicodedata.normalize("NFC", match.group("name"))
    name = " ".join(name.split()).strip()
    if not name or any(ord(character) < 32 for character in name):
        return None
    # A display name may contain slash-like punctuation. Use full-width
    # characters so the public notice can never be mistaken for a local path.
    name = name.replace("/", "／").replace("\\", "＼")[:120].rstrip()
    if not name:
        return None
    return (
        f"检测到重复的绘图名称：“{name}”。"
        "新数据已保留，请到“材料库”修改完整绘图名称后重试更新。"
    )


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "是"}


def _number(value: Any, default: float | None = None) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _work_step_current_ma_cm2(value: Any) -> int | None:
    """Return a stable protocol current without splitting on acquisition noise.

    ADT exports report measured medians such as -299.344 and +29.954
    mA cm-2. Five-milliamp bins retain meaningful protocol differences while
    grouping those values with their nominal -300/+30 mA cm-2 work step.
    """
    parsed = _number(value)
    if parsed is None:
        return None
    quantized = int(round((parsed * 1000.0) / 5.0) * 5)
    return 0 if quantized == 0 else quantized


def _work_step_duration_s(value: Any) -> int | None:
    parsed = _number(value)
    if parsed is None or parsed < 0:
        return None
    return max(0, int(round(parsed)))


def _work_step_number_token(value: int | None) -> str:
    return "na" if value is None else str(value)


def _format_signed_work_step_current(value: int | None) -> str:
    if value is None:
        return "?"
    if value < 0:
        return f"−{abs(value)}"
    if value > 0:
        return f"+{value}"
    return "0"


def _work_step_metadata(row: dict[str, Any]) -> dict[str, Any]:
    """Build the scientific comparison key for one analyzed series."""
    test_type = str(row.get("test_type") or "unknown_start_stop").strip()
    test_label = str(row.get("test_type_label_zh") or "").strip()
    if not test_label:
        test_label = {
            "standard_start_stop": "标准启停",
            "variable_start_stop": "变工步启停",
            "adt_start_stop": "ADT 启停",
        }.get(test_type, "启停工步")
    cathodic_current = _work_step_current_ma_cm2(
        row.get("cathodic_current_median_a_cm2")
    )
    recovery_current = _work_step_current_ma_cm2(
        row.get("recovery_current_median_a_cm2")
    )
    cathodic_duration = _work_step_duration_s(
        row.get("median_cathodic_phase_duration_s")
    )
    recovery_duration = _work_step_duration_s(
        row.get("median_reverse_phase_duration_s")
    )
    key = "|".join(
        (
            test_type,
            f"jc={_work_step_number_token(cathodic_current)}",
            f"jr={_work_step_number_token(recovery_current)}",
            f"tc={_work_step_number_token(cathodic_duration)}",
            f"tr={_work_step_number_token(recovery_duration)}",
        )
    )
    current_label = (
        f"{_format_signed_work_step_current(cathodic_current)} ↔ "
        f"{_format_signed_work_step_current(recovery_current)} mA·cm⁻²"
    )
    if cathodic_duration is None or recovery_duration is None:
        duration_label = "阶段时长未识别"
    elif (
        cathodic_duration > 0
        and recovery_duration > 0
        and cathodic_duration % 60 == 0
        and recovery_duration % 60 == 0
    ):
        duration_label = (
            f"{cathodic_duration // 60} + {recovery_duration // 60} min"
        )
    else:
        duration_label = f"{cathodic_duration} + {recovery_duration} s"
    return {
        "work_step_key": key,
        "work_step_label": f"{test_label} · {current_label} · {duration_label}",
        "work_step_test_type": test_type,
        "work_step_test_type_label_zh": test_label,
        "work_step_cathodic_current_ma_cm2": cathodic_current,
        "work_step_recovery_current_ma_cm2": recovery_current,
        "work_step_cathodic_duration_s": cathodic_duration,
        "work_step_recovery_duration_s": recovery_duration,
    }


class StartStopWorkspace:
    """Read derived start-stop artifacts and run the fixed local analysis workflow."""

    SCRIPT_NAME = "analyze_and_plot_start_stop.py"
    SNAPSHOT_NAME = "material_config_snapshot.json"
    READBACK_NAME = ".material_config_readback.json"
    SUMMARY_NAME = "analysis_summary.json"
    SERIES_NAME = "series_summary_raw.csv"
    SEGMENT_NAME = "segment_summary_raw.csv"
    CYCLE_NAME = "cycle_summary_raw.csv"
    OVERVIEW_NAME = "overview_downsampled_raw.csv"
    WATER_SERIES_NAME = (
        "water_compensation/series_summary_water_compensated.csv"
    )
    WATER_CYCLE_NAME = "water_compensation/cycle_summary_water_compensated.csv"
    WATER_OVERVIEW_NAME = (
        "water_compensation/overview_downsampled_water_compensated.csv"
    )
    STANDARD_PDF = "启停数据_原始电位_全量图集.pdf"
    WATER_PDF = "启停数据_水位补偿图集.pdf"
    SNAPSHOT_CACHE_DIR_NAME = ".start-stop-snapshot-cache"
    SNAPSHOT_MANIFEST_NAME = ".start-stop-manifest.json"
    CONFIG_FILE_NAME = "start-stop-material-config.json"
    REPOSITORY_STATUS_NAME = "repository_status.json"
    PROVENANCE_NAME = "analysis_provenance.json"
    ANALYSIS_WORKFLOW_VERSION = "start-stop-analysis/2"
    CONFIG_WORKBOOK_RELATIVE = (
        "outputs/019fa91d-602a-79a2-8637-6e9a3f699f66/"
        "启停绘图材料配置.xlsx"
    )
    SEED_INPUTS = {
        "scan": (CONFIG_WORKBOOK_RELATIVE,),
        "prepare_upload": (CONFIG_WORKBOOK_RELATIVE,),
        "render": (SNAPSHOT_NAME,),
    }
    MAX_SERIES_PER_CHART = 64
    RENDER_DATA_MODES = frozenset({"raw", "water", "both"})
    RENDER_MATERIAL_SCOPES = frozenset({"all", "updated"})
    MAX_POINTS_PER_SERIES = 6000
    MAX_CSV_BYTES = 96 * 1024 * 1024
    # Cache only final, filtered/downsampled API payloads as canonical JSON
    # bytes.  The byte ceiling is independent of (and below) the source CSV
    # ceiling, so the complete 60--75 MB table is never retained indefinitely.
    CHART_CACHE_MAX_ENTRIES = 12
    CHART_CACHE_MAX_BYTES = 48 * 1024 * 1024
    SERIES_CACHE_MAX_BYTES = 2 * 1024 * 1024
    MAX_UPLOAD_BYTES = 128 * 1024 * 1024
    MAX_UPLOAD_BATCH_FILES = 4096
    UPLOAD_EXTENSIONS = frozenset(
        {
            ".txt",
            ".bin",
            ".cor",
            ".z60",
            ".dta",
            ".mpr",
            ".mpt",
            ".csv",
            ".tsv",
            ".xls",
            ".xlsx",
            ".dat",
            ".zip",
            ".json",
            ".jsonl",
            ".log",
        }
    )

    METRICS = {
        "cathodic": {
            "label": "阴极段末端电位",
            "unit": "V vs Hg/HgO",
            "raw": "cathodic_last1s_median_raw_v",
            "water": "cathodic_last1s_median_water_compensated_v",
        },
        "negative_shift": {
            "label": "阴极电位负移",
            "unit": "mV",
            "raw": "cathodic_negative_shift_mv",
            "water": "cathodic_negative_shift_water_compensated_mv",
        },
        "reverse": {
            "label": "恢复段末端电位",
            "unit": "V vs Hg/HgO",
            "raw": "reverse_last1s_median_raw_v",
            "water": "reverse_last1s_median_water_compensated_v",
        },
        "minimum_time": {
            "label": "阴极段最低点位置",
            "unit": "s",
            "raw": "cathodic_phase_min_time_s",
            "water": "cathodic_phase_min_time_s",
        },
        "overview": {
            "label": "全程电位",
            "unit": "V vs Hg/HgO",
            "raw": "potential_raw_v",
            "water": "potential_water_compensated_v",
        },
    }

    def __init__(
        self,
        database: Any,
        analysis_dir: str | Path | None,
        *,
        collection_script: str | Path | None = None,
        analysis_script: str | Path | None = None,
        collection_config: str | Path | None = None,
        collection_config_provider: Any | None = None,
        scratch_dir: str | Path | None = None,
        backup_dir: str | Path | None = None,
        estimated_output_bytes: int = 0,
        repository_mode: bool = False,
        python_executable: str = "",
        node_executable: str = "",
        timeout_seconds: int = 3600,
    ):
        self.database = database
        raw_dir = str(analysis_dir or "").strip()
        self.analysis_dir = (
            Path(os.path.expandvars(os.path.expanduser(raw_dir))).resolve()
            if raw_dir
            else None
        )
        raw_collection = str(collection_script or "").strip()
        self.collection_script = (
            Path(
                os.path.expandvars(os.path.expanduser(raw_collection))
            ).resolve()
            if raw_collection
            else None
        )
        raw_analysis_script = str(analysis_script or "").strip()
        self.analysis_script = (
            Path(
                os.path.expandvars(os.path.expanduser(raw_analysis_script))
            ).resolve()
            if raw_analysis_script
            else None
        )
        raw_collection_config = str(collection_config or "").strip()
        self.collection_config = (
            Path(
                os.path.expandvars(os.path.expanduser(raw_collection_config))
            ).resolve()
            if raw_collection_config
            else None
        )
        self.collection_config_provider = collection_config_provider
        raw_scratch = str(scratch_dir or "").strip()
        default_scratch = self.database.path.parent / "start-stop-scratch"
        self.scratch_dir = Path(
            os.path.expandvars(os.path.expanduser(raw_scratch))
        ).resolve() if raw_scratch else default_scratch.resolve()
        raw_backup = str(backup_dir or "").strip()
        self.backup_dir = (
            Path(
                os.path.expandvars(os.path.expanduser(raw_backup))
            ).resolve()
            if raw_backup
            else None
        )
        self.estimated_output_bytes = int(estimated_output_bytes)
        if self.estimated_output_bytes < 0:
            raise ValueError("estimated_output_bytes must be non-negative")
        self.repository_mode = bool(repository_mode)
        raw_python = str(python_executable or "").strip()
        self.python_executable = raw_python or sys.executable
        self.node_executable = str(node_executable or "").strip()
        self.timeout_seconds = max(60, min(int(timeout_seconds), 4 * 3600))
        self.config_path = self.database.path.parent / self.CONFIG_FILE_NAME
        self._job_lock = threading.RLock()
        # A single lock also provides single-flight behaviour for cache misses:
        # concurrent identical requests perform one CSV scan, while readers of
        # cached payloads receive independent copies.
        self._chart_cache_lock = threading.RLock()
        self._chart_cache: OrderedDict[
            tuple[Any, ...], tuple[int, bytes]
        ] = OrderedDict()
        self._chart_cache_bytes = 0
        self._series_cache_key: tuple[Any, ...] | None = None
        self._series_cache_payload: dict[str, Any] | None = None
        self._series_cache_bytes = 0
        self._worker_instance_id = uuid.uuid4().hex
        self._verified_snapshot_cache_fingerprint = ""
        self._last_progress_persisted_at = 0.0
        self._last_progress_persisted_phase = ""
        self._active_job_id = ""
        self._job: dict[str, Any] = {
            "id": "",
            "action": "",
            "status": "idle",
            "stage": "idle",
            "started_utc": "",
            "completed_utc": "",
            "message": "",
            "result": {},
            "progress": {
                "phase": "idle",
                "phase_label": "等待任务",
                "phase_index": 0,
                "phase_count": 7,
                "mode": "determinate",
                "percent": 0,
                "completed": 0,
                "total": 7,
                "unit": "steps",
                "current_item": "",
                "detail": "",
                "machines": [],
            },
        }
        # Upload identifiers are deliberately scoped to this process.  They
        # prove that prepare_upload refers to files accepted by this local
        # service instance rather than arbitrary repository version IDs.
        self._recent_upload_ids: set[int] = set()
        latest_job = getattr(self.database, "latest_job", None)
        if callable(latest_job):
            persisted = latest_job()
            if isinstance(persisted, dict):
                self._job = persisted

    @property
    def configured(self) -> bool:
        return self.analysis_dir is not None

    def _analysis_script_path(self) -> Path | None:
        if self.analysis_script is not None:
            return self.analysis_script
        if self.analysis_dir is None:
            return None
        return self._path(self.SCRIPT_NAME)

    def _collection_config_path(self) -> Path | None:
        if self.collection_config is not None:
            return self.collection_config
        if self.collection_script is not None:
            return self.collection_script.with_name("采集配置.json")
        return None

    def _collection_config_payload(self) -> dict[str, Any]:
        provider = self.collection_config_provider
        if provider is not None:
            loader = getattr(provider, "effective_config", None)
            if not callable(loader):
                raise OSError("实验电脑搜索位置配置不可用")
            payload = loader()
        else:
            config_path = self._collection_config_path()
            if config_path is None:
                raise OSError("尚未配置实验电脑数据采集配置")
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("实验电脑采集配置无效")
        return payload

    def _write_effective_collection_config(self, destination: Path) -> Path:
        payload = self._collection_config_payload()
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
        return destination

    def _configured_progress_machines(self) -> list[dict[str, Any]]:
        try:
            payload = self._collection_config_payload()
        except (OSError, ValueError, json.JSONDecodeError):
            return []
        rows = payload.get("machines") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return []
        result: list[dict[str, Any]] = []
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            hostname = self._progress_text(raw.get("hostname"), limit=128)
            if not hostname:
                continue
            result.append(
                {
                    "machine_id": hostname,
                    "name": self._progress_text(
                        raw.get("name") or hostname,
                        limit=100,
                    ),
                    "status": "pending",
                    "completed": 0,
                    "total": 0,
                    "message": "等待连接",
                }
            )
        return result

    def _repository_api_ready(self) -> bool:
        return all(
            callable(getattr(self.database, name, None))
            for name in (
                "freeze_snapshot",
                "latest_snapshot",
                "materialize_snapshot",
                "publish_artifacts",
                "restore_current_artifacts",
                "repository_status",
            )
        )

    @staticmethod
    def _public_repository_status(payload: Any) -> dict[str, Any]:
        """Return repository counters without exposing host or database paths."""
        if not isinstance(payload, dict):
            return {}
        scalar_keys = {
            "available",
            "artifact_generation_count",
            "blob_count",
            "blob_bytes",
            "candidate_count",
            "candidate_file_count",
            "current_file_count",
            "current_source_count",
            "database_size_bytes",
            "file_count",
            "generation_count",
            "journal_mode",
            "latest_artifact_created_utc",
            "latest_artifact_generation_id",
            "latest_collection_utc",
            "latest_snapshot_created_utc",
            "latest_snapshot_fingerprint",
            "latest_snapshot_id",
            "oldest_source_modified_utc",
            "newest_source_modified_utc",
            "snapshot_count",
            "source_count",
            "source_first_modified_utc",
            "source_last_modified_utc",
            "total_bytes",
            "synchronous",
            "version_count",
        }
        result = {
            key: value
            for key, value in payload.items()
            if key in scalar_keys
            and isinstance(value, (str, int, float, bool))
            and not isinstance(value, (list, dict))
        }
        blobs = payload.get("blobs")
        if isinstance(blobs, dict):
            result.setdefault("blob_count", _integer(blobs.get("count")))
            result.setdefault("blob_bytes", _integer(blobs.get("total_bytes")))
        sources = payload.get("sources")
        if isinstance(sources, dict):
            result.setdefault("source_count", _integer(sources.get("count")))
            result.setdefault(
                "version_count", _integer(sources.get("version_count"))
            )
            result.setdefault(
                "current_source_count", _integer(sources.get("current_count"))
            )
            result.setdefault(
                "candidate_count", _integer(sources.get("candidate_count"))
            )
            result.setdefault(
                "candidate_file_count", result["candidate_count"]
            )
        snapshots = payload.get("snapshots")
        if isinstance(snapshots, dict):
            result.setdefault("snapshot_count", _integer(snapshots.get("count")))
            latest = snapshots.get("latest")
            if isinstance(latest, dict):
                for source, target in (
                    ("id", "latest_snapshot_id"),
                    ("snapshot_id", "latest_snapshot_id"),
                    ("dataset_fingerprint", "latest_snapshot_fingerprint"),
                    ("created_utc", "latest_snapshot_created_utc"),
                ):
                    value = latest.get(source)
                    if isinstance(value, (str, int)):
                        result.setdefault(target, value)
        artifacts = payload.get("artifacts")
        if isinstance(artifacts, dict):
            result.setdefault(
                "artifact_generation_count",
                _integer(artifacts.get("generation_count")),
            )
        collections = payload.get("collections")
        if isinstance(collections, dict):
            latest = collections.get("latest")
            if isinstance(latest, dict):
                value = latest.get(
                    "finished_utc",
                    latest.get(
                        "finished_at_utc", latest.get("completed_utc")
                    ),
                )
                if isinstance(value, str):
                    result.setdefault("latest_collection_utc", value)
        if "file_count" not in result and "current_source_count" in result:
            result["file_count"] = result["current_source_count"]
        if "total_bytes" not in result and "blob_bytes" in result:
            result["total_bytes"] = result["blob_bytes"]
        for key in ("data_range", "source_modified_range"):
            nested = payload.get(key)
            if not isinstance(nested, dict):
                continue
            public_nested = {
                name: value
                for name, value in nested.items()
                if name
                in {
                    "first_utc",
                    "last_utc",
                    "oldest_utc",
                    "newest_utc",
                    "start_utc",
                    "end_utc",
                }
                and isinstance(value, str)
            }
            if public_nested:
                result[key] = public_nested
        return result

    @staticmethod
    def _repository_status_has_data(payload: dict[str, Any]) -> bool:
        count_keys = (
            "file_count",
            "current_file_count",
            "version_count",
            "blob_count",
            "source_count",
            "current_source_count",
            "candidate_count",
            "candidate_file_count",
            "snapshot_count",
            "total_bytes",
            "blob_bytes",
        )
        return any(_integer(payload.get(key)) > 0 for key in count_keys) or bool(
            payload.get("latest_snapshot_id")
        )

    def _repository_status(self) -> dict[str, Any]:
        if not self.repository_mode:
            return {"enabled": False, "storage": "filesystem"}
        direct: dict[str, Any] = {}
        if callable(getattr(self.database, "repository_status", None)):
            try:
                direct = self._public_repository_status(
                    self.database.repository_status()
                )
            except (OSError, RuntimeError, ValueError):
                direct = {}
        cached = self._read_json(self.REPOSITORY_STATUS_NAME, optional=True)
        cached = self._public_repository_status(cached)
        selected = direct
        if cached and not self._repository_status_has_data(direct):
            selected = cached
        return {
            "enabled": self.repository_mode,
            "storage": "database" if self.repository_mode else "filesystem",
            **selected,
        }

    def ensure_published_cache(self) -> bool:
        """Restore the latest committed analysis artifacts into the read cache."""
        if not self.repository_mode or self.analysis_dir is None:
            return bool(self.analysis_dir and self.analysis_dir.is_dir())
        if not self._repository_api_ready():
            return bool(
                self.analysis_dir.is_dir() and any(self.analysis_dir.iterdir())
            )
        try:
            repository = self._public_repository_status(
                self.database.repository_status()
            )
        except (OSError, RuntimeError, ValueError):
            return bool(
                self.analysis_dir.is_dir() and any(self.analysis_dir.iterdir())
            )
        generation_count = max(
            _integer(repository.get("artifact_generation_count")),
            _integer(repository.get("generation_count")),
        )
        if generation_count <= 0:
            return bool(
                self.analysis_dir.is_dir() and any(self.analysis_dir.iterdir())
            )
        try:
            self._restore_published_cache_atomic()
        except (OSError, RuntimeError, ValueError):
            return False
        return self.analysis_dir.is_dir() and any(self.analysis_dir.iterdir())

    def _path(self, relative: str, *, require: bool = False) -> Path:
        if self.analysis_dir is None:
            raise StartStopWorkspaceError(
                "尚未配置启停分析目录。",
                503,
            )
        base = self.analysis_dir.resolve(strict=False)
        candidate = (base / relative).resolve(strict=False)
        if candidate != base and base not in candidate.parents:
            raise StartStopWorkspaceError("启停分析文件越出配置目录。", 403)
        if require and (not candidate.exists() or not candidate.is_file()):
            raise StartStopWorkspaceError(f"启停分析产物不存在：{relative}", 404)
        return candidate

    def _read_json(self, relative: str, *, optional: bool = False) -> dict[str, Any]:
        try:
            path = self._path(relative, require=not optional)
        except StartStopWorkspaceError:
            if optional:
                return {}
            raise
        if not path.exists():
            return {}
        try:
            if path.stat().st_size > 32 * 1024 * 1024:
                raise StartStopWorkspaceError("启停分析 JSON 超过安全读取上限。", 413)
            value = json.loads(path.read_text(encoding="utf-8"))
        except StartStopWorkspaceError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StartStopWorkspaceError(f"无法读取启停分析产物：{relative}", 500) from exc
        if not isinstance(value, dict):
            raise StartStopWorkspaceError(f"启停分析产物格式无效：{relative}", 500)
        return value

    def _snapshot(self) -> dict[str, Any]:
        return self._read_json(self.SNAPSHOT_NAME)

    def _workbook_config(self, dataset_fingerprint: str) -> dict[str, dict[str, Any]]:
        payload = self._read_json(self.READBACK_NAME, optional=True)
        if payload.get("dataset_fingerprint") != dataset_fingerprint:
            return {}
        rows = payload.get("materials")
        if not isinstance(rows, list):
            return {}
        return {
            str(row.get("key")): row
            for row in rows
            if isinstance(row, dict) and str(row.get("key") or "")
        }

    def _merged_materials(self) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        snapshot = self._snapshot()
        fingerprint = str(snapshot.get("dataset_fingerprint") or "")
        snapshot_rows = snapshot.get("materials")
        if not fingerprint or not isinstance(snapshot_rows, list):
            raise StartStopWorkspaceError("启停材料快照不完整，请先更新数据。", 409)
        source_modified_by_path: dict[str, tuple[float, str]] = {}
        snapshot_files = snapshot.get("files")
        if isinstance(snapshot_files, list):
            for file_row in snapshot_files:
                if not isinstance(file_row, dict):
                    continue
                relative_path = str(file_row.get("relative_path") or "").replace(
                    "\\", "/"
                )
                modified_at = str(
                    file_row.get("file_mtime")
                    or file_row.get("source_modified_utc")
                    or ""
                ).strip()
                if not relative_path or not modified_at:
                    continue
                try:
                    parsed_modified = dt.datetime.fromisoformat(
                        modified_at.replace("Z", "+00:00")
                    )
                    if parsed_modified.tzinfo is None:
                        parsed_modified = parsed_modified.replace(
                            tzinfo=dt.datetime.now().astimezone().tzinfo
                        )
                    modified_sort_value = parsed_modified.timestamp()
                except (ValueError, OverflowError, OSError):
                    continue
                previous = source_modified_by_path.get(relative_path)
                if previous is None or modified_sort_value > previous[0]:
                    source_modified_by_path[relative_path] = (
                        modified_sort_value,
                        modified_at,
                    )
        database_config = self.database.get_start_stop_config()
        database_rows = {
            row["material_key"]: row for row in database_config.get("materials", [])
        }
        workbook_rows = self._workbook_config(fingerprint)
        merged: list[dict[str, Any]] = []
        for raw in snapshot_rows:
            if not isinstance(raw, dict):
                continue
            key = str(raw.get("key") or "")
            if not key:
                continue
            saved = database_rows.get(key)
            workbook = workbook_rows.get(key, {})
            if saved:
                plot_name = str(saved.get("plot_name") or raw.get("auto_name") or "").strip()
                include = bool(saved.get("include_in_summary_atlas"))
                favorite = bool(saved.get("favorite"))
                notes = str(saved.get("notes") or "")
                source = "web"
                saved_fingerprint = str(saved.get("source_fingerprint") or "")
            else:
                plot_name = str(
                    workbook.get("plot_name") or raw.get("auto_name") or ""
                ).strip()
                include = _bool(
                    workbook.get("include_in_summary_atlas", True)
                )
                favorite = _bool(workbook.get("favorite", False))
                notes = str(workbook.get("notes") or "")
                source = "workbook" if workbook else "auto"
                saved_fingerprint = str(workbook.get("fingerprint") or "")
            current_fingerprint = str(raw.get("fingerprint") or "")
            status = (
                "新增"
                if not saved_fingerprint
                else "未变化"
                if saved_fingerprint == current_fingerprint
                else "数据已更新"
            )
            ordered_files = raw.get("ordered_source_files")
            if not isinstance(ordered_files, list):
                ordered_files = []
            material_modified = [
                source_modified_by_path[path]
                for item in ordered_files
                if (
                    path := str(item or "").replace("\\", "/")
                ) in source_modified_by_path
            ]
            latest_source_modified_at = (
                max(material_modified, default=(0.0, ""))[1]
                or str(raw.get("latest_source_modified_at") or "")
            )
            merged.append(
                {
                    "key": key,
                    "auto_name": str(raw.get("auto_name") or ""),
                    "plot_name": plot_name,
                    "include_in_summary_atlas": include,
                    "favorite": favorite,
                    "notes": notes,
                    "status": status,
                    "config_source": source,
                    "fingerprint": current_fingerprint,
                    "standard_file_count": _integer(raw.get("standard_file_count")),
                    "total_data_points": _integer(raw.get("total_data_points")),
                    "test_types": str(raw.get("test_types") or ""),
                    "cathodic_current_median_a_cm2": _number(
                        raw.get("cathodic_current_median_a_cm2")
                    ),
                    "recovery_current_median_a_cm2": _number(
                        raw.get("recovery_current_median_a_cm2")
                    ),
                    "latest_source_modified_at": latest_source_modified_at,
                    "ordered_source_files": [str(item) for item in ordered_files],
                }
            )
        revision = _integer(database_config.get("revision"))
        if self.repository_mode and revision <= 0:
            artifact_manifest = self._read_json(
                ".start-stop-artifacts.json", optional=True
            )
            revision = _integer(artifact_manifest.get("config_revision"))
        state = {
            "dataset_fingerprint": fingerprint,
            "generated_at": str(snapshot.get("generated_at") or ""),
            "revision": revision,
            "saved_dataset_fingerprint": str(
                database_config.get("dataset_fingerprint") or ""
            ),
            "updated_utc": str(database_config.get("updated_utc") or ""),
        }
        return state, merged

    def materials(self) -> dict[str, Any]:
        state, rows = self._merged_materials()
        return {
            **state,
            "materials": rows,
            "counts": {
                "materials": len(rows),
                "selected": sum(
                    1 for row in rows if row["include_in_summary_atlas"]
                ),
                "favorites": sum(1 for row in rows if row["favorite"]),
                "changed": sum(
                    1 for row in rows if row["status"] != "未变化"
                ),
            },
        }

    def save_materials(
        self,
        *,
        dataset_fingerprint: str,
        expected_revision: int,
        materials: Any,
    ) -> dict[str, Any]:
        state, current_rows = self._merged_materials()
        if str(dataset_fingerprint or "") != state["dataset_fingerprint"]:
            raise StartStopWorkspaceError(
                "源数据在页面打开后已更新，请先重新读取材料表。",
                409,
            )
        if not isinstance(materials, list):
            raise StartStopWorkspaceError("materials 必须是数组。")
        current_by_key = {row["key"]: row for row in current_rows}
        requested_by_key: dict[str, dict[str, Any]] = {}
        seen_names: dict[str, str] = {}
        normalized: list[dict[str, Any]] = []
        for raw in materials:
            if not isinstance(raw, dict):
                raise StartStopWorkspaceError("每条材料配置必须是 JSON 对象。")
            unknown = sorted(
                set(raw)
                - {
                    "key",
                    "plot_name",
                    "include_in_summary_atlas",
                    "favorite",
                    "notes",
                }
            )
            if unknown:
                raise StartStopWorkspaceError(
                    "材料配置包含未知字段：" + ", ".join(unknown)
                )
            key = str(raw.get("key") or "")
            if key not in current_by_key or key in requested_by_key:
                raise StartStopWorkspaceError("材料键与当前数据快照不一致。", 409)
            plot_name = str(raw.get("plot_name") or "").strip()
            if not plot_name or len(plot_name) > 180:
                raise StartStopWorkspaceError("绘图名称不能为空且不能超过 180 个字符。")
            if plot_name in seen_names:
                raise StartStopWorkspaceError(
                    f"绘图名称重复：{plot_name}"
                )
            seen_names[plot_name] = key
            include = raw.get("include_in_summary_atlas")
            if not isinstance(include, bool):
                raise StartStopWorkspaceError("进入总结图集必须是布尔值。")
            favorite = raw.get("favorite", current_by_key[key].get("favorite", False))
            if not isinstance(favorite, bool):
                raise StartStopWorkspaceError("收藏状态必须是布尔值。")
            notes = str(raw.get("notes") or "").strip()
            if len(notes) > 1000:
                raise StartStopWorkspaceError("材料备注不能超过 1000 个字符。")
            row = {
                "material_key": key,
                "plot_name": plot_name,
                "include_in_summary_atlas": include,
                "favorite": favorite,
                "notes": notes,
                "source_fingerprint": current_by_key[key]["fingerprint"],
            }
            requested_by_key[key] = row
            normalized.append(row)
        if set(requested_by_key) != set(current_by_key):
            raise StartStopWorkspaceError("材料集合不完整，请刷新页面后重试。", 409)
        if not any(row["include_in_summary_atlas"] for row in normalized):
            raise StartStopWorkspaceError("至少需要选择 1 种材料进入总结图集。")
        try:
            self.database.save_start_stop_config(
                dataset_fingerprint=state["dataset_fingerprint"],
                expected_revision=int(expected_revision),
                materials=normalized,
            )
        except ValueError as exc:
            raise StartStopWorkspaceError(str(exc), 409) from exc
        self._audit(
            "start_stop_config_saved",
            state["dataset_fingerprint"][:12],
            f"{len(normalized)} materials / "
            f"{sum(1 for row in normalized if row['include_in_summary_atlas'])} selected / "
            f"{sum(1 for row in normalized if row['favorite'])} favorites",
        )
        return self.materials()

    def _summary_config_map(self, summary: dict[str, Any]) -> dict[str, tuple[str, bool, str]]:
        config = summary.get("material_config")
        if not isinstance(config, dict):
            return {}
        result: dict[str, tuple[str, bool, str]] = {}
        for selected, field in (
            (True, "selected_materials"),
            (False, "excluded_from_atlas"),
        ):
            rows = config.get(field)
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                key = str(row.get("material_relative_path") or "")
                if not key:
                    continue
                result[key] = (
                    str(row.get("material_display_name") or ""),
                    selected,
                    str(row.get("material_user_notes") or ""),
                )
        return result

    def _public_job(self) -> dict[str, Any]:
        with self._job_lock:
            return json.loads(json.dumps(self._job, ensure_ascii=False))

    def _durable_jobs_available(self) -> bool:
        return all(
            callable(getattr(self.database, name, None))
            for name in (
                "create_job",
                "claim_job",
                "update_job",
                "update_job_progress",
                "latest_job",
                "recover_interrupted_jobs",
            )
        )

    def recover_after_restart(self) -> dict[str, Any]:
        """Reconcile only jobs owned by a previous local service process."""
        recover = getattr(self.database, "recover_interrupted_jobs", None)
        latest = getattr(self.database, "latest_job", None)
        if not callable(recover) or not callable(latest):
            return {"recovered": 0, "jobs": []}
        recovered = recover(worker_instance_id=self._worker_instance_id)
        for job in recovered if isinstance(recovered, list) else []:
            if not isinstance(job, dict) or job.get("status") != "interrupted":
                continue
            batch_id = str(job.get("collection_batch_id") or "")
            job_id = str(job.get("id") or "")
            if batch_id and job_id:
                self._recover_abandoned_collection_batch(batch_id, job_id, None)
        persisted = latest()
        with self._job_lock:
            self._active_job_id = ""
            if isinstance(persisted, dict):
                self._job = persisted
        return {
            "recovered": len(recovered) if isinstance(recovered, list) else 0,
            "jobs": recovered if isinstance(recovered, list) else [],
        }

    def _analysis_provenance_status(self) -> dict[str, Any]:
        getter = getattr(self.database, "current_analysis_run", None)
        if not callable(getter):
            return {"state": "none"}
        try:
            payload = getter("render")
        except (OSError, RuntimeError, ValueError):
            return {"state": "unavailable"}
        return payload if isinstance(payload, dict) else {"state": "none"}

    @staticmethod
    def _safety_provenance_status(payload: dict[str, Any]) -> dict[str, Any]:
        """Map analysis provenance into the path-free safety card contract."""
        state = str(payload.get("state") or "none")
        state = {
            "legacy_unverified": "legacy",
            "sealed": "sealed",
            "unavailable": "unavailable",
            "none": "none",
        }.get(state, "unavailable")
        result: dict[str, Any] = {"state": state}
        for key in (
            "analysis_run_id",
            "snapshot_id",
            "config_revision",
            "artifact_generation_id",
        ):
            value = payload.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                result[key] = value
        for key in (
            "dataset_fingerprint",
            "artifact_manifest_sha256",
            "analysis_script_sha256",
            "material_config_sha256",
            "analysis_summary_sha256",
        ):
            value = str(payload.get(key) or "").lower()
            if len(value) == 64 and all(
                character in "0123456789abcdef" for character in value
            ):
                result[key] = value
        created_utc = payload.get("created_utc")
        if isinstance(created_utc, str) and len(created_utc) <= 40:
            result["created_utc"] = created_utc
        runtime = payload.get("runtime")
        if isinstance(runtime, dict):
            image_reference = runtime.get("image_or_service_version")
            if isinstance(image_reference, str):
                image_reference = image_reference.strip()
                looks_like_file_path = (
                    image_reference.startswith(("/", "~", "\\"))
                    or (
                        len(image_reference) >= 3
                        and image_reference[1:3] in {":\\", ":/"}
                    )
                    or "/../" in image_reference
                    or "\\..\\" in image_reference
                )
                if (
                    image_reference
                    and len(image_reference) <= 240
                    and not looks_like_file_path
                ):
                    result["image_reference"] = image_reference
        return result

    @staticmethod
    def _path_free_storage_status(payload: Any) -> dict[str, Any]:
        source = payload if isinstance(payload, dict) else {}
        result: dict[str, Any] = {}
        for key in ("ok", "preflight_ok", "low_space"):
            if isinstance(source.get(key), bool):
                result[key] = source[key]
        for key in (
            "total_bytes",
            "free_bytes",
            "used_bytes",
            "required_bytes",
            "shortfall_bytes",
            "volume_count",
        ):
            value = source.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                result[key] = value
        used_percent = _number(source.get("used_percent"))
        if used_percent is not None:
            result["used_percent"] = max(0.0, min(used_percent, 100.0))
        low_space_ratio = _number(source.get("low_space_ratio"))
        if low_space_ratio is not None:
            result["low_space_ratio"] = max(0.0, min(low_space_ratio, 1.0))
        blocked_roles = source.get("blocked_roles")
        if isinstance(blocked_roles, list):
            allowed_roles = {"database", "scratch", "cache"}
            result["blocked_roles"] = sorted(
                {
                    str(role)
                    for role in blocked_roles
                    if str(role) in allowed_roles
                }
            )
        return result

    @staticmethod
    def _path_free_backup_status(payload: Any) -> dict[str, Any]:
        source = payload if isinstance(payload, dict) else {}
        result: dict[str, Any] = {}
        for key in (
            "configured",
            "available",
            "latest_valid",
            "latest_created_with_full_verification",
            "destination_preflight_ok",
            "same_filesystem",
            "off_disk",
            "routine_full_backup_required",
            "schema_change_full_backup_required",
            "database_repair_full_backup_required",
            "scheduled_background_enabled",
        ):
            if isinstance(source.get(key), bool):
                result[key] = source[key]
        for key in (
            "backup_count",
            "invalid_count",
            "orphan_database_count",
            "latest_size_bytes",
            "destination_required_bytes",
            "destination_shortfall_bytes",
        ):
            value = source.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                result[key] = value
        latest_created_utc = source.get("latest_created_utc")
        if isinstance(latest_created_utc, str) and len(latest_created_utc) <= 40:
            result["latest_created_utc"] = latest_created_utc
        verification_level = source.get("latest_verification_level")
        if verification_level in {"manifest_and_size", "full"}:
            result["latest_verification_level"] = verification_level
        policy_mode = source.get("policy_mode")
        if policy_mode == "risk_tiered":
            result["policy_mode"] = policy_mode
        scheduled_state = source.get("scheduled_state")
        if scheduled_state in {
            "never_run", "running", "completed", "failed", "skipped"
        }:
            result["scheduled_state"] = scheduled_state
        schedule_label = source.get("scheduled_background_label")
        if isinstance(schedule_label, str) and len(schedule_label) <= 80:
            result["scheduled_background_label"] = schedule_label
        for key in (
            "scheduled_started_utc",
            "scheduled_completed_utc",
            "scheduled_due_utc",
            "scheduled_next_due_utc",
        ):
            value = source.get(key)
            if isinstance(value, str) and len(value) <= 40:
                result[key] = value
        scheduled_exit_code = source.get("scheduled_exit_code")
        if (
            isinstance(scheduled_exit_code, int)
            and not isinstance(scheduled_exit_code, bool)
            and 0 <= scheduled_exit_code <= 255
        ):
            result["scheduled_exit_code"] = scheduled_exit_code
        return result

    @staticmethod
    def _backup_policy_status() -> dict[str, Any]:
        label = " ".join(
            str(
                os.environ.get(
                    "START_STOP_BACKUP_SCHEDULE_LABEL",
                    "每 4 周周日 03:00 后台执行",
                )
            ).split()
        )[:80]
        return {
            "policy_mode": "risk_tiered",
            "routine_full_backup_required": False,
            "schema_change_full_backup_required": True,
            "database_repair_full_backup_required": True,
            "scheduled_background_enabled": _bool(
                os.environ.get("START_STOP_BACKUP_SCHEDULE_ENABLED", "1")
            ),
            "scheduled_background_label": label,
        }

    def _safety_status(
        self,
        provenance: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return status-only storage, backup, and provenance information.

        Missing or unreadable backup storage is deliberately represented as an
        unavailable status.  Polling this method never creates a backup
        directory or any backup files.
        """
        mapped_provenance = self._safety_provenance_status(
            provenance
            if isinstance(provenance, dict)
            else self._analysis_provenance_status()
        )
        if self.backup_dir is None:
            return {
                "storage": {},
                "backup": {
                    "configured": False,
                    "available": False,
                    "backup_count": 0,
                    "invalid_count": 0,
                    **self._backup_policy_status(),
                },
                "provenance": mapped_provenance,
            }
        backup_policy = self._backup_policy_status()
        try:
            latest_snapshot = getattr(self.database, "latest_snapshot", None)
            snapshot_payload = latest_snapshot() if callable(latest_snapshot) else None
            snapshot_payload = (
                snapshot_payload if isinstance(snapshot_payload, dict) else {}
            )
            snapshot_bytes = _integer(snapshot_payload.get("total_bytes"))
            descriptor = self._snapshot_cache_descriptor(snapshot_payload)
            if descriptor is not None:
                fingerprint, _files = descriptor
                cached_root = (
                    self.scratch_dir
                    / self.SNAPSHOT_CACHE_DIR_NAME
                    / fingerprint
                )
                if (
                    self._verified_snapshot_cache_fingerprint == fingerprint
                    and cached_root.is_dir()
                    and (cached_root / self.SNAPSHOT_MANIFEST_NAME).is_file()
                ):
                    snapshot_bytes = 0
            safety = build_safety_status(
                self.database.path,
                self.backup_dir,
                estimated_output_bytes=self.estimated_output_bytes,
                scratch_dir=self.scratch_dir,
                cache_dir=(
                    self.analysis_dir.parent
                    if self.analysis_dir is not None
                    else self.database.path.parent
                ),
                estimated_snapshot_bytes=snapshot_bytes,
                low_space_ratio=0.15,
                backup_policy_mode=str(backup_policy["policy_mode"]),
                scheduled_background_enabled=bool(
                    backup_policy["scheduled_background_enabled"]
                ),
                scheduled_background_label=str(
                    backup_policy["scheduled_background_label"]
                ),
                scheduled_status_file=(
                    os.environ.get("START_STOP_BACKUP_STATUS_FILE") or None
                ),
            )
        except (OSError, RuntimeError, ValueError, sqlite3.Error):
            return {
                "storage": {},
                "backup": {
                    "configured": True,
                    "available": False,
                    "backup_count": 0,
                    "invalid_count": 0,
                    **self._backup_policy_status(),
                },
                "provenance": mapped_provenance,
            }
        storage = safety.get("storage")
        backup = safety.get("backup")
        return {
            "storage": self._path_free_storage_status(storage),
            "backup": (
                self._path_free_backup_status(backup)
                if isinstance(backup, dict)
                else {
                    "configured": True,
                    "available": False,
                }
            ),
            "provenance": mapped_provenance,
        }

    @staticmethod
    def _executable_available(value: str) -> bool:
        if not value:
            return False
        candidate = Path(value)
        if candidate.is_absolute() or candidate.parent != Path("."):
            return candidate.is_file() and os.access(candidate, os.X_OK)
        return shutil.which(value) is not None

    def _execution_status(self) -> dict[str, Any]:
        python_ready = self._executable_available(self.python_executable)
        analysis_script = self._analysis_script_path()
        analysis_script_ready = bool(
            analysis_script and analysis_script.is_file()
        )
        node_ready = (
            not self.node_executable
            or self._executable_available(self.node_executable)
        )
        analysis_runtime_ready = (
            python_ready and analysis_script_ready and node_ready
        )
        repository_ready = (
            not self.repository_mode or self._repository_api_ready()
        )
        repository_artifacts_ready = True
        if self.repository_mode and repository_ready:
            try:
                repository = self._public_repository_status(
                    self.database.repository_status()
                )
                repository_artifacts_ready = max(
                    _integer(repository.get("artifact_generation_count")),
                    _integer(repository.get("generation_count")),
                ) > 0
            except (OSError, RuntimeError, ValueError):
                repository_artifacts_ready = False
        render_ready = bool(
            analysis_runtime_ready
            and repository_ready
            and repository_artifacts_ready
        )
        upload_ready = bool(
            self.repository_mode
            and repository_ready
            and callable(getattr(self.database, "import_source_path", None))
        )
        prepare_upload_ready = bool(upload_ready and analysis_runtime_ready)
        collector_module = Path(__file__).with_name("start_stop_collection.py")
        collection_config_path = self._collection_config_path()
        collection_configured = bool(
            collection_config_path
            and (self.repository_mode or self.collection_script is not None)
        )
        collection_script_ready = (
            collector_module.is_file()
            if self.repository_mode
            else bool(self.collection_script and self.collection_script.is_file())
        )
        collection_config_file_ready = bool(
            collection_config_path and collection_config_path.is_file()
        )
        collection_config_ready = False
        collection_identity_ready = False
        collection_machine_count = 0
        if collection_config_file_ready and collection_config_path:
            try:
                collection_payload = self._collection_config_payload()
                machines = collection_payload.get("machines")
                if isinstance(machines, list) and machines:
                    collection_machine_count = len(machines)
                    collection_config_ready = all(
                        isinstance(machine, dict)
                        and isinstance(machine.get("roots"), list)
                        and bool(machine["roots"])
                        and bool(str(machine.get("identity_file") or "").strip())
                        for machine in machines
                    )
                    collection_identity_ready = collection_config_ready and all(
                        Path(
                            os.path.expandvars(
                                os.path.expanduser(
                                    str(machine["identity_file"])
                                )
                            )
                        ).is_file()
                        for machine in machines
                    )
            except (
                OSError,
                ValueError,
                json.JSONDecodeError,
                AttributeError,
                TypeError,
            ):
                collection_config_ready = False
                collection_identity_ready = False
        ssh_ready = shutil.which("ssh") is not None
        collection_ready = bool(
            collection_configured
            and collection_script_ready
            and collection_config_ready
            and collection_identity_ready
            and ssh_ready
            and repository_ready
        )
        update_ready = bool(
            analysis_runtime_ready and repository_ready and collection_ready
        )
        if not python_ready:
            message = "绘图运行环境尚未安装"
        elif not analysis_script_ready:
            message = "启停分析脚本当前不可用"
        elif not node_ready:
            message = "材料配置表运行环境当前不可用"
        elif not collection_configured:
            message = "尚未配置实验电脑数据采集脚本"
        elif not collection_script_ready:
            message = "实验电脑数据采集脚本当前不可用"
        elif not collection_config_file_ready or not collection_config_ready:
            message = "实验电脑采集配置当前不可用"
        elif not collection_identity_ready:
            message = "实验电脑采集密钥当前不可用"
        elif not ssh_ready:
            message = "实验电脑 SSH 采集环境当前不可用"
        elif not repository_ready:
            message = "启停数据数据库当前不可用"
        elif self.repository_mode and not repository_artifacts_ready:
            message = "可更新实验电脑数据；首次更新后即可重新绘图"
        else:
            message = "实验电脑采集、材料表更新与绘图环境已就绪"
        return {
            "ready": render_ready,
            "update_ready": update_ready,
            "upload_ready": upload_ready,
            "prepare_upload_ready": prepare_upload_ready,
            "render_ready": render_ready,
            "python_ready": python_ready,
            "analysis_script_ready": analysis_script_ready,
            "node_ready": node_ready,
            "collection_configured": collection_configured,
            "collection_script_ready": collection_script_ready,
            "collection_config_file_ready": collection_config_file_ready,
            "collection_config_ready": collection_config_ready,
            "collection_identity_ready": collection_identity_ready,
            "collection_machine_count": collection_machine_count,
            "ssh_ready": ssh_ready,
            "collection_ready": collection_ready,
            "repository_mode": self.repository_mode,
            "repository_ready": repository_ready,
            "repository_artifacts_ready": repository_artifacts_ready,
            "message": message,
        }

    def status(self) -> dict[str, Any]:
        analysis_provenance = self._analysis_provenance_status()
        safety = self._safety_status(analysis_provenance)
        if not self.configured:
            return {
                "configured": False,
                "available": False,
                "message": "尚未在本机配置启停分析目录。",
                "repository": self._repository_status(),
                "analysis_provenance": analysis_provenance,
                "safety": safety,
                "job": self._public_job(),
            }
        available = bool(
            self.analysis_dir
            and (self.analysis_dir.is_dir() or self.repository_mode)
        )
        if not available:
            return {
                "configured": True,
                "available": False,
                "message": "已配置的启停分析目录当前不可用。",
                "repository": self._repository_status(),
                "analysis_provenance": analysis_provenance,
                "safety": safety,
                "job": self._public_job(),
            }
        summary = self._read_json(self.SUMMARY_NAME, optional=True)
        snapshot = self._read_json(self.SNAPSHOT_NAME, optional=True)
        try:
            material_payload = self.materials()
        except StartStopWorkspaceError:
            material_payload = {"materials": [], "counts": {}, "revision": 0}
        current_map = {
            row["key"]: (
                row["plot_name"],
                row["include_in_summary_atlas"],
                row["notes"],
            )
            for row in material_payload.get("materials", [])
        }
        rendered_map = self._summary_config_map(summary)
        summary_config = summary.get("material_config")
        if not isinstance(summary_config, dict):
            summary_config = {}
        snapshot_fingerprint = str(snapshot.get("dataset_fingerprint") or "")
        rendered_fingerprint = str(summary_config.get("dataset_fingerprint") or "")
        data_stale = not summary or snapshot_fingerprint != rendered_fingerprint
        config_stale = bool(current_map) and current_map != rendered_map
        standard_pdf = self._path(self.STANDARD_PDF)
        water_pdf = self._path(self.WATER_PDF)
        counts = summary.get("counts") if isinstance(summary.get("counts"), dict) else {}
        water = summary.get("water_compensation")
        if not isinstance(water, dict):
            water = {}
        water_model = water.get("model")
        if not isinstance(water_model, dict):
            water_model = water
        snapshot_files = snapshot.get("files")
        current_included_files = (
            sum(
                1
                for row in snapshot_files
                if isinstance(row, dict) and _bool(row.get("included_in_analysis"))
            )
            if isinstance(snapshot_files, list) and snapshot_files
            else None
        )
        last_data_update_at = str(snapshot.get("generated_at") or "")
        if not last_data_update_at:
            snapshot_path = self._path(self.SNAPSHOT_NAME)
            if snapshot_path.is_file():
                try:
                    last_data_update_at = dt.datetime.fromtimestamp(
                        snapshot_path.stat().st_mtime,
                        tz=dt.timezone.utc,
                    ).isoformat(timespec="seconds")
                except OSError:
                    last_data_update_at = ""
        analysis_ready = not data_stale and not config_stale
        standard_pdf_ready = standard_pdf.is_file()
        water_pdf_ready = water_pdf.is_file()
        return {
            "configured": True,
            "available": True,
            "message": "启停分析工作区可用。",
            "last_data_update_at": last_data_update_at,
            "created_at": str(summary.get("created_at") or ""),
            "dataset_fingerprint": snapshot_fingerprint,
            "data_stale": data_stale,
            "configuration_stale": config_stale,
            "analysis_ready": analysis_ready,
            "export_ready": analysis_ready and (
                standard_pdf_ready or water_pdf_ready
            ),
            "counts": {
                "materials": len(current_map)
                if current_map
                else _integer(counts.get("materials")),
                "selected_materials": sum(
                    1 for value in current_map.values() if value[1]
                )
                if current_map
                else _integer(
                    counts.get(
                        "summary_atlas_materials",
                        counts.get("materials_in_atlas"),
                    )
                ),
                "included_files": current_included_files
                if current_included_files is not None
                else _integer(
                    counts.get(
                        "included_files",
                        _integer(counts.get("included_standard_files"))
                        + _integer(counts.get("included_variable_files"))
                        + _integer(counts.get("included_adt_files")),
                    )
                ),
                "complete_cycles": _integer(counts.get("complete_cycles")),
                "normal_cycles": _integer(counts.get("normal_cycles")),
                "abnormal_cycles": _integer(counts.get("abnormal_cycles")),
            },
            "rules": {
                "potential_reference": "Hg/HgO",
                "potential_basis": "raw_measured",
                "reference_conversion_applied": False,
                "endpoint": "方波启停取阶段最后 1 s 中位数；ADT 取末点",
                "anomaly": "阴极段最低点 <15 s 为异常，≥15 s 为正常",
                "water_reference": str(
                    water_model.get("reference_material")
                    or "NiMo-恒流-NH4-20ma-30min"
                ),
                "water_slope_mv_per_h": _number(
                    water_model.get("reference_fit_slope_mv_per_h"),
                    -6.3419003486,
                ),
            },
            "pdf": {
                "standard": standard_pdf_ready,
                "water": water_pdf_ready,
            },
            "execution": self._execution_status(),
            "repository": self._repository_status(),
            "analysis_provenance": analysis_provenance,
            "safety": safety,
            "config_revision": _integer(material_payload.get("revision")),
            "job": self._public_job(),
        }

    @staticmethod
    def _payload_size_bytes(payload: dict[str, Any]) -> int:
        """Estimate the retained payload size without retaining encoded JSON."""
        try:
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, OverflowError):
            return sys.maxsize
        # Python dictionaries and numeric objects require more memory than the
        # compact wire representation.  A conservative 2x estimate keeps the
        # resident cache materially below the source CSV size.
        return min(sys.maxsize, len(encoded) * 2)

    def _cache_generation_token(self) -> tuple[Any, ...]:
        """Return a path-free token that changes with the published generation."""
        if not self.repository_mode:
            return ("filesystem",)
        getter = getattr(self.database, "current_analysis_run", None)
        if callable(getter):
            try:
                payload = getter("render")
            except (OSError, RuntimeError, ValueError, sqlite3.Error):
                payload = None
            if isinstance(payload, dict):
                return (
                    "repository",
                    str(payload.get("state") or ""),
                    _integer(payload.get("artifact_generation_id"), -1),
                    str(payload.get("artifact_manifest_sha256") or ""),
                )
        status_getter = getattr(self.database, "repository_status", None)
        if callable(status_getter):
            try:
                payload = self._public_repository_status(status_getter())
            except (OSError, RuntimeError, ValueError, sqlite3.Error):
                payload = {}
            return (
                "repository",
                "status",
                _integer(payload.get("latest_artifact_generation_id"), -1),
                _integer(payload.get("artifact_generation_count"), -1),
            )
        return ("repository", "unavailable")

    @staticmethod
    def _file_cache_signature(path: Path) -> tuple[Any, ...]:
        try:
            stat = path.stat()
        except OSError as exc:
            raise StartStopWorkspaceError(
                "无法读取启停分析文件状态。",
                500,
            ) from exc
        return (
            str(path),
            int(stat.st_dev),
            int(stat.st_ino),
            int(stat.st_size),
            int(stat.st_mtime_ns),
            int(stat.st_ctime_ns),
        )

    def _chart_cache_get_locked(
        self,
        key: tuple[Any, ...],
    ) -> dict[str, Any] | None:
        cached = self._chart_cache.pop(key, None)
        if cached is None:
            return None
        self._chart_cache[key] = cached
        try:
            payload = json.loads(cached[1])
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._chart_cache.pop(key, None)
            self._chart_cache_bytes -= cached[0]
            return None
        if not isinstance(payload, dict):
            self._chart_cache.pop(key, None)
            self._chart_cache_bytes -= cached[0]
            return None
        return payload

    def _chart_cache_put_locked(
        self,
        key: tuple[Any, ...],
        payload: dict[str, Any],
    ) -> None:
        maximum_entries = max(0, int(self.CHART_CACHE_MAX_ENTRIES))
        maximum_bytes = max(0, int(self.CHART_CACHE_MAX_BYTES))
        try:
            encoded_payload = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, OverflowError):
            return
        estimated_bytes = len(encoded_payload)
        if (
            maximum_entries <= 0
            or maximum_bytes <= 0
            or estimated_bytes > maximum_bytes
        ):
            return
        previous = self._chart_cache.pop(key, None)
        if previous is not None:
            self._chart_cache_bytes -= previous[0]
        while self._chart_cache and (
            len(self._chart_cache) >= maximum_entries
            or self._chart_cache_bytes + estimated_bytes > maximum_bytes
        ):
            _, (removed_bytes, _) = self._chart_cache.popitem(last=False)
            self._chart_cache_bytes -= removed_bytes
        self._chart_cache[key] = (estimated_bytes, encoded_payload)
        self._chart_cache_bytes += estimated_bytes

    def _series_payload(
        self,
    ) -> tuple[dict[str, Any], tuple[Any, ...]]:
        path = self._path(self.SERIES_NAME, require=True)
        generation = self._cache_generation_token()
        signature = self._file_cache_signature(path)
        cache_key = ("series", generation, signature)
        with self._chart_cache_lock:
            if (
                cache_key == self._series_cache_key
                and self._series_cache_payload is not None
            ):
                return copy.deepcopy(self._series_cache_payload), cache_key

            rows = self._read_csv(self.SERIES_NAME)
            public = []
            for row in rows:
                work_step = _work_step_metadata(row)
                public.append(
                    {
                        "series_id": str(row.get("series_id") or ""),
                        "series_order": _integer(row.get("series_order")),
                        "series_display_name": str(
                            row.get("series_display_name") or ""
                        ),
                        "material_id": str(row.get("material_id") or ""),
                        "material_relative_path": str(
                            row.get("material_relative_path") or ""
                        ),
                        "test_type": str(row.get("test_type") or ""),
                        "test_type_label_zh": str(
                            row.get("test_type_label_zh") or ""
                        ),
                        "include_in_summary_atlas": _bool(
                            row.get("include_in_summary_atlas")
                        ),
                        "complete_cycles": _integer(row.get("complete_cycles")),
                        "duration_h": _number(row.get("duration_h"), 0.0),
                        "normal_cycles": _integer(row.get("normal_cycles")),
                        "abnormal_cycles": _integer(row.get("abnormal_cycles")),
                        "abnormal_fraction": _number(
                            row.get("abnormal_fraction"), 0.0
                        ),
                        "segment_count": _integer(row.get("segment_count")),
                        "source_files": self._json_list(row.get("source_files")),
                        "endpoint_statistic": str(
                            row.get("endpoint_statistic") or ""
                        ),
                        "cathodic_current_median_a_cm2": _number(
                            row.get("cathodic_current_median_a_cm2")
                        ),
                        "recovery_current_median_a_cm2": _number(
                            row.get("recovery_current_median_a_cm2")
                        ),
                        "median_cathodic_phase_duration_s": _number(
                            row.get("median_cathodic_phase_duration_s")
                        ),
                        "median_reverse_phase_duration_s": _number(
                            row.get("median_reverse_phase_duration_s")
                        ),
                        "median_cycle_duration_s": _number(
                            row.get("median_cycle_duration_s")
                        ),
                        "cathodic_negative_shift_first10_to_last10_mv": _number(
                            row.get(
                                "cathodic_negative_shift_first10_to_last10_mv"
                            )
                        ),
                        **work_step,
                    }
                )
            public.sort(key=lambda item: (item["series_order"], item["series_id"]))
            work_steps_by_key: OrderedDict[str, dict[str, Any]] = OrderedDict()
            work_step_materials: dict[str, set[str]] = {}
            for item in public:
                key = item["work_step_key"]
                if key not in work_steps_by_key:
                    work_steps_by_key[key] = {
                        field: item[field]
                        for field in (
                            "work_step_key",
                            "work_step_label",
                            "work_step_test_type",
                            "work_step_test_type_label_zh",
                            "work_step_cathodic_current_ma_cm2",
                            "work_step_recovery_current_ma_cm2",
                            "work_step_cathodic_duration_s",
                            "work_step_recovery_duration_s",
                        )
                    }
                    work_steps_by_key[key]["series_count"] = 0
                    work_step_materials[key] = set()
                work_steps_by_key[key]["series_count"] += 1
                material_key = str(item.get("material_relative_path") or "")
                if material_key:
                    work_step_materials[key].add(material_key)
            work_steps = []
            for key, work_step in work_steps_by_key.items():
                work_step["material_count"] = len(work_step_materials[key])
                work_steps.append(work_step)
            payload = {
                "series": public,
                "count": len(public),
                "work_steps": work_steps,
                "work_step_count": len(work_steps),
            }

            final_signature = self._file_cache_signature(path)
            final_generation = self._cache_generation_token()
            estimated_bytes = self._payload_size_bytes(payload)
            if (
                final_signature == signature
                and final_generation == generation
                and estimated_bytes <= max(0, int(self.SERIES_CACHE_MAX_BYTES))
            ):
                self._series_cache_key = cache_key
                self._series_cache_payload = copy.deepcopy(payload)
                self._series_cache_bytes = estimated_bytes
            return payload, cache_key

    def series(self) -> dict[str, Any]:
        payload, _ = self._series_payload()
        return payload

    def live_analysis_context(self) -> dict[str, Any]:
        """Return bounded formal metadata used to extend active files safely."""
        payload = self.series()
        raw_rows = self._read_csv(self.SERIES_NAME)
        raw_by_id = {
            str(row.get("series_id") or ""): row
            for row in raw_rows
            if str(row.get("series_id") or "")
        }
        water_by_id: dict[str, dict[str, str]] = {}
        water_path = self._path(self.WATER_SERIES_NAME, require=False)
        if water_path.is_file():
            water_by_id = {
                str(row.get("series_id") or ""): row
                for row in self._read_csv(self.WATER_SERIES_NAME)
                if str(row.get("series_id") or "")
            }
        series: list[dict[str, Any]] = []
        for public in payload.get("series", []):
            if not isinstance(public, dict):
                continue
            item = dict(public)
            series_id = str(item.get("series_id") or "")
            raw = raw_by_id.get(series_id, {})
            water = water_by_id.get(series_id, {})
            item.update(
                {
                    "is_primary_series": _bool(raw.get("is_primary_series")),
                    "is_special_series": _bool(raw.get("is_special_series")),
                    "special_file_name": str(raw.get("special_file_name") or ""),
                    "cathodic_baseline_first10_median_raw_v": _number(
                        raw.get("cathodic_baseline_first10_median_raw_v")
                    ),
                    "reverse_baseline_first10_median_raw_v": _number(
                        raw.get("reverse_baseline_first10_median_raw_v")
                    ),
                    "water_comp_cathodic_baseline_first10_raw_v": _number(
                        water.get("water_comp_cathodic_baseline_first10_raw_v")
                    ),
                    "water_comp_recovery_baseline_first10_raw_v": _number(
                        water.get("water_comp_recovery_baseline_first10_raw_v")
                    ),
                }
            )
            series.append(item)

        segments: list[dict[str, Any]] = []
        segment_path = self._path(self.SEGMENT_NAME, require=False)
        if segment_path.is_file():
            for row in self._read_csv(self.SEGMENT_NAME):
                segments.append(
                    {
                        "series_id": str(row.get("series_id") or ""),
                        "segment_index": _integer(row.get("segment_index")),
                        "source_file": str(row.get("source_file") or ""),
                        "complete_cycles": _integer(row.get("complete_cycles")),
                        "continuous_time_start_h": _number(
                            row.get("continuous_time_start_h"), 0.0
                        ),
                        "continuous_time_end_h": _number(
                            row.get("continuous_time_end_h"), 0.0
                        ),
                    }
                )
        summary = self._read_json(self.SUMMARY_NAME, optional=True)
        water = summary.get("water_compensation")
        water = water if isinstance(water, dict) else {}
        model = water.get("model")
        model = model if isinstance(model, dict) else water
        return {
            "series": series,
            "segments": segments,
            "rules": {
                "water_resistance_drift_ohm_cm2_per_h": _number(
                    model.get("area_specific_resistance_drift_ohm_cm2_per_h")
                ),
            },
        }

    @staticmethod
    def _json_list(value: Any) -> list[str]:
        try:
            parsed = json.loads(str(value or "[]"))
        except json.JSONDecodeError:
            return []
        if not isinstance(parsed, list):
            return []
        return [str(item) for item in parsed]

    def _read_csv(self, relative: str) -> list[dict[str, str]]:
        path = self._path(relative, require=True)
        try:
            if path.stat().st_size > self.MAX_CSV_BYTES:
                raise StartStopWorkspaceError(
                    "启停分析 CSV 超过网页安全读取上限。",
                    413,
                )
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                return list(csv.DictReader(handle))
        except StartStopWorkspaceError:
            raise
        except (OSError, UnicodeDecodeError, csv.Error) as exc:
            raise StartStopWorkspaceError(f"无法读取启停分析表：{relative}", 500) from exc

    @staticmethod
    def _downsample(points: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        if len(points) <= limit:
            return points
        if limit <= 2:
            return [points[0], points[-1]]
        indices = {
            round(index * (len(points) - 1) / (limit - 1))
            for index in range(limit)
        }
        return [points[index] for index in sorted(indices)]

    def chart_data(
        self,
        *,
        series_ids: Iterable[str],
        metric: str,
        x_axis: str,
        mode: str,
        max_points: int,
    ) -> dict[str, Any]:
        metric = str(metric or "cathodic")
        if metric not in self.METRICS:
            raise StartStopWorkspaceError("不支持的启停图表指标。")
        if x_axis not in {"cycle", "time"}:
            raise StartStopWorkspaceError("横轴只支持 cycle 或 time。")
        if mode not in {"raw", "water", "compare"}:
            raise StartStopWorkspaceError("曲线模式只支持 raw、water 或 compare。")
        requested = [str(item) for item in series_ids if str(item)]
        if not requested:
            raise StartStopWorkspaceError("至少需要选择 1 条分析序列。")
        if len(requested) > self.MAX_SERIES_PER_CHART:
            raise StartStopWorkspaceError(
                f"一次最多检查 {self.MAX_SERIES_PER_CHART} 条分析序列。"
            )
        if len(set(requested)) != len(requested):
            raise StartStopWorkspaceError("分析序列不能重复。")
        series_payload, series_cache_key = self._series_payload()
        series_rows = series_payload["series"]
        known = {row["series_id"]: row for row in series_rows}
        if not set(requested).issubset(known):
            raise StartStopWorkspaceError("选中的分析序列不在当前结果中。", 404)
        requested_work_steps = {
            str(known[series_id].get("work_step_key") or "")
            for series_id in requested
        }
        if len(requested_work_steps) != 1:
            raise StartStopWorkspaceError(
                "不同启停工步不能合并比较；请先选择同一类型、电流和阶段时长的工步。"
            )
        if metric == "minimum_time":
            material_keys = {
                str(known[series_id].get("material_relative_path") or f"series:{series_id}")
                for series_id in requested
            }
            if len(material_keys) != 1:
                raise StartStopWorkspaceError(
                    "异常判断一次只能分析一个材料；同一材料的多条接续序列可以同时判断。"
                )
        requested_max_points = int(max_points)
        limit = max(200, min(requested_max_points, self.MAX_POINTS_PER_SERIES))

        overview = metric == "overview"
        relative = (
            self.WATER_OVERVIEW_NAME
            if overview and mode in {"water", "compare"}
            else self.OVERVIEW_NAME
            if overview
            else self.WATER_CYCLE_NAME
            if mode in {"water", "compare"}
            else self.CYCLE_NAME
        )
        path = self._path(relative, require=True)
        data_signature = self._file_cache_signature(path)
        if data_signature[3] > self.MAX_CSV_BYTES:
            raise StartStopWorkspaceError(
                "当前交互曲线文件超过安全读取上限。",
                413,
            )
        variants = ["raw", "water"] if mode == "compare" else [mode]
        metric_spec = self.METRICS[metric]
        generation = self._cache_generation_token()
        cache_key = (
            "chart",
            generation,
            series_cache_key,
            data_signature,
            tuple(requested),
            metric,
            x_axis,
            mode,
            requested_max_points,
            limit,
        )
        with self._chart_cache_lock:
            cached = self._chart_cache_get_locked(cache_key)
            if cached is not None:
                return cached

            grouped: dict[tuple[str, str], list[dict[str, Any]]] = {
                (series_id, variant): []
                for series_id in requested
                for variant in variants
            }
            requested_set = set(requested)
            names: dict[str, str] = {}
            try:
                with path.open("r", encoding="utf-8-sig", newline="") as handle:
                    for row in csv.DictReader(handle):
                        series_id = str(row.get("series_id") or "")
                        if series_id not in requested_set:
                            continue
                        names[series_id] = str(
                            row.get("series_display_name")
                            or row.get("material_display_name")
                            or series_id
                        )
                        x = _number(
                            row.get("continuous_time_h")
                            if overview or x_axis == "time"
                            else row.get("cycle")
                        )
                        if x is None:
                            continue
                        for variant in variants:
                            y = _number(row.get(metric_spec[variant]))
                            if y is None:
                                continue
                            grouped[(series_id, variant)].append(
                                {
                                    "x": x,
                                    "y": y,
                                    "cycle": _integer(row.get("cycle")),
                                    "status": str(
                                        row.get("cathodic_shift_status") or ""
                                    ),
                                    "source_file": str(row.get("source_file") or ""),
                                    "segment_index": _integer(
                                        row.get("segment_index")
                                    ),
                                }
                            )
            except (OSError, UnicodeDecodeError, csv.Error) as exc:
                # Failed reads never enter the cache.
                raise StartStopWorkspaceError(
                    "无法读取交互曲线数据。",
                    500,
                ) from exc

            payload_series = []
            for series_id in requested:
                for variant in variants:
                    points = self._downsample(
                        grouped[(series_id, variant)],
                        limit,
                    )
                    payload_series.append(
                        {
                            "series_id": series_id,
                            "name": names.get(series_id, series_id),
                            "variant": variant,
                            "points": points,
                        }
                    )
            payload = {
                "metric": metric,
                "metric_label": metric_spec["label"],
                "unit": metric_spec["unit"],
                "x_axis": x_axis,
                "x_label": (
                    "累计时间 / h"
                    if x_axis == "time" or overview
                    else "循环数"
                ),
                "mode": mode,
                "work_step_key": known[requested[0]]["work_step_key"],
                "work_step_label": known[requested[0]]["work_step_label"],
                "series": payload_series,
                "anomaly_boundary_s": 15.0,
            }

            # A render may atomically replace ``current`` while a request is
            # reading the prior file.  That response is still internally
            # usable, but only a fully stable read may populate the cache.
            stable = (
                self._file_cache_signature(path) == data_signature
                and self._cache_generation_token() == generation
                and series_cache_key[1] == generation
                and self._file_cache_signature(
                    self._path(self.SERIES_NAME, require=True)
                )
                == series_cache_key[2]
            )
            if stable:
                self._chart_cache_put_locked(cache_key, payload)
            return payload

    def _config_for_render(
        self,
        material_payload: dict[str, Any] | None = None,
        *,
        data_mode: str = "both",
        material_scope: str = "all",
        updated_material_keys: Iterable[str] = (),
        export_pdf: bool = False,
    ) -> dict[str, Any]:
        payload = material_payload or self.materials()
        return {
            "dataset_fingerprint": payload["dataset_fingerprint"],
            "render_options": {
                "data_mode": data_mode,
                "material_scope": material_scope,
                "updated_material_keys": sorted(
                    {str(key) for key in updated_material_keys if str(key)}
                ),
                "export_pdf": bool(export_pdf),
            },
            "materials": [
                {
                    "key": row["key"],
                    "auto_name": row["auto_name"],
                    "plot_name": row["plot_name"],
                    "include_in_summary_atlas": row["include_in_summary_atlas"],
                    "status": row["status"],
                    "fingerprint": row["fingerprint"],
                    "notes": row["notes"],
                    "source_removed": False,
                }
                for row in payload["materials"]
            ],
        }

    @staticmethod
    def _changed_material_keys(
        material_payload: dict[str, Any],
        previous_fingerprints: dict[str, str],
    ) -> set[str]:
        rows = material_payload.get("materials")
        if not isinstance(rows, list):
            return set()
        return {
            str(row.get("key") or "")
            for row in rows
            if isinstance(row, dict)
            and str(row.get("key") or "")
            and previous_fingerprints.get(str(row.get("key") or ""))
            != str(row.get("fingerprint") or "")
        }

    def _last_render_material_fingerprints(self) -> dict[str, str]:
        selected_restore = getattr(
            self.database, "restore_current_artifact_files", None
        )
        full_restore = getattr(self.database, "restore_current_artifacts", None)
        if not callable(selected_restore) and not callable(full_restore):
            return {}
        self.scratch_dir.mkdir(parents=True, exist_ok=True)
        try:
            with tempfile.TemporaryDirectory(
                prefix="start-stop-render-baseline-",
                dir=str(self.scratch_dir),
            ) as temporary_name:
                target = Path(temporary_name) / "render"
                if callable(selected_restore):
                    restored = selected_restore(
                        target,
                        [self.SNAPSHOT_NAME],
                        kind="render",
                    )
                    if not restored or self.SNAPSHOT_NAME in set(
                        restored.get("missing") or []
                    ):
                        return {}
                else:
                    restored = full_restore(target, kind="render")
                    if not restored:
                        return {}
                snapshot_path = target / self.SNAPSHOT_NAME
                if not snapshot_path.is_file():
                    return {}
                payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, LookupError, KeyError, json.JSONDecodeError):
            return {}
        rows = payload.get("materials")
        if not isinstance(rows, list):
            return {}
        return {
            str(row.get("key")): str(row.get("fingerprint") or "")
            for row in rows
            if isinstance(row, dict) and str(row.get("key") or "")
        }

    def _write_render_config(
        self, payload: dict[str, Any] | None = None
    ) -> None:
        payload = payload or self._config_for_render()
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.config_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, self.config_path)

    @classmethod
    def _render_command_options(
        cls, render_config: dict[str, Any] | None
    ) -> list[str]:
        options = (
            render_config.get("render_options")
            if isinstance(render_config, dict)
            else {}
        )
        if not isinstance(options, dict):
            options = {}
        data_mode = str(options.get("data_mode") or "both")
        material_scope = str(options.get("material_scope") or "all")
        export_pdf = options.get("export_pdf", False)
        if data_mode not in cls.RENDER_DATA_MODES:
            raise RuntimeError("已固定的绘图数据模式无效")
        if material_scope not in cls.RENDER_MATERIAL_SCOPES:
            raise RuntimeError("已固定的绘图范围无效")
        if not isinstance(export_pdf, bool):
            raise RuntimeError("已固定的 PDF 导出选项无效")
        arguments = [
            "--data-mode",
            data_mode,
            "--material-scope",
            material_scope,
        ]
        updated_keys = options.get("updated_material_keys")
        if isinstance(updated_keys, list):
            for key in updated_keys:
                normalized = str(key or "")
                if normalized:
                    arguments.extend(["--updated-material-key", normalized])
        if export_pdf:
            arguments.append("--export-pdf")
        return arguments

    @staticmethod
    def _render_export_requested(
        render_config: dict[str, Any] | None,
    ) -> bool:
        options = (
            render_config.get("render_options")
            if isinstance(render_config, dict)
            else {}
        )
        return isinstance(options, dict) and options.get("export_pdf") is True

    def _run_process(
        self,
        command: list[str],
        *,
        cwd: Path,
        environment: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=os.name != "nt",
        )
        try:
            stdout, stderr = process.communicate(timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            if os.name != "nt":
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            else:
                process.terminate()
            try:
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                if os.name != "nt":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    process.kill()
                stdout, stderr = process.communicate()
            raise subprocess.TimeoutExpired(
                command,
                self.timeout_seconds,
                output=stdout,
                stderr=stderr,
            ) from exc
        return subprocess.CompletedProcess(
            command,
            process.returncode,
            stdout,
            stderr,
        )

    @staticmethod
    def _process_exit_detail(returncode: int | None) -> str:
        if returncode is None:
            return "子进程未返回退出码"
        code = int(returncode)
        if code < 0:
            signal_number = -code
            try:
                signal_name = signal.Signals(signal_number).name
            except ValueError:
                signal_name = f"SIG{signal_number}"
            return f"子进程被信号 {signal_name} 终止（returncode={code}）"
        return f"子进程退出码 {code}"

    @classmethod
    def _process_error(cls, completed: subprocess.CompletedProcess[str]) -> str:
        detail = (completed.stderr or completed.stdout or "").strip()
        exit_detail = cls._process_exit_detail(completed.returncode)
        if not detail:
            return exit_detail[:800]
        last_line = detail.splitlines()[-1].strip()
        return f"{last_line}；{exit_detail}"[:800]

    def _recover_abandoned_collection_batch(
        self,
        batch_id: str,
        job_id: str,
        returncode: int | None,
    ) -> dict[str, Any] | None:
        recover = getattr(self.database, "fail_collection_batch_if_running", None)
        if not callable(recover):
            return None
        reason = self._process_exit_detail(returncode)
        try:
            recovered = recover(
                batch_id,
                reason=reason,
                returncode=returncode,
                parent_job_id=job_id,
            )
        except Exception as exc:
            print(
                f"启停采集批次自动收尾失败：job={job_id}; {type(exc).__name__}",
                file=sys.stderr,
                flush=True,
            )
            return None
        if recovered:
            print(
                f"启停采集批次已自动收尾：job={job_id}; "
                f"batch={batch_id}; {reason}",
                file=sys.stderr,
                flush=True,
            )
        return recovered

    @staticmethod
    def _read_collection_result(path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("远程采集没有生成可验证的结果总结") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("mode") != "collect"
            or not isinstance(payload.get("totals"), dict)
            or not isinstance(payload.get("machines"), list)
        ):
            raise RuntimeError("远程采集结果总结格式无效")
        return payload

    @staticmethod
    def _public_collection_result(
        payload: dict[str, Any],
        *,
        returncode: int,
    ) -> dict[str, Any]:
        totals = payload["totals"]
        machines = [item for item in payload["machines"] if isinstance(item, dict)]
        roots = [
            root
            for machine in machines
            for root in (
                machine.get("roots") if isinstance(machine.get("roots"), list) else []
            )
            if isinstance(root, dict)
        ]
        roots_ok = sum(1 for root in roots if "inventoried" in root)
        roots_failed = sum(1 for root in roots if "error" in root)
        machines_with_errors = sum(
            1
            for machine in machines
            if isinstance(machine.get("errors"), list) and machine["errors"]
        )

        def count(key: str) -> int:
            try:
                return max(0, int(totals.get(key, 0)))
            except (TypeError, ValueError):
                return 0

        roots_ok = max(roots_ok, count("roots_succeeded"))
        roots_failed = max(roots_failed, count("roots_failed"))

        errors = count("errors")
        unsettled = count("unsettled_skipped")
        changed = count("changed_during_collection")
        warning = bool(returncode != 0 or errors or unsettled or changed or roots_failed)
        return {
            "collection_outcome": "partial" if warning else "complete",
            "collection_machines_total": len(machines),
            "collection_machines_with_errors": machines_with_errors,
            "collection_roots_total": len(roots),
            "collection_roots_ok": roots_ok,
            "collection_roots_failed": roots_failed,
            "collection_inventoried": count("inventoried"),
            "collection_stable": count("stable"),
            "collection_already_collected": count("already_collected"),
            "collection_planned_files": count("planned_files"),
            "collection_planned_bytes": count("planned_bytes"),
            "collection_copied": (
                count("copied")
                if "copied" in totals
                else max(0, count("ingested") - count("unchanged_content"))
            ),
            "collection_downloaded": count("downloaded"),
            "collection_ingested": count("ingested"),
            "collection_versioned": count("versioned"),
            "collection_unchanged_content": count("unchanged_content"),
            "collection_unsettled_skipped": unsettled,
            "collection_changed_during_collection": changed,
            "collection_errors": errors,
        }

    @staticmethod
    def _public_prepare_result(payload: dict[str, Any]) -> dict[str, Any]:
        keys = {
            "stage",
            "materials",
            "start_stop_candidates",
            "included_start_stop_files",
            "included_standard_files",
            "included_variable_files",
            "included_adt_files",
            "materials_analyzed",
            "materials_available",
            "materials_skipped_unchanged",
            "materials_in_atlas",
            "materials_excluded_from_atlas",
            "included_files",
            "analysis_series",
            "series_in_atlas",
            "complete_cycles",
            "normal_cycles",
            "abnormal_cycles",
        }
        return {key: payload[key] for key in keys if key in payload}

    def _update_job(self, job_id: str, **changes: Any) -> None:
        updater = getattr(self.database, "update_job", None)
        persisted: dict[str, Any] | None = None
        if callable(updater):
            candidate = updater(job_id, changes)
            if isinstance(candidate, dict):
                persisted = candidate
        with self._job_lock:
            if self._job.get("id") == job_id:
                if persisted is not None:
                    self._job = persisted
                else:
                    self._job.update(changes)

    @staticmethod
    def _progress_text(
        value: Any,
        *,
        limit: int,
        basename: bool = False,
    ) -> str:
        text = str(value or "").strip()
        if basename:
            text = text.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
            if len(text) >= 2 and text[1] == ":":
                text = text[2:]
        elif "/" in text or "\\" in text:
            return "[路径信息已隐藏]"
        text = "".join(
            " " if ord(character) < 32 or ord(character) == 127 else character
            for character in text
        )
        return " ".join(text.split())[:limit]

    def _update_progress(
        self,
        job_id: str,
        *,
        phase: str,
        phase_label: str,
        phase_index: int,
        phase_count: int = 7,
        mode: str = "determinate",
        percent: int | float | None = None,
        completed: int | None = None,
        total: int | None = None,
        unit: str = "steps",
        current_item: str = "",
        detail: str = "",
        machines: list[dict[str, Any]] | None = None,
    ) -> None:
        with self._job_lock:
            if self._job.get("id") != job_id:
                return
            previous = self._job.get("progress")
            previous = previous if isinstance(previous, dict) else {}
            previous_percent = _number(previous.get("percent"), 0) or 0
            previous_phase_index = max(0, _integer(previous.get("phase_index")))
            next_percent = (
                previous_percent
                if percent is None
                else max(previous_percent, min(100, max(0, float(percent))))
            )
            if float(next_percent).is_integer():
                next_percent = int(next_percent)
            safe_machines: list[dict[str, Any]] = []
            raw_machines = (
                machines
                if machines is not None
                else previous.get("machines", [])
            )
            for raw in raw_machines if isinstance(raw_machines, list) else []:
                if not isinstance(raw, dict):
                    continue
                machine_total = max(0, _integer(raw.get("total")))
                machine_completed = min(
                    max(0, _integer(raw.get("completed"))), machine_total
                )
                safe_machines.append(
                    {
                        "machine_id": self._progress_text(
                            raw.get("machine_id", raw.get("id")), limit=128
                        ),
                        "name": self._progress_text(raw.get("name"), limit=100),
                        "status": self._progress_text(raw.get("status"), limit=40),
                        "completed": machine_completed,
                        "total": machine_total,
                        "message": self._progress_text(raw.get("message"), limit=200),
                    }
                )
            next_progress = {
                "phase": self._progress_text(phase, limit=64),
                "phase_label": self._progress_text(phase_label, limit=100),
                "phase_index": max(
                    previous_phase_index,
                    max(0, min(int(phase_index), int(phase_count))),
                ),
                "phase_count": max(1, int(phase_count)),
                "mode": "indeterminate" if mode == "indeterminate" else "determinate",
                "percent": next_percent,
                "completed": (
                    None if completed is None else max(0, int(completed))
                ),
                "total": None if total is None else max(0, int(total)),
                "unit": (
                    unit
                    if unit in {
                        "files", "machines", "steps", "items", "bytes",
                        "materials", "pages",
                    }
                    else "items"
                ),
                "current_item": self._progress_text(
                    current_item,
                    limit=180,
                    basename=("/" in current_item or "\\" in current_item),
                ),
                "detail": self._progress_text(detail, limit=240),
                "machines": safe_machines,
            }
            self._job["progress"] = next_progress
            now = time.monotonic()
            next_phase = str(next_progress.get("phase") or "")
            should_persist = bool(
                next_phase != self._last_progress_persisted_phase
                or now - self._last_progress_persisted_at >= 1.0
                or (_number(next_progress.get("percent"), 0) or 0) >= 100
            )
            if should_persist:
                self._last_progress_persisted_at = now
                self._last_progress_persisted_phase = next_phase
                persisted_progress = json.loads(
                    json.dumps(next_progress, ensure_ascii=False)
                )
            else:
                persisted_progress = None
        progress_updater = getattr(self.database, "update_job_progress", None)
        if persisted_progress is not None and callable(progress_updater):
            progress_updater(job_id, persisted_progress)

    def _apply_collection_progress(self, job_id: str, path: Path) -> None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict):
            return
        child_phase = str(payload.get("phase") or "")
        stage_by_phase = {
            "connecting_remote": "connecting_remote",
            "scanning_remote": "scanning_files",
            "downloading_remote": "downloading_files",
            "storing_database": "storing_files",
            "collection_complete": "storing_files",
        }
        stage = stage_by_phase.get(child_phase)
        if stage is None:
            return
        child_percent = _number(payload.get("percent"), 0) or 0
        mapped_percent = min(64, max(0, child_percent) * 0.64)
        child_phase_index = max(0, _integer(payload.get("phase_index")))
        phase_index_by_phase = {
            "connecting_remote": 1,
            "scanning_remote": 3 if child_phase_index >= 3 else 2,
            "downloading_remote": 3,
            "storing_database": 3,
            "collection_complete": 4,
        }
        self._update_progress(
            job_id,
            phase=child_phase,
            phase_label=str(payload.get("phase_label") or "正在采集数据"),
            phase_index=phase_index_by_phase[child_phase],
            percent=mapped_percent,
            completed=(
                _integer(payload.get("completed"))
                if payload.get("completed") is not None
                else None
            ),
            total=(
                _integer(payload.get("total"))
                if payload.get("total") is not None
                else None
            ),
            unit=str(payload.get("unit") or "items"),
            current_item=str(payload.get("current_item") or ""),
            detail=str(payload.get("detail") or ""),
            machines=(
                payload.get("machines")
                if isinstance(payload.get("machines"), list)
                else []
            ),
            mode=str(payload.get("mode") or "determinate"),
        )
        progress = self._public_job().get("progress", {})
        current_item = str(progress.get("current_item") or "")
        label = str(progress.get("phase_label") or "正在采集数据")
        self._update_job(
            job_id,
            stage=stage,
            message=label + (f"：{current_item}" if current_item else ""),
        )

    def _run_collection_process(
        self,
        job_id: str,
        command: list[str],
        *,
        cwd: Path,
        environment: dict[str, str],
        progress_path: Path,
    ) -> subprocess.CompletedProcess[str]:
        progress_path.unlink(missing_ok=True)
        stopped = threading.Event()

        def watch_progress() -> None:
            while not stopped.wait(0.15):
                self._apply_collection_progress(job_id, progress_path)

        watcher = threading.Thread(
            target=watch_progress,
            name=f"start-stop-progress-{job_id}",
            daemon=True,
        )
        watcher.start()
        try:
            return self._run_process(
                command,
                cwd=cwd,
                environment=environment,
            )
        finally:
            self._apply_collection_progress(job_id, progress_path)
            stopped.set()
            watcher.join(timeout=1)

    def _apply_render_progress(self, job_id: str, path: Path) -> None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict):
            return
        child_phase = str(payload.get("phase") or "")
        phase_index_by_phase = {
            "loading_config": 2,
            "loading_files": 2,
            "analyzing_series": 3,
            "writing_tables": 3,
            "rendering_standard": 4,
            "rendering_water": 5,
            "finalizing_outputs": 6,
        }
        if child_phase not in phase_index_by_phase:
            return
        target_phase_index = phase_index_by_phase[child_phase]
        child_percent = min(
            96.0,
            max(7.0, float(_number(payload.get("percent"), 7) or 7)),
        )
        existing = self._public_job().get("progress", {})
        existing_phase_index = max(0, _integer(existing.get("phase_index")))
        existing_percent = float(_number(existing.get("percent"), 0) or 0)
        if target_phase_index < existing_phase_index or (
            target_phase_index == existing_phase_index
            and child_percent < existing_percent
        ):
            return
        self._update_progress(
            job_id,
            phase=child_phase,
            phase_label=str(payload.get("phase_label") or "正在重新绘图"),
            phase_index=target_phase_index,
            percent=child_percent,
            completed=(
                _integer(payload.get("completed"))
                if payload.get("completed") is not None
                else None
            ),
            total=(
                _integer(payload.get("total"))
                if payload.get("total") is not None
                else None
            ),
            unit=str(payload.get("unit") or "items"),
            current_item=str(payload.get("current_item") or ""),
            detail=str(payload.get("detail") or ""),
            mode="determinate",
        )
        progress = self._public_job().get("progress", {})
        current_item = str(progress.get("current_item") or "")
        label = str(progress.get("phase_label") or "正在重新绘图")
        self._update_job(
            job_id,
            stage="rendering",
            message=label + (f"：{current_item}" if current_item else ""),
        )

    def _run_render_process(
        self,
        job_id: str,
        command: list[str],
        *,
        cwd: Path,
        environment: dict[str, str],
        progress_path: Path,
    ) -> subprocess.CompletedProcess[str]:
        progress_path.unlink(missing_ok=True)
        stopped = threading.Event()

        def watch_progress() -> None:
            while not stopped.wait(0.15):
                self._apply_render_progress(job_id, progress_path)

        watcher = threading.Thread(
            target=watch_progress,
            name=f"start-stop-render-progress-{job_id}",
            daemon=True,
        )
        watcher.start()
        try:
            return self._run_process(
                command,
                cwd=cwd,
                environment=environment,
            )
        finally:
            self._apply_render_progress(job_id, progress_path)
            stopped.set()
            watcher.join(timeout=1)

    @staticmethod
    def _upload_component(value: Any, *, label: str) -> str:
        component = unicodedata.normalize("NFC", str(value or "")).strip()
        if (
            not component
            or component in {".", ".."}
            or "/" in component
            or "\\" in component
            or "\x00" in component
            or any(ord(character) < 32 or ord(character) == 127 for character in component)
        ):
            raise StartStopWorkspaceError(f"{label}不是安全的单一名称。")
        if len(component.encode("utf-8")) > 255:
            raise StartStopWorkspaceError(f"{label}过长。")
        return component

    @classmethod
    def validate_upload_metadata(
        cls,
        *,
        filename: Any,
        relative_path: Any,
        group: Any,
        last_modified: Any,
        size_bytes: Any,
    ) -> dict[str, Any]:
        """Validate browser-provided metadata without trusting local paths."""
        safe_filename = cls._upload_component(filename, label="文件名")
        safe_group = cls._upload_component(group, label="数据分组")
        suffix = PurePosixPath(safe_filename).suffix.casefold()
        if suffix not in cls.UPLOAD_EXTENSIONS:
            allowed = " ".join(sorted(cls.UPLOAD_EXTENSIONS))
            raise StartStopWorkspaceError(
                f"不支持 {suffix or '无扩展名'} 文件；可上传：{allowed}。",
                415,
            )

        raw_relative = unicodedata.normalize(
            "NFC", str(relative_path or safe_filename)
        )
        if (
            not raw_relative
            or raw_relative.startswith(("/", "\\"))
            or "\x00" in raw_relative
            or (len(raw_relative) >= 2 and raw_relative[0].isalpha() and raw_relative[1] == ":")
        ):
            raise StartStopWorkspaceError("上传相对路径无效。")
        raw_relative = raw_relative.replace("\\", "/")
        raw_parts = raw_relative.split("/")
        if not raw_parts or any(part in {"", ".", ".."} for part in raw_parts):
            raise StartStopWorkspaceError("上传相对路径包含不安全的目录。")
        parts = [cls._upload_component(part, label="上传路径") for part in raw_parts]
        if parts[-1] != safe_filename:
            raise StartStopWorkspaceError("文件名必须与相对路径的末级名称一致。")
        normalized_relative = PurePosixPath(*parts).as_posix()
        repository_path = PurePosixPath(
            "手动上传", safe_group, normalized_relative
        ).as_posix()
        if len(repository_path.encode("utf-8")) > 2048:
            raise StartStopWorkspaceError("上传相对路径过长。")

        size_text = str(size_bytes)
        if not size_text.isascii() or not size_text.isdigit():
            raise StartStopWorkspaceError("上传文件大小无效。")
        declared_size = int(size_text)
        if declared_size <= 0:
            raise StartStopWorkspaceError("不能上传空文件。")
        if declared_size > cls.MAX_UPLOAD_BYTES:
            raise StartStopWorkspaceError(
                f"单个上传文件不能超过 {cls.MAX_UPLOAD_BYTES // (1024 * 1024)} MiB。",
                413,
            )

        modified_text = str(last_modified)
        if not modified_text.isascii() or not modified_text.isdigit():
            raise StartStopWorkspaceError("文件修改时间无效。")
        modified_ms = int(modified_text)
        # Keep nanoseconds within SQLite's signed 64-bit INTEGER range.
        if modified_ms < 0 or modified_ms > 9_223_372_036_854:
            raise StartStopWorkspaceError("文件修改时间超出支持范围。")
        try:
            modified_utc = dt.datetime.fromtimestamp(
                modified_ms / 1000,
                tz=dt.timezone.utc,
            ).isoformat(timespec="milliseconds")
        except (OverflowError, OSError, ValueError) as exc:
            raise StartStopWorkspaceError("文件修改时间无效。") from exc

        return {
            "filename": safe_filename,
            "group": safe_group,
            "relative_path": normalized_relative,
            "repository_path": repository_path,
            "last_modified_ms": modified_ms,
            "modified_utc": modified_utc,
            "modified_ns": modified_ms * 1_000_000,
            "size_bytes": declared_size,
        }

    def upload_file(
        self,
        staged_path: str | Path,
        *,
        filename: Any,
        relative_path: Any,
        group: Any,
        last_modified: Any,
        size_bytes: Any,
    ) -> dict[str, Any]:
        """Atomically import one already-staged local upload into the repository."""
        if not self.repository_mode:
            raise StartStopWorkspaceError("文件上传仅在 Docker 启停数据仓库中可用。", 503)
        importer = getattr(self.database, "import_source_path", None)
        if not callable(importer):
            raise StartStopWorkspaceError("启停数据库当前不支持文件上传。", 503)
        metadata = self.validate_upload_metadata(
            filename=filename,
            relative_path=relative_path,
            group=group,
            last_modified=last_modified,
            size_bytes=size_bytes,
        )
        staged = Path(staged_path)
        if staged.is_symlink() or not staged.is_file():
            raise StartStopWorkspaceError("上传临时文件无效。")
        if staged.stat().st_size != metadata["size_bytes"]:
            raise StartStopWorkspaceError("上传内容大小与声明不一致。")

        with self._job_lock:
            if self._active_job_id or self._job.get("status") in {"queued", "running"}:
                raise StartStopWorkspaceError("启停数据任务运行中，请等待完成后再上传。", 409)
            imported = importer(
                staged,
                {
                    "source_kind": "browser_upload",
                    "machine_id": "browser-upload",
                    "root_label": "manual-upload",
                    "remote_path": metadata["repository_path"],
                    "remote_relative_path": metadata["repository_path"],
                    "logical_path": metadata["repository_path"],
                    "repository_path": metadata["repository_path"],
                    "size_bytes": metadata["size_bytes"],
                    "last_write_ticks": metadata["last_modified_ms"],
                    "last_write_utc": metadata["modified_utc"],
                    "modified_ns": metadata["modified_ns"],
                    "upload_group": metadata["group"],
                    "upload_relative_path": metadata["relative_path"],
                },
            )
            if not isinstance(imported, dict):
                raise StartStopWorkspaceError("启停数据库未返回可验证的上传结果。", 500)
            try:
                upload_id = int(imported["version_id"])
            except (KeyError, TypeError, ValueError) as exc:
                raise StartStopWorkspaceError("启停数据库未返回有效的上传编号。", 500) from exc
            self._recent_upload_ids.add(upload_id)

        self._audit(
            "start_stop_upload_ingested",
            str(upload_id),
            f"size={metadata['size_bytes']}; candidate={bool(imported.get('is_candidate'))}",
        )
        return {
            "upload_id": upload_id,
            "changed": bool(imported.get("changed")),
            "duplicate": not bool(imported.get("changed")),
            "filename": metadata["filename"],
            "repository_path": metadata["repository_path"],
            "size_bytes": int(imported.get("size_bytes", metadata["size_bytes"])),
            "sha256": str(imported.get("sha256") or ""),
            "is_candidate": bool(imported.get("is_candidate")),
            "candidate_kind": str(imported.get("candidate_kind") or ""),
            "prepare_required": True,
        }

    def run_collection_admin(self, operation: Any) -> Any:
        """Run one configuration/connectivity operation while no job can start.

        Holding the same lock used by :meth:`start_job` closes the race between
        a browser-side busy check and a queued remote collection.
        """
        if not callable(operation):
            raise TypeError("operation must be callable")
        with self._job_lock:
            if self._active_job_id or self._job.get("status") in {"queued", "running"}:
                raise StartStopWorkspaceError(
                    "启停数据任务运行中，请等待完成后再修改或检查采集配置。",
                    409,
                )
            return operation()

    def _run_operational_database_check(self) -> dict[str, Any]:
        """Require the fast metadata/FK gate before routine repository work."""

        checker = getattr(self.database, "operational_check", None)
        if not self.repository_mode or not callable(checker):
            return {
                "ok": True,
                "mode": "not_available",
                "elapsed_ms": 0,
            }
        try:
            raw = checker()
        except (OSError, RuntimeError, ValueError, sqlite3.Error):
            raise StartStopWorkspaceError(
                "数据库快速一致性检查未能完成，已停止本次任务。",
                503,
            ) from None
        payload = raw if isinstance(raw, dict) else {}
        if payload.get("ok") is not True:
            raise StartStopWorkspaceError(
                "数据库快速一致性检查发现异常，已停止本次任务；"
                "请先执行数据库修复流程和强制全量备份。",
                503,
            )
        result: dict[str, Any] = {
            "ok": True,
            "mode": "operational_metadata",
        }
        for key in (
            "schema_version",
            "expected_schema_version",
            "required_table_count",
            "foreign_key_errors",
            "blob_metadata_errors",
            "pointer_errors",
            "elapsed_ms",
        ):
            value = raw.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                result[key] = value
        checked_utc = raw.get("checked_utc")
        if isinstance(checked_utc, str) and len(checked_utc) <= 40:
            result["checked_utc"] = checked_utc
        return result

    def start_job(
        self,
        action: str,
        *,
        upload_ids: list[Any] | None = None,
        requested_via: str | None = None,
        render_data_mode: str = "both",
        render_material_scope: str = "all",
        export_pdf: bool = False,
    ) -> dict[str, Any]:
        action = str(action or "").strip().lower()
        if action not in {"scan", "render", "prepare_upload"}:
            raise StartStopWorkspaceError(
                "启停任务只支持 scan、prepare_upload 或 render。"
            )
        normalized_requested_via = str(requested_via or "").strip().lower()
        if not normalized_requested_via:
            normalized_requested_via = (
                "upload" if action == "prepare_upload" else "manual"
            )
        allowed_request_sources = {
            "scan": {"manual", "automatic"},
            "render": {"manual"},
            "prepare_upload": {"upload"},
        }[action]
        if normalized_requested_via not in allowed_request_sources:
            raise StartStopWorkspaceError(
                "启停任务来源与任务类型不匹配。"
            )
        if action == "prepare_upload" and not self.repository_mode:
            raise StartStopWorkspaceError("prepare_upload 仅在 Docker 启停数据仓库中可用。")
        normalized_render_data_mode = str(render_data_mode or "both").strip().lower()
        normalized_render_material_scope = str(
            render_material_scope or "all"
        ).strip().lower()
        if not isinstance(export_pdf, bool):
            raise StartStopWorkspaceError("export_pdf 必须是布尔值。")
        if action == "render":
            if normalized_render_data_mode not in self.RENDER_DATA_MODES:
                raise StartStopWorkspaceError(
                    "绘图数据只支持 raw、water 或 both。"
                )
            if normalized_render_material_scope not in self.RENDER_MATERIAL_SCOPES:
                raise StartStopWorkspaceError(
                    "绘图范围只支持 all 或 updated。"
                )
        elif (
            normalized_render_data_mode != "both"
            or normalized_render_material_scope != "all"
            or export_pdf
        ):
            raise StartStopWorkspaceError("绘图选项仅可用于 render 任务。")
        normalized_upload_ids: tuple[int, ...] = ()
        if action == "prepare_upload":
            if not isinstance(upload_ids, list):
                raise StartStopWorkspaceError("prepare_upload 必须提供 upload_ids。")
            if any(
                not isinstance(value, int) or isinstance(value, bool)
                for value in upload_ids
            ):
                raise StartStopWorkspaceError("upload_ids 必须是正整数列表。")
            normalized_upload_ids = tuple(upload_ids)
            if (
                not normalized_upload_ids
                or len(normalized_upload_ids) > self.MAX_UPLOAD_BATCH_FILES
                or any(value <= 0 for value in normalized_upload_ids)
                or len(set(normalized_upload_ids)) != len(normalized_upload_ids)
            ):
                raise StartStopWorkspaceError(
                    f"upload_ids 必须包含 1–{self.MAX_UPLOAD_BATCH_FILES} 个不重复的正整数。"
                )
        elif upload_ids not in (None, (), []):
            raise StartStopWorkspaceError("仅 prepare_upload 任务接受 upload_ids。")
        if (
            not self.configured
            or not self.analysis_dir
            or (not self.repository_mode and not self.analysis_dir.is_dir())
        ):
            raise StartStopWorkspaceError("启停分析目录当前不可用。", 503)
        execution = self._execution_status()
        ready_key = {
            "scan": "update_ready",
            "prepare_upload": "prepare_upload_ready",
            "render": "render_ready",
        }[action]
        if not execution[ready_key]:
            raise StartStopWorkspaceError(
                f"{execution['message']}，暂时不能"
                + (
                    "从实验电脑更新数据。"
                    if action == "scan"
                    else "准备上传数据。"
                    if action == "prepare_upload"
                    else "重新绘图。"
                ),
                503,
            )
        script = self._analysis_script_path()
        if script is None or not script.is_file():
            raise StartStopWorkspaceError("启停分析脚本当前不可用。", 503)
        render_config: dict[str, Any] | None = None
        render_revision: int | None = None
        operational_check: dict[str, Any] = {
            "ok": True,
            "mode": "not_available",
            "elapsed_ms": 0,
        }
        with self._job_lock:
            if self._active_job_id or self._job.get("status") in {"queued", "running"}:
                raise StartStopWorkspaceError("已有启停分析任务在运行。", 409)
            if self.repository_mode and self.backup_dir is not None:
                storage = self._safety_status({"state": "none"}).get("storage")
                if isinstance(storage, dict) and storage.get("preflight_ok") is False:
                    raise StartStopWorkspaceError(
                        "当前剩余空间不足以安全完成启停数据任务。",
                        507,
                    )
                # `low_space` is an operator warning only.  A task is blocked
                # solely when the workload-aware preflight reports a shortfall.
            operational_check = self._run_operational_database_check()
            if action == "prepare_upload":
                unknown_uploads = sorted(
                    set(normalized_upload_ids) - self._recent_upload_ids
                )
                if unknown_uploads:
                    raise StartStopWorkspaceError(
                        "upload_ids 包含非本服务本轮接收的文件编号。",
                        409,
                    )
            if action == "render":
                material_payload = self.materials()
                updated_material_keys = self._changed_material_keys(
                    material_payload,
                    self._last_render_material_fingerprints(),
                )
                if (
                    normalized_render_material_scope == "updated"
                    and not updated_material_keys
                ):
                    raise StartStopWorkspaceError(
                        "当前没有新增或数据已更新的材料，无需执行仅更新材料绘图。",
                        409,
                    )
                render_config = self._config_for_render(
                    material_payload,
                    data_mode=normalized_render_data_mode,
                    material_scope=normalized_render_material_scope,
                    updated_material_keys=updated_material_keys,
                    export_pdf=export_pdf,
                )
                render_revision = _integer(material_payload.get("revision"))
                if not self.repository_mode:
                    self._write_render_config(render_config)
            job_id = uuid.uuid4().hex[:12]
            collection_batch_id = (
                uuid.uuid4().hex
                if action == "scan" and self.repository_mode
                else ""
            )
            job_payload = {
                "id": job_id,
                "action": action,
                "requested_via": normalized_requested_via,
                "status": "queued",
                "stage": "queued",
                "failure_class": "none",
                "failure_code": "",
                "created_utc": _utc_now(),
                "updated_utc": _utc_now(),
                "started_utc": "",
                "completed_utc": "",
                "collection_batch_id": collection_batch_id,
                "message": "等待执行",
                "result": {
                    "database_operational_check": operational_check,
                },
                "progress": {
                    "phase": "queued",
                    "phase_label": "等待执行",
                    "phase_index": 0,
                    "phase_count": 7,
                    "mode": "indeterminate",
                    "percent": 0,
                    "completed": 0,
                    "total": 7,
                    "unit": "steps",
                    "current_item": "",
                    "detail": "任务已进入队列",
                    "machines": [],
                },
            }
            creator = getattr(self.database, "create_job", None)
            if callable(creator):
                try:
                    persisted = creator(
                        job_id=job_id,
                        action=action,
                        requested_via=job_payload["requested_via"],
                        message=job_payload["message"],
                        progress=job_payload["progress"],
                        result=job_payload["result"],
                        collection_batch_id=collection_batch_id,
                    )
                except sqlite3.IntegrityError as exc:
                    raise StartStopWorkspaceError(
                        "已有启停分析任务在运行。", 409
                    ) from exc
                if isinstance(persisted, dict):
                    job_payload = persisted
            self._active_job_id = job_id
            self._job = job_payload
        thread = threading.Thread(
            target=self._run_job_entry,
            args=(
                job_id,
                action,
                script,
                render_config,
                render_revision,
                normalized_upload_ids,
                collection_batch_id,
            ),
            name=f"start-stop-{action}-{job_id}",
            daemon=True,
        )
        try:
            thread.start()
        except Exception:
            try:
                self._finish_failed(
                    job_id,
                    "任务线程未能启动。",
                    failure_code="thread_start_failed",
                )
            finally:
                with self._job_lock:
                    if self._active_job_id == job_id:
                        self._active_job_id = ""
            raise
        return self._public_job()

    def _audit(self, action: str, target: str, detail: str) -> None:
        audit = getattr(self.database, "audit", None)
        if callable(audit):
            audit(action, target, detail)

    def _audit_best_effort(self, action: str, target: str, detail: str) -> bool:
        try:
            self._audit(action, target, detail)
        except Exception:
            return False
        return True

    def _finish_published_with_warning(
        self,
        job_id: str,
        *,
        result: dict[str, Any],
        message: str,
        failure_code: str,
    ) -> None:
        """Never convert a committed generation into a failed task state."""

        changes = {
            "status": "completed_with_warnings",
            "stage": "completed",
            "failure_class": "partial",
            "failure_code": failure_code,
            "completed_utc": _utc_now(),
            "message": message[:800],
            "result": result,
        }
        try:
            self._update_job(job_id, **changes)
        except Exception:
            # Recovery uses the already-committed generation pointer to
            # reconcile this row after restart. Keep the live UI truthful even
            # if the terminal DB write is temporarily unavailable.
            with self._job_lock:
                if self._job.get("id") == job_id:
                    self._job.update(changes)
        self._audit_best_effort(
            "start_stop_published_with_warning",
            job_id,
            "artifact generation committed; post-publication finalization incomplete",
        )

    @staticmethod
    def _snapshot_id(snapshot: Any) -> Any:
        if not isinstance(snapshot, dict):
            return None
        value = snapshot.get("id", snapshot.get("snapshot_id"))
        return value if value not in {None, ""} else None

    def _snapshot_has_current_artifact(
        self,
        *,
        snapshot_id: int,
        config_revision: int,
        analysis_script_sha256: str,
    ) -> bool:
        getter = getattr(self.database, "current_analysis_run", None)
        if not callable(getter):
            return False
        for kind in ("render", "scan", "prepare_upload"):
            try:
                payload = getter(kind)
            except (OSError, RuntimeError, ValueError):
                continue
            if not isinstance(payload, dict) or payload.get("state") != "sealed":
                continue
            if (
                _integer(payload.get("snapshot_id")) == int(snapshot_id)
                and _integer(payload.get("config_revision"))
                == int(config_revision)
                and str(payload.get("analysis_script_sha256") or "").casefold()
                == str(analysis_script_sha256).casefold()
            ):
                return True
        return False

    @staticmethod
    def _snapshot_cache_descriptor(
        snapshot: dict[str, Any],
    ) -> tuple[str, list[tuple[str, str, int]]] | None:
        fingerprint = str(snapshot.get("dataset_fingerprint") or "").casefold()
        files = snapshot.get("files")
        if re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None or not isinstance(
            files, list
        ):
            return None
        normalized: list[tuple[str, str, int]] = []
        try:
            for raw in files:
                if not isinstance(raw, dict):
                    return None
                relative = str(raw["path"]).replace("\\", "/")
                parts = PurePosixPath(relative).parts
                sha256 = str(raw["sha256"]).casefold()
                size = int(raw["size_bytes"])
                if (
                    not parts
                    or relative.startswith("/")
                    or any(part in {"", ".", ".."} for part in parts)
                    or re.fullmatch(r"[0-9a-f]{64}", sha256) is None
                    or size < 0
                ):
                    return None
                normalized.append((PurePosixPath(*parts).as_posix(), sha256, size))
        except (KeyError, TypeError, ValueError):
            return None
        if len(normalized) != len({item[0].casefold() for item in normalized}):
            return None
        return fingerprint, normalized

    def _snapshot_cache_is_valid(
        self,
        target: Path,
        *,
        snapshot_id: int,
        fingerprint: str,
        files: list[tuple[str, str, int]],
        verify_hashes: bool = False,
    ) -> bool:
        if not target.is_dir() or target.is_symlink():
            return False
        manifest_path = target / self.SNAPSHOT_MANIFEST_NAME
        if not manifest_path.is_file() or manifest_path.is_symlink():
            return False
        try:
            if manifest_path.stat().st_size > 32 * 1024 * 1024:
                return False
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                return False
            manifest_files = manifest.get("files")
            if (
                int(manifest.get("snapshot_id")) != int(snapshot_id)
                or str(manifest.get("dataset_fingerprint") or "").casefold()
                != fingerprint
                or not isinstance(manifest_files, list)
            ):
                return False
            observed = [
                (
                    str(item["path"]).replace("\\", "/"),
                    str(item["sha256"]).casefold(),
                    int(item["size_bytes"]),
                )
                for item in manifest_files
                if isinstance(item, dict)
            ]
            if observed != files:
                return False
            for relative, sha256, size in files:
                path = target.joinpath(*PurePosixPath(relative).parts)
                if path.is_symlink() or not path.is_file() or path.stat().st_size != size:
                    return False
                if verify_hashes and self._hash_file(path) != (sha256, size):
                    return False
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return False
        return True

    def _materialize_snapshot_source(
        self,
        snapshot: dict[str, Any],
        snapshot_id: int,
        fallback_target: Path,
    ) -> tuple[Path, bool, bool]:
        """Return source root, cache-hit state and manifest trust state."""
        descriptor = self._snapshot_cache_descriptor(snapshot)
        if descriptor is None:
            self.database.materialize_snapshot(snapshot_id, fallback_target)
            return fallback_target, False, False
        fingerprint, files = descriptor
        cache_root = self.scratch_dir / self.SNAPSHOT_CACHE_DIR_NAME
        cache_root.mkdir(parents=True, exist_ok=True)
        if cache_root.is_symlink():
            raise RuntimeError("启停快照缓存目录无效")
        target = cache_root / fingerprint
        if self._snapshot_cache_is_valid(
            target,
            snapshot_id=snapshot_id,
            fingerprint=fingerprint,
            files=files,
            verify_hashes=(
                self._verified_snapshot_cache_fingerprint != fingerprint
            ),
        ):
            self._verified_snapshot_cache_fingerprint = fingerprint
            return target, True, True

        # The cache is derived and bounded to one immutable snapshot so the
        # 4 GiB container scratch disk cannot retain multiple multi-GB copies.
        self._verified_snapshot_cache_fingerprint = ""
        for candidate in cache_root.iterdir():
            if candidate == target:
                continue
            if candidate.is_dir() and re.fullmatch(
                r"[0-9a-f]{64}", candidate.name.casefold()
            ):
                shutil.rmtree(candidate)
        if target.exists():
            if target.is_symlink() or not target.is_dir():
                raise RuntimeError("启停快照缓存目标无效")
            shutil.rmtree(target)
        self.database.materialize_snapshot(snapshot_id, target)
        if not self._snapshot_cache_is_valid(
            target,
            snapshot_id=snapshot_id,
            fingerprint=fingerprint,
            files=files,
        ):
            shutil.rmtree(target, ignore_errors=True)
            raise RuntimeError("启停数据库快照缓存校验失败")
        self._verified_snapshot_cache_fingerprint = fingerprint
        return target, False, True

    def _seed_private_output(
        self,
        output_dir: Path,
        *,
        action: str,
        required: bool,
    ) -> None:
        """Create a clean output tree containing only allow-listed inputs."""
        allowed = tuple(self.SEED_INPUTS.get(action, ()))
        if action not in self.SEED_INPUTS:
            raise RuntimeError("启停分析任务类型无效")
        if output_dir.exists():
            raise RuntimeError("启停分析私有输出目录必须为空")
        restore = getattr(self.database, "restore_current_artifacts", None)
        if not callable(restore):
            raise RuntimeError("启停数据数据库不支持恢复分析产物")
        repository = self._public_repository_status(
            self.database.repository_status()
        )
        generation_keys = (
            "artifact_generation_count",
            "generation_count",
        )
        generation_known = any(key in repository for key in generation_keys)
        generation_count = max(
            (_integer(repository.get(key)) for key in generation_keys),
            default=0,
        )
        if generation_known and generation_count <= 0:
            if required:
                raise RuntimeError("尚无可用于重新绘图的已发布材料表")
            output_dir.mkdir(parents=True, exist_ok=False)
            return

        selected_restore = getattr(
            self.database, "restore_current_artifact_files", None
        )
        try:
            if callable(selected_restore):
                restored = selected_restore(output_dir, list(allowed))
            else:
                output_dir.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(
                    prefix="start-stop-seed-",
                    dir=str(output_dir.parent),
                ) as temporary_name:
                    restored_root = Path(temporary_name) / "generation"
                    restored = restore(restored_root)
                    output_dir.mkdir(parents=True, exist_ok=False)
                    if restored_root.is_dir():
                        for relative in allowed:
                            source = restored_root.joinpath(
                                *PurePosixPath(relative).parts
                            )
                            if not source.is_file() or source.is_symlink():
                                continue
                            destination = output_dir.joinpath(
                                *PurePosixPath(relative).parts
                            )
                            destination.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(source, destination)
        except (FileNotFoundError, LookupError):
            if required:
                raise RuntimeError("尚无可用于重新绘图的已发布材料表")
            output_dir.mkdir(parents=True, exist_ok=False)
            return
        if not output_dir.is_dir():
            output_dir.mkdir(parents=True, exist_ok=False)
        allowed_set = set(allowed)
        observed: set[str] = set()
        for path in output_dir.rglob("*"):
            if path.is_symlink():
                raise RuntimeError("启停分析种子输入不能包含符号链接")
            if path.is_file():
                observed.add(path.relative_to(output_dir).as_posix())
        unexpected = sorted(observed - allowed_set)
        if unexpected:
            raise RuntimeError(
                "启停分析种子输入越出白名单：" + "、".join(unexpected[:5])
            )
        if required and self.SNAPSHOT_NAME not in observed:
            raise RuntimeError("尚无可用于重新绘图的已发布材料表")

    def _synchronize_seed_workbook_from_database(
        self,
        output_dir: Path,
        *,
        script: Path,
        job_dir: Path,
        environment: dict[str, str],
    ) -> None:
        """Apply the saved web material names before refreshing a workbook.

        The workbook updater preserves rows from the previous workbook.  The
        database is the authoritative source after a user saves the material
        library, so the private seed workbook must receive those edits before
        a new snapshot adds materials that may otherwise collide with stale
        workbook names.
        """
        workbook_path = output_dir.joinpath(
            *PurePosixPath(self.CONFIG_WORKBOOK_RELATIVE).parts
        )
        snapshot_path = self._path(self.SNAPSHOT_NAME, require=False)
        database_config = self.database.get_start_stop_config()
        if (
            not workbook_path.is_file()
            or not snapshot_path.is_file()
            or _integer(database_config.get("revision")) <= 0
        ):
            return
        builder = script.parent / "material_config_workbook.py"
        if not builder.is_file() or builder.is_symlink():
            raise RuntimeError("材料配置表同步程序不可用")
        payload = self._config_for_render(self.materials())
        config_path = job_dir / "saved-material-config.json"
        config_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        completed = self._run_process(
            [
                self.python_executable,
                str(builder),
                "apply-json",
                str(snapshot_path),
                str(config_path),
                str(workbook_path),
            ],
            cwd=script.parent,
            environment=environment,
        )
        if completed.returncode != 0:
            raise RuntimeError(self._process_error(completed))

    @staticmethod
    def _hash_file(path: Path) -> tuple[str, int]:
        digest = hashlib.sha256()
        size_bytes = 0
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
                size_bytes += len(chunk)
        return digest.hexdigest(), size_bytes

    @staticmethod
    def _remove_runtime_junk(output_dir: Path) -> None:
        for path in sorted(
            output_dir.rglob("*"),
            key=lambda item: len(item.parts),
            reverse=True,
        ):
            if path.is_dir() and path.name in {"__pycache__", ".matplotlib"}:
                shutil.rmtree(path)
            elif path.is_file() and (
                path.name
                in {
                    ".DS_Store",
                    ".start-stop-artifacts.json",
                    ".start-stop-manifest.json",
                }
                or path.suffix.casefold() == ".pyc"
            ):
                path.unlink()

    def _write_analysis_provenance(
        self,
        output_dir: Path,
        *,
        script: Path,
        snapshot: dict[str, Any],
        snapshot_id: int,
        config_revision: int,
        action: str,
        expected_script_sha256: str,
        expected_script_size: int,
    ) -> dict[str, Any]:
        published_script = output_dir / self.SCRIPT_NAME
        shutil.copy2(script, published_script)
        script_sha256, script_size = self._hash_file(published_script)
        source_sha256, source_size = self._hash_file(script)
        if (script_sha256, script_size) != (source_sha256, source_size):
            raise RuntimeError("启停分析脚本复制后校验失败")
        if (script_sha256, script_size) != (
            expected_script_sha256,
            expected_script_size,
        ):
            raise RuntimeError("启停分析脚本在任务执行期间发生了变化")
        summary_path = output_dir / self.SUMMARY_NAME
        if summary_path.is_file():
            try:
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
                summary_sha256 = str(
                    summary.get("runtime", {}).get("analysis_script_sha256")
                    or ""
                )
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, AttributeError) as exc:
                raise RuntimeError("启停分析摘要中的运行时信息无效") from exc
            if summary_sha256 and summary_sha256 != script_sha256:
                raise RuntimeError("启停分析摘要与实际执行脚本不一致")
        image_reference = str(
            os.environ.get("START_STOP_IMAGE_REFERENCE")
            or os.environ.get("START_STOP_SERVICE_VERSION")
            or ""
        ).strip()
        provenance = {
            "schema_version": 1,
            "created_utc": _utc_now(),
            "kind": action,
            "analysis_workflow_version": self.ANALYSIS_WORKFLOW_VERSION,
            "snapshot_id": int(snapshot_id),
            "dataset_fingerprint": str(
                snapshot.get("dataset_fingerprint") or ""
            ),
            "config_revision": int(config_revision),
            "analysis_script": {
                "published_path": self.SCRIPT_NAME,
                "sha256": script_sha256,
                "size_bytes": script_size,
                "version_basis": "sha256",
            },
            "runtime": {
                "python_version": sys.version.split()[0],
                "container_mode": _bool(os.environ.get("ECHEM_CONTAINER_MODE")),
                "image_or_service_version": image_reference,
                "artifact_seal_schema_version": ARTIFACT_SEAL_SCHEMA_VERSION,
            },
        }
        path = output_dir / self.PROVENANCE_NAME
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(provenance, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
        return provenance

    def _write_repository_status_artifact(self, output_dir: Path) -> None:
        status = self._public_repository_status(
            self.database.repository_status()
        )
        # The status file is written immediately before the enclosing artifact
        # generation is committed.  Record the generation that will become
        # current so read-only replicas do not permanently lag by one.
        generation_count = max(
            _integer(status.get("artifact_generation_count")),
            _integer(status.get("generation_count")),
        ) + 1
        status["artifact_generation_count"] = generation_count
        status["generation_count"] = generation_count
        path = output_dir / self.REPOSITORY_STATUS_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(status, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, path)

    def _analysis_run_record(
        self,
        output_dir: Path,
        *,
        snapshot: dict[str, Any],
        render_config: dict[str, Any],
        provenance: dict[str, Any],
        sealed_manifest_sha256: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        summary_path = output_dir / self.SUMMARY_NAME
        if not summary_path.is_file() or summary_path.is_symlink():
            raise RuntimeError("启停分析摘要缺失，无法密封本次分析记录")
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("启停分析摘要无效，无法密封本次分析记录") from exc
        if not isinstance(summary, dict):
            raise RuntimeError("启停分析摘要格式无效")
        script = provenance.get("analysis_script")
        script = script if isinstance(script, dict) else {}
        runtime = provenance.get("runtime")
        runtime = runtime if isinstance(runtime, dict) else {}
        rules = summary.get("rules")
        if not isinstance(rules, dict):
            water = summary.get("water_compensation")
            water = water if isinstance(water, dict) else {}
            model = water.get("model")
            model = model if isinstance(model, dict) else water
            reference_display_name = str(
                model.get("reference_material_display_name")
                or model.get("reference_material")
                or "NiMo-恒流-NH4-20ma-30min"
            )
            rules = {
                "potential_reference": "Hg/HgO",
                "potential_basis": "raw_measured",
                "reference_conversion_applied": False,
                "endpoint": "standard_last_1s_median; adt_last_point",
                "anomaly": "cathodic_minimum_time_lt_15s_is_abnormal",
                # Keep the display label for compatibility, but seal the
                # immutable material key and series identifier as the actual
                # scientific reference identity.
                "water_reference": reference_display_name,
                "water_reference_display_name": reference_display_name,
                "water_reference_key": str(
                    model.get("reference_material_key") or ""
                ),
                "water_reference_series_id": str(
                    model.get("reference_series_id") or ""
                ),
                "water_slope_mv_per_h": _number(
                    model.get("reference_fit_slope_mv_per_h"),
                    -6.3419003486,
                ),
            }
        config_bytes = json.dumps(
            render_config,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        summary_sha256, _ = self._hash_file(summary_path)
        return {
            "dataset_fingerprint": str(
                snapshot.get("dataset_fingerprint") or ""
            ),
            "artifact_manifest_sha256": str(sealed_manifest_sha256),
            "analysis_script_sha256": str(script.get("sha256") or ""),
            "material_config_sha256": hashlib.sha256(config_bytes).hexdigest(),
            "analysis_summary_sha256": summary_sha256,
            "rules": rules,
            "runtime": {
                **runtime,
                "analysis_workflow_version": provenance.get(
                    "analysis_workflow_version"
                ),
            },
            "result_summary": dict(result),
        }

    def _restore_published_cache_atomic(self) -> None:
        if self.analysis_dir is None:
            raise RuntimeError("尚未配置启停分析缓存目录")
        restore = getattr(self.database, "restore_current_artifacts", None)
        if not callable(restore):
            raise RuntimeError("启停数据数据库不支持恢复分析产物")
        target = self.analysis_dir
        target.parent.mkdir(parents=True, exist_ok=True)
        token = uuid.uuid4().hex
        staging = target.parent / f".{target.name}.restore-{token}"
        backup = target.parent / f".{target.name}.backup-{token}"
        moved_old = False
        try:
            restore(staging)
            if not staging.is_dir():
                raise RuntimeError("启停分析产物恢复结果无效")
            if target.exists():
                os.replace(target, backup)
                moved_old = True
            os.replace(staging, target)
            if moved_old:
                shutil.rmtree(backup, ignore_errors=True)
        except Exception:
            if moved_old and not target.exists() and backup.exists():
                os.replace(backup, target)
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            if backup.exists() and target.exists():
                shutil.rmtree(backup, ignore_errors=True)

    def _run_repository_job(
        self,
        job_id: str,
        action: str,
        script: Path,
        *,
        render_config: dict[str, Any] | None,
        render_revision: int | None,
        upload_ids: tuple[int, ...] = (),
        collection_batch_id: str = "",
    ) -> None:
        environment = os.environ.copy()
        environment["PYTHONUNBUFFERED"] = "1"
        if self.node_executable:
            environment["START_STOP_NODE_BIN"] = self.node_executable
        collection_batch_id = (
            str(collection_batch_id or uuid.uuid4().hex)
            if action == "scan"
            else ""
        )
        collection_started = False
        collection_finished = False
        collection_returncode: int | None = None
        publication_committed = False
        published_result: dict[str, Any] = {}
        export_pdf = action == "render" and self._render_export_requested(
            render_config
        )
        executed_script_sha256, executed_script_size = self._hash_file(script)
        try:
            self.scratch_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(
                prefix=f"start-stop-{job_id}-",
                dir=str(self.scratch_dir),
            ) as temporary_name:
                job_dir = Path(temporary_name)
                source_root = job_dir / "source"
                output_dir = job_dir / "output"
                collection_result_path = job_dir / "collection-result.json"
                collection_progress_path = job_dir / "collection-progress.json"
                render_progress_path = job_dir / "render-progress.json"
                result: dict[str, Any] = {}
                published_result = result
                warning = False
                post_publish_warning_code = ""

                if action == "scan":
                    # Machine identity comes from the image-owned template;
                    # user roots come from the persistent settings store.  A
                    # job-scoped effective file prevents either source from
                    # being mutated and is removed with the temporary job dir.
                    collection_config = self._write_effective_collection_config(
                        job_dir / "effective-collection-config.json"
                    )
                    collection_scratch = job_dir / "collection-scratch"
                    collection_command = [
                        self.python_executable,
                        "-m",
                        "echem_platform.start_stop_collection",
                        "--config",
                        str(collection_config),
                        "--database",
                        str(self.database.path),
                        "--result-json",
                        str(collection_result_path),
                        "--scratch",
                        str(collection_scratch),
                        "--progress-json",
                        str(collection_progress_path),
                        "--batch-id",
                        collection_batch_id,
                    ]
                    collection_started = True
                    collected = self._run_collection_process(
                        job_id,
                        collection_command,
                        cwd=Path(__file__).resolve().parents[1],
                        environment=environment,
                        progress_path=collection_progress_path,
                    )
                    collection_returncode = int(collected.returncode)
                    if collected.returncode not in {0, 1}:
                        raise RuntimeError(
                            "实验电脑数据采集未完成："
                            + self._process_error(collected)
                        )
                    collection_payload = self._read_collection_result(
                        collection_result_path
                    )
                    if str(collection_payload.get("batch_id") or "") != collection_batch_id:
                        raise RuntimeError("远程采集结果的批次编号不一致")
                    collection_finished = True
                    result.update(
                        self._public_collection_result(
                            collection_payload,
                            returncode=collected.returncode,
                        )
                    )
                    self._update_job(job_id, result=result)
                    if result["collection_roots_total"] <= 0:
                        raise RuntimeError("采集配置中没有可检查的数据来源")
                    if result["collection_roots_ok"] <= 0:
                        raise RuntimeError("所有实验电脑数据来源均未能完成检查")
                    warning = result["collection_outcome"] == "partial"
                    self._update_job(
                        job_id,
                        stage="freezing_snapshot",
                        message="远程采集完成，正在冻结数据库分析快照",
                    )
                    self._update_progress(
                        job_id,
                        phase="freezing_snapshot",
                        phase_label="固定数据库快照",
                        phase_index=5,
                        percent=68,
                        completed=4,
                        total=7,
                        unit="steps",
                        detail="远程文件已检查，正在固定本次分析数据",
                    )
                    snapshot = self.database.freeze_snapshot(
                        candidate_only=True
                    )
                    repository = self._public_repository_status(
                        self.database.repository_status()
                    )
                    result.update(
                        {
                            "repository_files": max(
                                _integer(repository.get("current_source_count")),
                                _integer(repository.get("file_count")),
                                _integer(repository.get("source_count")),
                            ),
                            "repository_bytes": max(
                                _integer(repository.get("blob_bytes")),
                                _integer(repository.get("total_bytes")),
                            ),
                            "repository_new_versions": _integer(
                                result.get("collection_copied")
                            ),
                            "repository_reused_blobs": _integer(
                                result.get("collection_unchanged_content")
                            ),
                        }
                    )
                    self._update_job(job_id, result=result)
                elif action == "prepare_upload":
                    result["uploaded_files"] = len(upload_ids)
                    self._update_job(
                        job_id,
                        stage="freezing_snapshot",
                        message="上传已入库，正在冻结数据库分析快照",
                        result=result,
                    )
                    self._update_progress(
                        job_id,
                        phase="freezing_snapshot",
                        phase_label="固定数据库快照",
                        phase_index=5,
                        percent=68,
                        completed=4,
                        total=7,
                        unit="steps",
                        detail="上传文件已入库，正在固定本次分析数据",
                    )
                    snapshot = self.database.freeze_snapshot(
                        candidate_only=True,
                        metadata={
                            "trigger": "prepare_upload",
                            "upload_ids": list(upload_ids),
                        },
                    )
                else:
                    snapshot = self.database.latest_snapshot()
                    if not snapshot:
                        raise RuntimeError("数据库中尚无可用于绘图的启停数据快照")

                snapshot_id = self._snapshot_id(snapshot)
                if snapshot_id is None:
                    raise RuntimeError("启停数据快照缺少有效标识")
                if _integer(snapshot.get("file_count")) <= 0:
                    raise RuntimeError("启停数据快照中没有可分析的数据文件")

                current_config_revision = _integer(
                    self.database.get_start_stop_config().get("revision")
                )
                if (
                    action == "scan"
                    and _integer(result.get("collection_copied")) == 0
                    and self._snapshot_has_current_artifact(
                        snapshot_id=int(snapshot_id),
                        config_revision=current_config_revision,
                        analysis_script_sha256=executed_script_sha256,
                    )
                ):
                    result["analysis_skipped_unchanged"] = 1
                    raise _UnchangedRepositoryScan(result, warning=warning)

                self._update_job(
                    job_id,
                    stage="materializing_snapshot",
                    message="正在从数据库准备本次分析数据",
                    snapshot_id=int(snapshot_id),
                )
                self._update_progress(
                    job_id,
                    phase="materializing_snapshot",
                    phase_label="准备分析数据",
                    phase_index=1 if action == "render" else 5,
                    percent=4 if action == "render" else 76,
                    completed=0 if action == "render" else 5,
                    total=1 if action == "render" else 7,
                    unit="steps",
                    current_item=(
                        "PDF 导出数据"
                        if export_pdf
                        else "数据库快照"
                        if action == "render"
                        else ""
                    ),
                    detail=(
                        "正在从数据库准备 PDF 图集所需数据和材料配置"
                        if export_pdf
                        else "正在从数据库准备本次数据和已发布材料快照"
                        if action == "render"
                        else "正在从数据库生成本次分析快照"
                    ),
                    mode="indeterminate" if action == "render" else "determinate",
                )
                source_root, snapshot_cache_hit, trusted_snapshot_manifest = (
                    self._materialize_snapshot_source(
                        snapshot,
                        int(snapshot_id),
                        source_root,
                    )
                )
                result["snapshot_cache_hits"] = int(snapshot_cache_hit)
                result["snapshot_materialized_files"] = (
                    0
                    if snapshot_cache_hit
                    else _integer(snapshot.get("file_count"))
                )
                self._update_job(job_id, result=result)
                self._update_progress(
                    job_id,
                    phase="materializing_snapshot",
                    phase_label="准备分析数据",
                    phase_index=1 if action == "render" else 5,
                    percent=6 if action == "render" else 78,
                    completed=1,
                    total=1,
                    unit="steps",
                    current_item=(
                        "已复用高速快照"
                        if snapshot_cache_hit
                        else "数据库快照已准备"
                    ),
                    detail=(
                        "数据未变化，已跳过整批 SQLite 解包"
                        if snapshot_cache_hit
                        else "本次快照已放入高速缓存，后续计算可直接复用"
                    ),
                )
                environment["START_STOP_SOURCE_ROOT"] = str(source_root)
                environment["START_STOP_OUTPUT_DIR"] = str(output_dir)
                matplotlib_cache = self.scratch_dir / ".matplotlib-cache"
                xdg_cache = self.scratch_dir / ".xdg-cache"
                for cache_path, seed_variable in (
                    (matplotlib_cache, "START_STOP_MPL_CACHE_SEED"),
                    (xdg_cache, "START_STOP_XDG_CACHE_SEED"),
                ):
                    if cache_path.exists() and (
                        cache_path.is_symlink() or not cache_path.is_dir()
                    ):
                        raise RuntimeError("启停绘图缓存目录无效")
                    seed_value = str(environment.get(seed_variable) or "").strip()
                    if not cache_path.exists() and seed_value:
                        seed_path = Path(seed_value)
                        if (
                            not seed_path.is_absolute()
                            or seed_path.is_symlink()
                            or not seed_path.is_dir()
                            or any(path.is_symlink() for path in seed_path.rglob("*"))
                        ):
                            raise RuntimeError("Docker 绘图缓存种子无效")
                        shutil.copytree(seed_path, cache_path)
                environment["MPLCONFIGDIR"] = str(matplotlib_cache)
                environment["XDG_CACHE_HOME"] = str(xdg_cache)
                if trusted_snapshot_manifest:
                    environment["START_STOP_TRUST_SNAPSHOT_MANIFEST"] = "1"
                self._seed_private_output(
                    output_dir,
                    action=action,
                    required=action == "render",
                )
                if action in {"scan", "prepare_upload"}:
                    self._synchronize_seed_workbook_from_database(
                        output_dir,
                        script=script,
                        job_dir=job_dir,
                        environment=environment,
                    )

                if action in {"scan", "prepare_upload"}:
                    self._update_job(
                        job_id,
                        stage="refreshing_material_table",
                        message=(
                            "上传快照已固定，正在更新材料列表"
                            if action == "prepare_upload"
                            else "数据库快照已固定，正在更新材料列表"
                        ),
                    )
                    self._update_progress(
                        job_id,
                        phase="refreshing_material_table",
                        phase_label="更新材料表",
                        phase_index=6,
                        percent=84,
                        completed=5,
                        total=7,
                        unit="steps",
                        detail="正在识别材料并生成最新配置表",
                        mode="indeterminate",
                    )
                    command = [
                        self.python_executable,
                        str(script),
                        "--prepare-config",
                    ]
                    config_revision = _integer(
                        self.database.get_start_stop_config().get("revision")
                    )
                else:
                    if render_config is None or render_revision is None:
                        raise RuntimeError("网页材料配置未能固定，已停止绘图")
                    private_config = job_dir / "material-config.json"
                    private_config.write_text(
                        json.dumps(
                            render_config,
                            ensure_ascii=False,
                            indent=2,
                        ),
                        encoding="utf-8",
                    )
                    command = [
                        self.python_executable,
                        str(script),
                        "--render",
                        "--material-config-json",
                        str(private_config),
                        "--progress-json",
                        str(render_progress_path),
                    ]
                    command.extend(self._render_command_options(render_config))
                    config_revision = render_revision
                    self._update_job(
                        job_id,
                        stage="rendering",
                        message=(
                            "正在生成 PDF 图集"
                            if export_pdf
                            else "正在计算并更新网页分析结果"
                        ),
                    )
                    self._update_progress(
                        job_id,
                        phase="loading_config",
                        phase_label=(
                            "启动 PDF 导出程序"
                            if export_pdf
                            else "启动网页分析程序"
                        ),
                        phase_index=2,
                        percent=7,
                        completed=0,
                        total=1,
                        unit="steps",
                        current_item="材料与绘图配置",
                        detail=(
                            "数据已准备完成，正在计算并生成两份 PDF 图集"
                            if export_pdf
                            else "数据已准备完成，正在计算网页曲线与统计结果"
                        ),
                        mode="indeterminate",
                    )
                self._update_job(
                    job_id,
                    config_revision=int(config_revision),
                )

                completed = (
                    self._run_render_process(
                        job_id,
                        command,
                        cwd=script.parent,
                        environment=environment,
                        progress_path=render_progress_path,
                    )
                    if action == "render"
                    else self._run_process(
                        command,
                        cwd=script.parent,
                        environment=environment,
                    )
                )
                if completed.returncode != 0:
                    raise RuntimeError(self._process_error(completed))
                try:
                    parsed = json.loads(completed.stdout.strip())
                except json.JSONDecodeError as exc:
                    raise RuntimeError(
                        "启停分析没有返回可验证的结果总结"
                    ) from exc
                if not isinstance(parsed, dict):
                    raise RuntimeError("启停分析结果总结格式无效")
                result.update(self._public_prepare_result(parsed))

                self._update_job(
                    job_id,
                    stage="publishing",
                    message="分析完成，正在发布数据库产物",
                    result=result,
                )
                self._update_progress(
                    job_id,
                    phase="publishing",
                    phase_label=(
                        "发布 PDF 图集"
                        if export_pdf
                        else "发布网页分析结果"
                        if action == "render"
                        else "发布更新结果"
                    ),
                    phase_index=7,
                    percent=97 if action == "render" else 94,
                    completed=0 if action == "render" else 6,
                    total=1 if action == "render" else 7,
                    unit="steps",
                    current_item=("数据库产物" if action == "render" else ""),
                    detail=(
                        "正在保存 PDF 图集并原子替换网页缓存"
                        if export_pdf
                        else "正在保存网页分析数据并原子替换网页缓存"
                        if action == "render"
                        else "正在发布材料表和数据库产物"
                    ),
                    mode="indeterminate",
                )
                self._remove_runtime_junk(output_dir)
                self._write_repository_status_artifact(output_dir)
                provenance = self._write_analysis_provenance(
                    output_dir,
                    script=script,
                    snapshot=snapshot,
                    snapshot_id=int(snapshot_id),
                    config_revision=config_revision,
                    action=action,
                    expected_script_sha256=executed_script_sha256,
                    expected_script_size=executed_script_size,
                )
                seal = seal_artifact_directory(
                    output_dir,
                    snapshot_id=int(snapshot_id),
                    config_revision=config_revision,
                    kind=action,
                    provenance=provenance,
                )
                # Refuse publication unless a second read verifies the exact
                # final tree immediately before the database transaction.
                verified_seal = validate_sealed_artifact_directory(output_dir)
                if (
                    seal.get("manifest_sha256")
                    != verified_seal.get("manifest_sha256")
                    or seal.get("checksums_sha256")
                    != verified_seal.get("checksums_sha256")
                ):
                    raise RuntimeError("启停分析产物封口复核失败")
                result["artifact_manifest_sha256"] = str(
                    verified_seal.get("manifest_sha256") or ""
                )
                result["artifact_checksum_file_sha256"] = str(
                    verified_seal.get("checksums_sha256") or ""
                )
                publisher = getattr(
                    self.database,
                    "publish_sealed_artifacts",
                    self.database.publish_artifacts,
                )
                publication_kwargs: dict[str, Any] = {}
                if self._durable_jobs_available():
                    publication_kwargs["job_id"] = job_id
                    if action == "render":
                        if render_config is None:
                            raise RuntimeError("本次绘图配置未能固定")
                        publication_kwargs["analysis_run"] = self._analysis_run_record(
                            output_dir,
                            snapshot=snapshot,
                            render_config=render_config,
                            provenance=provenance,
                            sealed_manifest_sha256=str(
                                verified_seal.get("manifest_sha256") or ""
                            ),
                            result=result,
                        )
                published = publisher(
                    output_dir,
                    snapshot_id,
                    config_revision,
                    action,
                    **publication_kwargs,
                )
                publication_committed = True
                cache_refresh_warning = False
                try:
                    self._restore_published_cache_atomic()
                except Exception:
                    # The database generation/current pointer is already the
                    # durable source of truth.  A derivative web-cache refresh
                    # failure must not rewrite that successful publication as
                    # a failed analysis job.
                    cache_refresh_warning = True
                    warning = True
                    post_publish_warning_code = "cache_refresh_failed"
                    result["cache_refresh_failed"] = True
                    self._audit_best_effort(
                        f"start_stop_{action}_cache_refresh_failed",
                        job_id,
                        "artifact generation committed; published cache refresh pending",
                    )
                if isinstance(published, dict):
                    generation_id = published.get(
                        "generation_id", published.get("id")
                    )
                    if generation_id not in {None, ""}:
                        result["artifact_generation_id"] = generation_id

            # Do not publish a terminal state until TemporaryDirectory has
            # removed every private source/output/progress file.
            audit_recorded = self._audit_best_effort(
                f"start_stop_{action}_completed",
                job_id,
                "database snapshot materialized and artifacts published",
            )
            if not audit_recorded:
                warning = True
                result["audit_record_failed"] = True
                if not post_publish_warning_code:
                    post_publish_warning_code = "post_publish_audit_failed"
            self._update_progress(
                job_id,
                phase="completed",
                phase_label=(
                    "PDF 导出完成"
                    if export_pdf
                    else "分析完成"
                    if action == "render"
                    else "更新完成"
                ),
                phase_index=7,
                percent=100,
                completed=1 if action == "render" else 7,
                total=1 if action == "render" else 7,
                unit="steps",
                current_item=(
                    "PDF 图集"
                    if export_pdf
                    else "网页分析结果"
                    if action == "render"
                    else ""
                ),
                detail=(
                    "数据已入库并更新材料表"
                    if action == "scan"
                    else "上传数据已入库并更新材料表"
                    if action == "prepare_upload"
                    else (
                        "PDF 图集已封存，网页缓存刷新待恢复"
                        if export_pdf and cache_refresh_warning
                        else "网页分析结果已封存，网页缓存刷新待恢复"
                        if cache_refresh_warning
                        else "PDF 图集已生成并发布"
                        if export_pdf
                        else "网页分析结果已生成并发布"
                    )
                ),
            )
            self._update_job(
                job_id,
                status=(
                    "completed_with_warnings" if warning else "completed"
                ),
                stage="completed",
                failure_class="partial" if warning else "none",
                failure_code=post_publish_warning_code,
                completed_utc=_utc_now(),
                message=(
                    "数据已存入数据库并更新材料表，但有未完成来源"
                    if warning
                    else "数据已存入数据库并更新材料表"
                )
                if action == "scan"
                else "上传数据已存入数据库并更新材料表"
                if action == "prepare_upload"
                else (
                    "PDF 图集已存入数据库，但网页缓存刷新失败；可重新导出恢复"
                    if export_pdf and cache_refresh_warning
                    else "网页分析结果已存入数据库，但网页缓存刷新失败；可重新分析恢复"
                    if cache_refresh_warning
                    else "PDF 图集已生成并存入数据库"
                    if export_pdf
                    else "网页分析结果已生成并存入数据库"
                ),
                result=result,
            )
        except _UnchangedRepositoryScan as unchanged:
            result = unchanged.result
            warning_code = ""
            if unchanged.warning:
                if (
                    _integer(result.get("collection_errors"))
                    or _integer(result.get("collection_roots_failed"))
                ):
                    warning_code = "remote_partial_failure"
                elif _integer(result.get("collection_changed_during_collection")):
                    warning_code = "source_changed"
                elif _integer(result.get("collection_unsettled_skipped")):
                    warning_code = "source_not_settled"
                else:
                    warning_code = "remote_partial_failure"
            self._audit_best_effort(
                "start_stop_scan_unchanged",
                job_id,
                "remote inventory completed; current sealed snapshot reused",
            )
            self._update_progress(
                job_id,
                phase="completed",
                phase_label="更新完成",
                phase_index=7,
                percent=100,
                completed=7,
                total=7,
                unit="steps",
                current_item="没有新文件",
                detail="数据库内容未变化，已跳过重复解包、计算与发布",
            )
            self._update_job(
                job_id,
                status=(
                    "completed_with_warnings"
                    if unchanged.warning
                    else "completed"
                ),
                stage="completed",
                failure_class="partial" if unchanged.warning else "none",
                failure_code=warning_code,
                completed_utc=_utc_now(),
                message=(
                    "远程检查完成；部分来源未完成，数据库无变化，未重复计算"
                    if unchanged.warning
                    else "远程检查完成，没有新文件，已跳过重复计算"
                ),
                result=result,
            )
        except subprocess.TimeoutExpired:
            if publication_committed:
                self._finish_published_with_warning(
                    job_id,
                    result=published_result,
                    message="产物已存入数据库，但发布后的状态整理未完成。",
                    failure_code="post_publish_finalization_failed",
                )
            else:
                self._finish_failed(
                    job_id,
                    "数据采集、材料表更新或绘图超时，已停止本次任务。",
                )
        except (OSError, RuntimeError, ValueError) as exc:
            message = str(exc)
            duplicate_notice = duplicate_plot_name_notice(message)
            for sensitive, replacement in (
                (str(self.scratch_dir), "[临时分析目录]"),
                (str(self.database.path), "[启停数据库]"),
                (str(script.parent), "[分析程序目录]"),
            ):
                if sensitive:
                    message = message.replace(sensitive, replacement)
            if publication_committed:
                self._finish_published_with_warning(
                    job_id,
                    result=published_result,
                    message="产物已存入数据库，但网页缓存或状态整理未完成。",
                    failure_code="post_publish_finalization_failed",
                )
            else:
                self._finish_failed(
                    job_id,
                    duplicate_notice or message,
                    failure_code=(
                        "duplicate_material_name"
                        if duplicate_notice is not None
                        else "job_failed"
                    ),
                )
        except Exception:
            # Once the generation pointer has been committed, even an
            # unexpected finalization error (for example sqlite3.Error while
            # writing the terminal progress event) must not rewrite the
            # scientifically valid published result as a failed analysis.
            if publication_committed:
                self._finish_published_with_warning(
                    job_id,
                    result=published_result,
                    message="产物已存入数据库，但发布后的状态整理未完成。",
                    failure_code="post_publish_finalization_failed",
                )
            else:
                raise
        finally:
            if collection_started and not collection_finished:
                self._recover_abandoned_collection_batch(
                    collection_batch_id,
                    job_id,
                    collection_returncode,
                )

    def _run_job_entry(
        self,
        job_id: str,
        action: str,
        script: Path,
        render_config: dict[str, Any] | None = None,
        render_revision: int | None = None,
        upload_ids: tuple[int, ...] = (),
        collection_batch_id: str = "",
    ) -> None:
        try:
            claimer = getattr(self.database, "claim_job", None)
            if callable(claimer):
                claimed = claimer(
                    job_id,
                    worker_instance_id=self._worker_instance_id,
                )
                if isinstance(claimed, dict):
                    with self._job_lock:
                        if self._job.get("id") == job_id:
                            self._job = claimed
            self._run_job(
                job_id,
                action,
                script,
                render_config,
                render_revision,
                upload_ids,
                collection_batch_id,
            )
        except Exception:
            # Keep unexpected implementation failures from leaving a task in
            # a permanent running state.  Details belong in server diagnostics,
            # never in the public job payload.
            try:
                self._finish_failed(
                    job_id,
                    "任务遇到意外错误，已停止本次执行。",
                    failure_code="unexpected_internal_error",
                )
            except Exception:
                with self._job_lock:
                    if self._job.get("id") == job_id:
                        self._job.update(
                            status="failed",
                            completed_utc=_utc_now(),
                            message="任务遇到意外错误，已停止本次执行。",
                        )
        finally:
            with self._job_lock:
                if self._active_job_id == job_id:
                    self._active_job_id = ""

    def _run_job(
        self,
        job_id: str,
        action: str,
        script: Path,
        render_config: dict[str, Any] | None = None,
        render_revision: int | None = None,
        upload_ids: tuple[int, ...] = (),
        collection_batch_id: str = "",
    ) -> None:
        export_pdf = action == "render" and self._render_export_requested(
            render_config
        )
        self._update_job(
            job_id,
            status="running",
            stage=(
                "collecting_remote"
                if action == "scan"
                else "preparing_upload"
                if action == "prepare_upload"
                else "rendering"
            ),
            started_utc=_utc_now(),
            message=(
                "正在连接实验电脑并检查新文件"
                if action == "scan"
                else "正在准备已上传的数据"
                if action == "prepare_upload"
                else "正在生成 PDF 图集"
                if export_pdf
                else "正在重新计算网页分析结果"
            ),
        )
        if action == "scan":
            machines = self._configured_progress_machines()
            first_name = str(machines[0].get("name") or "") if machines else ""
            self._update_progress(
                job_id,
                phase="connecting_remote",
                phase_label="连接实验电脑",
                phase_index=1,
                percent=0,
                completed=0,
                total=len(machines),
                unit="machines",
                current_item=first_name,
                detail="正在准备连接三台实验电脑",
                machines=machines,
                mode="indeterminate",
            )
        elif action == "prepare_upload":
            self._update_progress(
                job_id,
                phase="preparing_upload",
                phase_label="准备上传数据",
                phase_index=4,
                percent=60,
                completed=0,
                total=len(upload_ids),
                unit="files",
                detail=f"正在准备 {len(upload_ids)} 个上传文件",
                mode="indeterminate",
            )
        else:
            self._update_progress(
                job_id,
                phase="materializing_snapshot",
                phase_label="准备分析数据",
                phase_index=1,
                percent=0,
                completed=0,
                total=1,
                unit="steps",
                current_item="数据库快照",
                detail="正在固定本次数据与材料配置",
                mode="indeterminate",
            )
        if self.repository_mode:
            self._run_repository_job(
                job_id,
                action,
                script,
                render_config=render_config,
                render_revision=render_revision,
                upload_ids=upload_ids,
                collection_batch_id=collection_batch_id,
            )
            return
        environment = os.environ.copy()
        environment["PYTHONUNBUFFERED"] = "1"
        if self.node_executable:
            environment["START_STOP_NODE_BIN"] = self.node_executable
        collection_result_path: Path | None = None
        try:
            matplotlib_config_dir = self.database.path.parent / "matplotlib"
            matplotlib_config_dir.mkdir(parents=True, exist_ok=True)
            environment.setdefault("MPLCONFIGDIR", str(matplotlib_config_dir))
            result: dict[str, Any] = {}
            warning = False
            if action == "scan":
                assert self.collection_script is not None
                job_dir = self.database.path.parent / "start-stop-jobs"
                job_dir.mkdir(parents=True, exist_ok=True)
                collection_result_path = job_dir / f"collection-{job_id}.json"
                collection_result_path.unlink(missing_ok=True)
                collection_config = self._collection_config_path()
                if collection_config is None:
                    raise RuntimeError("尚未配置实验电脑数据采集配置")
                collection_command = [
                    self.python_executable,
                    str(self.collection_script),
                    "--config",
                    str(collection_config),
                    "--result-json",
                    str(collection_result_path),
                ]
                collected = self._run_process(
                    collection_command,
                    cwd=self.collection_script.parent,
                    environment=environment,
                )
                if collected.returncode not in {0, 1}:
                    raise RuntimeError(
                        "实验电脑数据采集未完成："
                        + self._process_error(collected)
                    )
                collection_payload = self._read_collection_result(
                    collection_result_path
                )
                result.update(
                    self._public_collection_result(
                        collection_payload,
                        returncode=collected.returncode,
                    )
                )
                self._update_job(job_id, result=result)
                if result["collection_roots_total"] <= 0:
                    raise RuntimeError("采集配置中没有可检查的数据来源")
                if result["collection_roots_ok"] <= 0:
                    raise RuntimeError("所有实验电脑数据来源均未能完成检查")
                warning = result["collection_outcome"] == "partial"
                self._update_job(
                    job_id,
                    stage="refreshing_material_table",
                    message="远程采集完成，正在更新材料列表",
                )
                command = [
                    self.python_executable,
                    str(script),
                    "--prepare-config",
                ]
            else:
                command = [
                    self.python_executable,
                    str(script),
                    "--render",
                    "--material-config-json",
                    str(self.config_path),
                ]
                command.extend(self._render_command_options(render_config))

            completed = self._run_process(
                command,
                cwd=self.analysis_dir,
                environment=environment,
            )
            if completed.returncode != 0:
                raise RuntimeError(self._process_error(completed))
            try:
                parsed = json.loads(completed.stdout.strip())
            except json.JSONDecodeError as exc:
                raise RuntimeError("启停分析没有返回可验证的结果总结") from exc
            if not isinstance(parsed, dict):
                raise RuntimeError("启停分析结果总结格式无效")
            result.update(self._public_prepare_result(parsed))
            self._audit(
                f"start_stop_{action}_completed",
                job_id,
                (
                    "remote collection then fixed local analysis"
                    if action == "scan"
                    else "fixed local analysis render"
                ),
            )
            self._update_job(
                job_id,
                status="completed_with_warnings" if warning else "completed",
                stage="completed",
                completed_utc=_utc_now(),
                message=(
                    "已更新材料表，但有暂缓文件或未完成来源"
                    if warning
                    else "已从实验电脑采集并更新材料表"
                )
                if action == "scan"
                else "PDF 图集已生成"
                if export_pdf
                else "网页分析结果已生成",
                result=result,
            )
        except subprocess.TimeoutExpired:
            stage = self._public_job().get("stage")
            self._finish_failed(
                job_id,
                "实验电脑数据采集超时，已停止本次任务。"
                if stage == "collecting_remote"
                else "材料表更新或绘图超时，已停止本次任务。",
            )
        except (OSError, RuntimeError) as exc:
            self._finish_failed(job_id, str(exc))
        finally:
            if collection_result_path is not None:
                collection_result_path.unlink(missing_ok=True)

    def _finish_failed(
        self,
        job_id: str,
        message: str,
        *,
        failure_code: str = "job_failed",
    ) -> None:
        stage = str(self._public_job().get("stage") or "failed")
        try:
            self._update_job(
                job_id,
                status="failed",
                stage=stage,
                failure_class="fatal",
                failure_code=failure_code,
                completed_utc=_utc_now(),
                message=message[:800],
            )
        except (KeyError, RuntimeError, ValueError):
            with self._job_lock:
                if self._job.get("id") == job_id:
                    self._job.update(
                        status="failed",
                        stage=stage,
                        failure_class="fatal",
                        failure_code=failure_code,
                        completed_utc=_utc_now(),
                        message=message[:800],
                    )
        self._audit_best_effort(
            "start_stop_job_failed",
            job_id,
            message[:500],
        )

    def pdf(self, kind: str) -> tuple[Path, str]:
        status = self.status()
        if kind not in {"standard", "water"}:
            raise StartStopWorkspaceError("PDF 类型只支持 standard 或 water。")
        if status.get("data_stale") or status.get("configuration_stale"):
            raise StartStopWorkspaceError(
                "当前分析结果已过期，请先更新网页分析，再到材料库生成 PDF 图集。",
                409,
            )
        if kind == "standard":
            path, filename = self._path(self.STANDARD_PDF), self.STANDARD_PDF
        else:
            path, filename = self._path(self.WATER_PDF), self.WATER_PDF
        if not path.is_file():
            raise StartStopWorkspaceError(
                "当前尚未生成这份 PDF，请到材料库点击“生成 PDF 图集”。",
                409,
            )
        return path, filename
