#!/usr/bin/env python3
"""
================================================================================
🎬 KuKu Downloader & Audio Engine for PrimeXMasoom
Author: PrimeX / Deepak Bajwa
Description: 
- For Video Shows: High-speed parallel MPEG-TS downloader (DashReels engine).
- For Audiobooks: High-speed parallel MP3 downloader with ID3 tags,
  Album/Show cover art embedding, and lossless concatenation for Combined Audiobooks.
================================================================================
"""

import asyncio
import inspect
import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import bot_config as cfg
import fastdl, task_stats
import progress as prog
from dashreels_downloader import (
    download_all_dashreels_episodes as download_all_kuku_episodes,
    download_episode_with_retry,
    PARALLEL_DOWNLOADS,
)

logger = logging.getLogger("primex.platforms.kuku.downloader")

AUDIO_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
MIN_VALID_AUDIO_DURATION_SEC = 0.5


def _is_valid_mp3_file(path: str) -> bool:
    """Verifies that the downloaded MP3 has valid audio frames and duration."""
    if not os.path.exists(path) or os.path.getsize(path) < 5 * 1024:
        return False
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration:stream=codec_type",
            "-of", "json", path,
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=12)
        if r.returncode != 0:
            return False
        data = json.loads(r.stdout)
        dur = float(data.get("format", {}).get("duration", 0) or 0)
        types = {s.get("codec_type") for s in data.get("streams", [])}
        return dur >= MIN_VALID_AUDIO_DURATION_SEC and ("audio" in types)
    except Exception:
        return False


async def download_single_kuku_audio_ep(
    url: str,
    out_path: str,
    title: str = "",
    artist: str = "",
    album: str = "",
    cover_art_path: Optional[str] = None,
    meter: Optional[fastdl.SpeedMeter] = None,
) -> bool:
    """
    Downloads an HLS/m3u8 audio stream into high-fidelity MP3 with ID3 tags.
    Fast path: parallel FastDL download to a local file, then encode locally.
    Fallback: original single-connection ffmpeg read + encode.
    """
    local_src = out_path + ".src.mkv"
    meter = meter or fastdl.SpeedMeter()  # always count bytes (task size on completion/stop cards)
    try:
        hdrs = {"User-Agent": AUDIO_USER_AGENT}
        if fastdl.is_hls_url(url):
            # audio_only: pure-audio masters -> best audio rung; Kuku /video-trailer/ slide videos ->
            # smallest rung that still carries full-quality audio (no 1080p video download).
            await fastdl.download_hls(url, local_src, "best", hdrs, meter, fastdl.MULTI_SEG_WORKERS, audio_only=True)
        else:
            await fastdl.download_direct(url, local_src, hdrs, meter, fastdl.MULTI_SEG_WORKERS)
        if await _encode_mp3(local_src, out_path, title, artist, album, cover_art_path):
            return True
        logger.warning(f"Local MP3 encode failed for {title}, falling back to direct ffmpeg")
    except Exception as e:
        logger.warning(f"FastDL audio download failed for {title}: {e!r} -> direct ffmpeg")
    finally:
        try:
            if os.path.exists(local_src):
                os.remove(local_src)
        except OSError:
            pass
    return await _encode_mp3(url, out_path, title, artist, album, cover_art_path, remote=True)


async def _encode_mp3(
    src: str,
    out_path: str,
    title: str = "",
    artist: str = "",
    album: str = "",
    cover_art_path: Optional[str] = None,
    remote: bool = False,
) -> bool:
    cmd = ["ffmpeg", "-y"]
    if remote:
        cmd += ["-headers", f"User-Agent: {AUDIO_USER_AGENT}\r\n", "-rw_timeout", "20000000"]
    cmd += ["-i", src]

    if cover_art_path and os.path.exists(cover_art_path):
        cmd.extend([
            "-i", cover_art_path,
            "-map", "0:a:0",
            "-map", "1:v:0",
            "-c:a", "libmp3lame", "-q:a", "0", "-ar", "48000", "-ac", "2",  # uniform -> clean audiobook joins
            "-c:v", "copy",
            "-id3v2_version", "3",
            "-metadata:s:v", 'title="Album cover"',
            "-metadata:s:v", 'comment="Cover (front)"',
        ])
    else:
        cmd.extend([
            "-map", "0:a:0",
            "-vn",
            "-c:a", "libmp3lame", "-q:a", "0", "-ar", "48000", "-ac", "2",  # uniform -> clean audiobook joins
            "-id3v2_version", "3",
        ])

    if title:
        cmd.extend(["-metadata", f"title={title}"])
    if artist:
        cmd.extend(["-metadata", f"artist={artist}"])
    if album:
        cmd.extend(["-metadata", f"album={album}"])

    cmd.append(out_path)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await proc.communicate()
        if proc.returncode == 0 and await fastdl.ffprobe_ok(out_path, need_audio=True, min_dur=MIN_VALID_AUDIO_DURATION_SEC):
            return True
        err_msg = stderr.decode(errors="replace")[-400:].strip() if stderr else ""
        logger.warning(f"FFmpeg audio {'download' if remote else 'encode'} returned {proc.returncode}: {err_msg}")
    except Exception as e:
        logger.error(f"Error encoding audio episode {title}: {e}")

    return False


