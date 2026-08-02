#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def app_version() -> str:
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    match = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', source, flags=re.MULTILINE)
    if not match:
        raise RuntimeError("APP_VERSION was not found in app.py")
    return match.group(1)


def sha256(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a fixed source ZIP from committed HEAD.")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    version = app_version()
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise RuntimeError(f"Refusing to build a non-release version: {version}")

    subprocess.run([sys.executable, "scripts/check_public_tree.py"], cwd=ROOT, check=True)
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout
    if status.strip():
        raise RuntimeError("Refusing to build from a dirty worktree. Commit and verify first.")

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / f"echem-platform-v{version}.zip"
    checksum = output_dir / f"{archive.name}.sha256"
    if archive.exists() or checksum.exists():
        raise RuntimeError("Release output already exists; use a new empty output directory.")

    subprocess.run(
        [
            "git",
            "archive",
            "--format=zip",
            f"--prefix=echem-platform-v{version}/",
            f"--output={archive}",
            "HEAD",
        ],
        cwd=ROOT,
        check=True,
    )
    digest = sha256(archive)
    checksum.write_text(f"{digest}  {archive.name}\n", encoding="ascii")
    print(f"Created {archive}")
    print(f"SHA-256 {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
