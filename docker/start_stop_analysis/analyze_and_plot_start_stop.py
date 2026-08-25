from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.metadata
import json
import math
import os
import re
import subprocess
import sys
import textwrap
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple


RENDER_PROGRESS_PATH: Path | None = None
_FIGURE_PROGRESS: dict = {}
EXPORT_STATIC_FIGURES = False
ANALYSIS_WORKFLOW_VERSION = "start-stop-analysis/3"


def configure_render_progress(path: Path | None) -> None:
    global RENDER_PROGRESS_PATH
    RENDER_PROGRESS_PATH = path.resolve() if path is not None else None
    _FIGURE_PROGRESS.clear()


def configure_static_figure_export(enabled: bool) -> None:
    global EXPORT_STATIC_FIGURES
    EXPORT_STATIC_FIGURES = bool(enabled)


def report_render_progress(
    *,
    phase: str,
    phase_label: str,
    phase_index: int,
    percent: float,
    completed: int | None = None,
    total: int | None = None,
    unit: str = "items",
    current_item: str = "",
    detail: str = "",
) -> None:
    """Write optional render progress without contaminating stdout JSON."""
    if RENDER_PROGRESS_PATH is None:
        return
    payload = {
        "phase": phase,
        "phase_label": phase_label,
        "phase_index": int(phase_index),
        "phase_count": 7,
        "mode": "determinate",
        "percent": max(0.0, min(100.0, float(percent))),
        "completed": None if completed is None else max(0, int(completed)),
        "total": None if total is None else max(0, int(total)),
        "unit": unit,
        "current_item": str(current_item),
        "detail": str(detail),
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    path = RENDER_PROGRESS_PATH
    temporary = path.with_name(".{}.{}.tmp".format(path.name, os.getpid()))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except OSError:
        # Progress reporting is advisory; it must never invalidate an analysis.
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def begin_figure_progress(
    *,
    phase: str,
    phase_label: str,
    phase_index: int,
    total: int,
    percent_start: float,
    percent_end: float,
    detail: str,
) -> None:
    _FIGURE_PROGRESS.clear()
    _FIGURE_PROGRESS.update(
        {
            "phase": phase,
            "phase_label": phase_label,
            "phase_index": phase_index,
            "total": max(1, int(total)),
            "completed": 0,
            "percent_start": float(percent_start),
            "percent_end": float(percent_end),
            "detail": detail,
        }
    )
    report_render_progress(
        phase=phase,
        phase_label=phase_label,
        phase_index=phase_index,
        percent=percent_start,
        completed=0,
        total=max(1, int(total)),
        unit="pages",
        current_item="准备第 1 页",
        detail=detail,
    )


def report_figure_progress(stem: Path, *, completed: bool) -> None:
    if not _FIGURE_PROGRESS:
        return
    total = int(_FIGURE_PROGRESS["total"])
    done = int(_FIGURE_PROGRESS["completed"])
    if completed:
        done = min(total, done + 1)
        _FIGURE_PROGRESS["completed"] = done
    fraction = done / total
    percent = float(_FIGURE_PROGRESS["percent_start"]) + fraction * (
        float(_FIGURE_PROGRESS["percent_end"])
        - float(_FIGURE_PROGRESS["percent_start"])
    )
    page_number = min(total, done + (0 if completed else 1))
    label = stem.name.replace("_", " ")
    report_render_progress(
        phase=str(_FIGURE_PROGRESS["phase"]),
        phase_label=str(_FIGURE_PROGRESS["phase_label"]),
        phase_index=int(_FIGURE_PROGRESS["phase_index"]),
        percent=percent,
        completed=done,
        total=total,
        unit="pages",
        current_item="第 {}／{} 页 · {}".format(page_number, total, label),
        detail=str(_FIGURE_PROGRESS["detail"]),
    )


DEFAULT_PROJECT_ROOT = Path("/Users/hive/Desktop/20260729 反向电流测试")
PROJECT_ROOT = Path(
    os.environ.get("START_STOP_PROJECT_ROOT", str(DEFAULT_PROJECT_ROOT))
).expanduser()
SOURCE_ROOT = Path(
    os.environ.get("START_STOP_SOURCE_ROOT", str(PROJECT_ROOT / "原始数据"))
).expanduser()
COLLECTION_RECORDS_DIR = SOURCE_ROOT / "_采集记录"
COLLECTION_MANIFEST = COLLECTION_RECORDS_DIR / "文件清单.jsonl"
COLLECTION_LOCK = COLLECTION_RECORDS_DIR / ".collection.lock"
OUTPUT_DIR = Path(
    os.environ.get(
        "START_STOP_OUTPUT_DIR",
        str(PROJECT_ROOT / "启停数据_原始电位_全量分析_20260729"),
    )
).expanduser()
FIGURE_DIR = OUTPUT_DIR / "figures"
DETAIL_FIGURE_DIR = FIGURE_DIR / "by_series"
WATER_COMP_DIR = OUTPUT_DIR / "water_compensation"
WATER_COMP_FIGURE_DIR = WATER_COMP_DIR / "figures"
WATER_COMP_DETAIL_DIR = WATER_COMP_FIGURE_DIR / "by_series"
MPL_CONFIG_DIR = OUTPUT_DIR / ".matplotlib"
THREAD_ID = "019fa91d-602a-79a2-8637-6e9a3f699f66"
CONFIG_OUTPUT_DIR = OUTPUT_DIR / "outputs" / THREAD_ID
CONFIG_WORKBOOK = CONFIG_OUTPUT_DIR / "启停绘图材料配置.xlsx"
CONFIG_SNAPSHOT_JSON = OUTPUT_DIR / "material_config_snapshot.json"
CONFIG_READBACK_JSON = OUTPUT_DIR / ".material_config_readback.json"
WORKBOOK_BUILDER_SETTING = os.environ.get(
    "START_STOP_WORKBOOK_BUILDER",
    "material_config_workbook.mjs",
)
WORKBOOK_BUILDER = Path(WORKBOOK_BUILDER_SETTING).expanduser()
if not WORKBOOK_BUILDER.is_absolute():
    WORKBOOK_BUILDER = OUTPUT_DIR / WORKBOOK_BUILDER
NODE_BIN = Path(
    os.environ.get(
        "START_STOP_NODE_BIN",
        "/Users/hive/.cache/codex-runtimes/codex-primary-runtime/"
        "dependencies/node/bin/node",
    )
)

for directory in (
    OUTPUT_DIR,
    FIGURE_DIR,
    DETAIL_FIGURE_DIR,
    WATER_COMP_DIR,
    WATER_COMP_FIGURE_DIR,
    WATER_COMP_DETAIL_DIR,
    MPL_CONFIG_DIR,
    CONFIG_OUTPUT_DIR,
):
    directory.mkdir(parents=True, exist_ok=True)

os.environ.setdefault("MPLCONFIGDIR", str(MPL_CONFIG_DIR))

import numpy as np
import pandas as pd

MATPLOTLIB_VERSION = importlib.metadata.version("matplotlib")
mpl = None
plt = None
PdfPages = None
FontProperties = None
fontManager = None
Line2D = None
Circle = None
PercentFormatter = None


def ensure_matplotlib() -> None:
    """Load the plotting stack only for an explicit PDF/static export."""
    global mpl, plt, PdfPages, FontProperties, fontManager
    global Line2D, Circle, PercentFormatter
    if mpl is not None:
        return
    import matplotlib as loaded_mpl

    loaded_mpl.use("Agg")
    import matplotlib.pyplot as loaded_plt
    from matplotlib.backends.backend_pdf import PdfPages as LoadedPdfPages
    from matplotlib.font_manager import FontProperties as LoadedFontProperties
    from matplotlib.font_manager import fontManager as loaded_font_manager
    from matplotlib.lines import Line2D as LoadedLine2D
    from matplotlib.patches import Circle as LoadedCircle
    from matplotlib.ticker import PercentFormatter as LoadedPercentFormatter

    mpl = loaded_mpl
    plt = loaded_plt
    PdfPages = LoadedPdfPages
    FontProperties = LoadedFontProperties
    fontManager = loaded_font_manager
    Line2D = LoadedLine2D
    Circle = LoadedCircle
    PercentFormatter = LoadedPercentFormatter


# Preserve the instrument's measured potential basis without reference
# conversion.  Water-level compensation is applied to this raw Hg/HgO basis.
TEMPERATURE_C = 25.0
ELECTROLYTE = "1 M KOH"
ELECTROLYTE_PH = 14.0
POTENTIAL_REFERENCE = "Hg/HgO"
RAW_POTENTIAL_OFFSET_V = 0.0
IR_CORRECTION_APPLIED = False

CATHODIC_CURRENT_A_CM2 = -0.30
REVERSE_CURRENT_A_CM2 = 0.03
CATHODIC_PHASE_DURATION_S = 30.0
STEADY_WINDOW_S = 1.0
ANOMALY_BOUNDARY_S = 15.0
BASELINE_CYCLES = 10
ENDPOINT_CYCLES = 10
MIN_STEADY_POINTS = 8
MIN_PHASE_POINTS = 240
MIN_COMPLETE_PHASE_ELAPSED_S = 29.0
ADT_CATHODIC_THRESHOLD_A_CM2 = -0.10
ADT_MIN_STEADY_POINTS = 1
ADT_MIN_PHASE_POINTS = 12
ADT_MIN_COMPLETE_PHASE_ELAPSED_S = 25.0
VARIABLE_MIN_STEADY_POINTS = 8
VARIABLE_MIN_PHASE_POINTS = 80
VARIABLE_MIN_COMPLETE_PHASE_ELAPSED_S = 9.0
CURRENT_ATOL = 1e-6
TIME_ATOL_S = 1e-6
OVERVIEW_INTERVAL_S = 5.0

EXPLICIT_START_STOP_FILE_KINDS = frozenset(
    {
        "base",
        "timestamp_continuation",
        "confirmed_continuation",
        "special_marked",
    }
)

DEFAULT_WATER_COMP_REFERENCE_KEY = (
    "03_制备室_A-9_192.168.110.164/"
    "桌面_XY电镀工艺/NiMo-恒流-NH4-20ma-30min"
)
WATER_COMP_REFERENCE_KEY_ENV = "START_STOP_WATER_COMP_REFERENCE_KEY"
WATER_COMP_REFERENCE_KEY = os.environ.get(
    WATER_COMP_REFERENCE_KEY_ENV,
    DEFAULT_WATER_COMP_REFERENCE_KEY,
).strip().replace("\\", "/")
if (
    not WATER_COMP_REFERENCE_KEY
    or WATER_COMP_REFERENCE_KEY.startswith("/")
    or re.match(r"^[A-Za-z]:/", WATER_COMP_REFERENCE_KEY)
    or any(
        part in {"", ".", ".."}
        for part in WATER_COMP_REFERENCE_KEY.split("/")
    )
):
    raise RuntimeError(
        "{} 必须是数据仓库内的安全材料相对键".format(
            WATER_COMP_REFERENCE_KEY_ENV
        )
    )
# Kept as a presentation fallback only. Reference selection must use the
# immutable repository key above, never this user-editable display name.
WATER_COMP_REFERENCE_NAME = Path(DEFAULT_WATER_COMP_REFERENCE_KEY).name
WATER_COMP_REFERENCE_TAIL_START_CYCLE = 10
WATER_COMP_SENSITIVITY_START_CYCLES = (10, 20, 50, 100)
WATER_COMP_OVERCOMPENSATION_TOLERANCE_MV = 0.001

INK = "#172033"
MUTED = "#667085"
GRID = "#D9DEE8"
AXIS = "#8A94A6"
PAPER = "#FFFFFF"
PANEL = "#F8FAFC"
NEUTRAL = "#8B95A5"
NEUTRAL_DARK = "#4B5565"
BLUE = "#2563EB"
BLUE_DARK = "#1D4ED8"
GOLD = "#B88713"
ORANGE = "#D97706"
PINK = "#C4517B"
OLIVE = "#667A2A"
NORMAL_GREEN = "#218B57"
ANOMALY_RED = "#C43D3D"
PALE_BLUE = "#EAF1FF"

# Twenty visually separated colors on white.  Each color may be reused only
# once with the second line style, supporting up to forty overlay series while
# preserving the user's two-line-styles-per-color rule.
OVERLAY_HIGH_CONTRAST_PALETTE = (
    "#0057B8",  # blue
    "#E66100",  # orange
    "#009E73",  # green
    "#7A3E9D",  # purple
    "#C58A00",  # dark gold
    "#D81B60",  # magenta
    "#007C91",  # teal
    "#8C564B",  # brown
    "#A50F15",  # deep red
    "#4D4D4D",  # charcoal
    "#6B8E23",  # olive
    "#56B4E9",  # sky blue
    "#B79F00",  # ochre
    "#CC79A7",  # rose
    "#332288",  # indigo
    "#44AA99",  # sea green
    "#AA4499",  # wine purple
    "#117733",  # forest green
    "#882255",  # burgundy
    "#6699CC",  # steel blue
    "#1B9E77",  # jade
    "#D95F02",  # burnt orange
    "#7570B3",  # muted violet
    "#E7298A",  # vivid pink
    "#66A61E",  # leaf green
    "#E6AB02",  # amber
    "#A6761D",  # bronze
    "#1F78B4",  # ocean blue
    "#33A02C",  # bright green
    "#E31A1C",  # signal red
    "#6A3D9A",  # royal purple
    "#B15928",  # rust
)
OVERLAY_LINE_STYLES = ("-", "--")
PALE_ORANGE = "#FFF2E4"

FONT_REGULAR_PATH = Path(
    os.environ.get(
        "START_STOP_FONT_REGULAR",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
    )
)
FONT_BOLD_PATH = Path(
    os.environ.get(
        "START_STOP_FONT_BOLD",
        "/System/Library/Fonts/STHeiti Medium.ttc",
    )
)
FONT_REGULAR_NAME = "DejaVu Sans"
FONT_BOLD_NAME = "DejaVu Sans"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_header_index(path: Path) -> int:
    with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
        for index, line in enumerate(handle):
            if line.lstrip("\ufeff").startswith("E(V)"):
                return index
            if index >= 50:
                break
    raise ValueError("未找到 E(V) 数据表头")


def detect_start_stop_source_type(
    path: Path,
    logical_path: Path | None = None,
) -> str:
    name_path = logical_path or path
    try:
        with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
            for _ in range(3):
                line = handle.readline()
                if not line:
                    break
                if "ID_GalSquareWave" in line:
                    return "gal_square_wave"
                if (
                    name_path.stem.casefold() == "adt"
                    and "ID_ScriptMethod" in line
                ):
                    return "adt_script_method"
    except OSError:
        return ""
    return ""


def classify_file_name(path: Path) -> str:
    stem = path.stem
    lower = stem.lower()
    if stem == "启停" or lower == "qiting":
        return "base"
    if lower == "adt":
        return "adt"
    if re.fullmatch(
        r"(?:启停|qiting)_\d{4}_\d{2}_\d{2}_\d{2}_\d{2}_\d{2}",
        stem,
        flags=re.IGNORECASE,
    ):
        return "timestamp_continuation"
    if "NiMoP" in path.parts and stem == "启停2":
        return "confirmed_continuation"
    if re.fullmatch(r"启停\d+", stem):
        return "special_marked"
    return "other"


def read_square_wave_table(path: Path) -> Tuple[pd.DataFrame, int, int]:
    header_index = find_header_index(path)
    try:
        raw = pd.read_csv(
            path,
            sep="\t",
            skiprows=header_index,
            encoding="utf-8-sig",
            engine="c",
        )
    except pd.errors.EmptyDataError:
        raw = pd.DataFrame(columns=["E(V)", "i(A/cm²)", "T(s)"])

    raw_row_count = int(len(raw))
    if raw.shape[1] < 3:
        if raw_row_count == 0:
            return pd.DataFrame(
                columns=["potential_hghgo_v", "current_a_cm2", "time_s"]
            ), raw_row_count, 0
        raise ValueError("有效列少于 3 列")

    numeric = raw.iloc[:, :3].copy()
    numeric.columns = ["potential_hghgo_v", "current_a_cm2", "time_s"]
    for column in numeric.columns:
        numeric[column] = pd.to_numeric(numeric[column], errors="coerce")
    valid_mask = numeric.notna().all(axis=1)
    parse_error_rows = int((~valid_mask).sum())
    frame = numeric.loc[valid_mask].reset_index(drop=True)
    return frame, raw_row_count, parse_error_rows


def current_levels(frame: pd.DataFrame) -> List[float]:
    if frame.empty:
        return []
    return sorted(float(value) for value in frame["current_a_cm2"].round(6).unique())


def has_standard_current_levels(levels: Sequence[float]) -> bool:
    if len(levels) != 2:
        return False
    expected = [CATHODIC_CURRENT_A_CM2, REVERSE_CURRENT_A_CM2]
    return bool(
        all(
            np.isclose(value, target, rtol=0.0, atol=CURRENT_ATOL)
            for value, target in zip(levels, expected)
        )
    )


def has_variable_start_stop_current_levels(
    levels: Sequence[float],
    file_name_kind: str,
) -> bool:
    """Accept a non-default work step only for an explicit start-stop file.

    The repository also contains many GalSquareWave electrodeposition files.
    Requiring an explicit qiting/启停 filename keeps those preparation pulses
    excluded while allowing new measured current pairs such as -0.30/+0.10
    A cm-2 to become their own comparison work step.
    """
    if file_name_kind not in EXPLICIT_START_STOP_FILE_KINDS or len(levels) != 2:
        return False
    cathodic, recovery = (float(levels[0]), float(levels[1]))
    return bool(
        math.isfinite(cathodic)
        and math.isfinite(recovery)
        and cathodic < -CURRENT_ATOL
        and recovery >= -CURRENT_ATOL
    )


def adt_phase_profile(frame: pd.DataFrame) -> dict:
    if frame.empty:
        return {
            "valid": False,
            "phase_count": 0,
            "cathodic_current_median_a_cm2": math.nan,
            "recovery_current_median_a_cm2": math.nan,
        }
    currents = frame["current_a_cm2"].to_numpy(dtype=float)
    cathodic = currents < ADT_CATHODIC_THRESHOLD_A_CM2
    starts = np.r_[0, np.flatnonzero(cathodic[1:] != cathodic[:-1]) + 1]
    cathodic_values = currents[cathodic]
    recovery_values = currents[~cathodic]
    return {
        "valid": bool(
            len(starts) >= 4
            and len(cathodic_values) > 0
            and len(recovery_values) > 0
            and float(np.median(cathodic_values))
            < ADT_CATHODIC_THRESHOLD_A_CM2
            and float(np.median(recovery_values))
            > ADT_CATHODIC_THRESHOLD_A_CM2
        ),
        "phase_count": int(len(starts)),
        "cathodic_current_median_a_cm2": float(
            np.median(cathodic_values)
        )
        if len(cathodic_values)
        else math.nan,
        "recovery_current_median_a_cm2": float(
            np.median(recovery_values)
        )
        if len(recovery_values)
        else math.nan,
    }


def positive_median_step(values: np.ndarray) -> float:
    if len(values) < 2:
        return math.nan
    differences = np.diff(values.astype(float))
    positive = differences[differences > 0]
    return float(np.median(positive)) if len(positive) else math.nan


def included_file_counts(inventory: pd.DataFrame) -> dict:
    """Return protocol-specific counts and enforce their conservation law."""
    required = {"included_in_analysis", "test_type"}
    missing = sorted(required.difference(inventory.columns))
    if missing:
        raise RuntimeError(
            "启停文件计数缺少必需列：{}".format("、".join(missing))
        )
    included = inventory["included_in_analysis"].astype(bool)
    standard = included & (inventory["test_type"] == "standard_start_stop")
    variable = included & (inventory["test_type"] == "variable_start_stop")
    adt = included & (inventory["test_type"] == "adt_start_stop")
    total_count = int(included.sum())
    standard_count = int(standard.sum())
    variable_count = int(variable.sum())
    adt_count = int(adt.sum())
    if total_count != standard_count + variable_count + adt_count:
        unknown = sorted(
            {
                str(value) or "<empty>"
                for value in inventory.loc[
                    included & ~(standard | variable | adt),
                    "test_type",
                ]
            }
        )
        raise RuntimeError(
            "纳入分析的启停文件协议计数不守恒："
            "total={} 不等于 standard={} + variable={} + ADT={}；未分类协议={}".format(
                total_count,
                standard_count,
                variable_count,
                adt_count,
                unknown,
            )
        )
    return {
        "included_files": total_count,
        "included_standard_files": standard_count,
        "included_variable_files": variable_count,
        "included_adt_files": adt_count,
    }


def acquire_collection_snapshot_lock(timeout_seconds: int = 600):
    COLLECTION_RECORDS_DIR.mkdir(parents=True, exist_ok=True)
    handle = COLLECTION_LOCK.open("a+", encoding="utf-8")
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except BlockingIOError:
            if time.monotonic() >= deadline:
                handle.close()
                raise RuntimeError("等待远程数据采集完成超时")
            time.sleep(1)


def collected_current_paths() -> Tuple[set[str], Dict[str, str]]:
    all_managed: set[str] = set()
    latest_by_source: Dict[str, Tuple[str, str]] = {}
    if not COLLECTION_MANIFEST.is_file():
        return all_managed, {}
    with COLLECTION_MANIFEST.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict) or record.get("status") not in {
                "copied",
                "versioned",
                "unchanged_content",
            }:
                continue
            values = [
                record.get("machine_id"),
                record.get("root_label"),
                record.get("remote_path"),
                record.get("remote_relative_path"),
                record.get("local_path"),
            ]
            if not all(isinstance(value, str) and value for value in values):
                continue
            local_candidate = (SOURCE_ROOT / values[4]).resolve(strict=False)
            try:
                local_relative = local_candidate.relative_to(SOURCE_ROOT.resolve())
            except ValueError:
                continue
            logical_parts = [values[0], values[1]] + [
                part
                for part in values[3].replace("\\", "/").split("/")
                if part not in {"", "."}
            ]
            if any(part == ".." for part in logical_parts):
                continue
            local_key = local_relative.as_posix()
            logical_key = Path(*logical_parts).as_posix()
            all_managed.add(local_key)
            source_key = "\x1f".join(
                (values[0], values[1], values[2].casefold())
            )
            latest_by_source[source_key] = (local_key, logical_key)
    current = {
        local_key: logical_key
        for local_key, logical_key in latest_by_source.values()
    }
    return all_managed, current


def _normalized_material_keys(values: Sequence[str]) -> set[str]:
    normalized: set[str] = set()
    for value in values:
        raw = str(value or "").replace("\\", "/").strip("/")
        parts = Path(raw).parts if raw else ()
        if (
            not parts
            or raw.startswith("/")
            or re.match(r"^[A-Za-z]:/", raw)
            or any(part in {"", ".", ".."} for part in parts)
        ):
            raise RuntimeError("仅更新材料键无效")
        normalized.add(Path(*parts).as_posix())
    return normalized


