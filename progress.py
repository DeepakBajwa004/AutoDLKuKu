#!/usr/bin/env python3
"""
Progress and Throttle Utilities for PrimeXMasoom.
Provides clean terminal & Telegram edit throttlers to avoid rate-limiting.
"""

import time
from typing import Optional


class Throttle:
    """Ensures callbacks are called at most once every `interval` seconds."""

    def __init__(self, interval: float = 4.0, **kwargs):
        self.interval = float(kwargs.get("interval_sec", interval))
        self.last_time = 0.0

    def ready(self, force: bool = False) -> bool:
        now = time.time()
        if force or (now - self.last_time >= self.interval):
            self.last_time = now
            return True
        return False


ProgressThrottle = Throttle


def human_mb(bytes_val: int) -> str:
    """Formats bytes to MB string (e.g. '142.5 MB')."""
    return f"{bytes_val / (1024 * 1024):.1f} MB"


def human_size(bytes_val: int) -> str:
    """Formats bytes to human-readable size (MB or GB)."""
    if bytes_val >= 1024**3:
        return f"{bytes_val / (1024**3):.2f} GB"
    return f"{bytes_val / (1024**2):.1f} MB"


def speed_mb_s(bytes_done: int, elapsed_sec: float) -> float:
    """Calculates transfer speed in MB/s."""
    if elapsed_sec <= 0.05:
        return 0.0
    return (bytes_done / (1024 * 1024)) / elapsed_sec


def progress_bar(current: int, total: int = 100, width: int = 12) -> str:
    """Renders a clean unicode progress bar: [██████░░░░░░] 50%"""
    if total <= 0:
        return f"[{'░' * width}] 0%"
    ratio = min(1.0, max(0.0, current / total))
    filled = int(round(ratio * width))
    bar = "█" * filled + "░" * (width - filled)
    pct = int(ratio * 100)
    return f"[{bar}] {pct}%"


def format_eta(remaining_seconds: float) -> str:
    """Formats seconds into clean ETA string (e.g. '45s', '2m 10s')."""
    if remaining_seconds <= 0 or remaining_seconds > 86400:
        return "--"
    m, s = divmod(int(remaining_seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}h {m}m"
    if m > 0:
        return f"{m}m {s}s"
    return f"{s}s"
