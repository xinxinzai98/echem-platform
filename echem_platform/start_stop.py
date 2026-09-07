from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import sqlite3
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
from .start_stop_runtime_status import RUNTIME_STATUS_NAME, read_runtime_safety
from .start_stop_resources import (
    ScratchBudget,
    estimate_output_bytes,
    obsolete_snapshot_cache,
)
from .start_stop_stability import StabilityRepositoryAnalyzer
# Re-export the established rule/error names for older integrations.
from .start_stop_contracts import (
    StartStopWorkspaceError, _UnchangedRepositoryScan, duplicate_plot_name_notice,
    _utc_now, _bool, _number, _integer, _work_step_current_ma_cm2,
    _work_step_duration_s, _work_step_number_token, _format_signed_work_step_current,
    _work_step_metadata,
)
from .start_stop_chart_export import ChartExportMixin
from .start_stop_materials import MaterialLibraryMixin
from .start_stop_queries import ChartQueryMixin
from .start_stop_job_runtime import JobRuntimeMixin
from .start_stop_publication import ArtifactPublicationMixin
from .start_stop_workflow import AnalysisWorkflowMixin


class StartStopWorkspace(
    AnalysisWorkflowMixin,
    ArtifactPublicationMixin,
    JobRuntimeMixin,
    ChartQueryMixin,
    MaterialLibraryMixin,
    ChartExportMixin,
):
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
    ANALYSIS_DEPENDENCIES = ("material_config_workbook.py", "material_result_cache.py", "water_result_cache.py", "export_existing.py")
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
    CACHE_SWAP_RETRY_DELAYS = (0.5, 1.0, 2.0, 4.0, 8.0, 15.0, 30.0)
    MAX_HIGHLIGHTED_EXPORT_SERIES = 16
    MAX_CHART_EXPORT_POINTS = 300_000
    MAX_CHART_EXPORT_BYTES = 256 * 1024 * 1024
    MAX_RAW_EXPORT_SOURCE_FILES = 128
    MAX_RAW_EXPORT_TOTAL_BYTES = 384 * 1024 * 1024
    MAX_RAW_EXPORT_FILE_BYTES = 128 * 1024 * 1024
    MAX_EXCEL_DATA_ROWS_PER_SHEET = 1_048_575
    MAX_EXCEL_CELL_TEXT = 32_767
    EXCEL_TEMP_BASE_BYTES = 64 * 1024 * 1024
    EXCEL_TEMP_BYTES_PER_RAW_ROW = 1_200
    EXCEL_TEMP_BYTES_PER_PROCESSED_POINT = 320
    EXCEL_TEMP_SAFETY_BYTES = 256 * 1024 * 1024
    PDF_EXPORT_POINTS_PER_CURVE = 12_000
    CHART_EXPORT_FORMATS = frozenset({"xlsx", "pdf"})
    CHART_EXPORT_PALETTE = (
        "#0057B8", "#E66100", "#009E73", "#7A3E9D",
        "#C58A00", "#D81B60", "#007C91", "#8C564B",
        "#A50F15", "#4D4D4D", "#6B8E23", "#56B4E9",
        "#B79F00", "#CC79A7", "#332288", "#44AA99",
    )
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
        source_database: Any | None = None,
        collection_script: str | Path | None = None,
        analysis_script: str | Path | None = None,
        collection_config: str | Path | None = None,
        collection_config_provider: Any | None = None,
        lanbts_config: str | Path | None = None,
        lanbts_channel_config: str | Path | None = None,
        scratch_dir: str | Path | None = None,
        backup_dir: str | Path | None = None,
        estimated_output_bytes: int = 0,
        repository_mode: bool = False,
        python_executable: str = "",
        node_executable: str = "",
        timeout_seconds: int = 3600,
    ):
        self.database = database
        self.source_database = source_database or database
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
        raw_lanbts_config = str(lanbts_config or "").strip()
        self.lanbts_config = (
            Path(
                os.path.expandvars(os.path.expanduser(raw_lanbts_config))
            ).resolve()
            if raw_lanbts_config
            else None
        )
        raw_lanbts_channel_config = str(lanbts_channel_config or "").strip()
        self.lanbts_channel_config = (
            Path(
                os.path.expandvars(
                    os.path.expanduser(raw_lanbts_channel_config)
                )
            ).resolve()
            if raw_lanbts_channel_config
            else None
        )
        raw_scratch = str(scratch_dir or "").strip()
        default_scratch = self.database.path.parent / "start-stop-scratch"
        self.scratch_dir = Path(
            os.path.expandvars(os.path.expanduser(raw_scratch))
        ).resolve() if raw_scratch else default_scratch.resolve()
        self._scratch_budget = ScratchBudget(self.scratch_dir)
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
        self.stability_analyzer = StabilityRepositoryAnalyzer(
            self.source_database
        )
        self._job_lock = threading.RLock()
        # A single lock also provides single-flight behaviour for cache misses:
        # concurrent identical requests perform one CSV scan, while readers of
        # cached payloads receive independent copies.
        self._chart_cache_lock = threading.RLock()
        self._chart_export_lock = threading.Lock()
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
        state_database = self.source_database if callable(getattr(self.source_database, "repository_status", None)) else self.database
        if callable(getattr(state_database, "repository_status", None)):
            try:
                direct = self._public_repository_status(
                    state_database.repository_status()
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
        if self._published_cache_matches_current_render():
            return True
        try:
            self._restore_published_cache_atomic(preferred_kind="render")
        except (OSError, RuntimeError, ValueError) as exc:
            print(
                json.dumps(
                    {
                        "event": "start_stop_startup_cache_refresh_failed",
                        "error": self._cache_refresh_error_summary(exc),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                file=sys.stderr,
                flush=True,
            )
            return False
        return self.analysis_dir.is_dir() and any(self.analysis_dir.iterdir())

    def _published_cache_marker(self) -> dict[str, Any]:
        if self.analysis_dir is None:
            return {}
        path = self.analysis_dir / ".start-stop-artifacts.json"
        try:
            if not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
                return {}
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _published_cache_matches_current_render(self) -> bool:
        marker = self._published_cache_marker()
        if marker.get("kind") != "render" or self.analysis_dir is None:
            return False
        if not (self.analysis_dir / self.SERIES_NAME).is_file():
            return False
        getter = getattr(self.database, "current_analysis_run", None)
        if not callable(getter):
            return False
        try:
            current = getter("render")
        except (OSError, RuntimeError, ValueError):
            return False
        return bool(
            isinstance(current, dict)
            and _integer(current.get("artifact_generation_id")) > 0
            and _integer(current.get("artifact_generation_id"))
            == _integer(marker.get("generation_id"))
        )

    @classmethod
    def _replace_cache_path_with_retry(cls, source: Path, target: Path) -> None:
        attempts = len(cls.CACHE_SWAP_RETRY_DELAYS) + 1
        for attempt in range(attempts):
            try:
                os.replace(source, target)
                return
            except OSError:
                if attempt >= attempts - 1:
                    raise
                time.sleep(cls.CACHE_SWAP_RETRY_DELAYS[attempt])

    @staticmethod
    def _cache_refresh_error_summary(exc: BaseException) -> str:
        values = [type(exc).__name__]
        for name in ("errno", "winerror"):
            value = getattr(exc, name, None)
            if isinstance(value, int) and not isinstance(value, bool):
                values.append(f"{name}={value}")
        return ",".join(values)[:160]

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


    def _public_job(self) -> dict[str, Any]:
        if self.source_database is not self.database:
            getter = getattr(self.source_database, "latest_job", None)
            if callable(getter):
                return getter() or {"status": "idle", "stage": "idle"}
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
        state_database = self.source_database if callable(getattr(self.source_database, "current_analysis_run", None)) else self.database
        getter = getattr(state_database, "current_analysis_run", None)
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

    def _estimated_analysis_output_bytes(self) -> int:
        getter = getattr(self.database, "analysis_storage_estimate", None)
        if not callable(getter):
            return self.estimated_output_bytes
        return estimate_output_bytes(**getter(), minimum=self.estimated_output_bytes)

    def _safety_status(
        self,
        provenance: dict[str, Any] | None = None,
        *, estimated_output_bytes: int | None = None,
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
            if self.source_database is not self.database and self.analysis_dir is not None:
                projected = read_runtime_safety(self.analysis_dir.parent / RUNTIME_STATUS_NAME)
                if projected is not None:
                    projected["provenance"] = mapped_provenance
                    return projected
                return {"storage": {}, "backup": {}, "provenance": mapped_provenance}
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
            reclaimable_scratch_bytes = 0
            descriptor = self._snapshot_cache_descriptor(snapshot_payload)
            if descriptor is not None:
                fingerprint, _files = descriptor
                reclaimable_scratch_bytes = obsolete_snapshot_cache(
                    self.scratch_dir / self.SNAPSHOT_CACHE_DIR_NAME, fingerprint
                )
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
                estimated_output_bytes=self._estimated_analysis_output_bytes() if estimated_output_bytes is None else estimated_output_bytes,
                scratch_dir=self.scratch_dir,
                cache_dir=(
                    self.analysis_dir.parent
                    if self.analysis_dir is not None
                    else self.database.path.parent
                ),
                estimated_snapshot_bytes=snapshot_bytes,
                reclaimable_scratch_bytes=reclaimable_scratch_bytes,
                output_estimate_includes_margin=True,
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
        lanbts_import_module = Path(__file__).with_name(
            "start_stop_lanbts_import.py"
        )
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
        lanbts_import_configured = bool(
            self.lanbts_config
            and self.lanbts_config.is_file()
            and lanbts_import_module.is_file()
        )
        lanbts_import_identity_ready = False
        if lanbts_import_configured and self.lanbts_config is not None:
            try:
                fixed = json.loads(self.lanbts_config.read_text(encoding="utf-8"))
                identity = Path(
                    os.path.expandvars(
                        os.path.expanduser(str(fixed.get("identity_file") or ""))
                    )
                )
                lanbts_import_identity_ready = identity.is_file()
            except (OSError, ValueError, json.JSONDecodeError, AttributeError):
                lanbts_import_identity_ready = False
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
            "lanbts_import_configured": lanbts_import_configured,
            "lanbts_import_identity_ready": lanbts_import_identity_ready,
            "lanbts_import_ready": bool(
                lanbts_import_configured
                and lanbts_import_identity_ready
                and ssh_ready
                and repository_ready
            ),
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
        snapshot_fingerprint = str(material_payload.get("dataset_fingerprint") or snapshot.get("dataset_fingerprint") or "")
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
        last_data_update_at = str(material_payload.get("generated_at") or snapshot.get("generated_at") or "")
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
        analyzed_mode = str((summary.get("render_options") or {}).get("data_mode") or "both")
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
            "analyzed_data_mode": analyzed_mode if analyzed_mode in self.RENDER_DATA_MODES else "both",
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


    def _can_export_existing(self, snapshot, revision, script_sha, mode):
        current = self._analysis_provenance_status()
        if not (
            current.get("state") == "sealed"
            and current.get("snapshot_id") == self._snapshot_id(snapshot)
            and current.get("config_revision") == revision
            and current.get("analysis_script_sha256") == script_sha
            and self._published_cache_matches_current_render()
        ):
            return False
        script = self._analysis_script_path()
        dependencies = self._analysis_dependencies(script) if script else {}
        if self._read_json(self.PROVENANCE_NAME, optional=True).get("analysis_dependencies", {}) != dependencies:
            return False
        schema = self._read_json("analysis_table_schema.json", optional=True)
        required = {"series_summary_raw.csv", "material_summary_raw.csv", "segment_summary_raw.csv"}
        if mode in {"raw", "both"}:
            required.update({"overview_downsampled_raw.csv", "cycle_summary_raw.csv", "representative_cycles_raw.csv", "segment_boundaries.csv"})
        if mode in {"water", "both"}:
            required.update({"water_compensation/cycle_summary_water_compensated.csv", "water_compensation/series_summary_water_compensated.csv"})
            if not self._path("water_compensation/water_compensation_model.json").is_file():
                return False
        return required.issubset(schema) and all(self._path(name).is_file() for name in required)

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
                storage = self._safety_status(
                    {"state": "none"},
                    estimated_output_bytes=self._estimated_analysis_output_bytes() if action == "render" else 32 * 1024 * 1024,
                ).get("storage")
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
                if self.repository_mode and self._durable_jobs_available() and not export_pdf and _integer(material_payload.get("revision")) == 0:
                    # A first render must seal the actual displayed defaults as a
                    # real revision, not refer to a nonexistent revision zero.
                    material_payload = self.save_materials(
                        dataset_fingerprint=material_payload["dataset_fingerprint"], expected_revision=0,
                        materials=[{key: row.get(key) for key in (
                            "key", "plot_name", "include_in_summary_atlas", "favorite", "notes"
                        )} for row in material_payload["materials"]],
                    )
                updated_material_keys = self._changed_material_keys(
                    material_payload,
                    self._last_render_material_fingerprints(),
                )
                if (
                    normalized_render_material_scope == "updated"
                    and not updated_material_keys
                    and not self.repository_mode
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
                if export_pdf and self.repository_mode and not self._can_export_existing(
                    self.database.latest_snapshot(), render_revision, self._hash_file(script)[0],
                    normalized_render_data_mode,
                ):
                    raise StartStopWorkspaceError(
                        "当前分析结果尚未就绪、已过期或缺少所选数据模式。请先更新平台分析，再导出 PDF；导出不会自动重算数据。",
                        409,
                    )
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
                    **({"render_data_mode": normalized_render_data_mode} if action == "render" else {}),
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
