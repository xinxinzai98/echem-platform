#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
import urllib.parse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")


def markdown_files() -> list[Path]:
    return sorted(
        path
        for path in ROOT.rglob("*.md")
        if ".git" not in path.parts and "output" not in path.parts
    )


def main() -> int:
    failures: list[str] = []
    checked = 0
    for document in markdown_files():
        text = document.read_text(encoding="utf-8")
        for raw_target in MARKDOWN_LINK.findall(text):
            target = raw_target.strip().strip("<>")
            parsed = urllib.parse.urlsplit(target)
            if parsed.scheme or target.startswith("#"):
                continue
            relative_path = urllib.parse.unquote(parsed.path)
            if not relative_path:
                continue
            checked += 1
            resolved = (document.parent / relative_path).resolve()
            try:
                resolved.relative_to(ROOT)
            except ValueError:
                failures.append(
                    f"{document.relative_to(ROOT)}: link escapes repository: {target}"
                )
                continue
            if not resolved.exists():
                failures.append(
                    f"{document.relative_to(ROOT)}: missing link target: {target}"
                )

    if failures:
        print("Documentation-link check failed:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1

    print(f"Documentation-link check passed for {checked} local links.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
