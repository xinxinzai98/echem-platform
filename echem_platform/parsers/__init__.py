from .models import ParsedCurve, ParsedTable
from .registry import ParserRoute, select_parser
from .text import (
    choose_delimiter,
    decimate_points,
    decode_bytes,
    find_axis_indices,
    infer_instrument,
    infer_technique,
    normalized_header,
    parse_curve,
    parse_float,
    parse_numeric_table,
    split_fields,
    unit_from_header,
)

PARSER_VERSION = "2026.07.26.4"

__all__ = [
    "ParsedCurve",
    "ParsedTable",
    "ParserRoute",
    "PARSER_VERSION",
    "choose_delimiter",
    "decimate_points",
    "decode_bytes",
    "find_axis_indices",
    "infer_instrument",
    "infer_technique",
    "normalized_header",
    "parse_curve",
    "parse_float",
    "parse_numeric_table",
    "select_parser",
    "split_fields",
    "unit_from_header",
]
