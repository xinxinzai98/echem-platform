#!/usr/bin/env python3
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAX_FILE_BYTES = 5 * 1024 * 1024
BLOCKED_SUFFIXES = {
    ".7z",
    ".bin",
    ".bz2",
    ".dmg",
    ".dll",
    ".exe",
    ".gz",
    ".key",
    ".msi",
    ".p12",
    ".pdb",
    ".pem",
    ".pfx",
    ".pkg",
    ".rar",
    ".tar",
    ".tgz",
    ".xz",
    ".zip",
}
BLOCKED_PARTS = {
    "diagnostics",
    "inventory",
    "logs",
    "packages",
    "private_data",
    "private_fixtures",
    "release-artifacts",
    "remote_tools",
    "runtime",
    "windows测试数据",
}
ALLOWED_BINARY_PATHS = {"demo_data/chi_ocpt_demo.bin"}
ALLOWED_IMAGE_SUFFIXES = {".gif", ".ico", ".jpeg", ".jpg", ".png", ".webp"}
PRIVATE_IPV4 = re.compile(
    r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b"
)
FALLBACK_SKIP_PARTS = {
    ".git",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "dist",
    "logs",
    "output",
    "packages",
    "release-artifacts",
    "runtime",
    "venv",
}


def candidate_paths() -> list[str]:
    try:
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=ROOT,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return sorted(
            path.relative_to(ROOT).as_posix()
            for path in ROOT.rglob("*")
            if (path.is_file() or path.is_symlink())
            and not any(part in FALLBACK_SKIP_PARTS for part in path.relative_to(ROOT).parts)
        )
    candidates = (item.decode("utf-8") for item in result.stdout.split(b"\0") if item)
    return sorted(
        item
        for item in candidates
        if (ROOT / item).exists() or (ROOT / item).is_symlink()
    )


def is_documentation_image(relative: Path) -> bool:
    return (
        len(relative.parts) >= 3
        and relative.parts[0] == "docs"
        and relative.parts[1] == "images"
        and relative.suffix.lower() in ALLOWED_IMAGE_SUFFIXES
    )


def scan_path(relative_text: str) -> list[str]:
    problems: list[str] = []
    relative = Path(relative_text)
    path = ROOT / relative
    lowered_parts = {part.casefold() for part in relative.parts}
    lowered_path = relative_text.casefold()

    if relative.name == ".DS_Store":
        problems.append("macOS metadata file")
    if any(part in lowered_parts for part in BLOCKED_PARTS) or "windows测试数据_" in lowered_path:
        problems.append("blocked private, diagnostic, runtime, or instrument-export path")

    suffix = relative.suffix.lower()
    if suffix in BLOCKED_SUFFIXES and relative_text not in ALLOWED_BINARY_PATHS:
        problems.append(f"blocked binary or credential suffix {suffix}")

    if path.is_symlink():
        problems.append("symbolic links are not allowed in the public release tree")
        return problems
    if not path.is_file():
        problems.append("not a regular file")
        return problems

    size = path.stat().st_size
    if size > MAX_FILE_BYTES:
        problems.append(f"file is larger than {MAX_FILE_BYTES} bytes")
        return problems

    data = path.read_bytes()
    if b"\0" in data and not is_documentation_image(relative):
        problems.append("unexpected binary content")

    if is_documentation_image(relative):
        return problems

    text = data.decode("utf-8", errors="ignore")
    private_addresses = sorted(set(PRIVATE_IPV4.findall(text)))
    if private_addresses:
        problems.append("private network address found: " + ", ".join(private_addresses))

    secret_prefixes = (
        "gh" + "o_",
        "gh" + "p_",
        "gh" + "s_",
        "github_" + "pat_",
        "sk-" + "proj-",
    )
    for prefix in secret_prefixes:
        if prefix in text:
            problems.append(f"possible secret prefix {prefix[:3]}...")

    private_key_markers = (
        "BEGIN " + "OPENSSH PRIVATE KEY",
        "BEGIN " + "RSA PRIVATE KEY",
        "BEGIN " + "EC PRIVATE KEY",
        "BEGIN " + "PRIVATE KEY",
    )
    if any(marker in text for marker in private_key_markers):
        problems.append("private-key marker found")

    return problems


def main() -> int:
    failures: list[tuple[str, list[str]]] = []
    paths = candidate_paths()
    for relative in paths:
        problems = scan_path(relative)
        if problems:
            failures.append((relative, problems))

    if failures:
        print("Public-tree check failed:", file=sys.stderr)
        for relative, problems in failures:
            for problem in problems:
                print(f"- {relative}: {problem}", file=sys.stderr)
        return 1

    print(f"Public-tree check passed for {len(paths)} files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
