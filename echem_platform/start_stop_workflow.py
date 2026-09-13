"""Collection, upload and analysis task orchestration."""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any
from .start_stop_resources import ScratchSpaceError, TASK_MARGIN_BYTES, obsolete_snapshot_cache
from .start_stop_database import seal_artifact_directory, validate_sealed_artifact_directory
from .start_stop_contracts import _UnchangedRepositoryScan, duplicate_plot_name_notice, _utc_now, _integer


class AnalysisWorkflowMixin:
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
        export_existing = False
        executed_script_sha256, executed_script_size = self._hash_file(script)
        executed_dependencies = self._analysis_dependencies(script)
        try:
            self.scratch_dir.mkdir(parents=True, exist_ok=True)
            reserved_output = self._estimated_analysis_output_bytes() if action == "render" else 32 * 1024 * 1024
            latest_for_budget = self._snapshot_cache_descriptor(self.database.latest_snapshot())
            if latest_for_budget is not None:
                obsolete_snapshot_cache(self.scratch_dir / self.SNAPSHOT_CACHE_DIR_NAME, latest_for_budget[0], remove=True)
            with self._scratch_budget.reserve(f"analysis-{job_id}", reserved_output + TASK_MARGIN_BYTES), tempfile.TemporaryDirectory(
                prefix=f"start-stop-{job_id}-",
                dir=str(self.scratch_dir),
            ) as temporary_name:
                job_dir = Path(temporary_name)
                environment["TMPDIR"] = str(job_dir)
                environment["TMP"] = str(job_dir)
                environment["TEMP"] = str(job_dir)
                source_root = job_dir / "source"
                output_dir = job_dir / "output"
                collection_result_path = job_dir / "collection-result.json"
                lanbts_result_path = job_dir / "lanbts-import-result.json"
                lanbts_progress_path = job_dir / "lanbts-import-progress.json"
                collection_progress_path = job_dir / "collection-progress.json"
                render_progress_path = job_dir / "render-progress.json"
                result: dict[str, Any] = (
                    {"render_data_mode": (render_config or {}).get("render_options", {}).get("data_mode", "both")}
                    if action == "render" else {}
                )
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
                    if self.lanbts_config and self.lanbts_config.is_file():
                        self._update_job(
                            job_id,
                            stage="importing_lanbts",
                            message="正在导入蓝博稳定性数据",
                        )
                        self._update_progress(
                            job_id,
                            phase="importing_lanbts",
                            phase_label="导入蓝博稳定性数据",
                            phase_index=5,
                            percent=66,
                            completed=4,
                            total=7,
                            unit="steps",
                            current_item="稳定 BTS 文件",
                            detail="正在保存原始 BTS，并按实测电流分类为启停或恒流",
                            mode="indeterminate",
                        )
                        lanbts_command = [
                            self.python_executable,
                            "-m",
                            "echem_platform.start_stop_lanbts_import",
                            "--config",
                            str(self.lanbts_config),
                            "--database",
                            str(self.database.path),
                            "--result-json",
                            str(lanbts_result_path),
                            "--progress-json",
                            str(lanbts_progress_path),
                            "--scratch",
                            str(job_dir / "lanbts-import-scratch"),
                            "--settle-seconds",
                            "300",
                        ]
                        if self.lanbts_channel_config is not None:
                            lanbts_command.extend(
                                [
                                    "--channel-config",
                                    str(self.lanbts_channel_config),
                                ]
                            )
                        lanbts_completed = self._run_collection_process(
                            job_id,
                            lanbts_command,
                            cwd=Path(__file__).resolve().parents[1],
                            environment=environment,
                            progress_path=lanbts_progress_path,
                        )
                        if lanbts_completed.returncode in {0, 1}:
                            lanbts_payload = self._read_lanbts_import_result(
                                lanbts_result_path
                            )
                            lanbts_public = self._public_lanbts_import_result(
                                lanbts_payload
                            )
                        else:
                            lanbts_public = {
                                "lanbts_import_outcome": "failed",
                                "lanbts_import_errors": 1,
                            }
                            print(
                                "蓝博稳定性数据导入失败："
                                + self._process_error(lanbts_completed),
                                file=sys.stderr,
                                flush=True,
                            )
                        result.update(lanbts_public)
                        derived_changes = _integer(
                            result.get("lanbts_derived_ingested")
                        )
                        result["collection_copied"] = _integer(
                            result.get("collection_copied")
                        ) + derived_changes
                        result["collection_ingested"] = _integer(
                            result.get("collection_ingested")
                        ) + _integer(result.get("lanbts_raw_ingested")) + derived_changes
                        lanbts_warning = (
                            result.get("lanbts_import_outcome") != "completed"
                            or _integer(result.get("lanbts_import_errors")) > 0
                        )
                        if lanbts_warning:
                            warning = True
                            result["collection_outcome"] = "partial"
                            result["collection_errors"] = _integer(
                                result.get("collection_errors")
                            ) + _integer(result.get("lanbts_import_errors"))
                        self._update_job(job_id, result=result)
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
                if export_pdf:
                    mode = (render_config or {}).get("render_options", {}).get("data_mode", "both")
                    export_existing = self._can_export_existing(snapshot, render_revision, executed_script_sha256, mode)
                    if not export_existing:
                        raise RuntimeError("已完成分析在导出准备期间发生变化，请先更新分析再导出 PDF")

                current_config_revision = _integer(
                    self.database.get_start_stop_config().get("revision")
                )
                if action == "render":
                    self._scratch_budget.resize(f"analysis-{job_id}", self._estimated_analysis_output_bytes() + TASK_MARGIN_BYTES)
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
                if export_existing:
                    source_root.mkdir(parents=True, exist_ok=True)
                    snapshot_cache_hit, trusted_snapshot_manifest = True, False
                    result["analysis_reused"] = 1
                else:
                    source_root, snapshot_cache_hit, trusted_snapshot_manifest = (
                        self._materialize_snapshot_source(snapshot, int(snapshot_id), source_root)
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
                        "复用已完成分析"
                        if export_existing
                        else
                        "已复用高速快照"
                        if snapshot_cache_hit
                        else "数据库快照已准备"
                    ),
                    detail=(
                        "直接读取封存的分析表；不解包原始文件，也不重新计算"
                        if export_existing
                        else
                        "数据未变化，已跳过整批 SQLite 解包"
                        if snapshot_cache_hit
                        else "本次快照已放入高速缓存，后续计算可直接复用"
                    ),
                )
                environment["START_STOP_SOURCE_ROOT"] = str(source_root)
                environment["START_STOP_COMPUTED_CACHE_DIR"] = str(self.database.path.parent / "computed-cache")
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
                if export_existing:
                    restored = self.database.restore_current_artifacts(output_dir, kind="render")
                    if not restored or _integer(restored.get("snapshot_id")) != snapshot_id or _integer(restored.get("config_revision")) != render_revision:
                        raise RuntimeError("已有分析在导出准备期间已更新，请重新导出")
                    summary_path = output_dir / self.SUMMARY_NAME
                    summary = json.loads(summary_path.read_text(encoding="utf-8"))
                    summary["export_source_generation_id"] = _integer(restored.get("generation_id"))
                    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
                else:
                    self._seed_private_output(output_dir, action=action, required=action == "render")
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
                    if export_existing:
                        command = [self.python_executable, str(script), "--export-existing",
                                   "--data-mode", mode, "--progress-json", str(render_progress_path)]
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
                            "从已有分析表生成所选 PDF；不重复解析、计算或拟合补偿模型"
                            if export_existing
                            else
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
                    expected_dependencies=executed_dependencies,
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
                    # A scan updates the immutable repository and material
                    # catalog, but must not replace a readable render cache
                    # with its small scan-only artifact package. A render
                    # always advances the web cache to the new generation.
                    if (
                        action == "render"
                        or not self._published_cache_matches_current_render()
                    ):
                        self._restore_published_cache_atomic(
                            preferred_kind="render"
                        )
                except Exception as exc:
                    # The database generation/current pointer is already the
                    # durable source of truth.  A derivative web-cache refresh
                    # failure must not rewrite that successful publication as
                    # a failed analysis job.
                    cache_refresh_warning = True
                    warning = True
                    post_publish_warning_code = "cache_refresh_failed"
                    result["cache_refresh_failed"] = True
                    cache_error = self._cache_refresh_error_summary(exc)
                    print(
                        json.dumps(
                            {
                                "event": f"start_stop_{action}_cache_refresh_failed",
                                "error": cache_error,
                            },
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        file=sys.stderr,
                        flush=True,
                    )
                    self._audit_best_effort(
                        f"start_stop_{action}_cache_refresh_failed",
                        job_id,
                        "artifact generation committed; published cache refresh pending; "
                        + cache_error,
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
                        "scratch_space_insufficient"
                        if isinstance(exc, ScratchSpaceError)
                        else "duplicate_material_name"
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
            logging.getLogger(__name__).exception("Unexpected start-stop worker failure (job=%s)", job_id)
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
