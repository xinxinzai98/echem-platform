#!/usr/bin/env python3
"""Package a committed source tree with per-file hashes; never include local state."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import subprocess
import tarfile
from pathlib import Path, PurePosixPath

RUNTIME_PATHS = [
    "Dockerfile", ".dockerignore", "compose.yaml", "compose.windows.yaml", "requirements.docker.txt",
    "start_stop_service.py", "echem_platform/start_stop*.py", "docker/start_stop_analysis",
    "docker/collection-config.json", "docker/lanbts-config.json", "static/start-stop*",
    "static/styles.css", "static/workbench.css", "static/icons/gear.svg",
    "scripts/create_start_stop_backup.py", "scripts/run_start_stop_backup_scheduler.py",
    "scripts/write_start_stop_backup_status.py",
]
MANIFEST_NAME = "RELEASE-MANIFEST.json"


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args])


def require_committed_runtime(repo):
    changed = git(repo, "diff", "--name-only", "HEAD", "--", *RUNTIME_PATHS).decode().splitlines()
    untracked = git(repo, "ls-files", "--others", "--exclude-standard", "--", *RUNTIME_PATHS).decode().splitlines()
    if changed or untracked:
        raise RuntimeError("Commit release runtime files first: " + ", ".join(changed + untracked))


def package(repo: Path, output: Path, version: str):
    repo = repo.resolve()
    if not re.fullmatch(r"[0-9][0-9A-Za-z.+_-]{0,63}", version):
        raise ValueError("Invalid release version")
    require_committed_runtime(repo)
    revision = git(repo, "rev-parse", "HEAD").decode().strip()
    epoch = int(git(repo, "show", "-s", "--format=%ct", revision))
    archive_path = output / f"start-stop-{version}-{revision[:12]}.tar"
    output.mkdir(parents=True, exist_ok=True)
    if archive_path.exists():
        raise FileExistsError(archive_path)
    try:
        with archive_path.open("xb") as handle:
            subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", revision], stdout=handle, check=True)
        entries = []
        with tarfile.open(archive_path, "r") as archive:
            for member in archive:
                path = PurePosixPath(member.name)
                if path.is_absolute() or ".." in path.parts or member.issym() or member.islnk():
                    raise ValueError("Unsafe source archive member")
                if not member.isfile():
                    continue
                placeholder = (member.name == "state/.gitkeep" and member.size <= 2
                               and archive.extractfile(member).read() in {b"", b"\n", b"\r\n"})
                if member.name == MANIFEST_NAME or (not placeholder and any(part in {".ssh", "private_data", "private_fixtures", "state", "output", "outputs"} for part in path.parts)):
                    raise ValueError("Private state or reserved manifest in source tree: " + member.name)
                if path.name == ".env" or path.suffix.lower() in {".sqlite3", ".msi", ".dll", ".exe", ".pem", ".key"}:
                    raise ValueError("Unexpected secret, database or vendor binary in source tree: " + member.name)
                digest = hashlib.sha256()
                with archive.extractfile(member) as source:
                    while chunk := source.read(1024 * 1024):
                        digest.update(chunk)
                entries.append({"path": member.name, "bytes": member.size, "sha256": digest.hexdigest()})
        manifest = {"schema_version": 1, "version": version, "git_commit": revision,
            "source": "git archive of the recorded commit", "files": entries}
        content = json.dumps(manifest, sort_keys=True, ensure_ascii=False, indent=2).encode()
        manifest_sha = hashlib.sha256(content).hexdigest()
        with tarfile.open(archive_path, "a") as archive:
            info = tarfile.TarInfo(MANIFEST_NAME)
            info.size, info.mtime, info.mode = len(content), epoch, 0o644
            archive.addfile(info, io.BytesIO(content))
        require_committed_runtime(repo)
        if git(repo, "rev-parse", "HEAD").decode().strip() != revision:
            raise RuntimeError("Repository commit changed during packaging")
        digest = hashlib.sha256()
        with archive_path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        return {"archive": str(archive_path.resolve()), "archive_sha256": digest.hexdigest(),
            "manifest_sha256": manifest_sha, "git_commit": revision, "version": version, "file_count": len(entries)}
    except BaseException:
        archive_path.unlink(missing_ok=True)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    print(json.dumps(package(args.repo, args.output, args.version), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