def trusted_snapshot_manifest_files() -> List[dict]:
    """Return database-owned immutable manifest rows when explicitly trusted."""
    if os.environ.get("START_STOP_TRUST_SNAPSHOT_MANIFEST") != "1":
        return []
    manifest_path = SOURCE_ROOT / ".start-stop-manifest.json"
    try:
        if (
            not manifest_path.is_file()
            or manifest_path.is_symlink()
            or manifest_path.stat().st_size > 32 * 1024 * 1024
        ):
            raise RuntimeError("可信数据库快照清单缺失")
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("可信数据库快照清单无法读取") from exc
    rows = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError("可信数据库快照清单格式无效")
    result: List[dict] = []
    seen: set[str] = set()
    for raw in rows:
        if not isinstance(raw, dict):
            raise RuntimeError("可信数据库快照清单格式无效")
        relative = str(raw.get("path") or "").replace("\\", "/")
        parts = Path(relative).parts
        sha256 = str(raw.get("sha256") or "").casefold()
        try:
            size = int(raw.get("size_bytes"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("可信数据库快照清单格式无效") from exc
        if (
            not parts
            or relative.startswith("/")
            or re.match(r"^[A-Za-z]:/", relative)
            or any(part in {"", ".", ".."} for part in parts)
            or re.fullmatch(r"[0-9a-f]{64}", sha256) is None
            or size < 0
            or relative.casefold() in seen
        ):
            raise RuntimeError("可信数据库快照清单格式无效")
        seen.add(relative.casefold())
        result.append(
            {
                **raw,
                "path": Path(*parts).as_posix(),
                "sha256": sha256,
                "size_bytes": size,
            }
        )
    return result


def discover_and_profile(
    material_keys: Sequence[str] = (),
) -> Tuple[List[dict], Dict[str, pd.DataFrame]]:
    report_render_progress(
        phase="loading_files",
        phase_label="读取并校验数据文件",
        phase_index=2,
        percent=10,
        completed=0,
        total=0,
        unit="files",
        current_item="正在查找启停数据",
        detail="正在识别数据库快照中的标准、变工步与 ADT 启停文件",
    )
    requested_materials = _normalized_material_keys(material_keys)
    candidates = []
    trusted_by_path: Dict[str, dict] = {}
    trusted_rows = trusted_snapshot_manifest_files()
    if trusted_rows:
        for row in sorted(trusted_rows, key=lambda item: str(item["path"]).casefold()):
            logical_path = Path(str(row["path"]))
            if logical_path.suffix.casefold() != ".txt":
                continue
            material_relative = logical_path.parent.as_posix()
            if requested_materials and material_relative not in requested_materials:
                continue
            path = SOURCE_ROOT.joinpath(*logical_path.parts)
            source_type = str(row.get("candidate_kind") or "")
            if source_type not in {"gal_square_wave", "adt_script_method"}:
                source_type = detect_start_stop_source_type(path, logical_path)
            if source_type:
                candidates.append((path, source_type, logical_path))
                trusted_by_path[str(path)] = row
    else:
        managed_paths, current_paths = collected_current_paths()
        for path in sorted(SOURCE_ROOT.rglob("*.txt")):
            actual_relative = path.relative_to(SOURCE_ROOT).as_posix()
            if "_采集记录" in path.relative_to(SOURCE_ROOT).parts:
                continue
            if actual_relative in managed_paths and actual_relative not in current_paths:
                continue
            if "_历史版本" in path.relative_to(SOURCE_ROOT).parts and actual_relative not in current_paths:
                continue
            logical_relative = current_paths.get(actual_relative, actual_relative)
            logical_path = Path(logical_relative)
            material_relative = logical_path.parent.as_posix()
            if requested_materials and material_relative not in requested_materials:
                continue
            source_type = detect_start_stop_source_type(path, logical_path)
            if source_type:
                candidates.append((path, source_type, logical_path))
    records: List[dict] = []
    data_cache: Dict[str, pd.DataFrame] = {}

    candidate_total = len(candidates)
    for candidate_index, (path, source_type, logical_path) in enumerate(
        candidates, start=1
    ):
        report_render_progress(
            phase="loading_files",
            phase_label="读取并校验数据文件",
            phase_index=2,
            percent=10.0 + 14.0 * (candidate_index - 1) / max(candidate_total, 1),
            completed=candidate_index - 1,
            total=candidate_total,
            unit="files",
            current_item="第 {}／{} 个 · {}".format(
                candidate_index, candidate_total, logical_path.name
            ),
            detail="正在解析时间、电位、电流与测试程序信息",
        )
        relative = logical_path.as_posix()
        material_relative = logical_path.parent.as_posix()
        stat = path.stat()
        trusted = trusted_by_path.get(str(path))
        trusted_sha256 = (
            str(trusted.get("sha256"))
            if isinstance(trusted, dict)
            and int(trusted.get("size_bytes", -1)) == int(stat.st_size)
            else ""
        )
        record = {
            "absolute_path": str(path),
            "relative_path": relative,
            "material_relative_path": material_relative,
            "file_name": logical_path.name,
            "file_name_kind": classify_file_name(logical_path),
            "source_program_type": source_type,
            "test_type": "",
            "test_type_label_zh": "",
            "file_size_bytes": int(stat.st_size),
            "file_mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(
                timespec="seconds"
            ),
            "_mtime_epoch": float(stat.st_mtime),
            "sha256": trusted_sha256 or sha256_file(path),
            "raw_row_count": 0,
            "valid_row_count": 0,
            "parse_error_rows": 0,
            "current_levels_a_cm2": "[]",
            "cathodic_current_median_a_cm2": math.nan,
            "recovery_current_median_a_cm2": math.nan,
            "sample_interval_s": math.nan,
            "time_start_s": math.nan,
            "time_end_s": math.nan,
            "time_decrease_count": 0,
            "duplicate_time_count": 0,
            "potential_hghgo_min_v": math.nan,
            "potential_hghgo_max_v": math.nan,
            "candidate_class": "",
            "included_in_analysis": False,
            "exclusion_reason": "",
            "material_id": "",
            "analysis_series_id": "",
            "segment_index": math.nan,
        }
        try:
            frame, raw_rows, parse_errors = read_square_wave_table(path)
            record["raw_row_count"] = raw_rows
            record["valid_row_count"] = int(len(frame))
            record["parse_error_rows"] = parse_errors
            adt_profile = adt_phase_profile(frame)
            levels = (
                [
                    adt_profile["cathodic_current_median_a_cm2"],
                    adt_profile["recovery_current_median_a_cm2"],
                ]
                if source_type == "adt_script_method"
                else current_levels(frame)
            )
            record["current_levels_a_cm2"] = json.dumps(levels)
            record["cathodic_current_median_a_cm2"] = (
                adt_profile["cathodic_current_median_a_cm2"]
                if source_type == "adt_script_method"
                else (
                    float(frame.loc[
                        frame["current_a_cm2"] < 0,
                        "current_a_cm2",
                    ].median())
                    if not frame.empty
                    else math.nan
                )
            )
            record["recovery_current_median_a_cm2"] = (
                adt_profile["recovery_current_median_a_cm2"]
                if source_type == "adt_script_method"
                else (
                    float(frame.loc[
                        frame["current_a_cm2"] >= 0,
                        "current_a_cm2",
                    ].median())
                    if not frame.empty
                    else math.nan
                )
            )
            if not frame.empty:
                times = frame["time_s"].to_numpy(dtype=float)
                differences = np.diff(times)
                record["sample_interval_s"] = positive_median_step(times)
                record["time_start_s"] = float(times[0])
                record["time_end_s"] = float(times[-1])
                record["time_decrease_count"] = int((differences < -TIME_ATOL_S).sum())
                record["duplicate_time_count"] = int(
                    np.isclose(differences, 0.0, rtol=0.0, atol=TIME_ATOL_S).sum()
                )
                record["potential_hghgo_min_v"] = float(
                    frame["potential_hghgo_v"].min()
                )
                record["potential_hghgo_max_v"] = float(
                    frame["potential_hghgo_v"].max()
                )

            if frame.empty:
                record["candidate_class"] = "excluded_empty_header_only"
                record["exclusion_reason"] = "仅有表头、没有数值数据"
            elif record["time_decrease_count"] > 0:
                record["candidate_class"] = "excluded_nonmonotonic_time"
                record["exclusion_reason"] = "文件内时间发生倒退"
            elif (
                source_type == "gal_square_wave"
                and not has_standard_current_levels(levels)
                and not has_variable_start_stop_current_levels(
                    levels,
                    record["file_name_kind"],
                )
            ):
                record["candidate_class"] = "excluded_nonstandard_square_wave"
                record["exclusion_reason"] = (
                    "方波程序不是 -0.30/+0.03 A cm^-2 标准启停，"
                    "且文件名未明确标注为启停数据或电流档位无效"
                )
            elif (
                source_type == "adt_script_method"
                and not adt_profile["valid"]
            ):
                record["candidate_class"] = "excluded_invalid_adt"
                record["exclusion_reason"] = (
                    "ADT 未识别到交替的阴极段与恢复段"
                )
            else:
                if source_type == "adt_script_method":
                    record["candidate_class"] = "included_adt_start_stop"
                    record["test_type"] = "adt_start_stop"
                    record["test_type_label_zh"] = "ADT 启停"
                elif has_standard_current_levels(levels):
                    record["candidate_class"] = "included_standard_start_stop"
                    record["test_type"] = "standard_start_stop"
                    record["test_type_label_zh"] = "标准启停"
                else:
                    record["candidate_class"] = "included_variable_start_stop"
                    record["test_type"] = "variable_start_stop"
                    record["test_type_label_zh"] = "变工步启停"
                record["included_in_analysis"] = True
                data_cache[str(path)] = frame
        except Exception as exc:
            record["candidate_class"] = "excluded_parse_error"
            record["exclusion_reason"] = "{}: {}".format(type(exc).__name__, exc)
        records.append(record)
        report_render_progress(
            phase="loading_files",
            phase_label="读取并校验数据文件",
            phase_index=2,
            percent=10.0 + 14.0 * candidate_index / max(candidate_total, 1),
            completed=candidate_index,
            total=candidate_total,
            unit="files",
            current_item="已读取 · {}".format(logical_path.name),
            detail="正在解析时间、电位、电流与测试程序信息",
        )

    included_by_hash: Dict[str, List[dict]] = defaultdict(list)
    for record in records:
        if record["included_in_analysis"]:
            included_by_hash[record["sha256"]].append(record)
    for duplicate_records in included_by_hash.values():
        if len(duplicate_records) <= 1:
            continue
        canonical = sorted(
            duplicate_records,
            key=lambda item: (len(item["relative_path"]), item["relative_path"]),
        )[0]
        for record in duplicate_records:
            if record is canonical:
                continue
            record["candidate_class"] = "excluded_exact_duplicate"
            record["included_in_analysis"] = False
            record["exclusion_reason"] = (
                "与 {} 的 SHA-256 完全相同".format(canonical["relative_path"])
            )
            data_cache.pop(record["absolute_path"], None)
    return records, data_cache


def protocol_summary(records: Sequence[dict]) -> dict:
    test_types = sorted({record["test_type"] for record in records})
    if len(test_types) != 1:
        raise AssertionError(
            "同一分析序列混入不同启停协议: {}".format(test_types)
        )
    cathodic_currents = [
        record["cathodic_current_median_a_cm2"] for record in records
    ]
    recovery_currents = [
        record["recovery_current_median_a_cm2"] for record in records
    ]
    return {
        "test_type": test_types[0],
        "test_type_label_zh": records[0]["test_type_label_zh"],
        "cathodic_current_median_a_cm2": float(
            np.nanmedian(cathodic_currents)
        ),
        "recovery_current_median_a_cm2": float(
            np.nanmedian(recovery_currents)
        ),
    }


def build_series_specs(
    records: List[dict],
    material_order: Sequence[str] = (),
) -> Tuple[List[dict], List[dict]]:
    included = [record for record in records if record["included_in_analysis"]]
    by_material: Dict[str, List[dict]] = defaultdict(list)
    for record in included:
        by_material[record["material_relative_path"]].append(record)

    if material_order:
        ordered_all = [str(value) for value in material_order]
        if len(ordered_all) != len(set(ordered_all)):
            raise RuntimeError("材料快照顺序包含重复项")
        missing = sorted(set(by_material) - set(ordered_all))
        if missing:
            raise RuntimeError("当前数据包含材料快照之外的材料")
        material_paths = [value for value in ordered_all if value in by_material]
        material_ids = {
            value: "M{:02d}".format(index)
            for index, value in enumerate(ordered_all, start=1)
        }
        base_names = Counter(Path(value).name for value in ordered_all)
    else:
        material_paths = sorted(by_material)
        material_ids = {
            value: "M{:02d}".format(index)
            for index, value in enumerate(material_paths, start=1)
        }
        base_names = Counter(Path(value).name for value in material_paths)
    materials: List[dict] = []
    series_specs: List[dict] = []

    for material_relative in material_paths:
        material_id = material_ids[material_relative]
        material_path = SOURCE_ROOT / material_relative
        base_name = material_path.name
        material_parts = Path(material_relative).parts
        if material_parts[0] == "北京数据列":
            display_name = "/".join(material_parts)
        elif material_path.parent.name == "NiMoP":
            display_name = "NiMoP/{}".format(base_name)
        elif base_names[base_name] == 1:
            display_name = base_name
        else:
            display_name = "{}/{}".format(material_path.parent.name, base_name)
        workstation = Path(material_relative).parts[0]
        material_records = by_material[material_relative]
        for record in material_records:
            record["material_id"] = material_id

        main_records = [
            record
            for record in material_records
            if record["file_name_kind"] != "special_marked"
        ]
        special_records = [
            record
            for record in material_records
            if record["file_name_kind"] == "special_marked"
        ]
        main_records.sort(
            key=lambda item: (
                item["_mtime_epoch"],
                item["file_name"],
                item["relative_path"],
            )
        )
        special_records.sort(
            key=lambda item: (
                item["_mtime_epoch"],
                item["file_name"],
                item["relative_path"],
            )
        )

        if not main_records:
            raise AssertionError("材料没有主启停序列: {}".format(material_relative))

        main_protocol = protocol_summary(main_records)

        primary_series_id = "{}-main".format(material_id)
        for segment_index, record in enumerate(main_records, start=1):
            record["analysis_series_id"] = primary_series_id
            record["segment_index"] = segment_index
        series_specs.append(
            {
                "series_id": primary_series_id,
                "series_order": len(series_specs) + 1,
                "material_id": material_id,
                "material_display_name": display_name,
                "series_display_name": display_name,
                "material_relative_path": material_relative,
                "workstation": workstation,
                "is_primary_series": True,
                "is_special_series": False,
                "special_file_name": "",
                "records": main_records,
                **main_protocol,
            }
        )

        for special_index, record in enumerate(special_records, start=1):
            special_protocol = protocol_summary([record])
            special_series_id = "{}-special-{:02d}".format(
                material_id, special_index
            )
            record["analysis_series_id"] = special_series_id
            record["segment_index"] = 1
            series_specs.append(
                {
                    "series_id": special_series_id,
                    "series_order": len(series_specs) + 1,
                    "material_id": material_id,
                    "material_display_name": display_name,
                    "series_display_name": (
                        "{}｜{}（文件名特殊标注，单独分析）".format(
                            display_name, record["file_name"]
                        )
                    ),
                    "material_relative_path": material_relative,
                    "workstation": workstation,
                    "is_primary_series": False,
                    "is_special_series": True,
                    "special_file_name": record["file_name"],
                    "records": [record],
                    **special_protocol,
                }
            )

        materials.append(
            {
                "material_id": material_id,
                "material_display_name": display_name,
                "material_relative_path": material_relative,
                "workstation": workstation,
                "primary_series_id": primary_series_id,
                "source_file_count": len(material_records),
                "main_segment_count": len(main_records),
                "special_series_count": len(special_records),
                "test_types": "、".join(
                    sorted(
                        {
                            record["test_type_label_zh"]
                            for record in material_records
                        }
                    )
                ),
                "cathodic_current_median_a_cm2": float(
                    np.nanmedian(
                        [
                            record["cathodic_current_median_a_cm2"]
                            for record in material_records
                        ]
                    )
                ),
                "recovery_current_median_a_cm2": float(
                    np.nanmedian(
                        [
                            record["recovery_current_median_a_cm2"]
                            for record in material_records
                        ]
                    )
                ),
                "endpoint_statistic": (
                    "阶段末个可用采样点"
                    if any(
                        record["test_type"] == "adt_start_stop"
                        for record in material_records
                    )
                    else "阶段最后 1 s 中位数"
                ),
            }
        )
    return materials, series_specs


def json_safe(value):
    if isinstance(value, dict):
        return {
            key: json_safe(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, (float, np.floating)):
        return float(value) if math.isfinite(float(value)) else None
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    return value


def stable_json_hash(value) -> str:
    payload = json.dumps(
        json_safe(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def build_material_config_snapshot(
    records: List[dict],
    materials: List[dict],
    series_specs: List[dict],
) -> dict:
    specs_by_material: Dict[str, List[dict]] = defaultdict(list)
    for spec in series_specs:
        specs_by_material[spec["material_relative_path"]].append(spec)

    snapshot_materials = []
    for material in materials:
        material_key = material["material_relative_path"]
        ordered_specs = sorted(
            specs_by_material[material_key],
            key=lambda item: item["series_order"],
        )
        ordered_records = [
            record
            for spec in ordered_specs
            for record in spec["records"]
        ]
        fingerprint_payload = [
            {
                "relative_path": record["relative_path"],
                "sha256": record["sha256"],
                "test_type": record["test_type"],
                "analysis_series_id": record["analysis_series_id"],
                "segment_index": int(record["segment_index"]),
            }
            for record in ordered_records
        ]
        snapshot_materials.append(
            {
                "key": material_key,
                "auto_name": material["material_display_name"],
                "standard_file_count": len(ordered_records),
                "test_types": material["test_types"],
                "cathodic_current_median_a_cm2": material[
                    "cathodic_current_median_a_cm2"
                ],
                "recovery_current_median_a_cm2": material[
                    "recovery_current_median_a_cm2"
                ],
                "total_data_points": int(
                    sum(record["valid_row_count"] for record in ordered_records)
                ),
                "ordered_source_files": [
                    record["relative_path"] for record in ordered_records
                ],
                "fingerprint": stable_json_hash(fingerprint_payload),
            }
        )

    file_payload = [
        {
            key: value
            for key, value in record.items()
            if not key.startswith("_") and key != "absolute_path"
        }
        for record in sorted(records, key=lambda item: item["relative_path"])
    ]
    dataset_fingerprint = stable_json_hash(
        [
            {
                "relative_path": record["relative_path"],
                "sha256": record["sha256"],
                "candidate_class": record["candidate_class"],
                "source_program_type": record["source_program_type"],
                "test_type": record["test_type"],
                "included_in_analysis": record["included_in_analysis"],
            }
            for record in sorted(
                records, key=lambda item: item["relative_path"]
            )
        ]
    )
    return json_safe(
        {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "source_root": str(SOURCE_ROOT),
            "dataset_fingerprint": dataset_fingerprint,
            "materials": snapshot_materials,
            "files": file_payload,
        }
    )


def run_workbook_builder(*arguments: str) -> str:
    if not WORKBOOK_BUILDER.exists():
        raise RuntimeError("找不到表格生成器: {}".format(WORKBOOK_BUILDER))
    if WORKBOOK_BUILDER.suffix.lower() == ".py":
        command = [sys.executable, str(WORKBOOK_BUILDER), *arguments]
    else:
        if not NODE_BIN.exists():
            raise RuntimeError("找不到表格运行环境: {}".format(NODE_BIN))
        command = [str(NODE_BIN), str(WORKBOOK_BUILDER), *arguments]
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        raise RuntimeError(detail) from exc
    return completed.stdout.strip()


def write_config_snapshot(snapshot: dict) -> None:
    CONFIG_SNAPSHOT_JSON.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def update_material_config_workbook() -> dict:
    records, _ = discover_and_profile()
    if not records:
        raise RuntimeError("没有识别到任何启停候选 .txt 文件")
    materials, series_specs = build_series_specs(records)
    snapshot = build_material_config_snapshot(
        records, materials, series_specs
    )
    write_config_snapshot(snapshot)

    inventory_columns = [
        key for key in records[0].keys() if not key.startswith("_")
    ]
    inventory = pd.DataFrame(records)[inventory_columns].sort_values(
        "relative_path"
    )
    file_counts = included_file_counts(inventory)
    csv_write(inventory, OUTPUT_DIR / "data_inventory.csv")
    builder_output = run_workbook_builder(
        "update",
        str(CONFIG_SNAPSHOT_JSON),
        str(CONFIG_WORKBOOK),
    )
    run_workbook_builder(
        "read",
        str(CONFIG_WORKBOOK),
        str(CONFIG_READBACK_JSON),
    )
    readback = json.loads(CONFIG_READBACK_JSON.read_text(encoding="utf-8"))
    if readback.get("dataset_fingerprint") != snapshot["dataset_fingerprint"]:
        raise RuntimeError("材料配置表更新后回读指纹不一致")
    result = {
        "stage": "配置表已更新，尚未绘图",
        "source_root": str(SOURCE_ROOT),
        "workbook": str(CONFIG_WORKBOOK),
        "materials": len(materials),
        "start_stop_candidates": len(records),
        "included_start_stop_files": file_counts["included_files"],
        "included_standard_files": file_counts["included_standard_files"],
        "included_variable_files": file_counts["included_variable_files"],
        "included_adt_files": file_counts["included_adt_files"],
        "next_step": (
            "修改黄色列并保存，然后执行 --render 或双击步骤 2"
        ),
        "workbook_update": builder_output,
    }
    return result


def read_and_validate_material_config(snapshot: dict) -> List[dict]:
    if not CONFIG_WORKBOOK.exists():
        raise RuntimeError(
            "材料配置表不存在，请先执行步骤 1 更新材料配置表"
        )
    run_workbook_builder(
        "read",
        str(CONFIG_WORKBOOK),
        str(CONFIG_READBACK_JSON),
    )
    payload = json.loads(CONFIG_READBACK_JSON.read_text(encoding="utf-8"))
    if payload.get("dataset_fingerprint") != snapshot["dataset_fingerprint"]:
        raise RuntimeError(
            "源文件在材料配置表更新后发生了变化。请先执行步骤 1，"
            "重新更新配置表并确认名称/选择，再生成图集。"
        )
    return payload["materials"]


def apply_material_config_json(config_path: Path) -> None:
    if not config_path.exists() or not config_path.is_file():
        raise RuntimeError("网页材料配置 JSON 不存在")
    if not CONFIG_SNAPSHOT_JSON.exists():
        raise RuntimeError("材料快照不存在，请先更新数据")
    run_workbook_builder(
        "apply-json",
        str(CONFIG_SNAPSHOT_JSON),
        str(config_path),
        str(CONFIG_WORKBOOK),
    )


def apply_material_config(
    materials: List[dict],
    series_specs: List[dict],
    config_rows: List[dict],
    snapshot: dict,
) -> None:
    config_by_key = {row["key"]: row for row in config_rows}
    snapshot_by_key = {
        row["key"]: row for row in snapshot["materials"]
    }
    current_keys = {
        material["material_relative_path"] for material in materials
    }
    if set(config_by_key) != current_keys:
        raise RuntimeError(
            "配置表中的材料集合与当前源文件不一致，请先执行步骤 1 更新配置表"
        )

    for material in materials:
        key = material["material_relative_path"]
        config = config_by_key[key]
        if config["fingerprint"] != snapshot_by_key[key]["fingerprint"]:
            raise RuntimeError(
                "{} 的源数据在配置表更新后发生变化，请先执行步骤 1".format(
                    key
                )
            )
        material["auto_detected_name"] = material[
            "material_display_name"
        ]
        material["material_display_name"] = config["plot_name"]
        material["include_in_summary_atlas"] = bool(
            config["include_in_summary_atlas"]
        )
        material["material_user_notes"] = config.get("notes", "")

    material_by_key = {
        material["material_relative_path"]: material
        for material in materials
    }
    for spec in series_specs:
        material = material_by_key[spec["material_relative_path"]]
        display_name = material["material_display_name"]
        spec["material_display_name"] = display_name
        spec["include_in_summary_atlas"] = material[
            "include_in_summary_atlas"
        ]
        spec["material_user_notes"] = material["material_user_notes"]
        if spec["is_special_series"]:
            spec["series_display_name"] = (
                "{}｜{}（文件名特殊标注，单独分析）".format(
                    display_name, spec["special_file_name"]
                )
            )
        else:
            spec["series_display_name"] = display_name


def phase_endpoint_mask(
    elapsed_s: np.ndarray,
    phase_duration_s: float,
    sample_interval_s: float,
) -> Tuple[np.ndarray, float]:
    effective_window_s = max(STEADY_WINDOW_S, sample_interval_s)
    mask = (
        elapsed_s
        >= phase_duration_s - effective_window_s - TIME_ATOL_S
    ) & (elapsed_s < phase_duration_s + TIME_ATOL_S)
    return mask, effective_window_s


def sampling_point_requirements(
    sample_interval_s: float,
    test_type: str,
) -> Tuple[int, int]:
    """Return interval-aware phase and endpoint sampling requirements.

    The legacy caps preserve the established 0.1 s quality gate. Slower,
    regular acquisition remains valid when the measured phase duration is
    complete and it contains the number of samples physically available at
    that interval.
    """
    if test_type == "adt_start_stop":
        phase_cap = ADT_MIN_PHASE_POINTS
        endpoint_cap = ADT_MIN_STEADY_POINTS
        minimum_duration_s = ADT_MIN_COMPLETE_PHASE_ELAPSED_S
    elif test_type == "variable_start_stop":
        phase_cap = VARIABLE_MIN_PHASE_POINTS
        endpoint_cap = VARIABLE_MIN_STEADY_POINTS
        minimum_duration_s = VARIABLE_MIN_COMPLETE_PHASE_ELAPSED_S
    else:
        phase_cap = MIN_PHASE_POINTS
        endpoint_cap = MIN_STEADY_POINTS
        minimum_duration_s = MIN_COMPLETE_PHASE_ELAPSED_S
    interval = float(sample_interval_s)
    if not math.isfinite(interval) or interval <= 0:
        return phase_cap, endpoint_cap
    duration_points = max(
        2,
        int(math.ceil((minimum_duration_s - TIME_ATOL_S) / interval)),
    )
    endpoint_window_s = max(STEADY_WINDOW_S, interval)
    endpoint_points = max(
        1,
        int(math.floor((endpoint_window_s + TIME_ATOL_S) / interval)),
    )
    return min(phase_cap, duration_points), min(endpoint_cap, endpoint_points)


def build_complete_cycle_pairs(
    frame: pd.DataFrame,
    test_type: str,
) -> Tuple[List[Tuple[int, int, int, int]], dict]:
    if frame.empty:
        return [], {
            "phase_count": 0,
            "cathodic_phase_candidates": 0,
            "incomplete_cycle_candidates": 0,
        }
    currents = frame["current_a_cm2"].to_numpy(dtype=float)
    cathodic_threshold = (
        ADT_CATHODIC_THRESHOLD_A_CM2
        if test_type == "adt_start_stop"
        else 0.0
    )
    signs = np.where(currents < cathodic_threshold, -1, 1)
    starts = np.r_[0, np.flatnonzero(signs[1:] != signs[:-1]) + 1]
    ends = np.r_[starts[1:], len(frame)]
    times = frame["time_s"].to_numpy(dtype=float)

    pairs: List[Tuple[int, int, int, int]] = []
    cathodic_candidates = 0
    incomplete_candidates = 0
    for phase_index, (start, end) in enumerate(zip(starts, ends)):
        if signs[start] >= 0:
            continue
        cathodic_candidates += 1
        if phase_index + 1 >= len(starts):
            incomplete_candidates += 1
            continue
        reverse_start = int(starts[phase_index + 1])
        reverse_end = int(ends[phase_index + 1])
        if signs[reverse_start] <= 0:
            incomplete_candidates += 1
            continue

        cathodic_start = int(start)
        cathodic_end = int(end)
        cathodic_elapsed = (
            times[cathodic_start:cathodic_end] - times[cathodic_start]
        )
        reverse_elapsed = (
            times[reverse_start:reverse_end] - times[reverse_start]
        )
        cathodic_duration_s = float(
            cathodic_elapsed[-1] + positive_median_step(cathodic_elapsed)
        )
        reverse_duration_s = float(
            reverse_elapsed[-1] + positive_median_step(reverse_elapsed)
        )
        cathodic_step_s = positive_median_step(cathodic_elapsed)
        reverse_step_s = positive_median_step(reverse_elapsed)
        cathodic_last, _ = phase_endpoint_mask(
            cathodic_elapsed,
            cathodic_duration_s,
            cathodic_step_s,
        )
        reverse_last, _ = phase_endpoint_mask(
            reverse_elapsed,
            reverse_duration_s,
            reverse_step_s,
        )
        if test_type == "adt_start_stop":
            min_phase_elapsed_s = ADT_MIN_COMPLETE_PHASE_ELAPSED_S
        elif test_type == "variable_start_stop":
            min_phase_elapsed_s = VARIABLE_MIN_COMPLETE_PHASE_ELAPSED_S
        else:
            min_phase_elapsed_s = MIN_COMPLETE_PHASE_ELAPSED_S
        cathodic_min_phase_points, cathodic_min_steady_points = (
            sampling_point_requirements(cathodic_step_s, test_type)
        )
        reverse_min_phase_points, reverse_min_steady_points = (
            sampling_point_requirements(reverse_step_s, test_type)
        )
        complete = (
            len(cathodic_elapsed) >= cathodic_min_phase_points
            and len(reverse_elapsed) >= reverse_min_phase_points
            and cathodic_duration_s >= min_phase_elapsed_s - TIME_ATOL_S
            and reverse_duration_s >= min_phase_elapsed_s - TIME_ATOL_S
            and int(cathodic_last.sum()) >= cathodic_min_steady_points
            and int(reverse_last.sum()) >= reverse_min_steady_points
        )
        if complete:
            pairs.append(
                (
                    cathodic_start,
                    cathodic_end,
                    reverse_start,
                    reverse_end,
                )
            )
        else:
            incomplete_candidates += 1
    return pairs, {
        "phase_count": int(len(starts)),
        "cathodic_phase_candidates": int(cathodic_candidates),
        "incomplete_cycle_candidates": int(incomplete_candidates),
    }


def linear_fit(x: np.ndarray, y: np.ndarray) -> Tuple[float, float, float]:
    if len(x) < 2:
        return math.nan, math.nan, math.nan
    slope, intercept = np.polyfit(x, y, 1)
    predicted = slope * x + intercept
    residual = float(np.sum((y - predicted) ** 2))
    total = float(np.sum((y - np.mean(y)) ** 2))
    r_squared = 1.0 - residual / total if total else 1.0
    return float(slope), float(intercept), float(r_squared)


def analyze_all_series(
    series_specs: List[dict],
    data_cache: Dict[str, pd.DataFrame],
) -> Tuple[
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
]:
    all_cycles: List[pd.DataFrame] = []
    all_overview: List[pd.DataFrame] = []
    all_representative: List[pd.DataFrame] = []
    segment_rows: List[dict] = []
    boundary_rows: List[dict] = []
    series_rows: List[dict] = []
    pairs_cache: Dict[Tuple[str, int], List[Tuple[int, int, int, int]]] = {}

    series_total = len(series_specs)
    for series_index, spec in enumerate(series_specs, start=1):
        series_name = str(
            spec.get("series_display_name")
            or spec.get("material_display_name")
            or "未命名材料"
        )
        report_render_progress(
            phase="analyzing_series",
            phase_label="计算启停循环",
            phase_index=3,
            percent=24.0 + 28.0 * (series_index - 1) / max(series_total, 1),
            completed=series_index - 1,
            total=series_total,
            unit="materials",
            current_item="第 {}／{} 种 · {}".format(
                series_index, series_total, series_name
            ),
            detail="正在逐材料识别完整循环并计算阴极与恢复段指标",
        )
        series_cycle_rows: List[dict] = []
        series_overview: List[pd.DataFrame] = []
        continuous_offset_s = 0.0
        global_cycle_offset = 0
        total_rows = 0

        for segment_index, record in enumerate(spec["records"], start=1):
            path = Path(record["absolute_path"])
            frame = data_cache[str(path)]
            times = frame["time_s"].to_numpy(dtype=float)
            potentials_hghgo = frame["potential_hghgo_v"].to_numpy(dtype=float)
            potentials_raw = potentials_hghgo.copy()
            currents = frame["current_a_cm2"].to_numpy(dtype=float)
            sample_interval_s = positive_median_step(times)
            inclusive_duration_s = float(times[-1] - times[0] + sample_interval_s)
            normalized_time_s = times - times[0]
            continuous_time_s = continuous_offset_s + normalized_time_s
            pairs, phase_profile = build_complete_cycle_pairs(
                frame, record["test_type"]
            )
            pairs_cache[(spec["series_id"], segment_index)] = pairs
            total_rows += len(frame)

            if segment_index > 1:
                boundary_rows.append(
                    {
                        "series_id": spec["series_id"],
                        "series_display_name": spec["series_display_name"],
                        "material_id": spec["material_id"],
                        "test_type": spec["test_type"],
                        "test_type_label_zh": spec["test_type_label_zh"],
                        "segment_index": segment_index,
                        "boundary_time_s": continuous_offset_s,
                        "boundary_time_h": continuous_offset_s / 3600.0,
                        "source_file": record["relative_path"],
                    }
                )

            for local_cycle, pair in enumerate(pairs, start=1):
                cathodic_start, cathodic_end, reverse_start, reverse_end = pair
                cathodic_elapsed = (
                    times[cathodic_start:cathodic_end] - times[cathodic_start]
                )
                reverse_elapsed = (
                    times[reverse_start:reverse_end] - times[reverse_start]
                )
                cathodic_values = potentials_raw[cathodic_start:cathodic_end]
                reverse_values = potentials_raw[reverse_start:reverse_end]
                cathodic_currents = currents[cathodic_start:cathodic_end]
                reverse_currents = currents[reverse_start:reverse_end]
                cathodic_duration_s = float(
                    cathodic_elapsed[-1]
                    + positive_median_step(cathodic_elapsed)
                )
                reverse_duration_s = float(
                    reverse_elapsed[-1] + positive_median_step(reverse_elapsed)
                )
                cathodic_last_mask, cathodic_endpoint_window_s = (
                    phase_endpoint_mask(
                        cathodic_elapsed,
                        cathodic_duration_s,
                        sample_interval_s,
                    )
                )
                reverse_last_mask, reverse_endpoint_window_s = (
                    phase_endpoint_mask(
                        reverse_elapsed,
                        reverse_duration_s,
                        sample_interval_s,
                    )
                )
                cathodic_phase_mask = np.ones(
                    len(cathodic_elapsed), dtype=bool
                )
                if sample_interval_s >= STEADY_WINDOW_S:
                    cathodic_initial_mask = np.isclose(
                        cathodic_elapsed,
                        min(sample_interval_s, cathodic_elapsed[-1]),
                        rtol=0.0,
                        atol=TIME_ATOL_S,
                    )
                    reverse_initial_mask = np.isclose(
                        reverse_elapsed,
                        min(sample_interval_s, reverse_elapsed[-1]),
                        rtol=0.0,
                        atol=TIME_ATOL_S,
                    )
                else:
                    cathodic_initial_mask = (
                        (cathodic_elapsed >= 0.5)
                        & (cathodic_elapsed < 1.5)
                    )
                    reverse_initial_mask = (
                        (reverse_elapsed >= 0.5)
                        & (reverse_elapsed < 1.5)
                    )
                cathodic_last_values = cathodic_values[cathodic_last_mask]
                reverse_last_values = reverse_values[reverse_last_mask]
                cathodic_phase_values = cathodic_values[cathodic_phase_mask]
                cathodic_phase_elapsed = cathodic_elapsed[cathodic_phase_mask]

                cathodic_last_min_position = int(np.argmin(cathodic_last_values))
                cathodic_phase_min_position = int(np.argmin(cathodic_phase_values))
                cathodic_last_indices = np.flatnonzero(cathodic_last_mask)
                phase_indices = np.flatnonzero(cathodic_phase_mask)
                cathodic_last_min_local_index = int(
                    cathodic_last_indices[cathodic_last_min_position]
                )
                cathodic_phase_min_local_index = int(
                    phase_indices[cathodic_phase_min_position]
                )
                cathodic_last_min_global_index = (
                    cathodic_start + cathodic_last_min_local_index
                )
                cathodic_phase_min_global_index = (
                    cathodic_start + cathodic_phase_min_local_index
                )
                cathodic_last_min = float(
                    potentials_raw[cathodic_last_min_global_index]
                )
                cathodic_phase_min = float(
                    potentials_raw[cathodic_phase_min_global_index]
                )
                min_elapsed_s = float(
                    cathodic_phase_elapsed[cathodic_phase_min_position]
                )
                status = "normal" if min_elapsed_s >= ANOMALY_BOUNDARY_S else "abnormal"
                status_zh = "正常" if status == "normal" else "异常"
                endpoint_times = continuous_time_s[
                    cathodic_start:cathodic_end
                ][cathodic_last_mask]
                reverse_endpoint_times = continuous_time_s[
                    reverse_start:reverse_end
                ][reverse_last_mask]
                global_cycle = global_cycle_offset + local_cycle
                cathodic_median = float(np.median(cathodic_last_values))
                reverse_median = float(np.median(reverse_last_values))
                minimum_phase_points, minimum_endpoint_points = (
                    sampling_point_requirements(
                        sample_interval_s,
                        record["test_type"],
                    )
                )

                series_cycle_rows.append(
                    {
                        "series_id": spec["series_id"],
                        "series_order": spec["series_order"],
                        "series_display_name": spec["series_display_name"],
                        "material_id": spec["material_id"],
                        "material_display_name": spec["material_display_name"],
                        "material_relative_path": spec["material_relative_path"],
                        "workstation": spec["workstation"],
                        "test_type": spec["test_type"],
                        "test_type_label_zh": spec["test_type_label_zh"],
                        "is_primary_series": spec["is_primary_series"],
                        "is_special_series": spec["is_special_series"],
                        "segment_index": segment_index,
                        "source_file": record["relative_path"],
                        "source_sha256": record["sha256"],
                        "cycle": global_cycle,
                        "cycle_in_segment": local_cycle,
                        "cathodic_endpoint_time_s": float(np.median(endpoint_times)),
                        "cathodic_endpoint_time_h": float(
                            np.median(endpoint_times) / 3600.0
                        ),
                        "reverse_endpoint_time_s": float(
                            np.median(reverse_endpoint_times)
                        ),
                        "reverse_endpoint_time_h": float(
                            np.median(reverse_endpoint_times) / 3600.0
                        ),
                        "cathodic_phase_duration_s": cathodic_duration_s,
                        "reverse_phase_duration_s": reverse_duration_s,
                        "cycle_duration_s": (
                            cathodic_duration_s + reverse_duration_s
                        ),
                        "sample_interval_s": sample_interval_s,
                        "minimum_phase_point_count": minimum_phase_points,
                        "minimum_endpoint_point_count": minimum_endpoint_points,
                        "endpoint_statistic": (
                            "阶段末个可用采样点"
                            if sample_interval_s >= STEADY_WINDOW_S
                            else "阶段最后 1 s 中位数"
                        ),
                        "cathodic_endpoint_window_s": (
                            cathodic_endpoint_window_s
                        ),
                        "reverse_endpoint_window_s": reverse_endpoint_window_s,
                        "cathodic_current_median_a_cm2": float(
                            np.median(cathodic_currents)
                        ),
                        "recovery_current_median_a_cm2": float(
                            np.median(reverse_currents)
                        ),
                        "cathodic_last1s_median_raw_v": cathodic_median,
                        "cathodic_last1s_mean_raw_v": float(
                            np.mean(cathodic_last_values)
                        ),
                        "cathodic_last1s_std_raw_v": (
                            float(np.std(cathodic_last_values, ddof=1))
                            if len(cathodic_last_values) > 1
                            else math.nan
                        ),
                        "cathodic_last1s_min_raw_v": cathodic_last_min,
                        "cathodic_last1s_min_phase_time_s": float(
                            cathodic_elapsed[cathodic_last_min_local_index]
                        ),
                        "cathodic_phase_min_raw_v": cathodic_phase_min,
                        "cathodic_phase_min_time_s": min_elapsed_s,
                        "cathodic_phase_min_continuous_time_s": float(
                            continuous_time_s[cathodic_phase_min_global_index]
                        ),
                        "cathodic_phase_min_continuous_time_h": float(
                            continuous_time_s[cathodic_phase_min_global_index]
                            / 3600.0
                        ),
                        "cathodic_last1s_contains_phase_min": bool(
                            np.isclose(
                                cathodic_last_min,
                                cathodic_phase_min,
                                rtol=0.0,
                                atol=1e-12,
                            )
                        ),
                        "cathodic_phase_min_in_second_half": bool(
                            min_elapsed_s >= ANOMALY_BOUNDARY_S
                        ),
                        "cathodic_shift_status": status,
                        "cathodic_shift_status_zh": status_zh,
                        "cathodic_last1s_point_count": int(
                            cathodic_last_mask.sum()
                        ),
                        "cathodic_phase_point_count": int(
                            cathodic_phase_mask.sum()
                        ),
                        "reverse_last1s_median_raw_v": reverse_median,
                        "reverse_last1s_mean_raw_v": float(
                            np.mean(reverse_last_values)
                        ),
                        "reverse_last1s_std_raw_v": (
                            float(np.std(reverse_last_values, ddof=1))
                            if len(reverse_last_values) > 1
                            else math.nan
                        ),
                        "reverse_last1s_point_count": int(
                            reverse_last_mask.sum()
                        ),
                        "reverse_phase_point_count": int(len(reverse_elapsed)),
                        "cathodic_initial_raw_median_v": float(
                            np.median(cathodic_values[cathodic_initial_mask])
                        )
                        if cathodic_initial_mask.any()
                        else math.nan,
                        "reverse_initial_raw_median_v": float(
                            np.median(reverse_values[reverse_initial_mask])
                        )
                        if reverse_initial_mask.any()
                        else math.nan,
                        "phase_gap_v": reverse_median - cathodic_median,
                        "cathodic_overpotential_magnitude_mv": -1000.0
                        * cathodic_median,
                    }
                )

            stride = max(
                1,
                int(
                    round(
                        OVERVIEW_INTERVAL_S / sample_interval_s
                        if sample_interval_s > 0
                        else 50
                    )
                ),
            )
            overview_indices = np.arange(0, len(frame), stride, dtype=int)
            overview = pd.DataFrame(
                {
                    "series_id": spec["series_id"],
                    "series_order": spec["series_order"],
                    "series_display_name": spec["series_display_name"],
                    "material_id": spec["material_id"],
                    "material_display_name": spec["material_display_name"],
                    "test_type": spec["test_type"],
                    "test_type_label_zh": spec["test_type_label_zh"],
                    "is_primary_series": spec["is_primary_series"],
                    "is_special_series": spec["is_special_series"],
                    "segment_index": segment_index,
                    "source_file": record["relative_path"],
                    "local_time_s": normalized_time_s[overview_indices],
                    "continuous_time_s": continuous_time_s[overview_indices],
                    "continuous_time_h": continuous_time_s[overview_indices]
                    / 3600.0,
                    "current_a_cm2": currents[overview_indices],
                    "potential_hghgo_v": potentials_hghgo[overview_indices],
                    "potential_raw_v": potentials_raw[overview_indices],
                }
            )
            series_overview.append(overview)

            segment_rows.append(
                {
                    "series_id": spec["series_id"],
                    "series_display_name": spec["series_display_name"],
                    "material_id": spec["material_id"],
                    "material_display_name": spec["material_display_name"],
                    "test_type": spec["test_type"],
                    "test_type_label_zh": spec["test_type_label_zh"],
                    "is_primary_series": spec["is_primary_series"],
                    "is_special_series": spec["is_special_series"],
                    "segment_index": segment_index,
                    "source_file": record["relative_path"],
                    "source_sha256": record["sha256"],
                    "row_count": int(len(frame)),
                    "sample_interval_s": sample_interval_s,
                    "local_time_start_s": float(times[0]),
                    "local_time_end_s": float(times[-1]),
                    "continuous_time_start_s": continuous_offset_s,
                    "continuous_time_end_s": float(continuous_time_s[-1]),
                    "continuous_time_start_h": continuous_offset_s / 3600.0,
                    "continuous_time_end_h": float(
                        continuous_time_s[-1] / 3600.0
                    ),
                    "inclusive_duration_s": inclusive_duration_s,
                    "complete_cycles": int(len(pairs)),
                    "median_cathodic_phase_duration_s": float(
                        np.median(
                            [
                                times[cathodic_end - 1]
                                - times[cathodic_start]
                                + sample_interval_s
                                for (
                                    cathodic_start,
                                    cathodic_end,
                                    _,
                                    _,
                                ) in pairs
                            ]
                        )
                    )
                    if pairs
                    else math.nan,
                    "median_reverse_phase_duration_s": float(
                        np.median(
                            [
                                times[reverse_end - 1]
                                - times[reverse_start]
                                + sample_interval_s
                                for (
                                    _,
                                    _,
                                    reverse_start,
                                    reverse_end,
                                ) in pairs
                            ]
                        )
                    )
                    if pairs
                    else math.nan,
                    "phase_count": phase_profile["phase_count"],
                    "cathodic_phase_candidates": phase_profile[
                        "cathodic_phase_candidates"
                    ],
                    "incomplete_cycle_candidates": phase_profile[
                        "incomplete_cycle_candidates"
                    ],
                    "time_decrease_count": record["time_decrease_count"],
                    "duplicate_time_count": record["duplicate_time_count"],
                    "parse_error_rows": record["parse_error_rows"],
                    "current_levels_a_cm2": record["current_levels_a_cm2"],
                    "file_name_kind": record["file_name_kind"],
                    "source_program_type": record["source_program_type"],
                    "endpoint_statistic": (
                        "阶段末个可用采样点"
                        if sample_interval_s >= STEADY_WINDOW_S
                        else "阶段最后 1 s 中位数"
                    ),
                }
            )
            continuous_offset_s += inclusive_duration_s
            global_cycle_offset += len(pairs)

        cycle_frame = pd.DataFrame(series_cycle_rows)
        if cycle_frame.empty:
            raise RuntimeError(
                "材料“{}”未识别到满足阶段时长与采样要求的完整启停循环。".format(
                    spec["series_display_name"]
                )
            )
        cycle_frame = cycle_frame.sort_values("cycle")
        baseline_count = min(BASELINE_CYCLES, len(cycle_frame))
        endpoint_count = min(ENDPOINT_CYCLES, len(cycle_frame))
        cathodic_baseline = float(
            cycle_frame.head(baseline_count)[
                "cathodic_last1s_median_raw_v"
            ].median()
        )
        reverse_baseline = float(
            cycle_frame.head(baseline_count)[
                "reverse_last1s_median_raw_v"
            ].median()
        )
        cycle_frame["cathodic_negative_shift_mv"] = 1000.0 * (
            cathodic_baseline
            - cycle_frame["cathodic_last1s_median_raw_v"]
        )
        cycle_frame["reverse_shift_mv"] = 1000.0 * (
            cycle_frame["reverse_last1s_median_raw_v"] - reverse_baseline
        )
        cathodic_endpoint = float(
            cycle_frame.tail(endpoint_count)[
                "cathodic_last1s_median_raw_v"
            ].median()
        )
        reverse_endpoint = float(
            cycle_frame.tail(endpoint_count)[
                "reverse_last1s_median_raw_v"
            ].median()
        )
        slope, intercept, r_squared = linear_fit(
            cycle_frame["cycle"].to_numpy(dtype=float),
            cycle_frame["cathodic_last1s_median_raw_v"].to_numpy(dtype=float),
        )
        normal_count = int(
            (cycle_frame["cathodic_shift_status"] == "normal").sum()
        )
        abnormal_count = int(
            (cycle_frame["cathodic_shift_status"] == "abnormal").sum()
        )
        source_files = [record["relative_path"] for record in spec["records"]]
        series_rows.append(
            {
                "series_id": spec["series_id"],
                "series_order": spec["series_order"],
                "series_display_name": spec["series_display_name"],
                "material_id": spec["material_id"],
                "material_display_name": spec["material_display_name"],
                "material_relative_path": spec["material_relative_path"],
                "workstation": spec["workstation"],
                "test_type": spec["test_type"],
                "test_type_label_zh": spec["test_type_label_zh"],
                "cathodic_current_median_a_cm2": float(
                    cycle_frame["cathodic_current_median_a_cm2"].median()
                ),
                "recovery_current_median_a_cm2": float(
                    cycle_frame["recovery_current_median_a_cm2"].median()
                ),
                "endpoint_statistic": (
                    "阶段末个可用采样点"
                    if float(cycle_frame["sample_interval_s"].median())
                    >= STEADY_WINDOW_S
                    else "阶段最后 1 s 中位数"
                ),
                "is_primary_series": spec["is_primary_series"],
                "is_special_series": spec["is_special_series"],
                "special_file_name": spec["special_file_name"],
                "include_in_summary_atlas": spec.get(
                    "include_in_summary_atlas", True
                ),
                "material_user_notes": spec.get(
                    "material_user_notes", ""
                ),
                "segment_count": len(spec["records"]),
                "source_files": json.dumps(source_files, ensure_ascii=False),
                "row_count": int(total_rows),
                "duration_s": continuous_offset_s,
                "duration_h": continuous_offset_s / 3600.0,
                "complete_cycles": int(len(cycle_frame)),
                "median_cathodic_phase_duration_s": float(
                    cycle_frame["cathodic_phase_duration_s"].median()
                ),
                "median_reverse_phase_duration_s": float(
                    cycle_frame["reverse_phase_duration_s"].median()
                ),
                "median_cycle_duration_s": float(
                    cycle_frame["cycle_duration_s"].median()
                ),
                "normal_cycles": normal_count,
                "abnormal_cycles": abnormal_count,
                "normal_fraction": normal_count / len(cycle_frame),
                "abnormal_fraction": abnormal_count / len(cycle_frame),
                "cathodic_baseline_first10_median_raw_v": cathodic_baseline,
                "cathodic_endpoint_last10_median_raw_v": cathodic_endpoint,
                "cathodic_negative_shift_first10_to_last10_mv": 1000.0
                * (cathodic_baseline - cathodic_endpoint),
                "reverse_baseline_first10_median_raw_v": reverse_baseline,
                "reverse_endpoint_last10_median_raw_v": reverse_endpoint,
                "reverse_shift_first10_to_last10_mv": 1000.0
                * (reverse_endpoint - reverse_baseline),
                "linear_cathodic_negative_shift_rate_mv_per_100_cycles": -1000.0
                * slope
                * 100.0,
                "linear_fit_intercept_raw_v": intercept,
                "linear_fit_r_squared": r_squared,
                "raw_potential_offset_v": RAW_POTENTIAL_OFFSET_V,
                "ir_correction_applied": IR_CORRECTION_APPLIED,
            }
        )
        all_cycles.append(cycle_frame)
        all_overview.extend(series_overview)

        total_cycles = int(len(cycle_frame))
        target_cycles = sorted(
            {1, int(math.ceil(total_cycles / 2.0)), total_cycles}
        )
        target_labels = {
            target_cycles[0]: "首循环",
            target_cycles[-1]: "末循环",
        }
        if len(target_cycles) == 3:
            target_labels[target_cycles[1]] = "中间循环"
        target_rows = cycle_frame[
            cycle_frame["cycle"].isin(target_cycles)
        ].copy()
        for _, target in target_rows.iterrows():
            segment_index = int(target["segment_index"])
            local_cycle = int(target["cycle_in_segment"])
            record = spec["records"][segment_index - 1]
            frame = data_cache[record["absolute_path"]]
            pair = pairs_cache[(spec["series_id"], segment_index)][local_cycle - 1]
            cathodic_start, cathodic_end, reverse_start, reverse_end = pair
            times = frame["time_s"].to_numpy(dtype=float)
            values_hghgo = frame["potential_hghgo_v"].to_numpy(dtype=float)
            values_raw = values_hghgo.copy()
            currents = frame["current_a_cm2"].to_numpy(dtype=float)
            segment_summary = segment_rows[-len(spec["records"]) + segment_index - 1]
            segment_offset = float(segment_summary["continuous_time_start_s"])
            segment_t0 = float(times[0])

            cathodic_elapsed = (
                times[cathodic_start:cathodic_end] - times[cathodic_start]
            )
            reverse_elapsed = (
                times[reverse_start:reverse_end] - times[reverse_start]
            )
            cathodic_duration_s = float(
                cathodic_elapsed[-1] + positive_median_step(cathodic_elapsed)
            )
            cathodic_indices = np.arange(cathodic_start, cathodic_end)
            reverse_indices = np.arange(reverse_start, reverse_end)
            point_indices = np.r_[cathodic_indices, reverse_indices]
            cycle_time = np.r_[
                cathodic_elapsed,
                cathodic_duration_s + reverse_elapsed,
            ]
            representative = pd.DataFrame(
                {
                    "series_id": spec["series_id"],
                    "series_order": spec["series_order"],
                    "series_display_name": spec["series_display_name"],
                    "material_id": spec["material_id"],
                    "material_display_name": spec["material_display_name"],
                    "test_type": spec["test_type"],
                    "test_type_label_zh": spec["test_type_label_zh"],
                    "cycle": int(target["cycle"]),
                    "cycle_position": target_labels[int(target["cycle"])],
                    "segment_index": segment_index,
                    "source_file": record["relative_path"],
                    "cycle_time_s": cycle_time,
                    "continuous_time_s": segment_offset
                    + (times[point_indices] - segment_t0),
                    "current_a_cm2": currents[point_indices],
                    "phase": np.r_[
                        np.repeat("阴极段", len(cathodic_indices)),
                        np.repeat("反向段", len(reverse_indices)),
                    ],
                    "potential_hghgo_v": values_hghgo[point_indices],
                    "potential_raw_v": values_raw[point_indices],
                }
            )
            all_representative.append(representative)

        report_render_progress(
            phase="analyzing_series",
            phase_label="计算启停循环",
            phase_index=3,
            percent=24.0 + 28.0 * series_index / max(series_total, 1),
            completed=series_index,
            total=series_total,
            unit="materials",
            current_item="已完成 · {}".format(series_name),
            detail="正在逐材料识别完整循环并计算阴极与恢复段指标",
        )

    cycle_summary = pd.concat(all_cycles, ignore_index=True)
    overview_summary = pd.concat(all_overview, ignore_index=True)
    representative_summary = pd.concat(all_representative, ignore_index=True)
    series_summary = pd.DataFrame(series_rows).sort_values("series_order")
    segment_summary = pd.DataFrame(segment_rows).sort_values(
        ["series_id", "segment_index"]
    )
    boundaries = pd.DataFrame(boundary_rows)
    if boundaries.empty:
        boundaries = pd.DataFrame(
            columns=[
                "series_id",
                "series_display_name",
                "material_id",
                "segment_index",
                "boundary_time_s",
                "boundary_time_h",
                "source_file",
            ]
        )
    return (
        cycle_summary,
        overview_summary,
        representative_summary,
        series_summary,
        segment_summary,
        boundaries,
    )


def make_material_summary(
    materials: List[dict], series_summary: pd.DataFrame
) -> pd.DataFrame:
    material_frame = pd.DataFrame(materials)
    primary = series_summary[series_summary["is_primary_series"]].copy()
    drop_columns = [
        "series_order",
        "series_display_name",
        "material_id",
        "material_display_name",
        "material_relative_path",
        "workstation",
        "is_primary_series",
        "is_special_series",
        "special_file_name",
        "include_in_summary_atlas",
        "material_user_notes",
        "test_type",
        "test_type_label_zh",
        "cathodic_current_median_a_cm2",
        "recovery_current_median_a_cm2",
        "endpoint_statistic",
    ]
    primary = primary.drop(columns=drop_columns)
    return material_frame.merge(
        primary,
        left_on="primary_series_id",
        right_on="series_id",
        how="left",
        validate="one_to_one",
    ).sort_values("material_id")


def annotate_config_fields(
    frames: Sequence[pd.DataFrame],
    series_summary: pd.DataFrame,
) -> None:
    include_by_series = series_summary.set_index("series_id")[
        "include_in_summary_atlas"
    ].to_dict()
    notes_by_series = series_summary.set_index("series_id")[
        "material_user_notes"
    ].to_dict()
    for frame in frames:
        if "series_id" not in frame.columns:
            continue
        frame["include_in_summary_atlas"] = frame["series_id"].map(
            include_by_series
        ).fillna(False).astype(bool)
        frame["material_user_notes"] = frame["series_id"].map(
            notes_by_series
        ).fillna("")


def configure_matplotlib() -> None:
    global FONT_REGULAR_NAME, FONT_BOLD_NAME
    ensure_matplotlib()
    for font_path in (FONT_REGULAR_PATH, FONT_BOLD_PATH):
        if font_path.exists():
            fontManager.addfont(str(font_path))
    FONT_REGULAR_NAME = (
        FontProperties(fname=str(FONT_REGULAR_PATH)).get_name()
        if FONT_REGULAR_PATH.exists()
        else "DejaVu Sans"
    )
    FONT_BOLD_NAME = (
        FontProperties(fname=str(FONT_BOLD_PATH)).get_name()
        if FONT_BOLD_PATH.exists()
        else FONT_REGULAR_NAME
    )
    mpl.rcParams.update(
        {
            "font.family": [FONT_REGULAR_NAME, "DejaVu Sans"],
            "font.size": 10.0,
            "axes.unicode_minus": False,
            "axes.edgecolor": AXIS,
            "axes.labelcolor": INK,
            "axes.titlecolor": INK,
            "axes.linewidth": 0.9,
            "xtick.color": MUTED,
            "ytick.color": MUTED,
            "xtick.major.size": 0,
            "ytick.major.size": 0,
            "grid.color": GRID,
            "grid.linewidth": 0.7,
            "grid.alpha": 0.72,
            "figure.facecolor": PAPER,
            "axes.facecolor": PAPER,
            "savefig.facecolor": PAPER,
            "savefig.edgecolor": PAPER,
            "svg.fonttype": "none",
            "pdf.fonttype": 42,
        }
    )


def add_blossom(fig: plt.Figure, x: float = 0.967, y: float = 0.970) -> None:
    for angle in np.linspace(0, 2 * math.pi, 6)[:-1]:
        fig.add_artist(
            Circle(
                (
                    x + 0.0095 * math.cos(float(angle)),
                    y + 0.0095 * math.sin(float(angle)),
                ),
                0.0038,
                transform=fig.transFigure,
                facecolor=BLUE,
                edgecolor="none",
                zorder=20,
            )
        )
    fig.add_artist(
        Circle(
            (x, y),
            0.0032,
            transform=fig.transFigure,
            facecolor=PAPER,
            edgecolor="none",
            zorder=21,
        )
    )


def add_header(
    fig: plt.Figure,
    title: str,
    subtitle: str,
    note: str = "",
    title_y: float = 0.985,
) -> None:
    fig.text(
        0.045,
        title_y,
        title,
        fontsize=20,
        fontfamily=FONT_BOLD_NAME,
        fontweight="semibold",
        color=INK,
        ha="left",
        va="top",
    )
    fig.text(
        0.045,
        title_y - 0.038,
        subtitle,
        fontsize=10.5,
        color=MUTED,
        ha="left",
        va="top",
    )
    if note:
        fig.text(
            0.045,
            title_y - 0.064,
            note,
            fontsize=9.2,
            color=MUTED,
            ha="left",
            va="top",
        )
    add_blossom(fig, y=title_y - 0.012)


def style_axis(ax: plt.Axes, grid_axis: str = "both") -> None:
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(True, axis=grid_axis, zorder=0)
    ax.set_axisbelow(True)


def wrap_full_label(value: str, width: int = 36) -> str:
    return textwrap.fill(
        str(value),
        width=width,
        break_long_words=True,
        break_on_hyphens=True,
    )


def protocol_caption(summary: pd.Series) -> str:
    label = str(
        summary.get("test_type_label_zh", summary.get("test_types", "启停"))
    )
    cathodic = float(summary["cathodic_current_median_a_cm2"])
    recovery = float(summary["recovery_current_median_a_cm2"])
    return "{}｜阴极 {:.3f}、恢复 {:.3f} A cm⁻²".format(
        label, cathodic, recovery
    )


def series_panel_title(summary: pd.Series, width: int = 40) -> str:
    return "{}\n{}".format(
        wrap_full_label(summary["series_display_name"], width),
        protocol_caption(summary),
    )


def material_axis_label(summary: pd.Series, width: int = 58) -> str:
    return wrap_full_label(
        "{}｜{}".format(
            summary["material_display_name"], protocol_caption(summary)
        ),
        width,
    )


def padded_limits(values: Iterable[float], fraction: float = 0.05) -> Tuple[float, float]:
    array = np.asarray(list(values), dtype=float)
    array = array[np.isfinite(array)]
    lower = float(np.min(array))
    upper = float(np.max(array))
    if upper == lower:
        return lower - 0.1, upper + 0.1
    padding = (upper - lower) * fraction
    return lower - padding, upper + padding


def add_segment_boundaries(
    ax: plt.Axes, boundaries: pd.DataFrame, series_id: str
) -> None:
    subset = boundaries[boundaries["series_id"] == series_id]
    for _, row in subset.iterrows():
        ax.axvline(
            float(row["boundary_time_h"]),
            color=NEUTRAL_DARK,
            linewidth=1.0,
            linestyle="--",
            alpha=0.8,
            zorder=2,
        )


def make_faceted_figure(
    count: int,
    sharey: bool = False,
    height_per_row: float = 4.0,
) -> Tuple[plt.Figure, np.ndarray]:
    columns = 3
    rows = int(math.ceil(count / columns))
    fig, axes = plt.subplots(
        rows,
        columns,
        figsize=(21.0, 2.6 + rows * height_per_row),
        sharey=sharey,
        squeeze=False,
    )
    fig.subplots_adjust(
        left=0.062,
        right=0.978,
        top=0.880,
        bottom=0.055,
        hspace=0.62,
        wspace=0.25,
    )
    return fig, axes


def save_figure(
    fig: plt.Figure, stem: Path, pdf: PdfPages, dpi: int = 180
) -> Tuple[Path, Path]:
    ensure_matplotlib()
    png = stem.with_suffix(".png")
    svg = stem.with_suffix(".svg")
    report_figure_progress(stem, completed=False)
    if EXPORT_STATIC_FIGURES:
        fig.savefig(png, dpi=dpi, bbox_inches="tight")
        fig.savefig(svg, bbox_inches="tight")
    pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)
    report_figure_progress(stem, completed=True)
    return png, svg


def plot_overview(
    overview: pd.DataFrame,
    cycles: pd.DataFrame,
    series_summary: pd.DataFrame,
    boundaries: pd.DataFrame,
    pdf: PdfPages,
) -> None:
    ordered = series_summary.sort_values("series_order")
    fig, axes = make_faceted_figure(len(ordered), sharey=True)
    fig.subplots_adjust(top=0.850)
    add_header(
        fig,
        "全部启停序列的电位—时间概览",
        "纵轴为仪器原始电位（vs Hg/HgO）；不做参比换算或 iR 补偿",
        "方波启停取阶段最后 1 s 中位数；ADT 取阶段末个采样点；虚线为接续文件边界",
    )
    y_limits = padded_limits(overview["potential_raw_v"], 0.025)
    for ax, (_, summary) in zip(axes.flat, ordered.iterrows()):
        series_id = summary["series_id"]
        raw = overview[overview["series_id"] == series_id].sort_values(
            "continuous_time_s"
        )
        subset = cycles[cycles["series_id"] == series_id].sort_values("cycle")
        ax.plot(
            raw["continuous_time_h"],
            raw["potential_raw_v"],
            color=NEUTRAL,
            linewidth=0.45,
            alpha=0.34,
            zorder=1,
        )
        ax.plot(
            subset["cathodic_endpoint_time_h"],
            subset["cathodic_last1s_median_raw_v"],
            color=BLUE,
            linewidth=1.25,
            zorder=3,
        )
        ax.plot(
            subset["reverse_endpoint_time_h"],
            subset["reverse_last1s_median_raw_v"],
            color=ORANGE,
            linewidth=1.20,
            zorder=3,
        )
        add_segment_boundaries(ax, boundaries, series_id)
        ax.set_title(
            series_panel_title(summary, 40),
            fontsize=9.6,
            loc="left",
            pad=9,
        )
        ax.set_xlabel("接续后时间 / h")
        ax.set_ylabel("E vs Hg/HgO / V")
        ax.set_ylim(y_limits)
        style_axis(ax)
    for ax in axes.flat[len(ordered) :]:
        ax.axis("off")
    fig.legend(
        handles=[
            Line2D([0], [0], color=NEUTRAL, lw=1.2, label="抽样原始轨迹"),
            Line2D([0], [0], color=BLUE, lw=2.0, label="阴极段末端统计值"),
            Line2D([0], [0], color=ORANGE, lw=2.0, label="恢复段末端统计值"),
            Line2D(
                [0],
                [0],
                color=NEUTRAL_DARK,
                lw=1.2,
                ls="--",
                label="接续文件边界",
            ),
        ],
        loc="upper center",
        bbox_to_anchor=(0.55, 0.898),
        ncol=4,
        frameon=False,
    )
    save_figure(fig, FIGURE_DIR / "01_全程原始电位_HgHgO_分面", pdf)


def plot_cycle_metric(
    cycles: pd.DataFrame,
    series_summary: pd.DataFrame,
    field: str,
    title: str,
    subtitle: str,
    note: str,
    y_label: str,
    output_name: str,
    color: str,
    pdf: PdfPages,
    zero_line: bool = False,
) -> None:
    ordered = series_summary.sort_values("series_order")
    fig, axes = make_faceted_figure(len(ordered), sharey=True)
    add_header(fig, title, subtitle, note)
    y_limits = padded_limits(cycles[field], 0.055)
    if zero_line:
        lower, upper = y_limits
        y_limits = (min(lower, 0.0), max(upper, 0.0))
    for ax, (_, summary) in zip(axes.flat, ordered.iterrows()):
        series_id = summary["series_id"]
        subset = cycles[cycles["series_id"] == series_id].sort_values("cycle")
        ax.plot(
            subset["cycle"],
            subset[field],
            color=color,
            linewidth=1.35,
            zorder=3,
        )
        if zero_line:
            ax.axhline(0.0, color=NEUTRAL_DARK, linewidth=0.9, zorder=1)
        ax.set_title(
            series_panel_title(summary, 40),
            fontsize=9.6,
            loc="left",
            pad=9,
        )
        ax.set_xlabel("连续循环编号")
        ax.set_ylabel(y_label)
        ax.set_ylim(y_limits)
        style_axis(ax)
    for ax in axes.flat[len(ordered) :]:
        ax.axis("off")
    save_figure(fig, FIGURE_DIR / output_name, pdf)


def plot_representative_cycles(
    representative: pd.DataFrame,
    series_summary: pd.DataFrame,
    pdf: PdfPages,
) -> None:
    ordered = series_summary.sort_values("series_order")
    fig, axes = make_faceted_figure(
        len(ordered), sharey=True, height_per_row=4.15
    )
    add_header(
        fig,
        "首—中—末代表循环响应",
        "每个序列各取首循环、中间循环和末循环；蓝底为阴极段，橙底为反向段",
        "横轴按各程序的实际阶段时长绘制；所有曲线均为原始 Hg/HgO 电位，未做 iR 补偿",
    )
    colors = {"首循环": BLUE, "中间循环": GOLD, "末循环": PINK}
    styles = {"首循环": "-", "中间循环": "--", "末循环": "-"}
    y_limits = padded_limits(representative["potential_raw_v"], 0.025)
    for ax, (_, summary) in zip(axes.flat, ordered.iterrows()):
        series_id = summary["series_id"]
        subset = representative[
            representative["series_id"] == series_id
        ].copy()
        cathodic_points = subset[subset["phase"] == "阴极段"]
        reverse_points = subset[subset["phase"] == "反向段"]
        phase_boundary = float(reverse_points["cycle_time_s"].min())
        sample_step = positive_median_step(
            subset.sort_values("cycle_time_s")["cycle_time_s"].to_numpy(
                dtype=float
            )
        )
        cycle_end = float(subset["cycle_time_s"].max() + sample_step)
        ax.axvspan(
            0,
            phase_boundary,
            color=PALE_BLUE,
            alpha=0.55,
            zorder=0,
        )
        ax.axvspan(
            phase_boundary,
            cycle_end,
            color=PALE_ORANGE,
            alpha=0.55,
            zorder=0,
        )
        for position in ("首循环", "中间循环", "末循环"):
            group = subset[subset["cycle_position"] == position].sort_values(
                "cycle_time_s"
            )
            if group.empty:
                continue
            cycle_number = int(group["cycle"].iloc[0])
            ax.plot(
                group["cycle_time_s"],
                group["potential_raw_v"],
                color=colors[position],
                linestyle=styles[position],
                linewidth=1.25,
                label="{}（第 {} 循环）".format(position, cycle_number),
                zorder=3,
            )
        ax.axvline(
            phase_boundary,
            color=NEUTRAL_DARK,
            linewidth=0.8,
            zorder=2,
        )
        ax.set_title(
            series_panel_title(summary, 40),
            fontsize=9.6,
            loc="left",
            pad=9,
        )
        ax.set_xlim(0, cycle_end)
        ax.set_ylim(y_limits)
        ax.set_xlabel("单循环时间 / s")
        ax.set_ylabel("E vs Hg/HgO / V")
        style_axis(ax)
        ax.legend(fontsize=7.8, frameon=False, loc="best")
    for ax in axes.flat[len(ordered) :]:
        ax.axis("off")
    save_figure(fig, FIGURE_DIR / "05_代表性循环响应", pdf)


def plot_material_shift_comparison(
    material_summary: pd.DataFrame, pdf: PdfPages
) -> None:
    data = material_summary.sort_values(
        "cathodic_negative_shift_first10_to_last10_mv", ascending=True
    ).copy()
    values = data[
        "cathodic_negative_shift_first10_to_last10_mv"
    ].to_numpy(dtype=float)
    labels = [material_axis_label(row, 58) for _, row in data.iterrows()]
    colors = [ORANGE if value >= 0 else BLUE for value in values]
    fig, ax = plt.subplots(figsize=(17.5, 11.5))
    fig.subplots_adjust(left=0.47, right=0.95, top=0.84, bottom=0.11)
    add_header(
        fig,
        "各材料阴极段电位负移对比",
        "指标 = 前 10 循环阴极末端统计值 − 后 10 循环阴极末端统计值",
        "正值表示阴极电位变得更负；测试类型与实际电流随材料标签列出，跨电流仅作描述性对照；共 {} 个材料".format(
            len(material_summary)
        ),
    )
    y = np.arange(len(data))
    ax.barh(
        y,
        values,
        color=colors,
        edgecolor=[BLUE_DARK if value < 0 else "#A95A05" for value in values],
        linewidth=0.8,
        zorder=3,
    )
    ax.axvline(0.0, color=NEUTRAL_DARK, linewidth=1.0, zorder=2)
    span = max(float(np.max(np.abs(values))), 1.0)
    for index, value in enumerate(values):
        if value >= 0:
            label_x = value + 0.018 * span
            label_alignment = "left"
        else:
            label_x = 0.018 * span
            label_alignment = "left"
        ax.text(
            label_x,
            index,
            "{:+.1f} mV".format(value),
            ha=label_alignment,
            va="center",
            fontsize=9.0,
            color=BLUE_DARK if value < 0 else INK,
        )
    ax.set_xlim(
        min(float(np.min(values)) * 1.15, -0.14 * span),
        max(float(np.max(values)) * 1.12, 0.14 * span),
    )
    ax.set_yticks(y)
    ax.set_yticklabels(labels, fontsize=9.5)
    ax.set_xlabel("阴极电位负移 / mV")
    ax.set_ylabel("")
    style_axis(ax, grid_axis="x")
    save_figure(fig, FIGURE_DIR / "06_材料阴极负移对比", pdf)


def plot_anomaly_facets(
    cycles: pd.DataFrame,
    series_summary: pd.DataFrame,
    boundaries: pd.DataFrame,
    pdf: PdfPages,
) -> None:
    ordered = series_summary.sort_values("series_order")
    fig, axes = make_faceted_figure(
        len(ordered), sharey=True, height_per_row=4.10
    )
    fig.subplots_adjust(top=0.850)
    add_header(
        fig,
        "阴极偏移异常判断图",
        "每个点为阴极段末端统计值；方波启停取最后 1 s 中位数，ADT 取末个采样点",
        "阴极段最低点位于 0–<15 s：异常（红叉）；位于 ≥15 s 至该段结束：正常（绿点）",
    )
    y_limits = padded_limits(
        cycles["cathodic_last1s_median_raw_v"], 0.045
    )
    for ax, (_, summary) in zip(axes.flat, ordered.iterrows()):
        series_id = summary["series_id"]
        subset = cycles[cycles["series_id"] == series_id].sort_values("cycle")
        normal = subset[subset["cathodic_shift_status"] == "normal"]
        abnormal = subset[subset["cathodic_shift_status"] == "abnormal"]
        ax.plot(
            subset["cathodic_endpoint_time_h"],
            subset["cathodic_last1s_median_raw_v"],
            color=NEUTRAL,
            linewidth=0.75,
            alpha=0.75,
            zorder=1,
        )
        ax.scatter(
            normal["cathodic_endpoint_time_h"],
            normal["cathodic_last1s_median_raw_v"],
            s=13,
            marker="o",
            facecolor=NORMAL_GREEN,
            edgecolor="white",
            linewidth=0.25,
            alpha=0.88,
            zorder=3,
        )
        ax.scatter(
            abnormal["cathodic_endpoint_time_h"],
            abnormal["cathodic_last1s_median_raw_v"],
            s=18,
            marker="x",
            color=ANOMALY_RED,
            linewidth=0.75,
            alpha=0.88,
            zorder=4,
        )
        add_segment_boundaries(ax, boundaries, series_id)
        ax.set_title(
            series_panel_title(summary, 40),
            fontsize=9.6,
            loc="left",
            pad=9,
        )
        ax.text(
            0.99,
            0.98,
            "正常 {}｜异常 {}".format(len(normal), len(abnormal)),
            transform=ax.transAxes,
            ha="right",
            va="top",
            fontsize=8.2,
            color=MUTED,
        )
        ax.set_xlabel("接续后时间 / h")
        ax.set_ylabel("阴极段末端统计值（vs Hg/HgO）/ V")
        ax.set_ylim(y_limits)
        style_axis(ax)
    for ax in axes.flat[len(ordered) :]:
        ax.axis("off")
    fig.legend(
        handles=[
            Line2D(
                [0],
                [0],
                marker="o",
                color="none",
                markerfacecolor=NORMAL_GREEN,
                markeredgecolor="white",
                markersize=7,
                label="正常：最低点在 ≥15 s 至阶段结束",
            ),
            Line2D(
                [0],
                [0],
                marker="x",
                color=ANOMALY_RED,
                linestyle="none",
                markersize=7,
                label="异常：最低点在 0–<15 s",
            ),
            Line2D(
                [0],
                [0],
                color=NEUTRAL_DARK,
                lw=1.2,
                ls="--",
                label="接续文件边界",
            ),
        ],
        loc="upper center",
        bbox_to_anchor=(0.55, 0.898),
        ncol=3,
        frameon=False,
    )
    save_figure(fig, FIGURE_DIR / "07_阴极偏移异常判断", pdf)


def plot_anomaly_fraction(
    material_summary: pd.DataFrame, pdf: PdfPages
) -> None:
    data = material_summary.sort_values("abnormal_fraction", ascending=False).copy()
    labels = [material_axis_label(row, 58) for _, row in data.iterrows()]
    abnormal = data["abnormal_fraction"].to_numpy(dtype=float)
    normal = data["normal_fraction"].to_numpy(dtype=float)
    y = np.arange(len(data))
    fig, ax = plt.subplots(figsize=(17.5, 11.5))
    fig.subplots_adjust(left=0.47, right=0.95, top=0.84, bottom=0.11)
    add_header(
        fig,
        "各材料阴极偏移正常/异常循环占比",
        "异常：阴极段最低点位于 0–<15 s；正常：位于 ≥15 s 至阶段结束",
        "仅比较 {} 个材料主序列；测试类型与实际电流随材料标签列出".format(
            len(material_summary)
        ),
    )
    ax.barh(
        y,
        normal,
        color=NORMAL_GREEN,
        edgecolor="#176B42",
        linewidth=0.7,
        label="正常",
        zorder=3,
    )
    ax.barh(
        y,
        abnormal,
        left=normal,
        color=ANOMALY_RED,
        edgecolor="#982C2C",
        linewidth=0.7,
        label="异常",
        zorder=3,
    )
    for index, value in enumerate(abnormal):
        ax.text(
            1.012,
            index,
            "异常 {:.1%}".format(value),
            transform=ax.get_yaxis_transform(),
            ha="left",
            va="center",
            fontsize=8.8,
            color=INK,
        )
    ax.set_yticks(y)
    ax.set_yticklabels(labels, fontsize=9.5)
    ax.set_xlim(0, 1)
    ax.set_xlabel("循环占比")
    ax.xaxis.set_major_formatter(PercentFormatter(1.0))
    ax.invert_yaxis()
    ax.legend(frameon=False, loc="lower right", ncol=2)
    style_axis(ax, grid_axis="x")
    save_figure(fig, FIGURE_DIR / "08_异常比例对比", pdf)


def plot_all_material_overlay(
    cycles: pd.DataFrame,
    series_summary: pd.DataFrame,
    pdf: PdfPages,
) -> None:
    ordered = series_summary[
        series_summary["is_primary_series"]
    ].sort_values("series_order")
    palette = OVERLAY_HIGH_CONTRAST_PALETTE
    line_styles = OVERLAY_LINE_STYLES
    if len(ordered) > len(palette) * len(line_styles):
        raise RuntimeError(
            "多材料叠加图超过高对比颜色×两种线形的可用组合"
        )

    fig, axes = plt.subplots(1, 2, figsize=(20.0, 15.0))
    fig.subplots_adjust(
        left=0.070,
        right=0.982,
        top=0.835,
        bottom=0.310,
        wspace=0.180,
    )
    add_header(
        fig,
        "全部材料阴极段叠加对比",
        "所有 {} 种材料绘制在同一坐标系；左为阴极末端统计值，右为相对前 10 循环的阴极负移".format(
            len(ordered)
        ),
        "高对比配色且每色最多实线/虚线两种线形；方波启停取最后 1 s 中位数，ADT 取末个采样点",
    )

    legend_handles: List[Line2D] = []
    legend_labels: List[str] = []
    for index, (_, summary) in enumerate(ordered.iterrows()):
        subset = cycles[
            cycles["series_id"] == summary["series_id"]
        ].sort_values("cycle")
        color = palette[index % len(palette)]
        line_style = line_styles[index // len(palette)]
        left_line = axes[0].plot(
            subset["cycle"],
            subset["cathodic_last1s_median_raw_v"],
            color=color,
            linestyle=line_style,
            linewidth=1.25,
            alpha=0.88,
            zorder=2,
        )[0]
        axes[1].plot(
            subset["cycle"],
            subset["cathodic_negative_shift_mv"],
            color=color,
            linestyle=line_style,
            linewidth=1.25,
            alpha=0.88,
            zorder=2,
        )
        legend_handles.append(left_line)
        legend_labels.append(material_axis_label(summary, 62))

    axes[0].set_title(
        "阴极段末端统计电位",
        loc="left",
        fontsize=12.2,
        pad=11,
    )
    axes[0].set_xlabel("连续循环编号")
    axes[0].set_ylabel("阴极段末端统计值（vs Hg/HgO）/ V")
    style_axis(axes[0])

    axes[1].axhline(
        0.0,
        color=NEUTRAL_DARK,
        linewidth=0.95,
        zorder=1,
    )
    axes[1].set_title(
        "阴极段电位负移",
        loc="left",
        fontsize=12.2,
        pad=11,
    )
    axes[1].set_xlabel("连续循环编号")
    axes[1].set_ylabel("相对前 10 循环 / mV")
    style_axis(axes[1])

    fig.legend(
        handles=legend_handles,
        labels=legend_labels,
        loc="lower center",
        bbox_to_anchor=(0.5, 0.045),
        ncol=3 if len(ordered) > 22 else 2,
        frameon=False,
        fontsize=8.4,
        handlelength=3.8,
        handletextpad=0.8,
        columnspacing=2.2,
        labelspacing=1.05,
    )
    save_figure(fig, FIGURE_DIR / "09_全部材料阴极叠加对比", pdf, dpi=190)


def plot_series_details(
    overview: pd.DataFrame,
    cycles: pd.DataFrame,
    representative: pd.DataFrame,
    series_summary: pd.DataFrame,
    segment_summary: pd.DataFrame,
    boundaries: pd.DataFrame,
    pdf: PdfPages,
) -> None:
    for _, summary in series_summary.sort_values("series_order").iterrows():
        series_id = summary["series_id"]
        raw = overview[overview["series_id"] == series_id].sort_values(
            "continuous_time_s"
        )
        cycle = cycles[cycles["series_id"] == series_id].sort_values("cycle")
        representative_subset = representative[
            representative["series_id"] == series_id
        ]
        segments = segment_summary[
            segment_summary["series_id"] == series_id
        ].sort_values("segment_index")
        normal = cycle[cycle["cathodic_shift_status"] == "normal"]
        abnormal = cycle[cycle["cathodic_shift_status"] == "abnormal"]
        fig, axes = plt.subplots(2, 2, figsize=(16.0, 12.0))
        fig.subplots_adjust(
            left=0.085,
            right=0.965,
            top=0.810,
            bottom=0.105,
            hspace=0.38,
            wspace=0.22,
        )
        add_header(
            fig,
            wrap_full_label(summary["series_display_name"], 82),
            "{}；{}；原始电位基准为 Hg/HgO，不做参比换算".format(
                protocol_caption(summary), summary["endpoint_statistic"]
            ),
            "共 {} 个完整循环，{} 个文件段；正常 {}，异常 {}；未做 iR 补偿".format(
                int(summary["complete_cycles"]),
                int(summary["segment_count"]),
                int(summary["normal_cycles"]),
                int(summary["abnormal_cycles"]),
            ),
            title_y=0.985,
        )

        ax = axes[0, 0]
        ax.plot(
            raw["continuous_time_h"],
            raw["potential_raw_v"],
            color=NEUTRAL,
            linewidth=0.55,
            alpha=0.55,
        )
        ax.plot(
            cycle["cathodic_endpoint_time_h"],
            cycle["cathodic_last1s_median_raw_v"],
            color=BLUE,
            linewidth=1.35,
        )
        ax.plot(
            cycle["reverse_endpoint_time_h"],
            cycle["reverse_last1s_median_raw_v"],
            color=ORANGE,
            linewidth=1.30,
        )
        add_segment_boundaries(ax, boundaries, series_id)
        ax.set_title("全程电位—时间", loc="left", fontsize=11.5)
        ax.set_xlabel("接续后时间 / h")
        ax.set_ylabel("E vs Hg/HgO / V")
        style_axis(ax)

        ax = axes[0, 1]
        ax.plot(
            cycle["cycle"],
            cycle["cathodic_last1s_median_raw_v"],
            color=BLUE,
            linewidth=1.35,
            label="阴极段",
        )
        ax.plot(
            cycle["cycle"],
            cycle["reverse_last1s_median_raw_v"],
            color=ORANGE,
            linewidth=1.25,
            label="反向段",
        )
        ax.set_title("逐循环末端统计电位", loc="left", fontsize=11.5)
        ax.set_xlabel("连续循环编号")
        ax.set_ylabel("E vs Hg/HgO / V")
        ax.legend(frameon=False, ncol=2)
        style_axis(ax)

        ax = axes[1, 0]
        ax.plot(
            cycle["cycle"],
            cycle["cathodic_negative_shift_mv"],
            color=ORANGE,
            linewidth=1.35,
        )
        ax.axhline(0.0, color=NEUTRAL_DARK, linewidth=0.9)
        ax.set_title("阴极段电位负移", loc="left", fontsize=11.5)
        ax.set_xlabel("连续循环编号")
        ax.set_ylabel("相对前 10 循环 / mV")
        style_axis(ax)

        ax = axes[1, 1]
        ax.plot(
            cycle["cathodic_endpoint_time_h"],
            cycle["cathodic_last1s_median_raw_v"],
            color=NEUTRAL,
            linewidth=0.75,
            alpha=0.75,
        )
        ax.scatter(
            normal["cathodic_endpoint_time_h"],
            normal["cathodic_last1s_median_raw_v"],
            s=18,
            marker="o",
            color=NORMAL_GREEN,
            edgecolor="white",
            linewidth=0.25,
            label="正常",
            zorder=3,
        )
        ax.scatter(
            abnormal["cathodic_endpoint_time_h"],
            abnormal["cathodic_last1s_median_raw_v"],
            s=23,
            marker="x",
            color=ANOMALY_RED,
            linewidth=0.8,
            label="异常",
            zorder=4,
        )
        add_segment_boundaries(ax, boundaries, series_id)
        ax.set_title("阴极偏移异常判断", loc="left", fontsize=11.5)
        ax.set_xlabel("接续后时间 / h")
        ax.set_ylabel("阴极段末端统计值（vs Hg/HgO）/ V")
        ax.legend(frameon=False, ncol=2)
        style_axis(ax)

        sources = "；".join(
            "{}: {}".format(int(row["segment_index"]), row["source_file"])
            for _, row in segments.iterrows()
        )
        fig.text(
            0.085,
            0.035,
            "源文件：{}".format(wrap_full_label(sources, 145)),
            ha="left",
            va="bottom",
            fontsize=7.8,
            color=MUTED,
        )
        stem = DETAIL_FIGURE_DIR / "{}_详细图".format(series_id)
        save_figure(fig, stem, pdf, dpi=190)


def render_all_figures(
    overview: pd.DataFrame,
    cycles: pd.DataFrame,
    representative: pd.DataFrame,
    series_summary: pd.DataFrame,
    material_summary: pd.DataFrame,
    segment_summary: pd.DataFrame,
    boundaries: pd.DataFrame,
) -> List[dict]:
    if series_summary.empty or material_summary.empty:
        raise RuntimeError("总结图集至少需要选择 1 种材料")
    figure_total = 9 + len(series_summary)
    begin_figure_progress(
        phase="rendering_standard",
        phase_label="生成标准图集",
        phase_index=4,
        total=figure_total,
        percent_start=60,
        percent_end=80,
        detail=(
            "每页均在生成 PNG、SVG，并写入标准 PDF 图集"
            if EXPORT_STATIC_FIGURES
            else "正在逐页写入标准 PDF 图集"
        ),
    )
    for path in DETAIL_FIGURE_DIR.glob("*_详细图.*"):
        if path.suffix.lower() in {".png", ".svg"}:
            path.unlink()
    configure_matplotlib()
    chart_rows: List[dict] = []
    pdf_path = OUTPUT_DIR / "启停数据_原始电位_全量图集.pdf"
    with PdfPages(pdf_path) as pdf:
        plot_overview(overview, cycles, series_summary, boundaries, pdf)
        chart_rows.append(
            {
                "chart_id": "01",
                "chart_title": "全部启停序列的电位—时间概览",
                "analytical_question": "每个序列的全程电位响应与文件接续位置是什么",
                "chart_family": "Trend",
                "chart_type": "faceted line",
                "fields": "continuous_time_h,potential_raw_v,phase",
                "output": "figures/01_全程原始电位_HgHgO_分面.png",
            }
        )
        plot_cycle_metric(
            cycles,
            series_summary,
            "cathodic_last1s_median_raw_v",
            "阴极段末端统计电位",
            "方波启停取阴极段最后 1 s 中位数；ADT 取阴极段末个采样点；循环编号按主序列接续",
            "纵轴为原始 Hg/HgO 电位；图内列出测试类型与实际电流；未做 iR 补偿",
            "阴极段末端统计值（vs Hg/HgO）/ V",
            "02_阴极段原始电位_HgHgO",
            BLUE,
            pdf,
        )
        chart_rows.append(
            {
                "chart_id": "02",
                "chart_title": "阴极段末端统计电位",
                "analytical_question": "阴极稳态电位如何随循环变化",
                "chart_family": "Trend",
                "chart_type": "faceted line",
                "fields": "cycle,cathodic_last1s_median_raw_v",
                "output": "figures/02_阴极段原始电位_HgHgO.png",
            }
        )
        plot_cycle_metric(
            cycles,
            series_summary,
            "cathodic_negative_shift_mv",
            "阴极段电位负移",
            "以每个序列前 10 循环的阴极段末端统计值为基线",
            "正值表示电位比初始基线更负；主序列跨文件时保持连续循环编号",
            "阴极电位负移 / mV",
            "03_阴极段电位负移",
            ORANGE,
            pdf,
            zero_line=True,
        )
        chart_rows.append(
            {
                "chart_id": "03",
                "chart_title": "阴极段电位负移",
                "analytical_question": "相对初始 10 个循环的阴极负移如何累积",
                "chart_family": "Trend",
                "chart_type": "faceted line with zero reference",
                "fields": "cycle,cathodic_negative_shift_mv",
                "output": "figures/03_阴极段电位负移.png",
            }
        )
        plot_cycle_metric(
            cycles,
            series_summary,
            "reverse_last1s_median_raw_v",
            "恢复段末端统计电位",
            "方波启停取反向段最后 1 s 中位数；ADT 取恢复段末个采样点；循环编号按主序列接续",
            "纵轴为原始 Hg/HgO 电位；各程序按实际阶段时长分段；未做 iR 补偿",
            "恢复段末端统计值（vs Hg/HgO）/ V",
            "04_反向段原始电位_HgHgO",
            ORANGE,
            pdf,
        )
        chart_rows.append(
            {
                "chart_id": "04",
                "chart_title": "恢复段末端统计电位",
                "analytical_question": "反向段稳态电位如何随循环变化",
                "chart_family": "Trend",
                "chart_type": "faceted line",
                "fields": "cycle,reverse_last1s_median_raw_v",
                "output": "figures/04_反向段原始电位_HgHgO.png",
            }
        )
        plot_representative_cycles(representative, series_summary, pdf)
        chart_rows.append(
            {
                "chart_id": "05",
                "chart_title": "首—中—末代表循环响应",
                "analytical_question": "单循环形状在测试前中后如何变化",
                "chart_family": "Trend",
                "chart_type": "faceted highlighted multi-series line",
                "fields": "cycle_time_s,potential_raw_v,cycle_position",
                "output": "figures/05_代表性循环响应.png",
            }
        )
        plot_material_shift_comparison(material_summary, pdf)
        chart_rows.append(
            {
                "chart_id": "06",
                "chart_title": "各材料阴极段电位负移对比",
                "analytical_question": "{} 个材料主序列的首末阴极负移有多大".format(
                    len(material_summary)
                ),
                "chart_family": "Comparison & Ranking",
                "chart_type": "horizontal diverging bar",
                "fields": "material_display_name,cathodic_negative_shift_first10_to_last10_mv",
                "output": "figures/06_材料阴极负移对比.png",
            }
        )
        plot_anomaly_facets(cycles, series_summary, boundaries, pdf)
        chart_rows.append(
            {
                "chart_id": "07",
                "chart_title": "阴极偏移异常判断图",
                "analytical_question": "每个阴极段最低点出现位置对应正常还是异常",
                "chart_family": "Trend",
                "chart_type": "faceted line with classified points",
                "fields": "cathodic_endpoint_time_h,cathodic_last1s_median_raw_v,cathodic_shift_status",
                "output": "figures/07_阴极偏移异常判断.png",
            }
        )
        plot_anomaly_fraction(material_summary, pdf)
        chart_rows.append(
            {
                "chart_id": "08",
                "chart_title": "各材料阴极偏移正常/异常循环占比",
                "analytical_question": "{} 个材料主序列的异常循环占比如何比较".format(
                    len(material_summary)
                ),
                "chart_family": "Composition",
                "chart_type": "100% stacked horizontal bar",
                "fields": "material_display_name,normal_fraction,abnormal_fraction",
                "output": "figures/08_异常比例对比.png",
            }
        )
        plot_all_material_overlay(cycles, series_summary, pdf)
        chart_rows.append(
            {
                "chart_id": "09",
                "chart_title": "全部材料阴极段叠加对比",
                "analytical_question": "{} 种材料在同一坐标系中的阴极稳态电位与负移如何比较".format(
                    len(series_summary)
                ),
                "chart_family": "Comparison & Trend",
                "chart_type": "overlaid multi-series line",
                "fields": "material_display_name,cycle,cathodic_last1s_median_raw_v,cathodic_negative_shift_mv",
                "output": "figures/09_全部材料阴极叠加对比.png",
            }
        )
        plot_series_details(
            overview,
            cycles,
            representative,
            series_summary,
            segment_summary,
            boundaries,
            pdf,
        )
    report_render_progress(
        phase="rendering_standard",
        phase_label="生成标准图集",
        phase_index=4,
        percent=80,
        completed=figure_total,
        total=figure_total,
        unit="pages",
        current_item="标准图集已完成",
        detail="标准图、材料详细图及 PDF 已全部生成",
    )
    _FIGURE_PROGRESS.clear()
    return chart_rows


def linear_fit_summary(x: np.ndarray, y: np.ndarray) -> dict:
    x_array = np.asarray(x, dtype=float)
    y_array = np.asarray(y, dtype=float)
    valid = np.isfinite(x_array) & np.isfinite(y_array)
    x_array = x_array[valid]
    y_array = y_array[valid]
    if len(x_array) < 3:
        raise RuntimeError("直线拟合至少需要 3 个有效点")
    slope, intercept = np.polyfit(x_array, y_array, 1)
    predicted = intercept + slope * x_array
    residual = y_array - predicted
    sse = float(np.sum(residual**2))
    centered = y_array - float(np.mean(y_array))
    sst = float(np.sum(centered**2))
    r_squared = 1.0 - sse / sst if sst > 0 else 1.0
    rmse = math.sqrt(sse / len(x_array))
    x_centered = x_array - float(np.mean(x_array))
    slope_stderr = math.sqrt(
        (sse / max(len(x_array) - 2, 1))
        / float(np.sum(x_centered**2))
    )
    return {
        "slope_v_per_h": float(slope),
        "intercept_v": float(intercept),
        "r_squared": float(r_squared),
        "rmse_v": float(rmse),
        "slope_standard_error_v_per_h": float(slope_stderr),
        "point_count": int(len(x_array)),
    }


def fit_water_compensation_model(
    cycles: pd.DataFrame,
    series_summary: pd.DataFrame,
) -> dict:
    if "material_relative_path" not in series_summary.columns:
        raise RuntimeError("水位补偿参照匹配缺少不可变材料键")
    reference_rows = series_summary[
        series_summary["material_relative_path"].astype(str)
        == WATER_COMP_REFERENCE_KEY
    ]
    if len(reference_rows) != 1:
        raise RuntimeError(
            "水位补偿参照材料键必须且只能匹配 1 条序列：{}".format(
                WATER_COMP_REFERENCE_KEY
            )
        )
    reference = reference_rows.iloc[0]
    series_id = reference["series_id"]
    reference_display_name = str(
        reference.get("material_display_name")
        or Path(WATER_COMP_REFERENCE_KEY).name
    )
    data = cycles[cycles["series_id"] == series_id].sort_values("cycle")
    tail = data[
        data["cycle"] >= WATER_COMP_REFERENCE_TAIL_START_CYCLE
    ].copy()
    if len(tail) < 100:
        raise RuntimeError("水位补偿参照尾段有效循环少于 100 个")
    fit = linear_fit_summary(
        tail["cathodic_endpoint_time_h"].to_numpy(dtype=float),
        tail["cathodic_last1s_median_raw_v"].to_numpy(dtype=float),
    )
    reference_current = float(reference["cathodic_current_median_a_cm2"])
    if reference_current >= 0:
        raise RuntimeError("水位补偿参照阴极电流必须为负值")
    if fit["slope_v_per_h"] >= 0 or fit["r_squared"] < 0.99:
        raise RuntimeError(
            "参照尾段不满足负斜率且 R²≥0.99 的线性补偿条件"
        )

    resistance_drift = fit["slope_v_per_h"] / reference_current
    sensitivity = []
    for start_cycle in WATER_COMP_SENSITIVITY_START_CYCLES:
        subset = data[data["cycle"] >= start_cycle]
        if len(subset) < 100:
            continue
        result = linear_fit_summary(
            subset["cathodic_endpoint_time_h"].to_numpy(dtype=float),
            subset["cathodic_last1s_median_raw_v"].to_numpy(dtype=float),
        )
        sensitivity.append(
            {
                "tail_start_cycle": int(start_cycle),
                "tail_start_time_h": float(
                    subset["cathodic_endpoint_time_h"].iloc[0]
                ),
                "point_count": int(len(subset)),
                "slope_mv_per_h": float(
                    result["slope_v_per_h"] * 1000.0
                ),
                "r_squared": float(result["r_squared"]),
                "rmse_mv": float(result["rmse_v"] * 1000.0),
            }
        )

    maximum_time_h = float(data["cathodic_endpoint_time_h"].max())
    return json_safe(
        {
            "model_name": "NiMo reference tail water-level compensation",
            # ``reference_material`` remains as a display-name compatibility
            # alias for existing status clients. Selection is always by key.
            "reference_material": reference_display_name,
            "reference_material_key": WATER_COMP_REFERENCE_KEY,
            "reference_material_display_name": reference_display_name,
            "reference_series_id": series_id,
            "reference_test_type": reference["test_type"],
            "reference_cathodic_current_a_cm2": reference_current,
            "tail_selection": (
                "cycles {}–{}; cycles 1–{} treated as startup transient"
            ).format(
                WATER_COMP_REFERENCE_TAIL_START_CYCLE,
                int(data["cycle"].max()),
                WATER_COMP_REFERENCE_TAIL_START_CYCLE - 1,
            ),
            "tail_start_cycle": WATER_COMP_REFERENCE_TAIL_START_CYCLE,
            "tail_end_cycle": int(data["cycle"].max()),
            "tail_start_time_h": float(
                tail["cathodic_endpoint_time_h"].iloc[0]
            ),
            "tail_end_time_h": float(
                tail["cathodic_endpoint_time_h"].iloc[-1]
            ),
            "tail_point_count": int(len(tail)),
            "reference_fit_slope_v_per_h": fit["slope_v_per_h"],
            "reference_fit_slope_mv_per_h": (
                fit["slope_v_per_h"] * 1000.0
            ),
            "reference_fit_intercept_v": fit["intercept_v"],
            "reference_fit_r_squared": fit["r_squared"],
            "reference_fit_rmse_mv": fit["rmse_v"] * 1000.0,
            "reference_fit_slope_standard_error_mv_per_h": (
                fit["slope_standard_error_v_per_h"] * 1000.0
            ),
            "area_specific_resistance_drift_ohm_cm2_per_h": (
                resistance_drift
            ),
            "cathodic_compensation_slope_mv_per_h_at_reference_current": (
                -fit["slope_v_per_h"] * 1000.0
            ),
            "maximum_reference_cathodic_compensation_mv": (
                -reference_current
                * resistance_drift
                * maximum_time_h
                * 1000.0
            ),
            "formula": "E_comp = E_raw - j * k_R * t",
            "formula_terms": {
                "j": "instantaneous current density in A cm^-2",
                "k_R": "area-specific resistance drift in ohm cm^2 h^-1",
                "t": "elapsed time within each analysis series in h",
            },
            "sensitivity_by_tail_start": sensitivity,
            "scope": (
                "每条分析序列均从本序列 t=0 开始；阴极段与恢复段按实际电流方向补偿"
            ),
            "caveat": (
                "这是共同漂移修正模型。参照尾段支持稳定的线性漂移，"
                "但仅凭当前数据不能证明液面下降是漂移的唯一原因。"
            ),
        }
    )


def apply_water_compensation(
    cycles: pd.DataFrame,
    overview: pd.DataFrame,
    representative: pd.DataFrame,
    series_summary: pd.DataFrame,
    model: dict,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    resistance_drift = float(
        model["area_specific_resistance_drift_ohm_cm2_per_h"]
    )

    compensated_cycles = cycles.sort_values(
        ["series_order", "cycle"]
    ).copy()
    compensated_cycles["water_resistance_increment_cathodic_ohm_cm2"] = (
        resistance_drift
        * compensated_cycles["cathodic_endpoint_time_h"].astype(float)
    )
    compensated_cycles["water_resistance_increment_recovery_ohm_cm2"] = (
        resistance_drift
        * compensated_cycles["reverse_endpoint_time_h"].astype(float)
    )
    compensated_cycles["cathodic_water_compensation_v"] = (
        -compensated_cycles["cathodic_current_median_a_cm2"].astype(float)
        * compensated_cycles[
            "water_resistance_increment_cathodic_ohm_cm2"
        ]
    )
    compensated_cycles["recovery_water_compensation_v"] = (
        -compensated_cycles["recovery_current_median_a_cm2"].astype(float)
        * compensated_cycles[
            "water_resistance_increment_recovery_ohm_cm2"
        ]
    )
    compensated_cycles[
        "cathodic_last1s_median_water_compensated_v"
    ] = (
        compensated_cycles["cathodic_last1s_median_raw_v"]
        + compensated_cycles["cathodic_water_compensation_v"]
    )
    compensated_cycles[
        "reverse_last1s_median_water_compensated_v"
    ] = (
        compensated_cycles["reverse_last1s_median_raw_v"]
        + compensated_cycles["recovery_water_compensation_v"]
    )
    compensated_cycles[
        "cathodic_negative_shift_water_compensated_mv"
    ] = compensated_cycles.groupby("series_id", sort=False)[
        "cathodic_last1s_median_water_compensated_v"
    ].transform(
        lambda values: (
            float(values.iloc[:BASELINE_CYCLES].median()) - values
        )
        * 1000.0
    )
    compensated_cycles[
        "reverse_shift_water_compensated_mv"
    ] = compensated_cycles.groupby("series_id", sort=False)[
        "reverse_last1s_median_water_compensated_v"
    ].transform(
        lambda values: (
            values - float(values.iloc[:BASELINE_CYCLES].median())
        )
        * 1000.0
    )

    compensated_overview = overview.copy()
    compensated_overview["water_resistance_increment_ohm_cm2"] = (
        resistance_drift
        * compensated_overview["continuous_time_h"].astype(float)
    )
    compensated_overview["water_compensation_v"] = (
        -compensated_overview["current_a_cm2"].astype(float)
        * compensated_overview["water_resistance_increment_ohm_cm2"]
    )
    compensated_overview["potential_water_compensated_v"] = (
        compensated_overview["potential_raw_v"]
        + compensated_overview["water_compensation_v"]
    )

    compensated_representative = representative.copy()
    compensated_representative["continuous_time_h"] = (
        compensated_representative["continuous_time_s"].astype(float)
        / 3600.0
    )
    compensated_representative["water_resistance_increment_ohm_cm2"] = (
        resistance_drift
        * compensated_representative["continuous_time_h"]
    )
    compensated_representative["water_compensation_v"] = (
        -compensated_representative["current_a_cm2"].astype(float)
        * compensated_representative["water_resistance_increment_ohm_cm2"]
    )
    compensated_representative["potential_water_compensated_v"] = (
        compensated_representative["potential_raw_v"]
        + compensated_representative["water_compensation_v"]
    )

    compensated_series = series_summary.copy()
    added_rows = []
    for _, summary in compensated_series.iterrows():
        subset = compensated_cycles[
            compensated_cycles["series_id"] == summary["series_id"]
        ].sort_values("cycle")
        cathodic = subset[
            "cathodic_last1s_median_water_compensated_v"
        ]
        recovery = subset[
            "reverse_last1s_median_water_compensated_v"
        ]
        cathodic_fit = linear_fit_summary(
            subset["cathodic_endpoint_time_h"].to_numpy(dtype=float),
            cathodic.to_numpy(dtype=float),
        )
        added_rows.append(
            {
                "series_id": summary["series_id"],
                "water_comp_reference_series": bool(
                    summary["series_id"]
                    == model["reference_series_id"]
                ),
                "water_comp_cathodic_baseline_first10_raw_v": float(
                    cathodic.iloc[:BASELINE_CYCLES].median()
                ),
                "water_comp_cathodic_endpoint_last10_raw_v": float(
                    cathodic.iloc[-ENDPOINT_CYCLES:].median()
                ),
                "water_comp_cathodic_negative_shift_first10_to_last10_mv": float(
                    (
                        cathodic.iloc[:BASELINE_CYCLES].median()
                        - cathodic.iloc[-ENDPOINT_CYCLES:].median()
                    )
                    * 1000.0
                ),
                "water_comp_recovery_baseline_first10_raw_v": float(
                    recovery.iloc[:BASELINE_CYCLES].median()
                ),
                "water_comp_recovery_endpoint_last10_raw_v": float(
                    recovery.iloc[-ENDPOINT_CYCLES:].median()
                ),
                "water_comp_recovery_shift_first10_to_last10_mv": float(
                    (
                        recovery.iloc[-ENDPOINT_CYCLES:].median()
                        - recovery.iloc[:BASELINE_CYCLES].median()
                    )
                    * 1000.0
                ),
                "water_comp_linear_cathodic_slope_mv_per_h": float(
                    cathodic_fit["slope_v_per_h"] * 1000.0
                ),
                "water_comp_linear_cathodic_fit_r_squared": float(
                    cathodic_fit["r_squared"]
                ),
                "maximum_cathodic_water_compensation_mv": float(
                    subset["cathodic_water_compensation_v"].max()
                    * 1000.0
                ),
            }
        )
    compensated_series = compensated_series.merge(
        pd.DataFrame(added_rows), on="series_id", how="left"
    )
    return (
        compensated_cycles,
        compensated_overview,
        compensated_representative,
        compensated_series,
    )


def plot_water_compensation_model(
    cycles: pd.DataFrame,
    model: dict,
    pdf: PdfPages,
) -> None:
    data = cycles[
        cycles["series_id"] == model["reference_series_id"]
    ].sort_values("cycle")
    tail = data[data["cycle"] >= model["tail_start_cycle"]].copy()
    x_tail = tail["cathodic_endpoint_time_h"].to_numpy(dtype=float)
    fitted_tail = (
        float(model["reference_fit_intercept_v"])
        + float(model["reference_fit_slope_v_per_h"]) * x_tail
    )
    residual_mv = (
        tail["cathodic_last1s_median_raw_v"].to_numpy(dtype=float)
        - fitted_tail
    ) * 1000.0

    fig, axes = plt.subplots(2, 2, figsize=(16.0, 12.0))
    fig.subplots_adjust(
        left=0.085,
        right=0.965,
        top=0.815,
        bottom=0.095,
        hspace=0.38,
        wspace=0.24,
    )
    add_header(
        fig,
        "水位补偿基线｜NiMo 恒流参照尾段",
        "参照：{}；拟合循环 {}–{}；阴极电流 {:.3f} A cm⁻²".format(
            model["reference_material_display_name"],
            model["tail_start_cycle"],
            model["tail_end_cycle"],
            model["reference_cathodic_current_a_cm2"],
        ),
        "尾段斜率 {:.3f} mV h⁻¹，R² = {:.5f}，RMSE = {:.3f} mV；补偿式 E_comp = E_raw − j·k_R·t".format(
            model["reference_fit_slope_mv_per_h"],
            model["reference_fit_r_squared"],
            model["reference_fit_rmse_mv"],
        ),
    )

    ax = axes[0, 0]
    ax.plot(
        data["cathodic_endpoint_time_h"],
        data["cathodic_last1s_median_raw_v"],
        color=NEUTRAL,
        linewidth=1.0,
        label="原始阴极末端电位",
    )
    ax.plot(
        x_tail,
        fitted_tail,
        color=ORANGE,
        linewidth=2.0,
        label="尾段直线拟合",
    )
    ax.axvspan(
        0,
        float(model["tail_start_time_h"]),
        color=PALE_ORANGE,
        alpha=0.7,
        label="启动瞬态（不参与拟合）",
    )
    ax.set_title("参照尾段定位", loc="left", fontsize=11.5)
    ax.set_xlabel("接续后时间 / h")
    ax.set_ylabel("阴极段末端统计值（vs Hg/HgO）/ V")
    ax.legend(frameon=False, fontsize=8.5)
    style_axis(ax)

    ax = axes[0, 1]
    ax.plot(x_tail, residual_mv, color=BLUE, linewidth=0.9)
    ax.axhline(0.0, color=NEUTRAL_DARK, linewidth=0.9)
    rmse_mv = float(model["reference_fit_rmse_mv"])
    ax.axhspan(-rmse_mv, rmse_mv, color=PALE_BLUE, alpha=0.8)
    ax.set_title("尾段拟合残差", loc="left", fontsize=11.5)
    ax.set_xlabel("接续后时间 / h")
    ax.set_ylabel("残差 / mV")
    style_axis(ax)

    ax = axes[1, 0]
    time = data["cathodic_endpoint_time_h"].to_numpy(dtype=float)
    correction_mv = (
        data["cathodic_water_compensation_v"].to_numpy(dtype=float)
        * 1000.0
    )
    ax.plot(time, correction_mv, color=GOLD, linewidth=2.0)
    ax.set_title("阴极水位补偿线", loc="left", fontsize=11.5)
    ax.set_xlabel("接续后时间 / h")
    ax.set_ylabel("加到阴极电位上的补偿 / mV")
    style_axis(ax)

    ax = axes[1, 1]
    ax.plot(
        time,
        data["cathodic_last1s_median_raw_v"],
        color=NEUTRAL,
        linewidth=1.0,
        linestyle="--",
        label="补偿前",
    )
    ax.plot(
        time,
        data["cathodic_last1s_median_water_compensated_v"],
        color=BLUE,
        linewidth=1.35,
        label="水位补偿后",
    )
    ax.set_title("参照曲线补偿前后", loc="left", fontsize=11.5)
    ax.set_xlabel("接续后时间 / h")
    ax.set_ylabel("阴极段末端统计值（vs Hg/HgO）/ V")
    ax.legend(frameon=False)
    style_axis(ax)
    save_figure(
        fig,
        WATER_COMP_FIGURE_DIR / "01_水位补偿基线拟合",
        pdf,
        dpi=190,
    )


def validate_water_compensation(
    original_cycles: pd.DataFrame,
    compensated_cycles: pd.DataFrame,
    original_overview: pd.DataFrame,
    compensated_overview: pd.DataFrame,
    compensated_series: pd.DataFrame,
    model: dict,
) -> dict:
    reference_tail = compensated_cycles[
        (compensated_cycles["series_id"] == model["reference_series_id"])
        & (compensated_cycles["cycle"] >= model["tail_start_cycle"])
    ].sort_values("cycle")
    corrected_tail_fit = linear_fit_summary(
        reference_tail["cathodic_endpoint_time_h"].to_numpy(dtype=float),
        reference_tail[
            "cathodic_last1s_median_water_compensated_v"
        ].to_numpy(dtype=float),
    )
    cycle_identity_error = float(
        np.max(
            np.abs(
                compensated_cycles[
                    "cathodic_last1s_median_water_compensated_v"
                ].to_numpy(dtype=float)
                - compensated_cycles[
                    "cathodic_last1s_median_raw_v"
                ].to_numpy(dtype=float)
                - compensated_cycles[
                    "cathodic_water_compensation_v"
                ].to_numpy(dtype=float)
            )
        )
    )
    overview_identity_error = float(
        np.max(
            np.abs(
                compensated_overview[
                    "potential_water_compensated_v"
                ].to_numpy(dtype=float)
                - compensated_overview["potential_raw_v"].to_numpy(
                    dtype=float
                )
                - compensated_overview["water_compensation_v"].to_numpy(
                    dtype=float
                )
            )
        )
    )
    sensitivity_slopes = np.asarray(
        [
            row["slope_mv_per_h"]
            for row in model["sensitivity_by_tail_start"]
        ],
        dtype=float,
    )
    sensitivity_span_pct = float(
        (np.max(sensitivity_slopes) - np.min(sensitivity_slopes))
        / abs(float(np.median(sensitivity_slopes)))
        * 100.0
    )
    selected_overcompensated = compensated_series[
        compensated_series["include_in_summary_atlas"].astype(bool)
        & (
            compensated_series[
                "water_comp_cathodic_negative_shift_first10_to_last10_mv"
            ]
            < -WATER_COMP_OVERCOMPENSATION_TOLERANCE_MV
        )
    ][
        [
            "series_id",
            "material_relative_path",
            "material_display_name",
            "water_comp_cathodic_negative_shift_first10_to_last10_mv",
        ]
    ].to_dict(orient="records")

    checks = [
        {
            "check": "循环表行数与原始分析完全一致",
            "passed": bool(len(original_cycles) == len(compensated_cycles)),
            "value": int(len(compensated_cycles)),
        },
        {
            "check": "全程抽样表行数与原始分析完全一致",
            "passed": bool(len(original_overview) == len(compensated_overview)),
            "value": int(len(compensated_overview)),
        },
        {
            "check": "参照尾段原始拟合 R²≥0.99",
            "passed": bool(model["reference_fit_r_squared"] >= 0.99),
            "value": float(model["reference_fit_r_squared"]),
        },
        {
            "check": "参照尾段补偿后斜率绝对值≤0.001 mV h⁻¹",
            "passed": bool(
                abs(corrected_tail_fit["slope_v_per_h"] * 1000.0)
                <= 0.001
            ),
            "value_mv_per_h": float(
                corrected_tail_fit["slope_v_per_h"] * 1000.0
            ),
        },
        {
            "check": "不同尾段起点的斜率跨度≤2%",
            "passed": bool(sensitivity_span_pct <= 2.0),
            "value_pct": sensitivity_span_pct,
        },
        {
            "check": "逐循环补偿公式数值恒等",
            "passed": bool(cycle_identity_error <= 1e-12),
            "maximum_absolute_error_v": cycle_identity_error,
        },
        {
            "check": "全程采样补偿公式数值恒等",
            "passed": bool(overview_identity_error <= 1e-12),
            "maximum_absolute_error_v": overview_identity_error,
        },
        {
            "check": "阴极补偿方向均为正向或零",
            "passed": bool(
                compensated_cycles["cathodic_water_compensation_v"].min()
                >= -1e-12
            ),
        },
        {
            "check": "恢复段补偿方向均为负向或零",
            "passed": bool(
                compensated_cycles["recovery_water_compensation_v"].max()
                <= 1e-12
            ),
        },
        {
            "check": "补偿结果关键列无缺失值",
            "passed": bool(
                not compensated_cycles[
                    [
                        "cathodic_last1s_median_water_compensated_v",
                        "reverse_last1s_median_water_compensated_v",
                        "cathodic_negative_shift_water_compensated_mv",
                    ]
                ].isna().any().any()
            ),
        },
    ]
    failed = [row for row in checks if not row["passed"]]
    if failed:
        raise RuntimeError(
            "水位补偿验证未通过：{}".format(
                "；".join(row["check"] for row in failed)
            )
        )
    return json_safe(
        {
            "status": "通过（保留模型边界）",
            "checks": checks,
            "reference_tail_corrected_slope_mv_per_h": float(
                corrected_tail_fit["slope_v_per_h"] * 1000.0
            ),
            "tail_start_sensitivity_span_pct": sensitivity_span_pct,
            "overcompensation_tolerance_mv": (
                WATER_COMP_OVERCOMPENSATION_TOLERANCE_MV
            ),
            "selected_series_with_negative_corrected_shift": (
                selected_overcompensated
            ),
            "caveats": [
                (
                    "参照尾段的线性验证了补偿基线的数值一致性，"
                    "但不能证明液面下降是漂移的唯一原因。"
                ),
                (
                    "补偿后首末负移小于 -{:.3f} mV 时标记为可能过补偿；"
                    "容差内的浮点残差不误报，真实负值不强制截断。".format(
                        WATER_COMP_OVERCOMPENSATION_TOLERANCE_MV
                    )
                ),
            ],
        }
    )


def plot_water_compensation_facets(
    cycles: pd.DataFrame,
    series_summary: pd.DataFrame,
    model: dict,
    pdf: PdfPages,
) -> None:
    ordered = series_summary.sort_values("series_order")
    fig, axes = make_faceted_figure(
        len(ordered), sharey=True, height_per_row=4.10
    )
    fig.subplots_adjust(top=0.850)
    add_header(
        fig,
        "全部材料阴极电位｜水位补偿前后",
        "每条序列均从本序列 t=0 开始按实际时间与阴极电流进行补偿",
        "灰色虚线为原始值；蓝色实线为水位补偿后；共同 k_R = {:.5f} Ω cm² h⁻¹".format(
            model["area_specific_resistance_drift_ohm_cm2_per_h"]
        ),
    )
    y_values = pd.concat(
        [
            cycles["cathodic_last1s_median_raw_v"],
            cycles[
                "cathodic_last1s_median_water_compensated_v"
            ],
        ],
        ignore_index=True,
    )
    y_limits = padded_limits(y_values, 0.04)
    for ax, (_, summary) in zip(axes.flat, ordered.iterrows()):
        subset = cycles[
            cycles["series_id"] == summary["series_id"]
        ].sort_values("cycle")
        ax.plot(
            subset["cycle"],
            subset["cathodic_last1s_median_raw_v"],
            color=NEUTRAL,
            linewidth=1.0,
            linestyle="--",
        )
        ax.plot(
            subset["cycle"],
            subset[
                "cathodic_last1s_median_water_compensated_v"
            ],
            color=BLUE,
            linewidth=1.35,
        )
        ax.set_title(
            series_panel_title(summary, 40),
            fontsize=9.6,
            loc="left",
            pad=9,
        )
        ax.set_xlabel("连续循环编号")
        ax.set_ylabel("阴极段末端统计值（vs Hg/HgO）/ V")
        ax.set_ylim(y_limits)
        style_axis(ax)
    for ax in axes.flat[len(ordered) :]:
        ax.axis("off")
    fig.legend(
        handles=[
            Line2D(
                [0],
                [0],
                color=NEUTRAL,
                lw=1.5,
                ls="--",
                label="补偿前",
            ),
            Line2D([0], [0], color=BLUE, lw=2.0, label="水位补偿后"),
        ],
        loc="upper center",
        bbox_to_anchor=(0.55, 0.898),
        ncol=2,
        frameon=False,
    )
    save_figure(
        fig,
        WATER_COMP_FIGURE_DIR / "02_全部材料水位补偿前后分面",
        pdf,
    )


def plot_water_compensation_overlay(
    cycles: pd.DataFrame,
    series_summary: pd.DataFrame,
    model: dict,
    pdf: PdfPages,
) -> None:
    ordered = series_summary.sort_values("series_order")
    palette = OVERLAY_HIGH_CONTRAST_PALETTE
    line_styles = OVERLAY_LINE_STYLES
    if len(ordered) > len(palette) * len(line_styles):
        raise RuntimeError("水位补偿叠加图的颜色/线形组合不足")

    fig, axes = plt.subplots(1, 2, figsize=(21.0, 16.0))
    fig.subplots_adjust(
        left=0.060,
        right=0.980,
        top=0.840,
        bottom=0.315,
        wspace=0.18,
    )
    add_header(
        fig,
        "全部材料阴极段叠加对比｜水位补偿后",
        "左图为补偿后阴极末端统计电位，右图为相对补偿后前 10 循环的阴极负移",
        "E_comp = E_raw − j·k_R·t；共同 k_R = {:.5f} Ω cm² h⁻¹；原始数据未覆盖".format(
            model["area_specific_resistance_drift_ohm_cm2_per_h"]
        ),
    )
    legend_handles = []
    legend_labels = []
    for index, (_, summary) in enumerate(ordered.iterrows()):
        subset = cycles[
            cycles["series_id"] == summary["series_id"]
        ].sort_values("cycle")
        color = palette[index % len(palette)]
        line_style = line_styles[index // len(palette)]
        left_line = axes[0].plot(
            subset["cycle"],
            subset[
                "cathodic_last1s_median_water_compensated_v"
            ],
            color=color,
            linestyle=line_style,
            linewidth=1.55,
        )[0]
        axes[1].plot(
            subset["cycle"],
            subset["cathodic_negative_shift_water_compensated_mv"],
            color=color,
            linestyle=line_style,
            linewidth=1.55,
        )
        legend_handles.append(left_line)
        legend_labels.append(material_axis_label(summary, 62))

    axes[0].set_title("水位补偿后阴极段末端统计电位", loc="left", fontsize=12.2)
    axes[0].set_xlabel("连续循环编号")
    axes[0].set_ylabel("阴极段末端统计值（vs Hg/HgO）/ V")
    style_axis(axes[0])
    axes[1].axhline(0.0, color=NEUTRAL_DARK, linewidth=0.95)
    axes[1].set_title("水位补偿后阴极段电位负移", loc="left", fontsize=12.2)
    axes[1].set_xlabel("连续循环编号")
    axes[1].set_ylabel("相对补偿后前 10 循环 / mV")
    style_axis(axes[1])
    fig.legend(
        handles=legend_handles,
        labels=legend_labels,
        loc="lower center",
        bbox_to_anchor=(0.5, 0.035),
        ncol=3 if len(ordered) > 22 else 2,
        frameon=False,
        fontsize=8.2,
        handlelength=3.8,
        handletextpad=0.8,
        columnspacing=2.2,
        labelspacing=0.95,
    )
    save_figure(
        fig,
        WATER_COMP_FIGURE_DIR / "03_全部材料水位补偿后叠加对比",
        pdf,
        dpi=190,
    )


def plot_water_compensation_shift_comparison(
    series_summary: pd.DataFrame,
    pdf: PdfPages,
) -> None:
    data = series_summary.sort_values(
        "water_comp_cathodic_negative_shift_first10_to_last10_mv",
        ascending=True,
    ).copy()
    labels = [material_axis_label(row, 58) for _, row in data.iterrows()]
    raw = data[
        "cathodic_negative_shift_first10_to_last10_mv"
    ].to_numpy(dtype=float)
    corrected = data[
        "water_comp_cathodic_negative_shift_first10_to_last10_mv"
    ].to_numpy(dtype=float)
    y = np.arange(len(data))
    height = 0.34
    fig, ax = plt.subplots(figsize=(17.5, 11.5))
    fig.subplots_adjust(left=0.47, right=0.95, top=0.84, bottom=0.11)
    add_header(
        fig,
        "各材料阴极负移｜水位补偿影响",
        "指标均为前 10 循环阴极末端统计值减去后 10 循环阴极末端统计值",
        "灰色为补偿前，蓝色为水位补偿后；差值代表模型归因给共同液面下降漂移的部分",
    )
    ax.barh(
        y + height / 2,
        raw,
        height=height,
        color="#C8CED8",
        edgecolor=NEUTRAL_DARK,
        linewidth=0.6,
        label="补偿前",
    )
    ax.barh(
        y - height / 2,
        corrected,
        height=height,
        color=BLUE,
        edgecolor=BLUE_DARK,
        linewidth=0.6,
        label="水位补偿后",
    )
    ax.axvline(0.0, color=NEUTRAL_DARK, linewidth=0.9)
    ax.set_yticks(y)
    ax.set_yticklabels(labels, fontsize=9.3)
    ax.set_xlabel("阴极电位负移 / mV")
    ax.legend(frameon=False, loc="lower right")
    style_axis(ax, grid_axis="x")
    save_figure(
        fig,
        WATER_COMP_FIGURE_DIR / "04_各材料水位补偿影响对比",
        pdf,
        dpi=190,
    )


def plot_water_compensation_series_details(
    cycles: pd.DataFrame,
    series_summary: pd.DataFrame,
    segment_summary: pd.DataFrame,
    model: dict,
    pdf: PdfPages,
) -> None:
    for _, summary in series_summary.sort_values("series_order").iterrows():
        subset = cycles[
            cycles["series_id"] == summary["series_id"]
        ].sort_values("cycle")
        segments = segment_summary[
            segment_summary["series_id"] == summary["series_id"]
        ].sort_values("segment_index")
        fig, axes = plt.subplots(2, 2, figsize=(16.0, 12.0))
        fig.subplots_adjust(
            left=0.085,
            right=0.965,
            top=0.810,
            bottom=0.105,
            hspace=0.38,
            wspace=0.22,
        )
        add_header(
            fig,
            wrap_full_label(summary["series_display_name"], 82),
            "水位补偿：E_comp = E_raw − j·k_R·t；k_R = {:.5f} Ω cm² h⁻¹".format(
                model["area_specific_resistance_drift_ohm_cm2_per_h"]
            ),
            "共同漂移模型仅校正液面下降假设项；材料本征变化与突变仍保留",
        )

        ax = axes[0, 0]
        ax.plot(
            subset["cycle"],
            subset["cathodic_last1s_median_raw_v"],
            color=NEUTRAL,
            linestyle="--",
            linewidth=1.0,
            label="补偿前",
        )
        ax.plot(
            subset["cycle"],
            subset[
                "cathodic_last1s_median_water_compensated_v"
            ],
            color=BLUE,
            linewidth=1.35,
            label="水位补偿后",
        )
        ax.set_title("阴极段末端统计电位", loc="left", fontsize=11.5)
        ax.set_xlabel("连续循环编号")
        ax.set_ylabel("E vs Hg/HgO / V")
        ax.legend(frameon=False)
        style_axis(ax)

        ax = axes[0, 1]
        ax.plot(
            subset["cycle"],
            subset["reverse_last1s_median_raw_v"],
            color=NEUTRAL,
            linestyle="--",
            linewidth=1.0,
            label="补偿前",
        )
        ax.plot(
            subset["cycle"],
            subset[
                "reverse_last1s_median_water_compensated_v"
            ],
            color=ORANGE,
            linewidth=1.30,
            label="水位补偿后",
        )
        ax.set_title("恢复段末端统计电位", loc="left", fontsize=11.5)
        ax.set_xlabel("连续循环编号")
        ax.set_ylabel("E vs Hg/HgO / V")
        ax.legend(frameon=False)
        style_axis(ax)

        ax = axes[1, 0]
        ax.plot(
            subset["cycle"],
            subset["cathodic_negative_shift_mv"],
            color=NEUTRAL,
            linestyle="--",
            linewidth=1.0,
            label="补偿前",
        )
        ax.plot(
            subset["cycle"],
            subset["cathodic_negative_shift_water_compensated_mv"],
            color=BLUE,
            linewidth=1.35,
            label="水位补偿后",
        )
        ax.axhline(0.0, color=NEUTRAL_DARK, linewidth=0.9)
        ax.set_title("阴极段电位负移", loc="left", fontsize=11.5)
        ax.set_xlabel("连续循环编号")
        ax.set_ylabel("相对各自前 10 循环 / mV")
        ax.legend(frameon=False)
        style_axis(ax)

        ax = axes[1, 1]
        ax.plot(
            subset["cathodic_endpoint_time_h"],
            subset["cathodic_water_compensation_v"] * 1000.0,
            color=BLUE,
            linewidth=1.35,
            label="阴极段补偿",
        )
        ax.plot(
            subset["reverse_endpoint_time_h"],
            subset["recovery_water_compensation_v"] * 1000.0,
            color=ORANGE,
            linewidth=1.25,
            label="恢复段补偿",
        )
        ax.axhline(0.0, color=NEUTRAL_DARK, linewidth=0.9)
        ax.set_title("实际施加的水位补偿量", loc="left", fontsize=11.5)
        ax.set_xlabel("接续后时间 / h")
        ax.set_ylabel("加到原始电位上的补偿 / mV")
        ax.legend(frameon=False)
        style_axis(ax)

        sources = "；".join(
            "{}: {}".format(int(row["segment_index"]), row["source_file"])
            for _, row in segments.iterrows()
        )
        fig.text(
            0.085,
            0.035,
            "源文件：{}".format(wrap_full_label(sources, 145)),
            ha="left",
            va="bottom",
            fontsize=7.8,
            color=MUTED,
        )
        save_figure(
            fig,
            WATER_COMP_DETAIL_DIR / "{}_水位补偿详细图".format(
                summary["series_id"]
            ),
            pdf,
            dpi=190,
        )


def write_water_compensation_method(model: dict) -> None:
    sensitivity_lines = [
        "| 尾段起始循环 | 起始时间 / h | 点数 | 斜率 / mV h⁻¹ | R² | RMSE / mV |",
        "|---:|---:|---:|---:|---:|---:|",
    ]
    for row in model["sensitivity_by_tail_start"]:
        sensitivity_lines.append(
            "| {tail_start_cycle} | {tail_start_time_h:.3f} | {point_count} | {slope_mv_per_h:.4f} | {r_squared:.5f} | {rmse_mv:.3f} |".format(
                **row
            )
        )
    content = """# 启停数据水位补偿方法

## 补偿基线

- 参照材料显示名：`{reference_material_display_name}`
- 参照材料不可变键：`{reference_material_key}`
- 参照分析序列：`{reference_series_id}`
- 参照尾段：第 {tail_start_cycle}–{tail_end_cycle} 循环
- 尾段斜率：{reference_fit_slope_mv_per_h:.4f} mV h⁻¹
- 拟合质量：R² = {reference_fit_r_squared:.5f}，RMSE = {reference_fit_rmse_mv:.3f} mV
- 推算面比电阻增长率：{area_specific_resistance_drift_ohm_cm2_per_h:.6f} Ω cm² h⁻¹

## 计算公式

`E_comp = E_raw - j × k_R × t`

其中 `j` 为每个采样点或阶段的实际电流密度，`k_R` 为参照尾段推算的面比电阻增长率，`t` 为每条分析序列从本序列开始计的接续时间。阴极电流为负，因此阴极补偿向正电位方向；恢复电流为正，因此恢复段补偿方向相反。

## 尾段敏感性检查

{sensitivity_table}

## 使用边界

该修正严格按“共同液面下降漂移”模型执行。参照尾段具有稳定的线性负漂移，但仅凭这些电位数据不能证明漂移全部来自液面下降；材料本征衰减、气泡覆盖、参比位置变化及温度变化仍可能叠加。原始数据和未补偿图集均保留，补偿结果应作为模型化对照使用。
""".format(
        sensitivity_table="\n".join(sensitivity_lines),
        **model,
    )
    (WATER_COMP_DIR / "水位补偿方法说明.md").write_text(
        content, encoding="utf-8"
    )


def write_water_compensation_validation(validation: dict) -> None:
    lines = [
        "# 水位补偿验证报告",
        "",
        "总体状态：{}".format(validation["status"]),
        "过补偿判定容差：补偿后首末负移小于 -{:.3f} mV".format(
            validation["overcompensation_tolerance_mv"]
        ),
        "",
        "## 计算与数据完整性检查",
        "",
    ]
    for row in validation["checks"]:
        detail = ""
        for key, value in row.items():
            if key not in {"check", "passed"}:
                detail = "；{}={}".format(key, value)
        lines.append(
            "- [{}] {}{}".format(
                "通过" if row["passed"] else "失败",
                row["check"],
                detail,
            )
        )
    lines.extend(
        [
            "",
            "## 需要保留的模型边界",
            "",
        ]
    )
    for caveat in validation["caveats"]:
        lines.append("- {}".format(caveat))
    overcompensated = validation[
        "selected_series_with_negative_corrected_shift"
    ]
    lines.extend(["", "## 可能过补偿的入选序列", ""])
    if overcompensated:
        for row in overcompensated:
            lines.append(
                "- {}：补偿后首末负移 {:.3f} mV".format(
                    row["material_display_name"],
                    row[
                        "water_comp_cathodic_negative_shift_first10_to_last10_mv"
                    ],
                )
            )
    else:
        lines.append("- 无")
    (WATER_COMP_DIR / "水位补偿验证报告.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def render_water_compensation_outputs(
    cycles: pd.DataFrame,
    overview: pd.DataFrame,
    representative: pd.DataFrame,
    series_summary: pd.DataFrame,
    selected_series_summary: pd.DataFrame,
    segment_summary: pd.DataFrame,
    *,
    export_pdf: bool = True,
) -> dict:
    figure_total = 4 + len(selected_series_summary)
    report_render_progress(
        phase="rendering_water" if export_pdf else "calculating_water",
        phase_label=(
            "生成水位补偿图集"
            if export_pdf
            else "计算水位补偿数据"
        ),
        phase_index=5,
        percent=80,
        completed=0,
        total=figure_total if export_pdf else 1,
        unit="pages" if export_pdf else "steps",
        current_item="计算水位补偿模型",
        detail=(
            "正在用参照曲线尾段拟合补偿线并生成 PDF 图集"
            if export_pdf
            else "正在用参照曲线尾段拟合补偿线并更新网页分析数据"
        ),
    )
    if export_pdf:
        for directory in (WATER_COMP_FIGURE_DIR, WATER_COMP_DETAIL_DIR):
            directory.mkdir(parents=True, exist_ok=True)
        for path in WATER_COMP_DETAIL_DIR.glob("*_水位补偿详细图.*"):
            if path.suffix.lower() in {".png", ".svg"}:
                path.unlink()

    model = fit_water_compensation_model(cycles, series_summary)
    (
        compensated_cycles,
        compensated_overview,
        compensated_representative,
        compensated_series,
    ) = apply_water_compensation(
        cycles,
        overview,
        representative,
        series_summary,
        model,
    )
    validation = validate_water_compensation(
        cycles,
        compensated_cycles,
        overview,
        compensated_overview,
        compensated_series,
        model,
    )
    selected_ids = set(selected_series_summary["series_id"])
    selected_cycles = compensated_cycles[
        compensated_cycles["series_id"].isin(selected_ids)
    ].copy()
    selected_series = compensated_series[
        compensated_series["series_id"].isin(selected_ids)
    ].copy()

    csv_write(
        compensated_cycles,
        WATER_COMP_DIR / "cycle_summary_water_compensated.csv",
    )
    csv_write(
        compensated_overview,
        WATER_COMP_DIR / "overview_downsampled_water_compensated.csv",
    )
    csv_write(
        compensated_representative,
        WATER_COMP_DIR / "representative_cycles_water_compensated.csv",
    )
    csv_write(
        compensated_series,
        WATER_COMP_DIR / "series_summary_water_compensated.csv",
    )
    (WATER_COMP_DIR / "water_compensation_model.json").write_text(
        json.dumps(model, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    write_water_compensation_method(model)
    (WATER_COMP_DIR / "water_compensation_validation.json").write_text(
        json.dumps(validation, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    write_water_compensation_validation(validation)

    chart_rows = [
        {
            "chart_id": "W01",
            "chart_title": "水位补偿基线拟合",
            "analytical_question": "参照材料尾段能否定义稳定的共同水位漂移率",
            "chart_family": "Trend & Uncertainty",
            "chart_type": "fit diagnostic and correction line",
            "fields": "time,raw_reference,linear_fit,residual,water_compensation",
            "output": "water_compensation/figures/01_水位补偿基线拟合.png",
        },
        {
            "chart_id": "W02",
            "chart_title": "全部材料水位补偿前后分面",
            "analytical_question": "每条入选曲线在水位补偿前后如何变化",
            "chart_family": "Trend",
            "chart_type": "faceted raw-versus-corrected line",
            "fields": "cycle,raw_cathodic_potential,water_compensated_cathodic_potential",
            "output": "water_compensation/figures/02_全部材料水位补偿前后分面.png",
        },
        {
            "chart_id": "W03",
            "chart_title": "全部材料水位补偿后叠加对比",
            "analytical_question": "去除共同水位漂移模型后各材料如何比较",
            "chart_family": "Comparison & Trend",
            "chart_type": "overlaid high-contrast multi-series line",
            "fields": "cycle,water_compensated_cathodic_potential,water_compensated_negative_shift",
            "output": "water_compensation/figures/03_全部材料水位补偿后叠加对比.png",
        },
        {
            "chart_id": "W04",
            "chart_title": "各材料水位补偿影响对比",
            "analytical_question": "共同水位漂移模型从各材料首末负移中扣除了多少",
            "chart_family": "Comparison",
            "chart_type": "grouped horizontal bar",
            "fields": "material,raw_negative_shift,water_compensated_negative_shift",
            "output": "water_compensation/figures/04_各材料水位补偿影响对比.png",
        },
    ] if export_pdf else []
    pdf_path = OUTPUT_DIR / "启停数据_水位补偿图集.pdf"
    if export_pdf:
        configure_matplotlib()
        begin_figure_progress(
            phase="rendering_water",
            phase_label="生成水位补偿图集",
            phase_index=5,
            total=figure_total,
            percent_start=82,
            percent_end=94,
            detail=(
                "每页均在生成 PNG、SVG，并写入水位补偿 PDF 图集"
                if EXPORT_STATIC_FIGURES
                else "正在逐页写入水位补偿 PDF 图集"
            ),
        )
        with PdfPages(pdf_path) as pdf:
            plot_water_compensation_model(compensated_cycles, model, pdf)
            plot_water_compensation_facets(
                selected_cycles, selected_series, model, pdf
            )
            plot_water_compensation_overlay(
                selected_cycles, selected_series, model, pdf
            )
            plot_water_compensation_shift_comparison(selected_series, pdf)
            plot_water_compensation_series_details(
                selected_cycles,
                selected_series,
                segment_summary,
                model,
                pdf,
            )
    csv_write(
        pd.DataFrame(chart_rows),
        WATER_COMP_DIR / "water_compensation_chart_map.csv",
    )
    report_render_progress(
        phase="rendering_water" if export_pdf else "calculating_water",
        phase_label=(
            "生成水位补偿图集"
            if export_pdf
            else "计算水位补偿数据"
        ),
        phase_index=5,
        percent=94,
        completed=figure_total if export_pdf else 1,
        total=figure_total if export_pdf else 1,
        unit="pages" if export_pdf else "steps",
        current_item=(
            "水位补偿图集已完成"
            if export_pdf
            else "水位补偿数据已完成"
        ),
        detail=(
            "水位补偿图、材料详细图及 PDF 已全部生成"
            if export_pdf
            else "水位补偿表已完成，可直接在平台中查看"
        ),
    )
    _FIGURE_PROGRESS.clear()
    return {
        "model": model,
        "pdf": str(pdf_path) if export_pdf else "",
        "selected_series": int(len(selected_series)),
        "selected_cycles": int(len(selected_cycles)),
        "validation": validation,
        "chart_rows": chart_rows,
    }


def csv_write(frame: pd.DataFrame, path: Path) -> None:
    frame.to_csv(path, index=False, encoding="utf-8-sig")


def write_requirements() -> None:
    content = "\n".join(
        [
            "matplotlib=={}".format(MATPLOTLIB_VERSION),
            "numpy=={}".format(np.__version__),
            "pandas=={}".format(pd.__version__),
            "",
        ]
    )
    (OUTPUT_DIR / "requirements-matplotlib.txt").write_text(
        content, encoding="utf-8"
    )


def write_readme(
    inventory: pd.DataFrame,
    materials: List[dict],
    series_summary: pd.DataFrame,
    selected_series_summary: pd.DataFrame,
    selected_material_summary: pd.DataFrame,
    segment_summary: pd.DataFrame,
    cycle_summary: pd.DataFrame,
    *,
    data_mode: str,
    material_scope: str,
    configured_material_count: int,
    export_pdf: bool,
) -> None:
    excluded = inventory[~inventory["included_in_analysis"]]
    included = inventory[inventory["included_in_analysis"]]
    standard_files = int(
        (included["test_type"] == "standard_start_stop").sum()
    )
    variable_files = int(
        (included["test_type"] == "variable_start_stop").sum()
    )
    adt_files = int((included["test_type"] == "adt_start_stop").sum())
    adt_materials = sum("ADT 启停" in item["test_types"] for item in materials)
    incomplete = int(segment_summary["incomplete_cycle_candidates"].sum())
    reverse_phase_durations = "、".join(
        "{:g} s".format(value)
        for value in sorted(
            cycle_summary["reverse_phase_duration_s"].round(6).unique()
        )
    )
    cycle_durations = "、".join(
        "{:g} s".format(value)
        for value in sorted(
            cycle_summary["cycle_duration_s"].round(6).unique()
        )
    )
    continuity_names = "、".join(
        "`{}`".format(value)
        for value in series_summary.loc[
            series_summary["segment_count"] > 1,
            "series_display_name",
        ].tolist()
    )
    render_scope = (
        "仅新增或数据已更新材料；未变化材料未重复计算"
        if material_scope == "updated"
        else "全部材料"
    )
    pdf_outputs = []
    if export_pdf and data_mode in {"raw", "both"}:
        pdf_outputs.append(
            "- `启停数据_原始电位_全量图集.pdf`：原始未补偿图集。"
        )
    if export_pdf and data_mode in {"water", "both"}:
        pdf_outputs.append(
            "- `启停数据_水位补偿图集.pdf`：水位补偿图集。"
        )
    figure_outputs = ""
    if export_pdf:
        figure_outputs = (
            "- `figures/*.png`、`figures/*.svg`：总览图。\n"
            "- `figures/by_series/*`：每个分析序列的独立明细图。"
        )
    else:
        pdf_outputs.append(
            "- 本次仅发布平台分析数据；PDF 图集可在材料库中按需生成。"
        )
    content = """# 启停数据原始电位分析

## 分析范围

- 启停材料：{materials} 种；其中 ADT 材料 {adt_materials} 种，{selected_materials} 种进入本次总结图集。
- 数据库当前材料共 {configured_materials} 种；本次计算范围：{render_scope}。
- 纳入启停源文件：{included_files} 个（标准启停 {standard_files} 个，变工步启停 {variable_files} 个，ADT {adt_files} 个）。
- 分析序列：{series_count} 个；其中 {selected_series_count} 个进入本次总结图集。
- 完整启停循环：{cycles} 个。
- 另发现并排除 {excluded_count} 个候选文件：包括空表头文件和不属于启停的沉积/脉冲文件。
- 材料的重命名、是否入图与备注由 `启停绘图材料配置.xlsx` 控制；未入图材料仍保留在 CSV 分析结果中。

## 文件接续规则

- 同一材料文件夹中的主启停文件按文件时间顺序接续，第二个文件从前一文件最后采样点之后一个采样间隔开始。
- 本次跨文件接续的材料主序列：{continuity_names}。
- `NiMoP/启停2.txt` 以及 `NiMoP/pulse-30s-30s/启停2.txt` 均按用户确认作为前一启停文件的续段。
- 新 `保时来` 与原 `保时来M` 分别作为独立材料，不跨文件夹接续。
- 每条逐循环记录都保留材料、分析序列、源文件、文件内循环号、连续循环号和连续时间。

## 电位换算

`E_raw = E_measured vs Hg/HgO`

- 参比：Hg/HgO。
- 电解液：1 M KOH。
- pH：14。
- 温度：25 °C。
- 未做 iR 补偿；没有为全部样品提供逐一配对的 Rs。

## 逐循环统计

- 标准启停电流档位为 −0.30/+0.03 A cm⁻²；明确标注为启停的其他双电流方波作为“变工步启停”，按文件实测电流与阶段时长单独分组；ADT 同样保留实测电流。
- 实测反向/恢复阶段包含 {reverse_phase_durations}，完整循环时长包含 {cycle_durations}。
- 标准启停与变工步启停的阶段末端值取最后 1 s 所有采样点中位数；ADT 采样间隔为 2 s，取各阶段最后一个实际采样点。
- 阴极电位负移 = 前 10 循环阴极段末端统计值 − 当前/末端统计值；正值表示电位变得更负。
- 异常判据：阴极阶段全段最低点出现在 0–<15 s 为异常；出现在 ≥15 s 至该阶段结束为正常。
- 共识别 {incomplete} 个不完整循环候选，未纳入逐循环稳态比较。

## 主要文件

- `data_inventory.csv`：{candidate_files} 个启停候选文件的测试类型、SHA-256、行数、电流与排除理由。
- `segment_summary_raw.csv`：{included_files} 个纳入文件的接续区间与完整循环数。
- `series_summary_raw.csv`：{series_count} 个材料主序列的首末变化、趋势与异常比例。
- `material_summary_raw.csv`：{materials} 个材料主序列的对比指标。
- `cycle_summary_raw.csv`：逐循环完整统计。
- `cathodic_shift_anomaly_raw.csv`：阴极异常判断专用明细。
- `representative_cycles_raw.csv`：每个序列首、中、末代表循环的原始点。
- `overview_downsampled_raw.csv`：每约 5 s 抽样的全程预览数据。
- `segment_boundaries.csv`：主序列文件接续边界。
- `data_quality_report.md`：数据质量检查和解释边界。
{pdf_outputs}
{figure_outputs}

## 解释边界

- 所有电位均保留为仪器原始 Hg/HgO 基准，不做 RHE 或其他参比换算，也不做 iR 补偿。
- 不同启停工步的电流与阶段时长并不完全相同；平台按工步强制分开比较，跨工步结果只作描述性对照。
- 每个材料当前只有一个主测试序列，图中不添加误差棒；误差棒需要独立重复实验。
- “正常/异常”只描述本次约定的阴极段最低点时间位置，不直接等同于材料失效或机理结论。
""".format(
        materials=len(materials),
        configured_materials=configured_material_count,
        render_scope=render_scope,
        pdf_outputs="\n".join(pdf_outputs),
        figure_outputs=figure_outputs,
        selected_materials=len(selected_material_summary),
        adt_materials=adt_materials,
        included_files=int(inventory["included_in_analysis"].sum()),
        standard_files=standard_files,
        variable_files=variable_files,
        adt_files=adt_files,
        candidate_files=len(inventory),
        series_count=len(series_summary),
        selected_series_count=len(selected_series_summary),
        cycles=len(cycle_summary),
        excluded_count=len(excluded),
        incomplete=incomplete,
        continuity_names=continuity_names,
        reverse_phase_durations=reverse_phase_durations,
        cycle_durations=cycle_durations,
    )
    (OUTPUT_DIR / "README.md").write_text(content, encoding="utf-8")


def write_data_quality_report(
    inventory: pd.DataFrame,
    segment_summary: pd.DataFrame,
    series_summary: pd.DataFrame,
    material_summary: pd.DataFrame,
    selected_series_summary: pd.DataFrame,
    selected_material_summary: pd.DataFrame,
    cycle_summary: pd.DataFrame,
    qa_results: List[dict],
) -> None:
    included = inventory[inventory["included_in_analysis"]]
    excluded = inventory[~inventory["included_in_analysis"]]
    class_counts = excluded["candidate_class"].value_counts().to_dict()
    incomplete_segments = segment_summary[
        segment_summary["incomplete_cycle_candidates"] > 0
    ]
    continuity_series = series_summary[series_summary["segment_count"] > 1]
    duplicate_hashes = int(
        included["sha256"].duplicated(keep=False).sum()
    )
    standard_files = int(
        (included["test_type"] == "standard_start_stop").sum()
    )
    variable_files = int(
        (included["test_type"] == "variable_start_stop").sum()
    )
    adt_files = int((included["test_type"] == "adt_start_stop").sum())
    cathodic_duration_variants = sorted(
        cycle_summary["cathodic_phase_duration_s"].round(6).unique()
    )
    reverse_duration_variants = sorted(
        cycle_summary["reverse_phase_duration_s"].round(6).unique()
    )
    cycle_duration_variants = sorted(
        cycle_summary["cycle_duration_s"].round(6).unique()
    )
    report = """# 数据质量检查报告

## 数据集与粒度

- 输入：`{source}` 下的 `ID_GalSquareWave` 文件，以及文件名为 ADT 且程序类型为 `ID_ScriptMethod` 的 `.txt` 文件。
- 候选文件 {candidates} 个；纳入启停文件 {included} 个（标准 {standard_files}，变工步 {variable_files}，ADT {adt_files}）；材料 {materials} 种；分析序列 {series} 个。
- 当前配置选入总结图集的材料 {selected_materials} 种、分析序列 {selected_series} 个；未入图材料仍保留完整计算结果。
- 逐循环表的粒度是一行一个完整阴极—反向阶段配对；共 {cycles} 行。
- 标准启停与变工步启停末端使用最后 1 s 中位数；ADT 因 2 s 采样使用阶段末个实际采样点。
- 实测阶段时长：阴极 {cathodic_durations} s；反向 {reverse_durations} s；完整循环 {cycle_durations} s。

## 已执行检查

- 表头与数值可解析性、有效行数、时间单调性、重复时间点。
- 标准启停电流档位是否匹配 −0.30/+0.03 A cm⁻²；变工步启停是否具有明确启停文件名及一负一非负的双电流档位；ADT 是否具有交替阴极/恢复阶段。
- 采样间隔、协议对应的阶段长度、末端点数和完整阴极—恢复配对。
- SHA-256 精确重复文件检查。
- 同文件夹多文件接续后的时间单调性与循环编号连续性。
- 原始 Hg/HgO 电位恒等保留和异常判据的一致性断言。

## 发现

1. **纳入数据可用于本次比较（低风险，高置信度）**
   - {included} 个纳入文件均为有效数值表；标准文件匹配约定电流，变工步与 ADT 文件保留实测电流中位数。
   - 文件内时间倒退总数：{time_decreases}；解析失败数据行：{parse_errors}。
   - 纳入文件中的重复 SHA-256 记录数：{duplicate_hashes}。

2. **排除的方波文件没有足够证据属于启停测试（中等影响，高置信度）**
   - 空表头文件：{empty_count} 个。
   - 未明确标注为启停、单电流或电流档位无效的沉积/其他方波文件：{nonstandard_count} 个。
   - 这些文件若混入会改变测试协议和循环定义，因此只保留在清单，不进入图表。

3. **文件末端存在不完整循环（低风险，高置信度）**
   - 涉及文件段：{incomplete_segment_count} 个；不完整阴极循环候选合计 {incomplete_cycle_count} 个。
   - 不完整循环不进入末端统计、首末变化或异常比例统计，避免用缺失的恢复段进行比较。

4. **{continuity_count} 个材料主序列跨多个文件接续（中等影响，高置信度）**
   - 跨文件主序列数：{continuity_count}。
   - 接续时使用“前文件最后时间 + 一个采样间隔”作为后文件起点；源文件与分段边界均保留。

5. **NiMoP 的编号文件按用户确认接续（方法选择，高置信度）**
   - `NiMoP/启停2.txt` 与 `NiMoP/pulse-30s-30s/启停2.txt` 均作为同文件夹前一启停文件的后续段；段间边界可审计。

6. **新保时来与原保时来M独立分析（方法选择，高置信度）**
   - 两者属于不同材料文件夹，未跨文件夹拼接；各自基线、循环编号和首末变化均独立计算。

## 分析风险与解释边界

- 未做参比换算或 iR 补偿；材料间原始电位与负移比较可能同时包含欧姆降变化。
- 没有独立重复实验，材料排序是“当前这一条主序列”的描述性比较，不代表统计显著性。
- 不同启停工步的实际电流与时长可能不同；平台按工步分开比较，跨协议排序只作描述性对照。
- 异常规则仅按阴极段最低点出现在 15 s 分界线前后分类，不能单独证明失效机理。
- 文件修改时间只用于同文件夹主序列的段落排序；所有接续边界在表中可审计。

## 自动核验

{qa_lines}
""".format(
        source=SOURCE_ROOT,
        candidates=len(inventory),
        included=len(included),
        standard_files=standard_files,
        variable_files=variable_files,
        adt_files=adt_files,
        materials=len(material_summary),
        series=len(series_summary),
        selected_materials=len(selected_material_summary),
        selected_series=len(selected_series_summary),
        cycles=len(cycle_summary),
        time_decreases=int(included["time_decrease_count"].sum()),
        parse_errors=int(included["parse_error_rows"].sum()),
        duplicate_hashes=duplicate_hashes,
        empty_count=int(class_counts.get("excluded_empty_header_only", 0)),
        nonstandard_count=int(
            class_counts.get("excluded_nonstandard_square_wave", 0)
        ),
        incomplete_segment_count=len(incomplete_segments),
        incomplete_cycle_count=int(
            incomplete_segments["incomplete_cycle_candidates"].sum()
        ),
        continuity_count=len(continuity_series),
        cathodic_durations=", ".join(
            "{:g}".format(value) for value in cathodic_duration_variants
        ),
        reverse_durations=", ".join(
            "{:g}".format(value) for value in reverse_duration_variants
        ),
        cycle_durations=", ".join(
            "{:g}".format(value) for value in cycle_duration_variants
        ),
        qa_lines="\n".join(
            "- [{}] {}".format("通过" if item["passed"] else "失败", item["check"])
            for item in qa_results
        ),
    )
    (OUTPUT_DIR / "data_quality_report.md").write_text(report, encoding="utf-8")


def run_qa(
    inventory: pd.DataFrame,
    materials: List[dict],
    series_summary: pd.DataFrame,
    segment_summary: pd.DataFrame,
    cycle_summary: pd.DataFrame,
    overview: pd.DataFrame,
) -> List[dict]:
    results: List[dict] = []

    def check(condition: bool, message: str) -> None:
        results.append({"check": message, "passed": bool(condition)})
        if not condition:
            raise AssertionError(message)

    included = inventory[inventory["included_in_analysis"]]
    check(
        len(materials) > 0,
        "至少识别到 1 种启停材料",
    )
    check(
        len(included) > 0,
        "至少纳入 1 个启停文件",
    )
    check(
        int(series_summary["is_primary_series"].sum()) == len(materials),
        "每种材料恰有 1 条主启停序列",
    )
    check(
        bool(series_summary["include_in_summary_atlas"].any()),
        "至少 1 种材料被选入总结图集",
    )
    configured_names = [
        str(item["material_display_name"]).strip() for item in materials
    ]
    check(
        all(configured_names) and len(configured_names) == len(set(configured_names)),
        "材料绘图名称均非空且不重复",
    )
    material_paths = [
        item["material_relative_path"] for item in materials
    ]
    check(
        len(material_paths) == len(set(material_paths)),
        "材料键均唯一",
    )
    check(
        not included["sha256"].duplicated().any(),
        "纳入文件之间不存在 SHA-256 精确重复",
    )
    check(
        int(included["time_decrease_count"].sum()) == 0,
        "纳入文件内时间均单调不减",
    )
    check(
        int(included["parse_error_rows"].sum()) == 0,
        "纳入文件不存在无法解析的数值行",
    )
    sample_intervals = included["sample_interval_s"].to_numpy(dtype=float)
    check(
        bool(
            np.isfinite(sample_intervals).all()
            and (sample_intervals > 0).all()
        ),
        "全部纳入文件的采样间隔均为正值",
    )
    standard_cycles = cycle_summary[
        cycle_summary["test_type"] == "standard_start_stop"
    ]
    variable_cycles = cycle_summary[
        cycle_summary["test_type"] == "variable_start_stop"
    ]
    adt_cycles = cycle_summary[
        cycle_summary["test_type"] == "adt_start_stop"
    ]
    check(
        set(cycle_summary["test_type"].unique()).issubset(
            {
                "standard_start_stop",
                "variable_start_stop",
                "adt_start_stop",
            }
        ),
        "循环测试类型仅包含标准启停、变工步启停或 ADT 启停",
    )
    if not standard_cycles.empty:
        standard_cathodic = standard_cycles[
            "cathodic_phase_duration_s"
        ].to_numpy(dtype=float)
        standard_recovery = standard_cycles[
            "reverse_phase_duration_s"
        ].to_numpy(dtype=float)
        check(
            bool(
                np.isfinite(standard_cathodic).all()
                and np.isfinite(standard_recovery).all()
                and (
                    standard_cathodic >= MIN_COMPLETE_PHASE_ELAPSED_S
                ).all()
                and (
                    standard_recovery >= MIN_COMPLETE_PHASE_ELAPSED_S
                ).all()
            ),
            "标准启停各工步的阴极/恢复阶段时长均满足完整性要求",
        )
    if not variable_cycles.empty:
        variable_cathodic = variable_cycles[
            "cathodic_phase_duration_s"
        ].to_numpy(dtype=float)
        variable_recovery = variable_cycles[
            "reverse_phase_duration_s"
        ].to_numpy(dtype=float)
        check(
            bool(
                np.isfinite(variable_cathodic).all()
                and np.isfinite(variable_recovery).all()
                and (
                    variable_cathodic
                    >= VARIABLE_MIN_COMPLETE_PHASE_ELAPSED_S
                ).all()
                and (
                    variable_recovery
                    >= VARIABLE_MIN_COMPLETE_PHASE_ELAPSED_S
                ).all()
            ),
            "变工步启停阴极/恢复阶段均达到最短完整时长",
        )
    if not adt_cycles.empty:
        adt_cathodic = adt_cycles[
            "cathodic_phase_duration_s"
        ].to_numpy(dtype=float)
        adt_recovery = adt_cycles[
            "reverse_phase_duration_s"
        ].to_numpy(dtype=float)
        check(
            bool(
                np.isfinite(adt_cathodic).all()
                and np.isfinite(adt_recovery).all()
                and (
                    adt_cathodic >= ADT_MIN_COMPLETE_PHASE_ELAPSED_S
                ).all()
                and (
                    adt_recovery >= ADT_MIN_COMPLETE_PHASE_ELAPSED_S
                ).all()
            ),
            "ADT 阴极/恢复阶段时长均满足完整性要求",
        )
    status_expected = np.where(
        cycle_summary["cathodic_phase_min_time_s"].to_numpy(dtype=float)
        >= ANOMALY_BOUNDARY_S,
        "normal",
        "abnormal",
    )
    check(
        bool(
            np.array_equal(
                status_expected,
                cycle_summary["cathodic_shift_status"].to_numpy(dtype=str),
            )
        ),
        "异常判定严格符合 0–<15 s 异常、≥15 s 至阶段结束正常",
    )
    check(
        bool(
            np.allclose(
                overview["potential_raw_v"].to_numpy(dtype=float)
                - overview["potential_hghgo_v"].to_numpy(dtype=float),
                RAW_POTENTIAL_OFFSET_V,
                atol=1e-12,
                rtol=0.0,
            )
        ),
        "原始电位与仪器 Hg/HgO 实测电位完全一致，不做参比换算",
    )
    square_wave_cycles = cycle_summary[
        cycle_summary["test_type"].isin(
            ["standard_start_stop", "variable_start_stop"]
        )
    ]
    if not square_wave_cycles.empty:
        check(
            bool(
                (
                    square_wave_cycles["cathodic_last1s_point_count"]
                    >= square_wave_cycles["minimum_endpoint_point_count"]
                ).all()
                and (
                    square_wave_cycles["cathodic_phase_point_count"]
                    >= square_wave_cycles["minimum_phase_point_count"]
                ).all()
                and (
                    square_wave_cycles["reverse_last1s_point_count"]
                    >= square_wave_cycles["minimum_endpoint_point_count"]
                ).all()
                and (
                    square_wave_cycles["reverse_phase_point_count"]
                    >= square_wave_cycles["minimum_phase_point_count"]
                ).all()
            ),
            "方波启停每循环都有足够的全段与最后 1 s 采样点",
        )
    if not adt_cycles.empty:
        check(
            bool(
                (
                    adt_cycles["cathodic_last1s_point_count"]
                    >= adt_cycles["minimum_endpoint_point_count"]
                ).all()
                and (
                    adt_cycles["cathodic_phase_point_count"]
                    >= adt_cycles["minimum_phase_point_count"]
                ).all()
                and (
                    adt_cycles["reverse_last1s_point_count"]
                    >= adt_cycles["minimum_endpoint_point_count"]
                ).all()
                and (
                    adt_cycles["reverse_phase_point_count"]
                    >= adt_cycles["minimum_phase_point_count"]
                ).all()
            ),
            "ADT 每循环都有足够的全段采样点和阶段末端采样点",
        )
    for series_id, group in segment_summary.groupby("series_id", sort=False):
        ordered = group.sort_values("segment_index")
        if len(ordered) < 2:
            continue
        previous = ordered.iloc[:-1].reset_index(drop=True)
        following = ordered.iloc[1:].reset_index(drop=True)
        expected_starts = (
            previous["continuous_time_end_s"].to_numpy(dtype=float)
            + previous["sample_interval_s"].to_numpy(dtype=float)
        )
        actual_starts = following["continuous_time_start_s"].to_numpy(dtype=float)
        check(
            bool(np.allclose(expected_starts, actual_starts, atol=1e-8, rtol=0.0)),
            "{} 的文件段时间无重叠、无额外空隙".format(series_id),
        )
    check(
        bool(
            cycle_summary.groupby("series_id")["cycle"].apply(
                lambda values: np.array_equal(
                    values.to_numpy(dtype=int),
                    np.arange(1, len(values) + 1, dtype=int),
                )
            ).all()
        ),
        "每个分析序列的连续循环编号从 1 开始且无断号",
    )
    return results


def select_render_scope(
    materials: List[dict],
    series_specs: List[dict],
    material_scope: str,
    updated_material_keys: Sequence[str],
) -> Tuple[List[dict], List[dict]]:
    if material_scope == "all":
        return materials, series_specs
    if material_scope != "updated":
        raise RuntimeError("绘图范围只支持 all 或 updated")
    current_keys = {
        str(material["material_relative_path"]) for material in materials
    }
    requested_keys = {str(key) for key in updated_material_keys if str(key)}
    unknown = sorted(requested_keys - current_keys)
    if unknown:
        raise RuntimeError("仅更新绘图包含已不存在的材料，请重新读取材料表")
    selected_keys = current_keys & requested_keys
    if not selected_keys:
        raise RuntimeError("当前没有新增或数据已更新的材料可供绘图")
    scoped_materials = [
        material
        for material in materials
        if str(material["material_relative_path"]) in selected_keys
    ]
    scoped_series = [
        spec
        for spec in series_specs
        if str(spec["material_relative_path"]) in selected_keys
    ]
    return scoped_materials, scoped_series


def render_from_config(
    *,
    data_mode: str = "both",
    material_scope: str = "all",
    updated_material_keys: Sequence[str] = (),
    export_pdf: bool = False,
    export_static_figures: bool = False,
) -> dict:
    if data_mode not in {"raw", "water", "both"}:
        raise RuntimeError("绘图数据只支持 raw、water 或 both")
    if material_scope not in {"all", "updated"}:
        raise RuntimeError("绘图范围只支持 all 或 updated")
    if export_static_figures and not export_pdf:
        raise RuntimeError("PNG/SVG 导出必须同时启用 PDF 导出")
    configure_static_figure_export(export_static_figures)
    if export_pdf and not export_static_figures:
        for directory in (FIGURE_DIR, WATER_COMP_FIGURE_DIR):
            if not directory.is_dir():
                continue
            for path in directory.rglob("*"):
                if path.is_file() and path.suffix.casefold() in {".png", ".svg"}:
                    path.unlink()
    report_render_progress(
        phase="loading_config",
        phase_label="校验材料与绘图配置",
        phase_index=2,
        percent=8,
        completed=0,
        total=1,
        unit="steps",
        current_item="材料与绘图配置",
        detail="正在固定本次材料名称、入图选择与数据快照",
    )
    if material_scope == "updated":
        try:
            baseline_snapshot = json.loads(
                CONFIG_SNAPSHOT_JSON.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("当前材料快照无法读取，请先更新数据") from exc
        baseline_materials = (
            baseline_snapshot.get("materials")
            if isinstance(baseline_snapshot, dict)
            else None
        )
        baseline_files = (
            baseline_snapshot.get("files")
            if isinstance(baseline_snapshot, dict)
            else None
        )
        if not isinstance(baseline_materials, list) or not isinstance(
            baseline_files, list
        ):
            raise RuntimeError("当前材料快照格式无效，请先更新数据")
        material_order = [
            str(row.get("key") or "")
            for row in baseline_materials
            if isinstance(row, dict)
        ]
        if (
            not material_order
            or any(not value for value in material_order)
            or len(material_order) != len(set(material_order))
        ):
            raise RuntimeError("当前材料快照格式无效，请先更新数据")
        requested_keys = _normalized_material_keys(updated_material_keys)
        unknown = sorted(requested_keys - set(material_order))
        if unknown:
            raise RuntimeError("仅更新绘图包含已不存在的材料，请重新读取材料表")
        if not requested_keys:
            raise RuntimeError("当前没有新增或数据已更新的材料可供绘图")

        records, data_cache = discover_and_profile(sorted(requested_keys))
        if not records:
            raise RuntimeError("没有识别到仅更新范围内的启停候选 .txt 文件")
        baseline_file_by_path = {
            str(row.get("relative_path") or ""): row
            for row in baseline_files
            if isinstance(row, dict) and str(row.get("relative_path") or "")
        }
        # Exact-duplicate classification is global. Reapply the already fixed
        # current-snapshot decision so a scoped run never has to parse another
        # material merely to rediscover the canonical duplicate.
        for record in records:
            baseline_file = baseline_file_by_path.get(record["relative_path"])
            if (
                isinstance(baseline_file, dict)
                and baseline_file.get("candidate_class")
                == "excluded_exact_duplicate"
                and str(baseline_file.get("sha256") or "") == record["sha256"]
            ):
                record["candidate_class"] = "excluded_exact_duplicate"
                record["included_in_analysis"] = False
                record["exclusion_reason"] = str(
                    baseline_file.get("exclusion_reason") or "与其他文件完全相同"
                )
                data_cache.pop(record["absolute_path"], None)

        materials, series_specs = build_series_specs(records, material_order)
        scoped_snapshot = build_material_config_snapshot(
            records, materials, series_specs
        )
        fresh_fingerprints = {
            str(row["key"]): str(row["fingerprint"])
            for row in scoped_snapshot["materials"]
        }
        baseline_fingerprints = {
            str(row.get("key") or ""): str(row.get("fingerprint") or "")
            for row in baseline_materials
            if isinstance(row, dict)
        }
        if set(fresh_fingerprints) != requested_keys or any(
            fresh_fingerprints[key] != baseline_fingerprints.get(key, "")
            for key in requested_keys
        ):
            raise RuntimeError(
                "仅更新材料与当前数据快照不一致，请先更新材料表后重试"
            )
        all_config_rows = read_and_validate_material_config(baseline_snapshot)
        config_rows = [
            row
            for row in all_config_rows
            if str(row.get("key") or "") in requested_keys
        ]
        apply_material_config(
            materials,
            series_specs,
            config_rows,
            scoped_snapshot,
        )
        snapshot = baseline_snapshot
        configured_material_count = len(material_order)
    else:
        records, data_cache = discover_and_profile()
        if not records:
            raise RuntimeError("没有识别到任何方波候选 .txt 文件")
        materials, series_specs = build_series_specs(records)
        snapshot = build_material_config_snapshot(
            records, materials, series_specs
        )
        config_rows = read_and_validate_material_config(snapshot)
        apply_material_config(
            materials,
            series_specs,
            config_rows,
            snapshot,
        )
        configured_material_count = len(materials)
    if not any(
        bool(material.get("include_in_summary_atlas"))
        for material in materials
    ):
        raise RuntimeError("当前绘图范围内没有材料被选择进入总结图集")
    if data_mode in {"water", "both"} and material_scope == "updated":
        scoped_keys = {
            str(material["material_relative_path"]) for material in materials
        }
        if WATER_COMP_REFERENCE_KEY not in scoped_keys:
            raise RuntimeError(
                "仅更新材料绘图不包含水位补偿参照材料；"
                "请选择原始未补偿数据，或改为绘制全部材料。"
            )
    report_render_progress(
        phase="loading_config",
        phase_label="校验材料与绘图配置",
        phase_index=2,
        percent=24,
        completed=1,
        total=1,
        unit="steps",
        current_item="配置校验完成",
        detail="数据文件与材料配置一致，开始计算启停循环",
    )
    (
        cycle_summary,
        overview,
        representative,
        series_summary,
        segment_summary,
        boundaries,
    ) = analyze_all_series(series_specs, data_cache)
    material_summary = make_material_summary(materials, series_summary)
    annotate_config_fields(
        [
            cycle_summary,
            overview,
            representative,
            segment_summary,
            boundaries,
        ],
        series_summary,
    )

    selected_series_summary = series_summary[
        series_summary["include_in_summary_atlas"]
    ].copy()
    selected_material_summary = material_summary[
        material_summary["include_in_summary_atlas"]
    ].copy()
    selected_series_ids = set(selected_series_summary["series_id"])
    selected_cycles = cycle_summary[
        cycle_summary["series_id"].isin(selected_series_ids)
    ].copy()
    selected_overview = overview[
        overview["series_id"].isin(selected_series_ids)
    ].copy()
    selected_representative = representative[
        representative["series_id"].isin(selected_series_ids)
    ].copy()
    selected_segments = segment_summary[
        segment_summary["series_id"].isin(selected_series_ids)
    ].copy()
    selected_boundaries = boundaries[
        boundaries["series_id"].isin(selected_series_ids)
    ].copy()

    inventory_columns = [
        key for key in records[0].keys() if not key.startswith("_")
    ]
    inventory = pd.DataFrame(records)[inventory_columns].sort_values(
        "relative_path"
    )
    file_counts = included_file_counts(inventory)
    qa_results = run_qa(
        inventory,
        materials,
        series_summary,
        segment_summary,
        cycle_summary,
        overview,
    )
    report_render_progress(
        phase="writing_tables",
        phase_label="整理分析表与质量检查",
        phase_index=3,
        percent=55,
        completed=0,
        total=1,
        unit="steps",
        current_item="分析结果表",
        detail="正在写入循环、材料、分段与异常判断结果",
    )

    anomaly_columns = [
        "series_id",
        "series_order",
        "series_display_name",
        "material_id",
        "material_display_name",
        "is_primary_series",
        "is_special_series",
        "include_in_summary_atlas",
        "material_user_notes",
        "segment_index",
        "source_file",
        "cycle",
        "cycle_in_segment",
        "cathodic_endpoint_time_s",
        "cathodic_endpoint_time_h",
        "cathodic_last1s_median_raw_v",
        "cathodic_last1s_min_raw_v",
        "cathodic_last1s_min_phase_time_s",
        "cathodic_phase_min_raw_v",
        "cathodic_phase_min_time_s",
        "cathodic_phase_min_continuous_time_s",
        "cathodic_phase_min_continuous_time_h",
        "cathodic_last1s_contains_phase_min",
        "cathodic_phase_min_in_second_half",
        "cathodic_shift_status",
        "cathodic_shift_status_zh",
        "cathodic_negative_shift_mv",
        "cathodic_last1s_point_count",
        "cathodic_phase_point_count",
    ]
    csv_write(inventory, OUTPUT_DIR / "data_inventory.csv")
    csv_write(segment_summary, OUTPUT_DIR / "segment_summary_raw.csv")
    csv_write(series_summary, OUTPUT_DIR / "series_summary_raw.csv")
    csv_write(material_summary, OUTPUT_DIR / "material_summary_raw.csv")
    csv_write(cycle_summary, OUTPUT_DIR / "cycle_summary_raw.csv")
    csv_write(
        cycle_summary[anomaly_columns],
        OUTPUT_DIR / "cathodic_shift_anomaly_raw.csv",
    )
    csv_write(representative, OUTPUT_DIR / "representative_cycles_raw.csv")
    csv_write(overview, OUTPUT_DIR / "overview_downsampled_raw.csv")
    csv_write(boundaries, OUTPUT_DIR / "segment_boundaries.csv")

    write_requirements()
    write_readme(
        inventory,
        materials,
        series_summary,
        selected_series_summary,
        selected_material_summary,
        segment_summary,
        cycle_summary,
        data_mode=data_mode,
        material_scope=material_scope,
        configured_material_count=configured_material_count,
        export_pdf=export_pdf,
    )
    write_data_quality_report(
        inventory,
        segment_summary,
        series_summary,
        material_summary,
        selected_series_summary,
        selected_material_summary,
        cycle_summary,
        qa_results,
    )
    report_render_progress(
        phase="writing_tables",
        phase_label="整理分析表与质量检查",
        phase_index=3,
        percent=60,
        completed=1,
        total=1,
        unit="steps",
        current_item="分析表已完成",
        detail=(
            "数据表与质量检查已写入，开始生成 PDF 图集"
            if export_pdf
            else "数据表与质量检查已写入，正在准备网页曲线"
        ),
    )
    chart_rows: List[dict] = []
    if export_pdf and data_mode in {"raw", "both"}:
        chart_rows.extend(
            render_all_figures(
                selected_overview,
                selected_cycles,
                selected_representative,
                selected_series_summary,
                selected_material_summary,
                selected_segments,
                selected_boundaries,
            )
        )
    water_compensation: dict = {
        "enabled": False,
        "skipped": True,
        "skip_reason": "raw_only",
        "chart_rows": [],
    }
    if data_mode in {"water", "both"}:
        water_compensation = render_water_compensation_outputs(
            cycle_summary,
            overview,
            representative,
            series_summary,
            selected_series_summary,
            segment_summary,
            export_pdf=export_pdf,
        )
        water_compensation["enabled"] = True
        water_compensation["skipped"] = False
        water_compensation["skip_reason"] = ""
        chart_rows.extend(water_compensation["chart_rows"])
    csv_write(pd.DataFrame(chart_rows), OUTPUT_DIR / "chart_map.csv")

    report_render_progress(
        phase="finalizing_outputs",
        phase_label="校验并整理输出文件",
        phase_index=6,
        percent=95,
        completed=0,
        total=2,
        unit="steps",
        current_item="分析摘要与文件清单",
        detail="正在生成分析摘要并核对全部输出文件",
    )

    special_series = series_summary[series_summary["is_special_series"]]
    summary = {
        "analysis_name": "CorrTest all-material start-stop raw-potential analysis",
        "source_root": str(SOURCE_ROOT),
        "analysis_dir": str(OUTPUT_DIR),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "render_options": {
            "data_mode": data_mode,
            "material_scope": material_scope,
            "export_pdf": export_pdf,
            "export_static_figures": export_static_figures,
            "updated_material_keys": sorted(
                {str(key) for key in updated_material_keys if str(key)}
            ),
        },
        "material_config": {
            "workbook": str(CONFIG_WORKBOOK),
            "dataset_fingerprint": snapshot["dataset_fingerprint"],
            "selected_materials": [
                {
                    "material_relative_path": row["key"],
                    "material_display_name": row["plot_name"],
                    "material_user_notes": row.get("notes", ""),
                }
                for row in config_rows
                if bool(row.get("include_in_summary_atlas"))
            ],
            "excluded_from_atlas": [
                {
                    "material_relative_path": row["key"],
                    "material_display_name": row["plot_name"],
                    "material_user_notes": row.get("notes", ""),
                }
                for row in config_rows
                if not bool(row.get("include_in_summary_atlas"))
            ],
        },
        "runtime": {
            "python_invocation": "arch -x86_64 /usr/bin/python3",
            "matplotlib": MATPLOTLIB_VERSION,
            "numpy": np.__version__,
            "pandas": pd.__version__,
            "network_install_performed": False,
            "analysis_workflow_version": ANALYSIS_WORKFLOW_VERSION,
            "analysis_script_name": Path(__file__).name,
            "analysis_script_sha256": sha256_file(Path(__file__).resolve()),
        },
        "potential_basis": {
            "reference": POTENTIAL_REFERENCE,
            "electrolyte": ELECTROLYTE,
            "pH": ELECTROLYTE_PH,
            "temperature_c": TEMPERATURE_C,
            "formula": "E_raw = E_measured vs Hg/HgO",
            "raw_potential_offset_v": RAW_POTENTIAL_OFFSET_V,
            "reference_conversion_applied": False,
            "ir_correction_applied": IR_CORRECTION_APPLIED,
        },
        "water_compensation": {
            key: value
            for key, value in water_compensation.items()
            if key != "chart_rows"
        },
        "protocol": {
            "cathodic_current_a_cm2": CATHODIC_CURRENT_A_CM2,
            "reverse_current_a_cm2": REVERSE_CURRENT_A_CM2,
            "cathodic_phase_duration_s_expected": None,
            "phase_duration_policy": "observed_per_work_step",
            "standard_phase_minimum_complete_s": (
                MIN_COMPLETE_PHASE_ELAPSED_S
            ),
            "cathodic_phase_duration_s_observed": sorted(
                float(value)
                for value in cycle_summary[
                    "cathodic_phase_duration_s"
                ].round(6).unique()
            ),
            "reverse_phase_duration_s_observed": sorted(
                float(value)
                for value in cycle_summary[
                    "reverse_phase_duration_s"
                ].round(6).unique()
            ),
            "cycle_duration_s_observed": sorted(
                float(value)
                for value in cycle_summary[
                    "cycle_duration_s"
                ].round(6).unique()
            ),
            "steady_window_s": STEADY_WINDOW_S,
            "steady_window_definition": "final 1 s of each actual phase",
            "anomaly_rule": (
                "abnormal when cathodic minimum occurs at 0 <= t < 15 s; "
                "normal when it occurs at 15 <= t < 30 s"
            ),
        },
        "continuity_policy": {
            "group_by": "material folder",
            "main_file_order": "file modification time",
            "time_offset": (
                "previous segment last sample plus one median sample interval"
            ),
            "special_marked_file_policy": (
                "启停 followed directly by a number is analyzed as a separate "
                "series unless an explicit continuation override is confirmed"
            ),
            "explicit_continuation_overrides": [
                "NiMoP/启停2.txt",
                "NiMoP/pulse-30s-30s/启停2.txt",
            ],
            "explicit_distinct_materials": ["保时来", "保时来M"],
            "special_series": special_series[
                ["series_id", "series_display_name", "special_file_name"]
            ].to_dict(orient="records"),
        },
        "counts": {
            "square_wave_candidates": int(len(inventory)),
            "included_files": file_counts["included_files"],
            "included_standard_files": file_counts[
                "included_standard_files"
            ],
            "included_variable_files": file_counts[
                "included_variable_files"
            ],
            "included_adt_files": file_counts["included_adt_files"],
            "excluded_square_wave_files": int(
                (~inventory["included_in_analysis"]).sum()
            ),
            "materials": int(len(material_summary)),
            "available_materials": int(configured_material_count),
            "materials_skipped_unchanged": int(
                configured_material_count - len(material_summary)
            ),
            "summary_atlas_materials": int(len(selected_material_summary)),
            "analysis_series": int(len(series_summary)),
            "summary_atlas_series": int(len(selected_series_summary)),
            "primary_series": int(series_summary["is_primary_series"].sum()),
            "special_series": int(series_summary["is_special_series"].sum()),
            "complete_cycles": int(len(cycle_summary)),
            "summary_atlas_complete_cycles": int(len(selected_cycles)),
            "normal_cycles": int(
                (cycle_summary["cathodic_shift_status"] == "normal").sum()
            ),
            "abnormal_cycles": int(
                (cycle_summary["cathodic_shift_status"] == "abnormal").sum()
            ),
            "incomplete_cycle_candidates_excluded": int(
                segment_summary["incomplete_cycle_candidates"].sum()
            ),
        },
        "qa": qa_results,
        "outputs": sorted(
            path.relative_to(OUTPUT_DIR).as_posix()
            for path in OUTPUT_DIR.rglob("*")
            if path.is_file()
            and ".matplotlib" not in path.parts
            and "__pycache__" not in path.parts
        ),
    }
    (OUTPUT_DIR / "analysis_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    report_render_progress(
        phase="finalizing_outputs",
        phase_label="校验并整理输出文件",
        phase_index=6,
        percent=96,
        completed=2,
        total=2,
        unit="steps",
        current_item="输出文件已固定",
        detail="分析结果已写定，等待服务统一生成清单与 SHA-256 校验",
    )

    return {
        "stage": (
            "PDF 图集已按材料配置生成"
            if export_pdf
            else "网页分析结果已按材料配置生成"
        ),
        "output_dir": str(OUTPUT_DIR),
        "pdf": (
            str(OUTPUT_DIR / "启停数据_原始电位_全量图集.pdf")
            if export_pdf and data_mode in {"raw", "both"}
            else ""
        ),
        "water_pdf": (
            str(OUTPUT_DIR / "启停数据_水位补偿图集.pdf")
            if export_pdf and data_mode in {"water", "both"}
            else ""
        ),
        "pdf_exported": export_pdf,
        "static_figures_exported": export_static_figures,
        "render_data_mode": data_mode,
        "render_material_scope": material_scope,
        "materials_available": configured_material_count,
        "materials_skipped_unchanged": int(
            configured_material_count - len(material_summary)
        ),
        "materials_analyzed": len(material_summary),
        "materials_in_atlas": len(selected_material_summary),
        "materials_excluded_from_atlas": int(
            len(material_summary) - len(selected_material_summary)
        ),
        "included_files": file_counts["included_files"],
        "analysis_series": len(series_summary),
        "series_in_atlas": len(selected_series_summary),
        "complete_cycles": len(cycle_summary),
        "normal_cycles": int(
            (cycle_summary["cathodic_shift_status"] == "normal").sum()
        ),
        "abnormal_cycles": int(
            (cycle_summary["cathodic_shift_status"] == "abnormal").sum()
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="启停数据工作流：更新配置、发布网页分析，并可按需导出 PDF"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--prepare-config",
        action="store_true",
        help="扫描本地原始数据并更新材料配置表，不生成图集",
    )
    mode.add_argument(
        "--render",
        action="store_true",
        help="读取已保存的材料配置并发布网页分析；默认不生成 PDF",
    )
    parser.add_argument(
        "--export-pdf",
        action="store_true",
        help="与 --render 一起使用，按需生成 PDF 图集",
    )
    parser.add_argument(
        "--export-static-figures",
        action="store_true",
        help="与 --render --export-pdf 一起使用，额外导出 PNG 和 SVG",
    )
    parser.add_argument(
        "--material-config-json",
        type=Path,
        help="网页工作流用：绘图前将受校验的 JSON 配置同步回 Excel",
    )
    parser.add_argument(
        "--progress-json",
        type=Path,
        help="网页工作流用：原子写入不含路径的绘图进度 JSON",
    )
    parser.add_argument(
        "--data-mode",
        choices=("raw", "water", "both"),
        default="both",
        help="绘图数据：原始未补偿、水位补偿或两者",
    )
    parser.add_argument(
        "--material-scope",
        choices=("all", "updated"),
        default="all",
        help="绘图范围：全部材料或仅新增/数据已更新材料",
    )
    parser.add_argument(
        "--updated-material-key",
        action="append",
        default=[],
        help="仅更新绘图时允许计算的材料键；可重复",
    )
    arguments = parser.parse_args()
    configure_render_progress(arguments.progress_json)
    collection_lock = acquire_collection_snapshot_lock()
    try:
        if arguments.render:
            if arguments.material_config_json:
                apply_material_config_json(
                    arguments.material_config_json.resolve()
                )
            result = render_from_config(
                data_mode=arguments.data_mode,
                material_scope=arguments.material_scope,
                updated_material_keys=arguments.updated_material_key,
                export_pdf=arguments.export_pdf,
                export_static_figures=arguments.export_static_figures,
            )
        else:
            if arguments.material_config_json:
                parser.error("--material-config-json 只能与 --render 一起使用")
            if (
                arguments.data_mode != "both"
                or arguments.material_scope != "all"
                or arguments.updated_material_key
                or arguments.export_pdf
                or arguments.export_static_figures
            ):
                parser.error("绘图、材料范围与 PDF 导出选项只能与 --render 一起使用")
            result = update_material_config_workbook()
    except Exception as exc:
        print(
            "操作未完成：{}".format(exc),
            file=sys.stderr,
        )
        raise
    finally:
        fcntl.flock(collection_lock.fileno(), fcntl.LOCK_UN)
        collection_lock.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
