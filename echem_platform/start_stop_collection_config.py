from __future__ import annotations

import json
import re
import threading
import unicodedata
from pathlib import Path
from typing import Any


CONFIG_VERSION = 1
MAX_PATHS_PER_MACHINE = 32
MAX_PATH_BYTES = 1024
MAX_LABEL_BYTES = 160

_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:\\")
_WINDOWS_INVALID = re.compile(r'[\x00-\x1f\x7f<>:"|?*]')

# Reading a whole drive or Windows' operating-system trees is never a sensible
# electrochemistry collection root.  Rejecting them also bounds mistakes made
# while editing the configuration in a browser.
_SYSTEM_TREE_NAMES = frozenset(
    {
        "windows",
        "program files",
        "program files (x86)",
        "programdata",
        "$recycle.bin",
        "system volume information",
        "boot",
        "recovery",
    }
)
_DANGEROUS_EXACT_NAMES = frozenset({"users", "documents and settings"})
_WINDOWS_RESERVED_DEVICE = re.compile(
    r"^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?$", re.IGNORECASE
)


class CollectionConfigError(ValueError):
    """A saved collection-location configuration is invalid."""


class CollectionConfigConflict(CollectionConfigError):
    """The optimistic configuration revision no longer matches."""


def normalize_windows_directory(value: Any) -> str:
    """Validate and normalize a local Windows absolute directory.

    UNC/device paths, drive roots, parent traversal, wildcard syntax and common
    operating-system trees are deliberately rejected.  The resulting value is
    only ever passed to a fixed read-only PowerShell script as base64 data.
    """

    if not isinstance(value, str):
        raise CollectionConfigError("搜索位置必须是 Windows 绝对目录。")
    original = unicodedata.normalize("NFC", value)
    if original != original.strip():
        raise CollectionConfigError("搜索位置首尾不能包含空白字符。")
    raw = original.replace("/", "\\")
    if not raw or len(raw.encode("utf-8")) > MAX_PATH_BYTES:
        raise CollectionConfigError("搜索位置为空或过长。")
    if raw.startswith("\\") or not _WINDOWS_ABSOLUTE.match(raw):
        raise CollectionConfigError("搜索位置必须是带盘符的 Windows 本地绝对目录。")
    # The colon after the drive letter is the only colon Windows permits.
    if _WINDOWS_INVALID.search(raw[2:]):
        raise CollectionConfigError("搜索位置包含 Windows 目录不允许的字符。")
    drive = raw[:2].upper()
    tail = re.sub(r"\\+", r"\\", raw[2:])
    pieces = [part for part in tail.split("\\") if part]
    if not pieces:
        raise CollectionConfigError("不能把整个盘符作为搜索位置。")
    if any(part in {".", ".."} for part in pieces):
        raise CollectionConfigError("搜索位置不能包含 . 或 .. 路径段。")
    if any(part.endswith((".", " ")) for part in pieces):
        raise CollectionConfigError("搜索位置的目录名不能以点或空格结尾。")
    if any(_WINDOWS_RESERVED_DEVICE.fullmatch(part) for part in pieces):
        raise CollectionConfigError("搜索位置不能包含 Windows 保留设备名。")
    normalized = drive + "\\" + "\\".join(pieces)
    folded_parts = [part.casefold() for part in pieces]
    if folded_parts[0] in _SYSTEM_TREE_NAMES or (
        len(folded_parts) == 1 and folded_parts[0] in _DANGEROUS_EXACT_NAMES
    ):
        raise CollectionConfigError("不能把 Windows 系统目录作为搜索位置。")
    return normalized


def _label(value: Any, path: str) -> str:
    if value is None or (isinstance(value, str) and not value.strip()):
        result = path.rsplit("\\", 1)[-1]
    elif isinstance(value, str):
        result = unicodedata.normalize("NFC", value).strip()
    else:
        raise CollectionConfigError("搜索位置标签必须是文字。")
    if (
        not result
        or len(result.encode("utf-8")) > MAX_LABEL_BYTES
        or result in {".", ".."}
        or any(ord(character) < 32 or ord(character) == 127 for character in result)
        or any(character in result for character in "/\\")
    ):
        raise CollectionConfigError("搜索位置标签为空、过长或包含不允许的字符。")
    return result


