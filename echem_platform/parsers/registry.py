from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ParserRoute:
    parser_id: str
    instrument: str
    mode: str


CHI_BINARY = ParserRoute("chi.binary.metadata", "CHI", "metadata_only")
CHI_TEXT = ParserRoute("chi.delimited_text", "CHI", "text")
CORRTEST_EIS = ParserRoute("corrtest.z60_text", "CorrTest", "text")
CORRTEST_TEXT = ParserRoute("corrtest.delimited_text", "CorrTest", "text")
GENERIC_TEXT = ParserRoute("generic.delimited_text", "未知", "text")


def select_parser(path: Path, text: str = "") -> ParserRoute:
    """Select a deterministic parser route without invoking an instrument interface."""
    suffix = path.suffix.lower()
    probe = f"{path.name}\n{text[:3000]}".lower()
    if suffix == ".bin":
        return CHI_BINARY
    if suffix == ".z60":
        return CORRTEST_EIS
    if suffix == ".cor" or "csstudiofile" in probe or "corrtest" in probe:
        return CORRTEST_TEXT
    if "chi instrument" in probe or re.search(r"\bchi\d", probe):
        return CHI_TEXT
    return GENERIC_TEXT
