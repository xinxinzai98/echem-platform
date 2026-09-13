from __future__ import annotations

import concurrent.futures
import datetime as dt
import hashlib
import json
import math
import ntpath
import os
import re
import tempfile
import threading
import unicodedata
import uuid
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

from .start_stop_collection import CollectionError, SSHWindowsTransport
from .start_stop_live_analysis import analyze_live_start_stop_rows


LIVE_PREVIEW_INTERVAL_MINUTES = 5
LIVE_PREVIEW_POLL_SECONDS = 5.0
LIVE_PREVIEW_MAX_POINTS = 2400
LIVE_PREVIEW_MAX_ANALYSIS_OVERVIEW_POINTS = 1200
LIVE_PREVIEW_MAX_ANALYSIS_CYCLE_POINTS = 800
LIVE_PREVIEW_MAX_SOURCES = 6
LIVE_PREVIEW_CACHE_SCHEMA_VERSION = 1
LIVE_PREVIEW_MAX_CACHE_BYTES = 8 * 1024 * 1024
_ACTIVE_FILE_MARKERS = ("qiting", "启停", "adt")
_active_scheduler_keys: set[str] = set()
_active_scheduler_lock = threading.Lock()


class LivePreviewError(RuntimeError):
    """A public, path-free live-preview failure."""


def _unavailable_cache_state() -> dict[str, Any]:
    return {
        "available": False,
        "revision": 0,
        "enabled": False,
        "interval_minutes": LIVE_PREVIEW_INTERVAL_MINUTES,
        "next_run_utc": "",
        "last_attempt_utc": "",
        "last_started_utc": "",
        "last_finished_utc": "",
        "last_completed_utc": "",
        "last_status": "unavailable",
        "phase": "unavailable",
        "message": "服务器尚未发布实时预览状态",
        "items_completed": 0,
        "items_total": 0,
        "preview": {"schema_version": 1, "items": [], "errors": []},
    }


