from .models import ParsedCurve
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
    split_fields,
    unit_from_header,
)

PARSER_VERSION = "2026.07.24.2"

__all__ = [
    "ParsedCurve",
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
    "select_parser",
    "split_fields",
    "unit_from_header",
]
