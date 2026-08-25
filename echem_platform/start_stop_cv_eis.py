from __future__ import annotations

import base64
import contextlib
import csv
import datetime as dt
import gzip
import hashlib
import io
import json
import math
import re
import sqlite3
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


RHE_OFFSET_V = 0.9268
IR_COMPENSATION_FRACTION = 0.90
TARGET_CURRENT_DENSITIES_MA_CM2 = (10, 50, 100, 200, 300, 500)
MAX_SOURCE_BYTES = 16 * 1024 * 1024
MAX_DECOMPRESSED_HEADER_BYTES = 2 * 1024 * 1024
MAX_CHART_POINTS = 2400
MIN_RS_CROSS_FREQUENCY_HZ = 100.0


class CvEisAnalysisError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = int(status)


class ReadOnlyCvEisDatabase:
    """Narrow query-only adapter for the LAN CV/EIS source volume."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()

    @contextlib.contextmanager
    def session(self):
        connection = sqlite3.connect(
            f"file:{self.path}?mode=ro",
            uri=True,
            timeout=60,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        try:
            yield connection
        finally:
            connection.close()


@dataclass(frozen=True)
class SourceRecord:
    source_version_id: int
    blob_id: int
    repository_path: str
    size_bytes: int
    sha256: str
    source_modified_utc: str
    kind: str
    stage: str
    priority: int


@dataclass(frozen=True)
class CvData:
    potential_v: tuple[float, ...]
    current: tuple[float, ...]
    current_basis: str
    current_unit: str
    area_cm2: float | None
    instrument_ir_applied: bool | None
    instrument: str
    format_name: str


@dataclass(frozen=True)
class EisData:
    frequency_hz: tuple[float, ...]
    z_real: tuple[float, ...]
    z_imag: tuple[float, ...]
    impedance_basis: str
    impedance_unit: str
    instrument: str
    format_name: str


def _safe_float(value: Any) -> float | None:
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _normalized_header(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value)).casefold()
    return re.sub(r"[^a-z0-9]+", "", text)


def _decode_text(payload: bytes) -> str:
    for encoding in ("utf-8-sig", "gb18030", "utf-16"):
        try:
            text = payload.decode(encoding)
        except UnicodeError:
            continue
        if "\x00" not in text[:4096]:
            return text
    raise CvEisAnalysisError("文件不是可识别的文本导出。", 422)


def _corrtest_header(text: str) -> str:
    first_line = text.splitlines()[0] if text.splitlines() else ""
    if not first_line.startswith("CSStudioFile,"):
        return ""
    parts = first_line.split(",", 2)
    if len(parts) != 3 or len(parts[2]) > 4 * 1024 * 1024:
        return ""
    try:
        compressed = base64.b64decode(parts[2], validate=True)
        with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as source:
            payload = source.read(MAX_DECOMPRESSED_HEADER_BYTES + 1)
        if len(payload) > MAX_DECOMPRESSED_HEADER_BYTES:
            return ""
        return payload.decode("utf-8", "replace")
    except (ValueError, OSError, EOFError):
        return ""


def _corrtest_metadata(text: str) -> dict[str, Any]:
    header = _corrtest_header(text)
    if not header:
        return {}
    area_match = re.search(r"(?:CellParam=Area=|Surface Area:\s*)([0-9.eE+-]+)", header)
    ir_match = re.search(r"\bIsIR=(True|False)\b", header, re.IGNORECASE)
    area = _safe_float(area_match.group(1)) if area_match else None
    return {
        "area_cm2": area if area is not None and area > 0 else None,
        "instrument_ir_applied": (
            ir_match.group(1).casefold() == "true" if ir_match else None
        ),
    }


def _table_rows(text: str, header_index: int, delimiter: str) -> tuple[list[str], list[list[str]]]:
    lines = text.splitlines()
    reader = csv.reader(lines[header_index:], delimiter=delimiter, skipinitialspace=True)
    rows = list(reader)
    if not rows:
        raise CvEisAnalysisError("数据表为空。", 422)
    header = [item.strip() for item in rows[0]]
    data = [[item.strip() for item in row] for row in rows[1:] if any(item.strip() for item in row)]
    return header, data


def _find_header(text: str, required_tokens: tuple[str, ...]) -> tuple[int, str]:
    for index, line in enumerate(text.splitlines()):
        normalized = _normalized_header(line)
        if all(token in normalized for token in required_tokens):
            return index, "\t" if "\t" in line else ","
    raise CvEisAnalysisError("找不到要求的数据列。", 422)


def _column_index(headers: Iterable[str], alternatives: tuple[str, ...]) -> int:
    normalized = [_normalized_header(item) for item in headers]
    for alternative in alternatives:
        for index, value in enumerate(normalized):
            if alternative in value:
                return index
    raise CvEisAnalysisError("数据列名称无法识别。", 422)


def parse_cv_text(payload: bytes) -> CvData:
    text = _decode_text(payload)
    corrtest = text.startswith("CSStudioFile,ID_CV,")
    if corrtest:
        header_index, delimiter = _find_header(text, ("ev", "iacm2"))
        format_name = "CorrTest CV"
        instrument = "CorrTest"
    elif "Cyclic Voltammetry" in text:
        header_index, delimiter = _find_header(text, ("potentialv", "currenta"))
        format_name = "CHI CV"
        instrument = "CHI"
    else:
        raise CvEisAnalysisError("不是受支持的 CV 文本导出。", 422)
    headers, rows = _table_rows(text, header_index, delimiter)
    potential_index = _column_index(headers, ("potentialv", "ev"))
    current_index = _column_index(headers, ("iacm2", "currenta", "ia"))
    density_basis = any("cm2" in _normalized_header(item) for item in headers)
    potential: list[float] = []
    current: list[float] = []
    for row in rows:
        if max(potential_index, current_index) >= len(row):
            continue
        e_value = _safe_float(row[potential_index])
        i_value = _safe_float(row[current_index])
        if e_value is None or i_value is None:
            continue
        potential.append(e_value)
        current.append(i_value)
    if len(potential) < 40:
        raise CvEisAnalysisError("CV 有效数据点不足。", 422)
    metadata = _corrtest_metadata(text) if corrtest else {}
    return CvData(
        potential_v=tuple(potential),
        current=tuple(current),
        current_basis="density" if density_basis else "absolute",
        current_unit="A/cm²" if density_basis else "A",
        area_cm2=metadata.get("area_cm2"),
        instrument_ir_applied=metadata.get("instrument_ir_applied"),
        instrument=instrument,
        format_name=format_name,
    )


def parse_eis_text(payload: bytes) -> EisData:
    text = _decode_text(payload)
    corrtest = text.startswith("CSStudioFile,ID_EIS")
    if corrtest:
        header_index, delimiter = _find_header(text, ("freqhz", "zohmcm2"))
        format_name = "CorrTest EIS"
        instrument = "CorrTest"
    elif "A.C. Impedance" in text:
        header_index, delimiter = _find_header(text, ("freqhz", "zohm"))
        format_name = "CHI EIS"
        instrument = "CHI"
    else:
        raise CvEisAnalysisError("不是受支持的 EIS 文本导出。", 422)
    headers, rows = _table_rows(text, header_index, delimiter)
    frequency_index = _column_index(headers, ("frequencyhz", "freqhz"))
    real_index = _column_index(headers, ("zohmcm2", "zohm"))
    normalized = [_normalized_header(item) for item in headers]
    imag_candidates = [
        index
        for index, value in enumerate(normalized)
        if value.startswith("z") and index != real_index and ("ohm" in value)
    ]
    if not imag_candidates:
        raise CvEisAnalysisError("EIS 虚部列无法识别。", 422)
    imag_index = imag_candidates[0]
    density_basis = any("cm2" in value for value in normalized)
    frequency: list[float] = []
    z_real: list[float] = []
    z_imag: list[float] = []
    for row in rows:
        if max(frequency_index, real_index, imag_index) >= len(row):
            continue
        f_value = _safe_float(row[frequency_index])
        real_value = _safe_float(row[real_index])
        imag_value = _safe_float(row[imag_index])
        if f_value is None or real_value is None or imag_value is None or f_value <= 0:
            continue
        frequency.append(f_value)
        z_real.append(real_value)
        z_imag.append(imag_value)
    if len(frequency) < 8:
        raise CvEisAnalysisError("EIS 有效数据点不足。", 422)
    return EisData(
        frequency_hz=tuple(frequency),
        z_real=tuple(z_real),
        z_imag=tuple(z_imag),
        impedance_basis="area_normalized" if density_basis else "absolute",
        impedance_unit="Ω·cm²" if density_basis else "Ω",
        instrument=instrument,
        format_name=format_name,
    )


def extract_high_frequency_rs(eis: EisData) -> dict[str, float | str]:
    points = sorted(
        zip(eis.frequency_hz, eis.z_real, eis.z_imag),
        key=lambda item: item[0],
        reverse=True,
    )
    for (f1, r1, x1), (f2, r2, x2) in zip(points, points[1:]):
        if min(f1, f2) < MIN_RS_CROSS_FREQUENCY_HZ:
            break
        if x1 == 0:
            rs = r1
            crossing_frequency = f1
        elif x2 == 0:
            rs = r2
            crossing_frequency = f2
        elif x1 * x2 < 0:
            fraction = -x1 / (x2 - x1)
            rs = r1 + fraction * (r2 - r1)
            crossing_frequency = f1 + fraction * (f2 - f1)
        else:
            continue
        if not math.isfinite(rs) or rs <= 0 or rs > 1000:
            raise CvEisAnalysisError("EIS 高频交点得到的 Rs 超出合理范围。", 422)
        return {
            "rs": rs,
            "unit": eis.impedance_unit,
            "crossing_frequency_hz": crossing_frequency,
            "method": "highest_frequency_first_zimag_zero_crossing",
        }
    raise CvEisAnalysisError("EIS 在 100 Hz 以上没有可确认的 Z''=0 高频交点。", 422)


def return_scan_indices(cv: CvData) -> tuple[int, int]:
    potential = cv.potential_v
    initial = sum(potential[: min(5, len(potential))]) / min(5, len(potential))
    minimum = min(potential)
    excursion = initial - minimum
    if excursion < 0.05:
        raise CvEisAnalysisError("CV 未形成可识别的阴极扫描转折。", 422)
    ordered = sorted(set(potential))
    typical_step = min(
        (abs(right - left) for left, right in zip(ordered, ordered[1:]) if right != left),
        default=1e-4,
    )
    tolerance = max(typical_step * 0.75, 1e-6)
    candidates = [
        index
        for index, value in enumerate(potential[:-20])
        if value <= minimum + tolerance
    ]
    if not candidates:
        raise CvEisAnalysisError("CV 回扫起点无法确认。", 422)
    start = candidates[-1]
    if len(potential) - start < 20 or potential[-1] - potential[start] < 0.05:
        raise CvEisAnalysisError("CV 回扫数据不完整。", 422)
    return start, len(potential)


def _interpolate_crossing(
    current_density_ma_cm2: list[float],
    values: list[float],
    target: float,
) -> float | None:
    for index in range(len(current_density_ma_cm2) - 1):
        left = current_density_ma_cm2[index]
        right = current_density_ma_cm2[index + 1]
        if (left - target) * (right - target) > 0:
            continue
        if left == right:
            return values[index]
        fraction = (target - left) / (right - left)
        return values[index] + fraction * (values[index + 1] - values[index])
    return None


def _downsample_indices(length: int, maximum: int = MAX_CHART_POINTS) -> list[int]:
    if length <= maximum:
        return list(range(length))
    return sorted(
        {
            round(index * (length - 1) / (maximum - 1))
            for index in range(maximum)
        }
    )


def analyze_pair(cv: CvData, eis: EisData) -> dict[str, Any]:
    if cv.instrument_ir_applied is True:
        raise CvEisAnalysisError("CV 仪器参数显示已启用 iR 补偿，禁止再次离线补偿。", 422)
    compatible = (
        cv.current_basis == "density" and eis.impedance_basis == "area_normalized"
    ) or (
        cv.current_basis == "absolute" and eis.impedance_basis == "absolute"
    )
    if not compatible:
        raise CvEisAnalysisError("CV 电流单位与 EIS 阻抗单位不兼容。", 422)
    rs = extract_high_frequency_rs(eis)
    start, end = return_scan_indices(cv)
    potential = list(cv.potential_v[start:end])
    current = list(cv.current[start:end])
    corrected = [
        e_value - IR_COMPENSATION_FRACTION * i_value * float(rs["rs"])
        for e_value, i_value in zip(potential, current)
    ]
    raw_rhe = [value + RHE_OFFSET_V for value in potential]
    corrected_rhe = [value + RHE_OFFSET_V for value in corrected]
    if cv.current_basis == "density":
        display_current = [value * 1000 for value in current]
        display_unit = "mA/cm²"
    else:
        display_current = [value * 1000 for value in current]
        display_unit = "mA"
    overpotentials = []
    if cv.current_basis == "density":
        cathodic_magnitude = [-value for value in display_current]
        for target in TARGET_CURRENT_DENSITIES_MA_CM2:
            raw_value = _interpolate_crossing(cathodic_magnitude, raw_rhe, float(target))
            corrected_value = _interpolate_crossing(
                cathodic_magnitude,
                corrected_rhe,
                float(target),
            )
            if raw_value is None or corrected_value is None:
                continue
            overpotentials.append(
                {
                    "target_ma_cm2": target,
                    "raw_eta_mv": -1000 * raw_value,
                    "ir90_eta_mv": -1000 * corrected_value,
                }
            )
    indices = _downsample_indices(len(potential))
    points = [
        {
            "index": index,
            "current": display_current[index],
            "raw_e_rhe_v": raw_rhe[index],
            "ir90_e_rhe_v": corrected_rhe[index],
        }
        for index in indices
    ]
    return {
        "rs": rs,
        "current_basis": cv.current_basis,
        "current_unit": display_unit,
        "cv_format": cv.format_name,
        "eis_format": eis.format_name,
        "instrument": cv.instrument,
        "area_cm2": cv.area_cm2,
        "return_scan_points": len(potential),
        "overpotentials": overpotentials,
        "points": points,
    }


def _path_stem(path: str) -> str:
    return PurePosixPath(path).stem.casefold().replace("_", " ").replace("-", " ")


def _stage_for(path: str, kind: str) -> str:
    stem = _path_stem(path)
    if "post" in stem:
        return "post"
    if "pre" in stem:
        return "pre"
    if (kind == "cv" and "s2cv" in stem.replace(" ", "")) or (
        kind == "eis" and "s3eis" in stem.replace(" ", "")
    ):
        return "sequence"
    if "her" in stem:
        return "her"
    return "main"


def _source_kind_and_priority(path: str) -> tuple[str, int]:
    suffix = PurePosixPath(path).suffix.casefold()
    if suffix not in {".txt", ".csv"}:
        return "", 0
    stem = _path_stem(path)
    compact = stem.replace(" ", "")
    if any(token in compact for token in ("diag", "copy", "cdl")):
        return "", 0
    if "eis" in compact:
        if "hereis" in compact:
            return "eis", 100
        if "s3eis" in compact:
            return "eis", 95
        if "post" in stem:
            return "eis", 90
        if compact == "eis":
            return "eis", 85
        return "eis", 60
    if "cv" not in compact:
        return "", 0
    if re.fullmatch(r"cv\d+", compact):
        return "", 0
    if "hercv" in compact:
        return "cv", 100
    if "s2cv" in compact:
        return "cv", 95
    if "post" in stem:
        return "cv", 90
    if "cvpre" in compact:
        return "cv", 85
    if compact == "cv":
        return "cv", 80
    return "cv", 55


def _excluded_path(path: str) -> bool:
    normalized = unicodedata.normalize("NFKC", path).casefold()
    parts = [part for part in normalized.replace("\\", "/").split("/") if part]
    return (
        any(token in normalized for token in ("rejected_data", "wrong_wiring"))
        or any(part.startswith("diag") or part.startswith("diagnostic") for part in parts)
    )


def _display_name(parent: PurePosixPath) -> str:
    leaf = parent.name
    generic = re.fullmatch(
        r"(?:main|retest|test|run|full|cv.eis)[-_ ]?[0-9a-z_ -]*",
        leaf.casefold(),
    )
    if generic and parent.parent.name:
        return f"{parent.parent.name} · {leaf}"
    return leaf or parent.as_posix()


def _pair_score(cv: SourceRecord, eis: SourceRecord) -> float:
    score = float(cv.priority + eis.priority)
    cv_stem = _path_stem(cv.repository_path).replace(" ", "")
    eis_stem = _path_stem(eis.repository_path).replace(" ", "")
    cv_signature = re.sub(r"(?:s2)?cv", "", cv_stem)
    eis_signature = re.sub(r"(?:s3)?eis", "", eis_stem)
    if cv_signature and cv_signature == eis_signature:
        score += 400
    elif cv.stage == "main" and eis.stage == "main":
        score -= 80
    if cv.stage == eis.stage:
        score += 160
    if "hercv" in cv_stem and "hereis" in eis_stem:
        score += 220
    if "s2cv" in cv_stem and "s3eis" in eis_stem:
        score += 220
    if "cvpre" in cv_stem and eis_stem == "eis":
        score += 220
    if cv.stage == "post" and eis.stage == "post":
        score += 220
    try:
        cv_time = dt.datetime.fromisoformat(cv.source_modified_utc.replace("Z", "+00:00"))
        eis_time = dt.datetime.fromisoformat(eis.source_modified_utc.replace("Z", "+00:00"))
        delta_seconds = (eis_time - cv_time).total_seconds()
        if 0 <= delta_seconds <= 2 * 3600:
            score += 80 - min(60, delta_seconds / 120)
        elif abs(delta_seconds) <= 24 * 3600:
            score += 10
        else:
            score -= min(120, abs(delta_seconds) / (24 * 3600))
    except (TypeError, ValueError):
        pass
    return score


class CvEisRepositoryAnalyzer:
    def __init__(self, database: Any):
        self.database = database
        self._lock = threading.RLock()
        self._token: tuple[int, int, int] | None = None
        self._catalog: dict[str, Any] | None = None
        self._curves: dict[str, dict[str, Any]] = {}

    def _generation_token(self) -> tuple[int, int, int]:
        with self.database.session() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*), COALESCE(MAX(sc.selection_id), 0),
                       COALESCE(SUM(ss.source_version_id), 0)
                FROM source_current sc
                JOIN source_selections ss ON ss.id = sc.selection_id
                """
            ).fetchone()
        return int(row[0]), int(row[1]), int(row[2])

    def _inventory(self) -> list[SourceRecord]:
        with self.database.session() as connection:
            rows = connection.execute(
                """
                SELECT sv.id AS source_version_id, sv.blob_id, sv.repository_path,
                       sv.size_bytes, sv.source_modified_utc, b.sha256
                FROM source_current sc
                JOIN source_selections ss ON ss.id = sc.selection_id
                JOIN source_versions sv ON sv.id = ss.source_version_id
                JOIN content_blobs b ON b.id = sv.blob_id
                ORDER BY sv.repository_path COLLATE NOCASE, sv.repository_path
                """
            ).fetchall()
        result = []
        for row in rows:
            path = str(row["repository_path"] or "").replace("\\", "/")
            kind, priority = _source_kind_and_priority(path)
            if not kind or _excluded_path(path):
                continue
            result.append(
                SourceRecord(
                    source_version_id=int(row["source_version_id"]),
                    blob_id=int(row["blob_id"]),
                    repository_path=path,
                    size_bytes=int(row["size_bytes"]),
                    sha256=str(row["sha256"]),
                    source_modified_utc=str(row["source_modified_utc"] or ""),
                    kind=kind,
                    stage=_stage_for(path, kind),
                    priority=priority,
                )
            )
        return result

    def _read_blob(self, source: SourceRecord) -> bytes:
        if source.size_bytes <= 0 or source.size_bytes > MAX_SOURCE_BYTES:
            raise CvEisAnalysisError("源文件大小超出 CV/EIS 安全读取范围。", 422)
        with self.database.session() as connection:
            with connection.blobopen(
                "content_blobs",
                "content",
                source.blob_id,
                readonly=True,
            ) as blob:
                payload = blob.read(source.size_bytes + 1)
        if len(payload) != source.size_bytes:
            raise CvEisAnalysisError("源文件长度校验失败。", 500)
        if hashlib.sha256(payload).hexdigest() != source.sha256:
            raise CvEisAnalysisError("源文件 SHA-256 校验失败。", 500)
        return payload

    def _build(self) -> None:
        inventory = self._inventory()
        groups: dict[str, dict[str, list[SourceRecord]]] = {}
        for source in inventory:
            parent = PurePosixPath(source.repository_path).parent.as_posix()
            group = groups.setdefault(parent, {"cv": [], "eis": []})
            group[source.kind].append(source)
        records: list[dict[str, Any]] = []
        curves: dict[str, dict[str, Any]] = {}
        for parent_text, group in groups.items():
            if not group["cv"]:
                continue
            parent = PurePosixPath(parent_text)
            for cv_source in sorted(
                group["cv"],
                key=lambda item: (-item.priority, item.repository_path.casefold()),
            ):
                eis_source = max(
                    group["eis"],
                    key=lambda item: _pair_score(cv_source, item),
                    default=None,
                )
                identifier_basis = (
                    f"{cv_source.source_version_id}:"
                    f"{eis_source.source_version_id if eis_source else 0}"
                )
                analysis_id = hashlib.sha256(identifier_basis.encode("ascii")).hexdigest()[:20]
                display_name = _display_name(parent)
                stage_label = {
                    "pre": "前测",
                    "post": "后测",
                    "sequence": "序列测试",
                    "her": "HER",
                    "main": "主测试",
                }.get(cv_source.stage, cv_source.stage)
                record: dict[str, Any] = {
                    "analysis_id": analysis_id,
                    "material_key": parent_text,
                    "display_name": display_name,
                    "stage": cv_source.stage,
                    "stage_label": stage_label,
                    "status": "pending",
                    "status_label": "正在检查",
                    "cv": {
                        "name": PurePosixPath(cv_source.repository_path).name,
                        "repository_path": cv_source.repository_path,
                        "sha256": cv_source.sha256,
                        "source_modified_utc": cv_source.source_modified_utc,
                    },
                    "eis": None,
                    "warnings": [],
                    "overpotentials": [],
                }
                if eis_source is None:
                    record.update(
                        status="missing_eis",
                        status_label="缺少可配对 EIS",
                    )
                    record["warnings"].append("同一材料目录没有受支持的 EIS 文本。")
                    records.append(record)
                    continue
                record["eis"] = {
                    "name": PurePosixPath(eis_source.repository_path).name,
                    "repository_path": eis_source.repository_path,
                    "sha256": eis_source.sha256,
                    "source_modified_utc": eis_source.source_modified_utc,
                    "pairing_method": "same_material_directory_stage_and_name",
                }
                try:
                    cv_data = parse_cv_text(self._read_blob(cv_source))
                    eis_data = parse_eis_text(self._read_blob(eis_source))
                    analysis = analyze_pair(cv_data, eis_data)
                except CvEisAnalysisError as exc:
                    record.update(status="invalid", status_label="无法安全计算")
                    record["warnings"].append(str(exc))
                    records.append(record)
                    continue
                density_basis = analysis["current_basis"] == "density"
                density_ready = density_basis and bool(analysis["overpotentials"])
                if density_ready:
                    status = "ready"
                    status_label = "可计算过电位"
                elif density_basis:
                    status = "range_insufficient"
                    status_label = "未覆盖目标电流"
                else:
                    status = "area_required"
                    status_label = "缺少电极面积"
                record.update(
                    status=status,
                    status_label=status_label,
                    rs=analysis["rs"],
                    current_basis=analysis["current_basis"],
                    current_unit=analysis["current_unit"],
                    area_cm2=analysis["area_cm2"],
                    instrument=analysis["instrument"],
                    cv_format=analysis["cv_format"],
                    eis_format=analysis["eis_format"],
                    return_scan_points=analysis["return_scan_points"],
                    overpotentials=analysis["overpotentials"],
                )
                if status == "area_required":
                    record["warnings"].append(
                        "CV 为绝对电流且文件未提供几何面积；曲线可 iR 校正，但不报告 mA/cm² 过电位。"
                    )
                elif status == "range_insufficient":
                    record["warnings"].append(
                        "CV 回扫未覆盖 10–500 mA/cm² 的默认目标电流，保留曲线但不报告标准过电位。"
                    )
                curves[analysis_id] = {
                    "analysis_id": analysis_id,
                    "display_name": display_name,
                    "stage_label": stage_label,
                    "current_unit": analysis["current_unit"],
                    "rs": analysis["rs"],
                    "points": analysis["points"],
                    "overpotentials": analysis["overpotentials"],
                    "rules": {
                        "scan": "last_cathodic_turn_return_branch",
                        "ir_fraction": IR_COMPENSATION_FRACTION,
                        "rhe_offset_v": RHE_OFFSET_V,
                        "rs_method": analysis["rs"]["method"],
                    },
                }
                records.append(record)
        status_order = {
            "ready": 0,
            "area_required": 1,
            "range_insufficient": 2,
            "invalid": 3,
            "missing_eis": 4,
        }
        records.sort(
            key=lambda item: (
                status_order.get(str(item.get("status")), 9),
                str(item.get("display_name") or "").casefold(),
                str(item.get("stage_label") or ""),
            )
        )
        ready = sum(item["status"] == "ready" for item in records)
        area_required = sum(item["status"] == "area_required" for item in records)
        paired = sum(item.get("eis") is not None for item in records)
        self._catalog = {
            "generated_from": "current_repository_sources",
            "rules": {
                "rhe_offset_v": RHE_OFFSET_V,
                "ir_fraction": IR_COMPENSATION_FRACTION,
                "scan": "CV 最后一次阴极转折后的回扫",
                "rs": "EIS 从最高频向低频的首个 Z''=0 交点",
            },
            "counts": {
                "recognized_cv": sum(item.kind == "cv" for item in inventory),
                "recognized_eis": sum(item.kind == "eis" for item in inventory),
                "analyses": len(records),
                "paired": paired,
                "ready": ready,
                "area_required": area_required,
                "attention": len(records) - ready,
            },
            "materials": records,
        }
        self._curves = curves

    def _refresh(self) -> None:
        token = self._generation_token()
        with self._lock:
            if self._catalog is not None and token == self._token:
                return
            self._build()
            self._token = token

    def catalog(self) -> dict[str, Any]:
        self._refresh()
        assert self._catalog is not None
        return json.loads(json.dumps(self._catalog, ensure_ascii=False))

    def curve(self, analysis_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"[a-f0-9]{20}", str(analysis_id or "")):
            raise CvEisAnalysisError("CV/EIS 分析编号无效。")
        self._refresh()
        payload = self._curves.get(str(analysis_id))
        if payload is None:
            raise CvEisAnalysisError("找不到可绘制的 CV/EIS 分析结果。", 404)
        return json.loads(json.dumps(payload, ensure_ascii=False))
