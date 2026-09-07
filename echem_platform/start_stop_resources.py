"""Measured output estimates and conservative per-process scratch reservations."""
from __future__ import annotations

import contextlib
import re
import shutil
import threading
from pathlib import Path

TASK_MARGIN_BYTES = 128 * 1024 * 1024


class ScratchSpaceError(RuntimeError):
    def __init__(self, required: int, available: int):
        self.required_bytes = required
        self.available_bytes = max(0, available)
        super().__init__(
            f"临时分析空间不足：需要 {required / 1024**3:.2f} GiB，当前可用 {max(0, available) / 1024**3:.2f} GiB。"
        )


def estimate_output_bytes(*, baseline_output: int, baseline_source: int,
                          current_source: int, minimum: int) -> int:
    """Use the last actual generation, input growth, and a 20% output margin."""
    baseline_output = max(0, int(baseline_output))
    baseline_source = max(0, int(baseline_source))
    current_source = max(0, int(current_source))
    predicted = baseline_output or current_source
    if baseline_output and baseline_source and current_source > baseline_source:
        predicted = (baseline_output * current_source + baseline_source - 1) // baseline_source
    return max(max(0, int(minimum)), (predicted * 12 + 9) // 10)


def obsolete_snapshot_cache(root: Path, current_fingerprint: str, *, remove: bool = False) -> int:
    """Count or evict only obsolete hash-named derivative snapshot directories."""
    if not root.exists():
        return 0
    if root.is_symlink() or not root.is_dir():
        raise ValueError("快照缓存目录无效")
    total = 0
    for candidate in root.iterdir():
        if candidate.name == current_fingerprint or not re.fullmatch(r"[a-fA-F0-9]{64}", candidate.name):
            continue
        if candidate.is_symlink():
            raise ValueError("快照缓存不能包含符号链接")
        if not candidate.is_dir():
            continue
        for item in candidate.rglob("*"):
            if item.is_symlink():
                raise ValueError("快照缓存不能包含符号链接")
            if item.is_file():
                total += item.stat().st_size
        if remove:
            shutil.rmtree(candidate)
    return total


class ScratchBudget:
    """Reservations prevent another task from consuming future output capacity.

    The budget intentionally keeps each reservation until task cleanup. Actual
    disk usage is checked as well, so temporary over-reservation is conservative.
    """
    def __init__(self, root: Path):
        self.root = root
        self._lock = threading.RLock()
        self._reservations: dict[str, int] = {}

    def available(self) -> int:
        with self._lock:
            return max(0, shutil.disk_usage(self.root).free - sum(self._reservations.values()))

    def require_unreserved(self, required: int) -> None:
        available = self.available()
        if required > available:
            raise ScratchSpaceError(required, available)

    def resize(self, key: str, required: int) -> None:
        with self._lock:
            previous = self._reservations[key]
            available = self.available() + previous
            if required > available:
                raise ScratchSpaceError(required, available)
            self._reservations[key] = max(0, int(required))

    @contextlib.contextmanager
    def reserve(self, key: str, required: int):
        required = max(0, int(required))
        with self._lock:
            if key in self._reservations:
                raise ValueError("Duplicate scratch reservation")
            self.require_unreserved(required)
            self._reservations[key] = required
        try:
            yield
        finally:
            with self._lock:
                self._reservations.pop(key, None)
