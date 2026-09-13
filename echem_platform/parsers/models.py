from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ParsedTable:
    """A complete numeric table extracted from an instrument export.

    ``rows`` deliberately keeps one cell per source header.  Non-numeric cells
    are represented by ``None``; ``numeric_indices`` identifies columns that
    contain at least one finite numeric value.  Unlike ``ParsedCurve``, this
    representation is never decimated and is therefore suitable for
    calculations that must use the complete source table.
    """

    status: str
    encoding: str
    delimiter: str
    headers: list[str]
    units: list[str]
    numeric_indices: list[int]
    row_count: int
    rows: list[list[float | None]]
    instrument: str
    technique: str
    parser_id: str
    error: str = ""


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