class CollectionConfigStore:
    """Persist user-controlled roots as revisions in the repository database.

    Fixed machine identity, SSH users and key paths are always loaded from the
    image-owned base file.  The writable state contains only labels, Windows
    paths and enabled flags.
    """

    def __init__(self, base_config: str | Path, database: Any) -> None:
        self.base_config = Path(base_config)
        self.database = database
        self._lock = threading.RLock()
        self._base = self._read_base()
        if not all(
            callable(getattr(database, method, None))
            for method in ("get_collection_config", "save_collection_config")
        ):
            raise CollectionConfigError(
                "启停数据库不支持搜索位置版本配置。"
            )
        # Validate an existing revision eagerly.  Revision zero intentionally
        # means the image defaults have not yet been changed by the user.
        self._read_state()

    def _read_base(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.base_config.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CollectionConfigError("实验电脑基础配置不可用。") from exc
        machines = payload.get("machines") if isinstance(payload, dict) else None
        if not isinstance(machines, list) or len(machines) != 3:
            raise CollectionConfigError("实验电脑基础配置必须包含固定的三台机器。")
        seen: set[str] = set()
        for machine in machines:
            if not isinstance(machine, dict):
                raise CollectionConfigError("实验电脑基础配置无效。")
            machine_id = machine.get("id")
            if (
                not isinstance(machine_id, str)
                or not machine_id.strip()
                or len(machine_id.encode("utf-8")) > 256
                or any(ord(character) < 32 or ord(character) == 127 for character in machine_id)
                or machine_id in seen
                or not all(
                    isinstance(machine.get(field), str) and machine.get(field)
                    for field in ("name", "hostname", "ip", "user", "identity_file")
                )
            ):
                raise CollectionConfigError("实验电脑固定身份配置无效。")
            seen.add(machine_id)
        return payload

    @property
    def machine_ids(self) -> tuple[str, ...]:
        return tuple(str(machine["id"]) for machine in self._base["machines"])

    def fixed_config(self) -> dict[str, Any]:
        """Return a fresh fixed-identity config for connectivity probes."""
        return json.loads(json.dumps(self._base, ensure_ascii=False))

    def _default_state(self) -> dict[str, Any]:
        machines: list[dict[str, Any]] = []
        for machine in self._base["machines"]:
            paths: list[dict[str, Any]] = []
            for root in machine.get("roots", []):
                if not isinstance(root, dict):
                    continue
                path = normalize_windows_directory(root.get("remote_path"))
                paths.append(
                    {
                        "label": _label(root.get("label"), path),
                        "path": path,
                        "enabled": True,
                    }
                )
            machines.append({"id": str(machine["id"]), "paths": paths})
        return {
            "version": CONFIG_VERSION,
            "revision": 0,
            "updated_utc": "",
            "machines": machines,
        }

    def _validate_machine_rows(self, rows: Any) -> list[dict[str, Any]]:
        if not isinstance(rows, list) or len(rows) != len(self.machine_ids):
            raise CollectionConfigError("搜索位置配置必须完整包含固定的实验电脑。")
        expected = set(self.machine_ids)
        seen: set[str] = set()
        normalized_rows: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"id", "paths"}:
                raise CollectionConfigError("每台实验电脑只能提交 id 和 paths。")
            machine_id = row.get("id")
            if not isinstance(machine_id, str) or machine_id not in expected or machine_id in seen:
                raise CollectionConfigError("实验电脑 ID 不在固定配置中或重复。")
            seen.add(machine_id)
            paths = row.get("paths")
            if not isinstance(paths, list) or len(paths) > MAX_PATHS_PER_MACHINE:
                raise CollectionConfigError(
                    f"每台实验电脑最多配置 {MAX_PATHS_PER_MACHINE} 个搜索位置。"
                )
            normalized_paths: list[dict[str, Any]] = []
            path_keys: list[str] = []
            labels: set[str] = set()
            for raw_path in paths:
                if not isinstance(raw_path, dict) or not set(raw_path).issubset(
                    {"label", "path", "enabled"}
                ) or "path" not in raw_path:
                    raise CollectionConfigError(
                        "搜索位置只能提交 label、path 和 enabled。"
                    )
                path = normalize_windows_directory(raw_path.get("path"))
                label = _label(raw_path.get("label"), path)
                enabled = raw_path.get("enabled", True)
                if not isinstance(enabled, bool):
                    raise CollectionConfigError("enabled 必须是 true 或 false。")
                path_key = path.casefold()
                label_key = label.casefold()
                if path_key in path_keys:
                    raise CollectionConfigError("同一台实验电脑的搜索位置不能重复。")
                if label_key in labels:
                    raise CollectionConfigError("同一台实验电脑的搜索位置标签不能重复。")
                for previous in path_keys:
                    if path_key.startswith(previous + "\\") or previous.startswith(
                        path_key + "\\"
                    ):
                        raise CollectionConfigError(
                            "同一台实验电脑的搜索位置不能相互包含。"
                        )
                path_keys.append(path_key)
                labels.add(label_key)
                normalized_paths.append(
                    {"label": label, "path": path, "enabled": enabled}
                )
            normalized_rows.append({"id": machine_id, "paths": normalized_paths})
            if not any(item["enabled"] for item in normalized_paths):
                raise CollectionConfigError(
                    "每台实验电脑至少需要一个已启用的搜索位置。"
                )
        if seen != expected:
            raise CollectionConfigError("搜索位置配置缺少固定实验电脑。")
        by_id = {row["id"]: row for row in normalized_rows}
        return [by_id[machine_id] for machine_id in self.machine_ids]

    def _read_state(self) -> dict[str, Any]:
        try:
            payload = self.database.get_collection_config()
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
            raise CollectionConfigError("已保存的搜索位置配置无法读取。") from exc
        if not isinstance(payload, dict):
            raise CollectionConfigError("已保存的搜索位置配置格式无效。")
        revision = payload.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise CollectionConfigError("已保存的搜索位置修订号无效。")
        if revision == 0:
            if payload.get("machines") not in (None, []):
                raise CollectionConfigError("未初始化的搜索位置配置格式无效。")
            return self._default_state()
        updated = payload.get("updated_utc")
        if not isinstance(updated, str) or not updated:
            raise CollectionConfigError("已保存的搜索位置更新时间无效。")
        return {
            "revision": revision,
            "updated_utc": updated,
            "machines": self._validate_machine_rows(payload.get("machines")),
        }

    def snapshot(self, *, include_paths: bool = True, can_edit: bool = True) -> dict[str, Any]:
        with self._lock:
            state = self._read_state()
            state_by_id = {row["id"]: row for row in state["machines"]}
            machines: list[dict[str, Any]] = []
            for fixed in self._base["machines"]:
                row = state_by_id[str(fixed["id"])]
                public = {
                    "id": str(fixed["id"]),
                    "name": str(fixed["name"]),
                    "hostname": str(fixed["hostname"]),
                    "ip": str(fixed["ip"]),
                    "path_count": len(row["paths"]),
                    "enabled_path_count": sum(
                        1 for item in row["paths"] if item["enabled"]
                    ),
                }
                if include_paths:
                    public["paths"] = [dict(item) for item in row["paths"]]
                machines.append(public)
            return {
                "revision": state["revision"],
                "updated_utc": state["updated_utc"],
                "can_edit": bool(can_edit),
                "machines": machines,
            }

    def save(self, *, expected_revision: Any, machines: Any) -> dict[str, Any]:
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
            raise CollectionConfigError("expected_revision 必须是整数。")
        normalized = self._validate_machine_rows(machines)
        with self._lock:
            current = self._read_state()
            if expected_revision != current["revision"]:
                raise CollectionConfigConflict(
                    "搜索位置配置已被更新，请刷新后重试。"
                )
            try:
                self.database.save_collection_config(
                    expected_revision=current["revision"],
                    machines=normalized,
                )
            except ValueError as exc:
                if "更新" in str(exc) or "revision" in str(exc).casefold():
                    raise CollectionConfigConflict(str(exc)) from exc
                raise CollectionConfigError(str(exc)) from exc
            return self.snapshot(include_paths=True, can_edit=True)

    def effective_config(self) -> dict[str, Any]:
        """Merge enabled roots into fixed identities without mutating the base."""
        with self._lock:
            state = self._read_state()
            state_by_id = {row["id"]: row for row in state["machines"]}
            effective = {
                key: json.loads(json.dumps(value, ensure_ascii=False))
                for key, value in self._base.items()
                if key != "machines"
            }
            effective["collection_config_revision"] = int(state["revision"])
            machines: list[dict[str, Any]] = []
            for fixed in self._base["machines"]:
                saved = state_by_id[str(fixed["id"])]
                base_metadata: dict[str, dict[str, Any]] = {}
                for root in fixed.get("roots", []):
                    if not isinstance(root, dict):
                        continue
                    try:
                        key = normalize_windows_directory(root.get("remote_path")).casefold()
                    except CollectionConfigError:
                        continue
                    base_metadata[key] = {
                        "exclude_directories": list(root.get("exclude_directories", [])),
                        "recursive": bool(root.get("recursive", True)),
                    }
                roots: list[dict[str, Any]] = []
                for item in saved["paths"]:
                    if not item["enabled"]:
                        continue
                    metadata = base_metadata.get(item["path"].casefold(), {})
                    roots.append(
                        {
                            "label": item["label"],
                            "remote_path": item["path"],
                            "exclude_directories": list(
                                metadata.get("exclude_directories", [])
                            ),
                            "recursive": bool(metadata.get("recursive", True)),
                        }
                    )
                if not roots:
                    continue
                machine = json.loads(json.dumps(fixed, ensure_ascii=False))
                machine["roots"] = roots
                machines.append(machine)
            effective["machines"] = machines
            return effective


__all__ = [
    "CONFIG_VERSION",
    "MAX_LABEL_BYTES",
    "MAX_PATHS_PER_MACHINE",
    "MAX_PATH_BYTES",
    "CollectionConfigConflict",
    "CollectionConfigError",
    "CollectionConfigStore",
    "normalize_windows_directory",
]
