from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ParsedCurve:
    status: str
    encoding: str
    delimiter: str
    headers: list[str]
    x_name: str
    x_unit: str
    y_name: str
    y_unit: str
    point_count: int
    points: list[list[float]]
    instrument: str
    technique: str
    parser_id: str
    error: str = ""