async def download_kuku_audio_episode_with_retry(
    ep: Dict[str, Any],
    out_dir: str,
    show_title: str,
    release_tag: str,
    cover_art_path: Optional[str] = None,
    retries: int = 3,
    out_filename: Optional[str] = None,
    meter: Optional[fastdl.SpeedMeter] = None,
) -> Optional[str]:
    """Downloads a single audio episode with retry logic."""
    ep_num = ep.get("episode_number") or ep.get("index") or 1
    ep_name = ep.get("name") or f"{show_title} Episode {ep_num}"
    url = ep.get("stream_url") or ep.get("master_stream_url") or ep.get("video_url")

    # If stream_qualities dict present, pick audio_hls or first available
    if not url and ep.get("stream_qualities"):
        sq = ep["stream_qualities"]
        url = sq.get("audio_hls") or next(iter(sq.values()), None)

    if not url:
        logger.error(f"Episode {ep_num} has no audio stream URL")
        return None

    if out_filename:
        clean_name = "".join(c for c in out_filename if c.isalnum() or c in (" ", "_", "-", ".", "[", "]", "(", ")")).strip()
    else:
        clean_name = f"{show_title} - Ep {ep_num:02d} - {release_tag}.mp3"
        clean_name = "".join(c for c in clean_name if c.isalnum() or c in (" ", "_", "-", ".", "(", ")")).strip()
    out_path = os.path.join(out_dir, clean_name)

    if await fastdl.ffprobe_ok(out_path, need_audio=True, min_dur=MIN_VALID_AUDIO_DURATION_SEC):
        return out_path

    for attempt in range(1, retries + 1):
        ok = await download_single_kuku_audio_ep(
            url=url,
            out_path=out_path,
            title=f"{show_title} - Ep {ep_num:02d}",
            artist=release_tag,
            album=show_title,
            cover_art_path=cover_art_path,
            meter=meter,
        )
        if ok:
            return out_path
        logger.warning(f"Audio Ep {ep_num} attempt {attempt} failed, retrying...")
        await asyncio.sleep(1.5 * attempt)

    if os.path.exists(out_path):
        try:
            os.remove(out_path)
        except Exception:
            pass
    return None


async def download_all_kuku_audio_episodes(
    episodes: List[Dict[str, Any]],
    out_dir: str,
    title: str = "Audiobook",
    release_tag: str = "PrimeXOfficial",
    cover_art_path: Optional[str] = None,
    status_cb: Optional[Callable] = None,
) -> List[str]:
    """Downloads all requested audio episodes in parallel with live speed/ETA."""
    import messages

    os.makedirs(out_dir, exist_ok=True)
    total = len(episodes)
    task_stats.set_phase("Download")
    sem = asyncio.Semaphore(PARALLEL_DOWNLOADS)
    meter = fastdl.SpeedMeter()
    state = {"done": 0, "ok": 0}
    results: List[Optional[str]] = [None] * total

    def _render():
        spd = meter.rate_mb_s()
        done = state["done"]
        pct = (done / total * 100.0) if total else 0.0
        bar_len = 12
        filled = int(round((pct / 100.0) * bar_len))
        bar_str = "█" * filled + "░" * (bar_len - filled)
        return (
            f"⚡️ **Status:** Downloading Episodes ({done}/{total} @ 48kHz)...\n\n"
            f"[{bar_str}] {pct:.1f}%\n"
            f"🚀 `{spd:.2f} MB/s`  •  `{done}/{total}` Downloaded\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )

    reporter = fastdl.ProgressReporter(status_cb, _render, interval=5.0).start()

    async def _worker(idx: int, ep: Dict[str, Any]):
        async with sem:
            path = await download_kuku_audio_episode_with_retry(
                ep=ep,
                out_dir=out_dir,
                show_title=title,
                release_tag=release_tag,
                cover_art_path=cover_art_path,
                meter=meter,
            )
            state["done"] += 1
            results[idx] = path
            if path and os.path.exists(path):
                state["ok"] += 1

    try:
        await asyncio.gather(*[_worker(i, ep) for i, ep in enumerate(episodes)])
    finally:
        await reporter.stop(final=bool(status_cb))
    logger.info(f"📊 [kuku-audio] {title}: {state['ok']}/{total} eps, {meter.total / 1048576:.1f} MB (avg {meter.avg_mb_s():.1f} MB/s)")
    return [r for r in results if r and os.path.exists(r)]


def get_audio_duration(path: str) -> float:
    """Returns duration in seconds using ffprobe."""
    if not os.path.exists(path):
        return 0.0
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "csv=p=0", path
        ]
        out = subprocess.check_output(cmd, text=True, timeout=10).strip()
        return float(out) if out else 0.0
    except Exception:
        return 0.0


