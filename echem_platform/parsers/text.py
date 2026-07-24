from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from .models import ParsedCurve
from .registry import select_parser


def decode_bytes(data: bytes) -> tuple[str, str]:
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return data.decode("utf-16"), "utf-16"
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig"), "utf-8-sig"
    for encoding in ("utf-8", "gb18030", "cp1252"):
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace"), "utf-8-replace"


def split_fields(line: str, delimiter: str) -> list[str]:
    if delimiter == "whitespace":
        return [part.strip() for part in re.split(r"\s+", line.strip()) if part.strip()]
    return [part.strip().strip('"') for part in line.split(delimiter)]


def choose_delimiter(line: str) -> str:
    counts = {"\t": line.count("\t"), ",": line.count(","), ";": line.count(";")}
    delimiter, count = max(counts.items(), key=lambda item: item[1])
    return delimiter if count else "whitespace"


def parse_float(value: str) -> float | None:
    cleaned = (
        value.strip()
        .replace("\u2212", "-")
        .replace("−", "-")
        .replace("D+", "E+")
        .replace("D-", "E-")
        .replace("d+", "e+")
        .replace("d-", "e-")
    )
    cleaned = cleaned.strip("()[]")
    if not cleaned:
        return None
    try:
        number = float(cleaned)
    except ValueError:
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def unit_from_header(header: str) -> str:
    match = re.search(r"\(([^)]+)\)", header)
    if match:
        return match.group(1).strip()
    if "/" in header:
        return header.rsplit("/", 1)[-1].strip()
    return ""


def normalized_header(value: str) -> str:
    return re.sub(r"[\s_\-]+", "", value.lower())


def find_axis_indices(headers: list[str], technique: str = "") -> tuple[int, int]:
    normalized = [normalized_header(item) for item in headers]

    def first_matching(
        patterns: Iterable[str],
        excluded: set[int] | None = None,
    ) -> int | None:
        excluded = excluded or set()
        for index, header in enumerate(normalized):
            if index in excluded:
                continue
            if any(pattern in header for pattern in patterns):
                return index
        return None

    real_index = first_matching(("zreal", "z'", "rez", "zre"))
    imag_index = first_matching(("zimag", "z''", "imz", "zim"))
    if real_index is not None and imag_index is not None and real_index != imag_index:
        return real_index, imag_index

    frequency_index = first_matching(("frequency", "freq", "f(hz", "f/"))
    if frequency_index is not None:
        y_index = first_matching(
            ("|z|", "zmod", "modulus", "phase", "zimag", "zreal"),
            {frequency_index},
        )
        if y_index is not None:
            return frequency_index, y_index

    time_index = first_matching(("time", "t(s", "t/sec", "t/second"))
    potential_index = first_matching(("potential", "voltage", "e(v", "e/v"))
    current_index = first_matching(("current", "i(a", "i/ma", "i/ua", "i/a"))

    if time_index is not None:
        if technique in {"CP/GCD", "OCP"}:
            y_index = potential_index if potential_index is not None else current_index
        elif technique == "CA":
            y_index = current_index if current_index is not None else potential_index
        else:
            y_index = current_index if current_index is not None else potential_index
        if y_index is not None and y_index != time_index:
            return time_index, y_index
    if potential_index is not None and current_index is not None:
        return potential_index, current_index

    return (0, 1 if len(headers) > 1 else 0)


def infer_instrument(path: Path, text: str) -> str:
    return select_parser(path, text).instrument


def infer_technique(path: Path, headers: list[str], text: str) -> str:
    probe = " ".join([path.stem, *headers, text[:1000]]).lower()
    if path.suffix.lower() == ".z60" or any(
        token in probe
        for token in ("zreal", "zimag", "frequency", "freq(hz", "eis", "impedance")
    ):
        return "EIS"
    ordered = (
        ("OCP", ("ocp", "open circuit")),
        ("LSV", ("lsv", "linear sweep")),
        ("CV", ("cyclic volt", "cv_", "_cv", "cv1", "cv2")),
        ("CA", ("chronoamper", "ca_", "_ca", "it_", "_it")),
        ("CP/GCD", ("chronopot", "galstatic", "galvano", "gcd", "cp_", "_cp", "cc_")),
        ("Tafel", ("tafel",)),
    )
    for technique, tokens in ordered:
        if any(token in probe for token in tokens):
            return technique
    return "未识别"


