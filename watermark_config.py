#!/usr/bin/env python3
"""
Central Watermark Configuration for PrimeXMasoom.

Single source of truth for all OTT platforms:
- Change percentages or episode count here, and it updates across all bots.
- Smart middle-window episode selector:
    Skips the first X%, skips the last Y%, and picks N episodes from the middle Z%.
"""

import os
import random
from typing import List

# Master toggle (default for all jobs unless overridden per user)
DEFAULT_ENABLED: bool = os.environ.get("WATERMARK_DEFAULT_ENABLED", "true").lower() in ("true", "1", "yes")

# Window skipping percentages
SKIP_START_PERCENT: float = float(os.environ.get("WATERMARK_SKIP_START_PCT", "20.0"))
SKIP_END_PERCENT: float = float(os.environ.get("WATERMARK_SKIP_END_PCT", "20.0"))

# How many episodes to watermark in the middle window
WATERMARK_EPISODES_COUNT: int = int(os.environ.get("WATERMARK_EPISODES_COUNT", "4"))

# Watermark branding text
DEFAULT_WATERMARK_TEXT: str = os.environ.get("WATERMARK_TEXT") or f"TG : @{os.environ.get('BOT_NAME', 'DramaBot').lstrip('@')}"

# Video encoding settings for watermark filter
WATERMARK_CRF: int = int(os.environ.get("WATERMARK_CRF", "20"))
WATERMARK_X264_PRESET: str = os.environ.get("WATERMARK_X264_PRESET", "superfast")
# Max output video bitrate as a multiple of the source bitrate (keeps file size ~same as original).
WATERMARK_BITRATE_CAP_RATIO: float = float(os.environ.get("WATERMARK_BITRATE_CAP_RATIO", "1.05"))

# Font styling
WATERMARK_FONT_SIZE: int = int(os.environ.get("WATERMARK_FONT_SIZE", "32"))
WATERMARK_FONT_COLOR: str = "white"
WATERMARK_BOX_COLOR: str = "black@0.45"
WATERMARK_BOX_BORDER: int = 10


def select_episodes_to_watermark(total_episodes: int, count: int = WATERMARK_EPISODES_COUNT) -> List[int]:
    """
    Given total episode count, calculates the eligible window by skipping
    SKIP_START_PERCENT from the front and SKIP_END_PERCENT from the end,
    then selects a consecutive block of `count` episodes (e.g. 11, 12, 13, 14)
    from within that middle window.
    """
    if total_episodes <= 0:
        return []

    # If total episodes is small (<= count), watermark all available
    if total_episodes <= count:
        return list(range(1, total_episodes + 1))

    # Calculate index boundaries (1-indexed)
    start_skip_count = int(total_episodes * (SKIP_START_PERCENT / 100.0))
    end_skip_count = int(total_episodes * (SKIP_END_PERCENT / 100.0))

    start_ep = max(1, start_skip_count + 1)
    end_ep = min(total_episodes, total_episodes - end_skip_count)

    # Maximum starting episode for a consecutive block of `count` episodes
    max_start = end_ep - count + 1
    if max_start < start_ep:
        # Fallback if middle window is narrower than `count`
        max_start = total_episodes - count + 1
        if max_start < 1:
            return list(range(1, total_episodes + 1))
        chosen_start = random.randint(1, max_start)
    else:
        chosen_start = random.randint(start_ep, max_start)

    return list(range(chosen_start, chosen_start + count))
