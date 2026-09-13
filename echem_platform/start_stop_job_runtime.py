"""Worker subprocess lifecycle, sanitized results and progress transitions."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any
from .start_stop_contracts import _number, _integer


class JobRuntimeMixin:
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
    def _read_lanbts_import_result(path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("蓝博数据导入没有生成可验证的结果总结") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("status") not in {"completed", "partial", "failed"}
            or not isinstance(payload.get("totals"), dict)
        ):
            raise RuntimeError("蓝博数据导入结果总结格式无效")
        return payload

    @staticmethod
    def _public_lanbts_import_result(payload: dict[str, Any]) -> dict[str, Any]:
        totals = payload["totals"]

        def count(key: str) -> int:
            try:
                return max(0, int(totals.get(key, 0)))
            except (TypeError, ValueError):
                return 0

        return {
            "lanbts_import_outcome": str(payload.get("status") or "failed"),
            "lanbts_inventoried": count("inventoried"),
            "lanbts_stable": count("stable"),
            "lanbts_unsettled_skipped": count("unsettled_skipped"),
            "lanbts_raw_downloaded": count("raw_downloaded"),
            "lanbts_raw_ingested": count("raw_ingested"),
            "lanbts_derived_ingested": count("derived_ingested"),
            "lanbts_derived_reused": count("derived_reused"),
            "lanbts_start_stop": count("start_stop"),
            "lanbts_constant_current": count("constant_current"),
            "lanbts_known_failure_skipped": count("known_failure_skipped"),
            "lanbts_import_errors": count("errors"),
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
            "importing_lanbts": "importing_lanbts",
        }
        stage = stage_by_phase.get(child_phase)
        if stage is None:
            return
        child_percent = _number(payload.get("percent"), 0) or 0
        mapped_percent = (
            min(76, 64 + max(0, child_percent) * 0.12)
            if child_phase == "importing_lanbts"
            else min(64, max(0, child_percent) * 0.64)
        )
        child_phase_index = max(0, _integer(payload.get("phase_index")))
        phase_index_by_phase = {
            "connecting_remote": 1,
            "scanning_remote": 3 if child_phase_index >= 3 else 2,
            "downloading_remote": 3,
            "storing_database": 3,
            "collection_complete": 4,
            "importing_lanbts": 5,
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