def decimate_points(points: list[list[float]], limit: int) -> list[list[float]]:
    if len(points) <= limit:
        return points
    if limit <= 2:
        return [points[0], points[-1]]
    step = (len(points) - 1) / (limit - 1)
    indices = [round(position * step) for position in range(limit)]
    return [points[index] for index in indices]


def parse_curve(path: Path, data: bytes, max_points: int) -> ParsedCurve:
    route = select_parser(path)
    if route.mode == "metadata_only":
        return ParsedCurve(
            status="metadata_only",
            encoding="binary",
            delimiter="",
            headers=[],
            x_name="",
            x_unit="",
            y_name="",
            y_unit="",
            point_count=0,
            points=[],
            instrument=route.instrument,
            technique=infer_technique(path, [], ""),
            parser_id=route.parser_id,
        )

    text, encoding = decode_bytes(data)
    route = select_parser(path, text)
    lines = [line.replace("\x00", "").strip() for line in text.splitlines()]
    lines = [line for line in lines if line and not line.startswith(("#", "//"))]
    candidates: list[tuple[int, int, str, list[str]]] = []

    for index, line in enumerate(lines):
        delimiter = choose_delimiter(line)
        headers = split_fields(line, delimiter)
        if not (2 <= len(headers) <= 30):
            continue
        if sum(parse_float(item) is None for item in headers) < 1:
            continue
        for next_index in range(index + 1, min(index + 3, len(lines))):
            values = split_fields(lines[next_index], delimiter)
            numeric_count = sum(parse_float(item) is not None for item in values)
            if len(values) >= 2 and numeric_count >= 2:
                header_probe = " ".join(normalized_header(item) for item in headers)
                axis_terms = (
                    "potential",
                    "voltage",
                    "current",
                    "frequency",
                    "freq",
                    "zreal",
                    "zimag",
                    "time",
                    "e(v",
                    "i(a",
                    "t(s",
                )
                axis_score = sum(term in header_probe for term in axis_terms)
                distance_penalty = next_index - index - 1
                score = axis_score * 100 + min(len(headers), 10) - distance_penalty
                candidates.append((score, index, delimiter, headers))
                break

    selected = max(candidates, default=None, key=lambda item: item[0])
    instrument = route.instrument
    if not selected:
        return ParsedCurve(
            status="unparsed",
            encoding=encoding,
            delimiter="",
            headers=[],
            x_name="",
            x_unit="",
            y_name="",
            y_unit="",
            point_count=0,
            points=[],
            instrument=instrument,
            technique=infer_technique(path, [], text),
            parser_id=route.parser_id,
            error="未找到至少包含两列数值的表格。",
        )

    _, header_index, delimiter, headers = selected
    technique = infer_technique(path, headers, text)
    x_index, y_index = find_axis_indices(headers, technique)
    points: list[list[float]] = []
    for line in lines[header_index + 1 :]:
        values = split_fields(line, delimiter)
        if len(values) <= max(x_index, y_index):
            continue
        x_value = parse_float(values[x_index])
        y_value = parse_float(values[y_index])
        if x_value is not None and y_value is not None:
            points.append([x_value, y_value])

    if not points:
        status = "unparsed"
        error = "表头已识别，但没有可用的二维数值点。"
    else:
        status = "parsed"
        error = ""

    return ParsedCurve(
        status=status,
        encoding=encoding,
        delimiter="TAB" if delimiter == "\t" else delimiter,
        headers=headers,
        x_name=headers[x_index] if headers else "",
        x_unit=unit_from_header(headers[x_index]) if headers else "",
        y_name=headers[y_index] if headers else "",
        y_unit=unit_from_header(headers[y_index]) if headers else "",
        point_count=len(points),
        points=decimate_points(points, max_points),
        instrument=instrument,
        technique=technique,
        parser_id=route.parser_id,
        error=error,
    )
