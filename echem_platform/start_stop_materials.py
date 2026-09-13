"""Material catalog, naming and saved analysis configuration."""
from __future__ import annotations

import datetime as dt
from typing import Any
from .start_stop_contracts import StartStopWorkspaceError, _bool, _number, _integer


class MaterialLibraryMixin:
    def _workbook_config(self, dataset_fingerprint: str, payload=None) -> dict[str, dict[str, Any]]:
        if payload is None:
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
        catalog_getter = getattr(self.source_database, "current_material_catalog", None)
        catalog = catalog_getter() if self.repository_mode and callable(catalog_getter) else {}
        snapshot = catalog.get("snapshot") or self._snapshot()
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
        config_database = self.source_database if callable(getattr(self.source_database, "get_start_stop_config", None)) else self.database
        database_config = config_database.get_start_stop_config()
        database_rows = {
            row["material_key"]: row for row in database_config.get("materials", [])
        }
        has_saved_selection = self.repository_mode and _integer(database_config.get("revision")) > 0
        workbook_rows = self._workbook_config(fingerprint, catalog.get("readback"))
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
                if has_saved_selection:
                    # Regenerated workbooks describe new sources, not a user's
                    # consent to add them to an existing comparison selection.
                    include = False
                    source = "auto"
                    saved_fingerprint = ""
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
