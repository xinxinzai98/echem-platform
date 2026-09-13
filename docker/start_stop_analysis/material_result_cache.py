"""Exact, non-executable per-series calculation cache.

The source repository remains authoritative. Corrupt/old caches are misses;
numeric arrays use NPY with pickle disabled and metadata carries checksums.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sys
import uuid
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

SCHEMA = 1
MAX_CACHE_BYTES = 1024 * 1024 * 1024
MAX_ROWS = 20_000_000
MAX_ENTRY_DISK_BYTES = 64 * 1024 * 1024
MAX_DISK_CACHE_BYTES = 512 * 1024 * 1024
LABEL_FIELDS = (
    "series_id", "series_order", "series_display_name", "material_id",
    "material_display_name", "material_relative_path", "workstation",
    "test_type", "test_type_label_zh", "is_primary_series", "is_special_series",
    "special_file_name", "include_in_summary_atlas", "material_user_notes",
)


def verified_profile_records(source_root, snapshot, trusted_rows):
    """Reuse the sealed scan's profiles only when bound to the exact source manifest."""
    if not trusted_rows or not isinstance(snapshot, dict) or not isinstance(snapshot.get("files"), list):
        return None
    trusted = {row["path"]: row for row in trusted_rows}
    records = []
    for item in snapshot["files"]:
        relative = str(item.get("relative_path") or "")
        expected = trusted.get(relative)
        if not expected or item.get("sha256") != expected.get("sha256"):
            return None
        path = source_root.joinpath(*Path(relative).parts)
        if path.is_symlink() or not path.is_file() or source_root.resolve() not in path.resolve().parents:
            return None
        if path.stat().st_size != expected["size_bytes"] or item.get("file_size_bytes") != expected["size_bytes"]:
            return None
        record = dict(item)
        record["absolute_path"] = str(path)
        record["_mtime_epoch"] = path.stat().st_mtime
        records.append(record)
    required = {row["path"] for row in trusted_rows if row.get("candidate_kind") in {"gal_square_wave", "adt_script_method"}}
    if not required.issubset({row["relative_path"] for row in records}):
        return None
    return records or None


def identity(spec, algorithm):
    semantic = {
        "schema": SCHEMA, "algorithm": algorithm,
        "material": spec["material_relative_path"],
        "test_type": spec["test_type"],
        "primary": bool(spec["is_primary_series"]),
        "records": [{key: row.get(key) for key in ("relative_path", "sha256", "test_type", "file_name_kind")}
                    for row in spec["records"]],
    }
    encoded = json.dumps(semantic, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def relabel(frames, spec):
    for frame in frames:
        for key in LABEL_FIELDS:
            if key in frame.columns and key in spec:
                frame[key] = spec[key]
    return frames


def algorithm_fingerprint(*paths, parameters=None):
    """Bind caches to calculation code, numerical runtimes and effective settings."""
    payload = {"schema": SCHEMA, "python": list(sys.version_info[:3]), "numpy": np.__version__, "pandas": pd.__version__,
               "files": [hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in paths],
               "parameters": parameters or {}}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def prune_computed_cache(root, max_bytes=MAX_DISK_CACHE_BYTES):
    """Bound only disposable files in the three cache-owned directories."""
    root = Path(root)
    if root.is_symlink():
        return
    entries = []
    for directory in (root, root / "water", root / "water" / "materials"):
        if directory.is_symlink() or not directory.is_dir() or (root / "water").is_symlink():
            continue
        try:
            for path in directory.iterdir():
                if re.fullmatch(r"[0-9a-f]{64}\.(zip|json)", path.name) and path.is_file() and not path.is_symlink():
                    stat = path.stat()
                    entries.append((stat.st_mtime_ns, stat.st_size, path))
        except OSError:
            continue
    total = sum(size for _, size, _ in entries)
    for _, size, path in sorted(entries):
        if total <= max_bytes:
            break
        try:
            path.unlink()
            total -= size
        except OSError:
            continue


class _OversizedCacheEntry(Exception):
    pass


class MaterialResultCache:
    def __init__(self, root: Path, algorithm: str, *, frame_count=6):
        self.root = root
        self.algorithm = algorithm
        self.frame_count = frame_count

    def load(self, spec):
        key = identity(spec, self.algorithm)
        path = self.root / (key + ".zip")
        if self.root.is_symlink() or not path.is_file() or path.is_symlink():
            return None
        try:
            with zipfile.ZipFile(path) as archive:
                entries = archive.infolist()
                if len(entries) > 1500 or sum(item.file_size for item in entries) > MAX_CACHE_BYTES:
                    return None
                info = archive.getinfo("manifest.json")
                if info.file_size > 1024 * 1024:
                    return None
                manifest = json.loads(archive.read(info))
                if manifest.get("key") != key or manifest.get("schema") != SCHEMA or len(manifest.get("frames", [])) != self.frame_count:
                    return None
                frames = []
                for frame_spec in manifest["frames"]:
                    count = frame_spec["rows"]
                    if type(count) is not int or not 0 <= count <= MAX_ROWS:
                        return None
                    columns = {}
                    for column in frame_spec["columns"]:
                        member = column["member"]
                        if not re.fullmatch(r"f\d+-c\d+\.(npy|json)", member):
                            return None
                        content = archive.read(member)
                        if hashlib.sha256(content).hexdigest() != column["sha256"]:
                            return None
                        if member.endswith(".npy"):
                            stream = io.BytesIO(content)
                            version = np.lib.format.read_magic(stream)
                            if version != (1, 0):
                                return None
                            shape, _order, dtype = np.lib.format.read_array_header_1_0(stream)
                            if shape != (count,) or dtype.kind not in "biuf" or stream.tell() + count * dtype.itemsize != len(content):
                                return None
                            stream.seek(0)
                            values = np.load(stream, allow_pickle=False)
                        else:
                            values = json.loads(content)
                            if not isinstance(values, list) or len(values) != count:
                                return None
                        columns[column["name"]] = pd.Series(values, dtype=column["dtype"])
                    frames.append(pd.DataFrame(columns))
                return relabel(frames, spec)
        except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile, EOFError):
            return None

    def store(self, spec, frames):
        if self.root.is_symlink():
            raise ValueError("计算缓存不能是符号链接")
        key = identity(spec, self.algorithm)
        target = self.root / (key + ".zip")
        temporary = self.root / ("." + key + "." + uuid.uuid4().hex + ".tmp")
        manifest = {"schema": SCHEMA, "key": key, "frames": []}
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(temporary, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
                for frame_index, frame in enumerate(frames):
                    description = {"rows": len(frame), "columns": []}
                    for column_index, name in enumerate(frame.columns):
                        series = frame[name]
                        numeric = series.dtype.kind in "biuf"
                        member = f"f{frame_index}-c{column_index}." + ("npy" if numeric else "json")
                        if numeric:
                            buffer = io.BytesIO()
                            np.save(buffer, series.to_numpy(), allow_pickle=False)
                            content = buffer.getvalue()
                        else:
                            content = json.dumps(series.tolist(), ensure_ascii=False, allow_nan=True).encode()
                        archive.writestr(member, content)
                        if archive.fp.tell() > MAX_ENTRY_DISK_BYTES:
                            raise _OversizedCacheEntry()
                        description["columns"].append({"name": name, "dtype": str(series.dtype), "member": member,
                                                        "sha256": hashlib.sha256(content).hexdigest()})
                    manifest["frames"].append(description)
                archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
            os.replace(temporary, target)
            return True
        except (OSError, _OversizedCacheEntry):
            # Preserve valid calculations if the optional cache is full.
            return False
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
