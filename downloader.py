#!/usr/bin/env python3
"""
================================================================================
⚡ PrimeX FastDL Master Downloader Interface
================================================================================
Unified wrapper routing video reels to dashreels_downloader and audiobooks
to kuku_audio_downloader with full FastDL parallel engine from PrimeXMasoom.
================================================================================
"""

import os
import sys
from typing import Any, Callable, Dict, List, Optional

from dashreels_downloader import download_all_dashreels_episodes
from kuku_audio_downloader import download_all_kuku_audio_episodes
from logger import logger


async def download_all_episodes(
    episodes: List[Dict[str, Any]],
    work_dir: str,
    is_audio: bool,
    show_title: str,
    artist_name: str,
    cover_art_path: Optional[str] = None,
    progress_cb: Optional[Callable] = None,
    selected_quality: str = "1080p",
) -> List[str]:
    """
    Downloads all episodes using PrimeXMasoom's native parallel engines.
    """
    if is_audio:
        logger.info(f"🎧 [kuku-audio] Launching audio FastDL engine for {len(episodes)} episodes: '{show_title}'")
        return await download_all_kuku_audio_episodes(
            episodes=episodes,
            out_dir=work_dir,
            title=show_title,
            release_tag=artist_name,
            cover_art_path=cover_art_path,
            status_cb=progress_cb,
        )
    else:
        logger.info(f"🎬 [kuku-video] Launching video FastDL engine for {len(episodes)} episodes @ {selected_quality}: '{show_title}'")
        return await download_all_dashreels_episodes(
            episodes=episodes,
            out_dir=work_dir,
            title=show_title,
            quality=selected_quality,
            status_cb=progress_cb,
            platform="kuku",
        )
