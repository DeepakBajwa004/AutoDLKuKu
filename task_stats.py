#!/usr/bin/env python3
"""
Per-task statistics (size, files, time) for the final "Task Completed / Download Stopped" cards.

Uses a ContextVar so every coroutine/task spawned inside one user job (downloads, uploads)
reports into that job's stats object without threading parameters through every provider.
"""

import contextvars
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


@dataclass
class TaskStats:
    started_at: float = field(default_factory=time.time)
    downloaded_bytes: int = 0
    uploaded: List[Tuple[str, int]] = field(default_factory=list)  # (file_name, size_bytes)
    user_id: int = 0
    first_name: str = ""
    full_name: str = ""
    username: str = ""
    upload_dest: str = "tg"
    phase: str = "Download"   # Download | Merging | Watermark | Uploading

    @property
    def uploaded_bytes(self) -> int:
        return sum(sz for _, sz in self.uploaded)

    @property
    def elapsed(self) -> float:
        return max(0.0, time.time() - self.started_at)


_current: contextvars.ContextVar[Optional[TaskStats]] = contextvars.ContextVar("primex_task_stats", default=None)


def start_task(user_id: int = 0, first_name: str = "", full_name: str = "", username: str = "",
               upload_dest: str = "tg") -> TaskStats:
    stats = TaskStats(user_id=user_id, first_name=first_name, full_name=full_name or first_name,
                      username=username, upload_dest=upload_dest or "tg")
    _current.set(stats)
    return stats


def set_phase(phase: str) -> None:
    s = _current.get()
    if s is not None and phase:
        s.phase = phase


def current() -> Optional[TaskStats]:
    return _current.get()


def record_download(n: int) -> None:
    s = _current.get()
    if s is not None and n > 0:
        s.downloaded_bytes += n


def record_upload(file_name: str, size_bytes: int) -> None:
    s = _current.get()
    if s is not None:
        s.uploaded.append((file_name, int(size_bytes or 0)))


def human_size(n: int) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.2f}{unit}" if unit in ("GB", "TB") else f"{n:.1f}{unit}" if unit == "MB" else f"{int(n)}{unit}"
        n /= 1024
    return f"{n:.2f}TB"


def human_time(sec: float) -> str:
    sec = int(max(0, sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m}m{s}s"
    if m:
        return f"{m}m{s}s"
    return f"{s}s"