class LivePreviewStateFile:
    """Atomic shared cache for the isolated LAN read-only container."""

    def __init__(self, path: str | Path | None) -> None:
        raw = str(path or "").strip()
        self.path = Path(raw).expanduser().absolute() if raw else None

    def snapshot(self) -> dict[str, Any]:
        path = self.path
        if path is None:
            return _unavailable_cache_state()
        try:
            metadata = path.stat()
            if (
                not path.is_file()
                or path.is_symlink()
                or metadata.st_size > LIVE_PREVIEW_MAX_CACHE_BYTES
            ):
                return _unavailable_cache_state()
            wrapper = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return _unavailable_cache_state()
        if (
            not isinstance(wrapper, dict)
            or wrapper.get("schema_version") != LIVE_PREVIEW_CACHE_SCHEMA_VERSION
            or not isinstance(wrapper.get("payload"), dict)
        ):
            return _unavailable_cache_state()
        payload = dict(wrapper["payload"])
        preview = payload.get("preview")
        if not isinstance(preview, dict) or not isinstance(preview.get("items"), list):
            return _unavailable_cache_state()
        payload["available"] = True
        payload["interval_minutes"] = LIVE_PREVIEW_INTERVAL_MINUTES
        return payload

    def write(self, payload: Mapping[str, Any]) -> None:
        path = self.path
        if path is None:
            return
        public_payload = dict(payload)
        public_payload.pop("can_edit", None)
        data = json.dumps(
            {
                "schema_version": LIVE_PREVIEW_CACHE_SCHEMA_VERSION,
                "payload": public_payload,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(data) > LIVE_PREVIEW_MAX_CACHE_BYTES:
            raise LivePreviewError("实时预览共享缓存超过安全上限")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
            try:
                descriptor = os.open(
                    temporary,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_CLOEXEC", 0),
                    0o600,
                )
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
                try:
                    os.chmod(path, 0o600)
                except OSError:
                    pass
            finally:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
        except OSError as exc:
            raise LivePreviewError("实时预览共享缓存无法写入") from exc


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _utc_text(value: dt.datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("live preview clock must return a timezone-aware datetime")
    return value.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def _parse_utc(value: Any) -> dt.datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(dt.timezone.utc)


def _safe_text(value: Any, *, limit: int = 240) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or ""))
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _is_start_stop_file(file_name: Any) -> bool:
    folded = unicodedata.normalize("NFC", str(file_name or "")).casefold()
    return any(marker in folded for marker in _ACTIVE_FILE_MARKERS)


def _downsample_rows(
    rows: list[tuple[float, float, float]],
    *,
    max_points: int = LIVE_PREVIEW_MAX_POINTS,
) -> list[tuple[float, float, float]]:
    """Min/max bucket reduction that retains spikes and phase transitions."""
    limit = max(32, int(max_points))
    if len(rows) <= limit:
        return rows
    bucket_count = max(1, (limit - 2) // 2)
    interior = len(rows) - 2
    selected = {0, len(rows) - 1}
    for bucket in range(bucket_count):
        start = 1 + (interior * bucket) // bucket_count
        end = 1 + (interior * (bucket + 1)) // bucket_count
        if end <= start:
            continue
        indices = range(start, end)
        selected.add(min(indices, key=lambda index: rows[index][1]))
        selected.add(max(indices, key=lambda index: rows[index][1]))
    ordered = sorted(selected)
    if len(ordered) > limit:
        step = (len(ordered) - 1) / (limit - 1)
        ordered = sorted({ordered[round(index * step)] for index in range(limit)})
    return [rows[index] for index in ordered]


def parse_live_preview_file(
    path: str | Path,
    *,
    max_points: int = LIVE_PREVIEW_MAX_POINTS,
) -> dict[str, Any]:
    """Parse only complete tabular rows from one fixed activity snapshot."""
    rows, skipped_partial_tail = _read_live_preview_rows(path)
    current_counts: Counter[float] = Counter(
        round(current, 6) for _elapsed, _potential, current in rows
    )
    sampled = _downsample_rows(rows, max_points=max_points)
    last_elapsed, last_potential, last_current = rows[-1]
    current_levels = [
        value
        for value, count in sorted(current_counts.items())
        if count >= 3
    ][:12]
    return {
        "complete_row_count": len(rows),
        "skipped_partial_tail": skipped_partial_tail,
        "time_start_s": rows[0][0],
        "time_end_s": last_elapsed,
        "last_potential_v": last_potential,
        "last_current_a_cm2": last_current,
        "current_levels_a_cm2": current_levels,
        "phase": "阴极段" if last_current < -0.005 else "恢复段",
        "points": [
            [round(elapsed, 5), round(potential, 8), round(current, 8)]
            for elapsed, potential, current in sampled
        ],
    }


def _read_live_preview_rows(
    path: str | Path,
) -> tuple[list[tuple[float, float, float]], bool]:
    source = Path(path)
    rows: list[tuple[float, float, float]] = []
    header_found = False
    skipped_partial_tail = False
    try:
        with source.open("rb") as handle:
            for raw_line in handle:
                if not raw_line.endswith(b"\n"):
                    skipped_partial_tail = bool(raw_line.strip())
                    continue
                stripped = raw_line.strip().lstrip(b"\xef\xbb\xbf")
                if not stripped:
                    continue
                if b"E(V)" in stripped and b"T(s)" in stripped:
                    header_found = True
                    continue
                fields = stripped.split(b"\t")
                if len(fields) < 3:
                    continue
                try:
                    potential = float(fields[0].decode("ascii"))
                    current = float(fields[1].decode("ascii"))
                    elapsed = float(fields[2].decode("ascii"))
                except (UnicodeDecodeError, ValueError):
                    continue
                if not all(math.isfinite(value) for value in (potential, current, elapsed)):
                    continue
                rows.append((elapsed, potential, current))
    except OSError as exc:
        raise LivePreviewError("活动文件临时快照无法读取") from exc
    if not header_found:
        raise LivePreviewError("活动文件尚未写出可识别的数据表头")
    if not rows:
        raise LivePreviewError("活动文件尚未写出完整数值行")
    return rows, skipped_partial_tail


def _configured_machines(config: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    machines = config.get("machines")
    if not isinstance(machines, list):
        raise LivePreviewError("实验电脑搜索配置当前不可用")
    return {
        str(machine.get("id") or ""): dict(machine)
        for machine in machines
        if isinstance(machine, Mapping) and str(machine.get("id") or "")
    }


def build_live_preview_sources(
    monitor_payload: Mapping[str, Any],
    config: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Resolve active public monitor rows to private, validated remote paths."""
    configured = _configured_machines(config)
    observed = monitor_payload.get("machines")
    if not isinstance(observed, list):
        raise LivePreviewError("工作站监控尚未完成首次识别")
    sources: list[dict[str, Any]] = []
    seen: set[str] = set()
    for public_machine in observed:
        if not isinstance(public_machine, Mapping):
            continue
        machine_id = str(public_machine.get("id") or "")
        machine = configured.get(machine_id)
        if machine is None:
            continue
        roots = {
            unicodedata.normalize("NFC", str(root.get("label") or "")).casefold(): dict(root)
            for root in machine.get("roots", [])
            if isinstance(root, Mapping) and str(root.get("label") or "")
        }
        activities = public_machine.get("material_activities")
        if not isinstance(activities, list):
            continue
        for activity in activities:
            if not isinstance(activity, Mapping) or activity.get("activity_status") != "active":
                continue
            file_name = str(activity.get("current_file") or "")
            if not _is_start_stop_file(file_name):
                continue
            if not file_name or file_name != ntpath.basename(file_name):
                continue
            root_label = unicodedata.normalize(
                "NFC", str(activity.get("root_label") or "")
            ).strip()
            root = roots.get(root_label.casefold())
            if root is None:
                continue
            relative_folder = str(activity.get("relative_folder") or "").replace("\\", "/")
            relative = PurePosixPath(relative_folder, file_name)
            if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
                continue
            remote_root = str(root.get("remote_path") or "").strip()
            if not remote_root:
                continue
            remote_path = ntpath.join(remote_root, *relative.parts)
            identity = f"{machine_id}/{root_label}/{relative.as_posix()}"
            identity_key = unicodedata.normalize("NFC", identity).casefold()
            if identity_key in seen:
                continue
            seen.add(identity_key)
            source_id = hashlib.sha256(identity_key.encode("utf-8")).hexdigest()[:20]
            task = activity.get("task")
            sources.append(
                {
                    "source_id": source_id,
                    "machine": machine,
                    "machine_id": machine_id,
                    "machine_name": _safe_text(public_machine.get("name"), limit=80),
                    "hostname": _safe_text(public_machine.get("hostname"), limit=80),
                    "material_key": _safe_text(activity.get("material_key"), limit=320),
                    "display_name": _safe_text(activity.get("display_name"), limit=180),
                    "favorite": bool(activity.get("favorite")),
                    "file_name": _safe_text(file_name, limit=180),
                    "task": dict(task) if isinstance(task, Mapping) else {},
                    "remote_path": remote_path,
                }
            )
    sources.sort(
        key=lambda item: (
            item["machine_name"].casefold(),
            item["display_name"].casefold(),
            item["source_id"],
        )
    )
    return sources[:LIVE_PREVIEW_MAX_SOURCES]


class LivePreviewScheduler:
    """Five-minute read-only active-file snapshots and downsampled curves."""

    def __init__(
        self,
        database: Any,
        workstation_monitor: Any,
        config_provider: Callable[[], Mapping[str, Any]],
        scratch_root: str | Path,
        *,
        transport: Any | None = None,
        state_file: LivePreviewStateFile | str | Path | None = None,
        formal_context_provider: Callable[[], Mapping[str, Any]] | None = None,
        now: Callable[[], dt.datetime] = _utc_now,
        poll_seconds: float = LIVE_PREVIEW_POLL_SECONDS,
    ) -> None:
        required = (
            "get_live_preview_config",
            "save_live_preview_config",
            "ensure_live_preview_schedule",
            "record_live_preview_started",
            "record_live_preview_progress",
            "record_live_preview_finished",
            "record_live_preview_failed",
        )
        if any(not callable(getattr(database, name, None)) for name in required):
            raise TypeError("database does not support live preview scheduling")
        if not callable(getattr(workstation_monitor, "snapshot", None)):
            raise TypeError("workstation monitor does not support snapshots")
        if not callable(config_provider):
            raise TypeError("live preview config provider must be callable")
        self.database = database
        self.workstation_monitor = workstation_monitor
        self.config_provider = config_provider
        self.scratch_root = Path(scratch_root)
        self.transport = transport or SSHWindowsTransport()
        self.formal_context_provider = formal_context_provider
        self.state_file = (
            state_file
            if isinstance(state_file, LivePreviewStateFile)
            else LivePreviewStateFile(state_file)
        )
        self._now = now
        self.poll_seconds = max(0.05, float(poll_seconds))
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None
        database_path = getattr(database, "path", None)
        self._singleton_key = (
            str(database_path.resolve())
            if database_path is not None and callable(getattr(database_path, "resolve", None))
            else f"database-object:{id(database)}"
        )
        self._owns_singleton_key = False

    def snapshot(self) -> dict[str, Any]:
        return self.database.get_live_preview_config()

    def _publish(self, state: Mapping[str, Any]) -> None:
        self.state_file.write(state)

    def save_config(self, *, expected_revision: int, enabled: bool) -> dict[str, Any]:
        saved = self.database.save_live_preview_config(
            expected_revision=expected_revision,
            enabled=enabled,
            now_utc=_utc_text(self._now()),
        )
        self._publish(saved)
        self._wake_event.set()
        return saved

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        with _active_scheduler_lock:
            if self._singleton_key in _active_scheduler_keys:
                raise RuntimeError("a live preview scheduler is already active")
            _active_scheduler_keys.add(self._singleton_key)
            self._owns_singleton_key = True
        self._stop_event.clear()
        self._wake_event.clear()
        self._publish(self.snapshot())
        self._thread = threading.Thread(
            target=self._run_loop,
            name="start-stop-live-preview",
            daemon=True,
        )
        try:
            self._thread.start()
        except BaseException:
            self._release_singleton_key()
            self._thread = None
            raise

    def close(self, timeout: float = 2.0) -> None:
        self._stop_event.set()
        self._wake_event.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(max(0.0, float(timeout)))
        if thread is None or not thread.is_alive():
            self._thread = None
            self._release_singleton_key()

    def _release_singleton_key(self) -> None:
        if not self._owns_singleton_key:
            return
        with _active_scheduler_lock:
            _active_scheduler_keys.discard(self._singleton_key)
        self._owns_singleton_key = False

    def _run_loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                try:
                    self.run_once()
                except Exception:
                    pass
                self._wake_event.wait(self.poll_seconds)
                self._wake_event.clear()
        finally:
            self._release_singleton_key()

    def _capture_source(
        self,
        source: Mapping[str, Any],
        destination: Path,
        formal_context: Mapping[str, Any],
    ) -> dict[str, Any]:
        fetch = getattr(self.transport, "fetch_file_snapshot", None)
        if not callable(fetch):
            raise LivePreviewError("SSH 下载器不支持活动文件快照")
        metadata = fetch(source["machine"], source["remote_path"], destination)
        parsed = parse_live_preview_file(destination)
        rows, _skipped_partial_tail = _read_live_preview_rows(destination)
        logical_source_file = PurePosixPath(
            str(source.get("material_key") or ""),
            str(source.get("file_name") or ""),
        ).as_posix()
        try:
            analysis = analyze_live_start_stop_rows(
                rows,
                task=(
                    source.get("task")
                    if isinstance(source.get("task"), Mapping)
                    else {}
                ),
                file_name=str(source.get("file_name") or ""),
                material_key=str(source.get("material_key") or ""),
                logical_source_file=logical_source_file,
                formal_context=formal_context,
                max_overview_points=LIVE_PREVIEW_MAX_ANALYSIS_OVERVIEW_POINTS,
                max_cycle_points=LIVE_PREVIEW_MAX_ANALYSIS_CYCLE_POINTS,
            )
        except ValueError as exc:
            raise LivePreviewError(_safe_text(exc, limit=180)) from exc
        captured = _utc_text(self._now())
        return {
            "source_id": source["source_id"],
            "machine_id": source["machine_id"],
            "machine_name": source["machine_name"],
            "hostname": source["hostname"],
            "material_key": source["material_key"],
            "display_name": source["display_name"],
            "favorite": source["favorite"],
            "file_name": source["file_name"],
            "task": source["task"],
            "captured_at_utc": captured,
            "source_modified_utc": _safe_text(metadata.get("source_modified_utc"), limit=80),
            "source_size_bytes": max(0, int(metadata.get("size") or 0)),
            "grew_during_snapshot": bool(metadata.get("grew_during_snapshot")),
            "potential_basis": "Hg/HgO 原始电位",
            "potential_unit": "V vs Hg/HgO",
            "analysis": analysis,
            **parsed,
        }

    @staticmethod
    def _safe_capture_error(error: Exception) -> str:
        if isinstance(error, CollectionError):
            return _safe_text(error, limit=180)
        if isinstance(error, LivePreviewError):
            return _safe_text(error, limit=180)
        return "活动文件预览处理失败"

    def _collect_preview(self) -> tuple[dict[str, Any], bool]:
        monitor_payload = self.workstation_monitor.snapshot()
        if not isinstance(monitor_payload, Mapping):
            raise LivePreviewError("工作站监控当前不可用")
        monitor_status = str(monitor_payload.get("status") or "")
        if monitor_status in {"unavailable", "initializing"}:
            raise LivePreviewError("工作站监控尚未完成活动文件识别")
        config = self.config_provider()
        if not isinstance(config, Mapping):
            raise LivePreviewError("实验电脑搜索配置当前不可用")
        sources = build_live_preview_sources(monitor_payload, config)
        progress = self.database.record_live_preview_progress(
            phase="downloading",
            message=(
                f"已识别 {len(sources)} 个活动启停文件，正在下载只读快照"
                if sources
                else "当前没有识别到活动启停文件"
            ),
            items_completed=0,
            items_total=len(sources),
        )
        self._publish(progress)
        if not sources:
            return (
                {
                    "schema_version": 1,
                    "generated_at_utc": _utc_text(self._now()),
                    "items": [],
                    "errors": [],
                    "monitor_status": monitor_status,
                },
                False,
            )

        formal_context: Mapping[str, Any] = {}
        if callable(self.formal_context_provider):
            try:
                candidate = self.formal_context_provider()
                if isinstance(candidate, Mapping):
                    formal_context = candidate
            except Exception:
                # A missing/stale published analysis must not block the
                # read-only active-file snapshot; it simply starts at t=0.
                formal_context = {}

        items: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        self.scratch_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="start-stop-live-preview-",
            dir=str(self.scratch_root),
        ) as temporary:
            root = Path(temporary)
            workers = min(3, len(sources))
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(
                        self._capture_source,
                        source,
                        root / f"{source['source_id']}.snapshot",
                        formal_context,
                    ): source
                    for source in sources
                }
                completed = 0
                for future in concurrent.futures.as_completed(futures):
                    source = futures[future]
                    try:
                        items.append(future.result())
                    except Exception as exc:
                        errors.append(
                            {
                                "source_id": source["source_id"],
                                "machine_name": source["machine_name"],
                                "display_name": source["display_name"],
                                "message": self._safe_capture_error(exc),
                            }
                        )
                    completed += 1
                    progress = self.database.record_live_preview_progress(
                        phase="downloading",
                        message=f"已处理 {completed}/{len(sources)} 个活动文件快照",
                        items_completed=completed,
                        items_total=len(sources),
                    )
                    self._publish(progress)
        items.sort(
            key=lambda item: (
                str(item.get("machine_name") or "").casefold(),
                str(item.get("display_name") or "").casefold(),
                str(item.get("source_id") or ""),
            )
        )
        errors.sort(
            key=lambda item: (
                str(item.get("machine_name") or "").casefold(),
                str(item.get("display_name") or "").casefold(),
            )
        )
        progress = self.database.record_live_preview_progress(
            phase="plotting",
            message=f"正在整理 {len(items)} 条正式启停规则计算结果",
            items_completed=len(sources),
            items_total=len(sources),
        )
        self._publish(progress)
        return (
            {
                "schema_version": 1,
                "generated_at_utc": _utc_text(self._now()),
                "items": items,
                "errors": errors,
                "monitor_status": monitor_status,
            },
            bool(errors),
        )

    def run_once(self) -> dict[str, Any]:
        now = self._now()
        now_text = _utc_text(now)
        state = self.database.ensure_live_preview_schedule(now_utc=now_text)
        if not bool(state.get("enabled")):
            return self.snapshot()
        due = _parse_utc(state.get("next_run_utc"))
        if due is None or due > now.astimezone(dt.timezone.utc):
            return self.snapshot()
        started = self.database.record_live_preview_started(now_utc=now_text)
        self._publish(started)
        try:
            payload, warning = self._collect_preview()
            count = len(payload.get("items", []))
            errors = len(payload.get("errors", []))
            message = (
                f"实时预览已更新：{count} 条曲线，{errors} 项未完成"
                if warning
                else f"实时预览已更新：{count} 条曲线"
            )
            finished = self.database.record_live_preview_finished(
                status="completed_with_warnings" if warning else "completed",
                payload=payload,
                message=message,
                now_utc=_utc_text(self._now()),
            )
            self._publish(finished)
        except Exception as exc:
            failed = self.database.record_live_preview_failed(
                message=self._safe_capture_error(exc),
                now_utc=_utc_text(self._now()),
            )
            self._publish(failed)
        return self.snapshot()

    def __enter__(self) -> "LivePreviewScheduler":
        self.start()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()


__all__ = [
    "LIVE_PREVIEW_INTERVAL_MINUTES",
    "LIVE_PREVIEW_MAX_ANALYSIS_CYCLE_POINTS",
    "LIVE_PREVIEW_MAX_ANALYSIS_OVERVIEW_POINTS",
    "LIVE_PREVIEW_MAX_POINTS",
    "LIVE_PREVIEW_MAX_SOURCES",
    "LivePreviewError",
    "LivePreviewScheduler",
    "LivePreviewStateFile",
    "build_live_preview_sources",
    "parse_live_preview_file",
]
