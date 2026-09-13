"""Snapshot materialization, provenance and atomic derived-artifact publication."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import uuid
from pathlib import Path, PurePosixPath
from typing import Any
from .start_stop_database import ARTIFACT_SEAL_SCHEMA_VERSION
from .start_stop_contracts import _utc_now, _bool, _number, _integer


class ArtifactPublicationMixin:
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
        if not isinstance(snapshot, dict):
            return None
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
            self._scratch_budget.require_unreserved(max(0, _integer(snapshot.get("total_bytes"))))
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
        self._scratch_budget.require_unreserved(sum(size for _relative, _sha, size in files))
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

    def _analysis_dependencies(self, script: Path) -> dict[str, Any]:
        dependencies = {}
        for name in self.ANALYSIS_DEPENDENCIES:
            path = script.parent / name
            if path.is_symlink():
                raise RuntimeError("分析依赖不能是符号链接")
            if path.is_file():
                sha, size = self._hash_file(path)
                dependencies[name] = {"sha256": sha, "size_bytes": size}
        return dependencies

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
        expected_dependencies: dict[str, Any] | None = None,
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
        dependencies = self._analysis_dependencies(script)
        if expected_dependencies is not None and dependencies != expected_dependencies:
            raise RuntimeError("分析依赖在任务执行期间发生了变化")
        for name, metadata in dependencies.items():
            shutil.copy2(script.parent / name, output_dir / name)
            sha, size = self._hash_file(output_dir / name)
            if {"sha256": sha, "size_bytes": size} != metadata:
                raise RuntimeError("分析依赖复制后校验失败")
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
            "analysis_dependencies": dependencies,
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

    def _restore_published_cache_atomic(
        self,
        *,
        preferred_kind: str | None = "render",
    ) -> None:
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
            restored = restore(staging, kind=preferred_kind)
            if restored is None and preferred_kind is not None:
                shutil.rmtree(staging, ignore_errors=True)
                restored = restore(staging)
            if restored is None:
                raise RuntimeError("启停分析数据库中没有可恢复的网页产物")
            if not staging.is_dir():
                raise RuntimeError("启停分析产物恢复结果无效")
            if target.exists():
                self._replace_cache_path_with_retry(target, backup)
                moved_old = True
            self._replace_cache_path_with_retry(staging, target)
            if moved_old:
                shutil.rmtree(backup, ignore_errors=True)
        except Exception:
            if moved_old and not target.exists() and backup.exists():
                self._replace_cache_path_with_retry(backup, target)
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            if backup.exists() and target.exists():
                shutil.rmtree(backup, ignore_errors=True)