async def merge_kuku_audio_episodes(
    ep_files: List[str],
    out_path: str,
    title: str = "Audiobook",
    artist: Optional[str] = None,
    album: str = "Audiobook",
    cover_art_path: Optional[str] = None,
    inject_watermark: bool = True,
) -> Tuple[Optional[str], Optional[str]]:
    """
    Concatenates multiple MP3 episodes into a single Master Audiobook MP3
    using FFmpeg concat demuxer with embedded Cover Art & ID3 metadata.
    Injects audio watermark at ~40% episode transition and returns (out_path, wm_injected_time).
    """
    if not ep_files:
        return None, None
    if len(ep_files) == 1 and not inject_watermark:
        return ep_files[0], None

    out_dir = os.path.dirname(out_path)
    concat_txt = os.path.join(out_dir, "audio_concat_list.txt")

    # Watermark injection
    wm_injected_time = None
    concat_list = list(ep_files)

    if inject_watermark and getattr(cfg, "ENABLE_WATERMARK", True):
        # Look for normalized 48k watermark first, then standard watermark
        wm_candidates = [
            os.path.join(os.path.dirname(__file__), "assets", "audio_watermark_48k.mp3"),
            getattr(cfg, "AUDIO_WATERMARK_PATH", ""),
            os.path.join(os.path.dirname(__file__), "assets", "audio_watermark.mp3"),
        ]
        wm_file = next((f for f in wm_candidates if f and os.path.exists(f)), None)
        if wm_file:
            # Place at ~40% point
            wm_idx = max(1, int(len(ep_files) * 0.40)) if len(ep_files) > 1 else 1
            # Calculate duration up to insertion point
            acc_sec = sum(get_audio_duration(f) for f in ep_files[:wm_idx])
            hh = int(acc_sec // 3600)
            mm = int((acc_sec % 3600) // 60)
            ss = int(acc_sec % 60)
            wm_injected_time = f"[{hh:02d}:{mm:02d}:{ss:02d}]"
            concat_list = list(ep_files[:wm_idx]) + [wm_file] + list(ep_files[wm_idx:])

    with open(concat_txt, "w", encoding="utf-8") as f:
        for p in concat_list:
            f.write(f"file '{os.path.abspath(p)}'\n")

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", concat_txt,
    ]

    if cover_art_path and os.path.exists(cover_art_path):
        cmd.extend([
            "-i", cover_art_path,
            "-map", "0:a:0", "-map", "1:v:0",
            "-c:a", "copy",
            "-c:v", "copy",
            "-id3v2_version", "3",
            "-metadata:s:v", 'title="Album cover"',
            "-metadata:s:v", 'comment="Cover (front)"',
        ])
    else:
        cmd.extend([
            "-map", "0:a:0",
            "-c:a", "copy",
            "-id3v2_version", "3",
        ])

    cmd.extend(["-metadata", f"title={title}"])
    if artist:
        cmd.extend(["-metadata", f"artist={artist}"])
    if album:
        cmd.extend(["-metadata", f"album={album}"])
    cmd.append(out_path)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await proc.communicate()
        if os.path.exists(concat_txt):
            try:
                os.remove(concat_txt)
            except Exception:
                pass

        if proc.returncode == 0 and _is_valid_mp3_file(out_path):
            return out_path, wm_injected_time
        err_msg = stderr.decode(errors="replace")[-400:].strip() if stderr else ""
        logger.error(f"FFmpeg audio merge failed ({proc.returncode}): {err_msg}")
    except Exception as e:
        logger.error(f"Error merging audio files into {out_path}: {e}")

    return None, None


__all__ = [
    "download_all_kuku_episodes",
    "download_episode_with_retry",
    "download_all_kuku_audio_episodes",
    "download_kuku_audio_episode_with_retry",
    "merge_kuku_audio_episodes",
    "get_audio_duration",
    "PARALLEL_DOWNLOADS",
]
