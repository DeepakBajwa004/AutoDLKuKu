#!/usr/bin/env python3
"""
Shared Episode Downloader for PrimeXMasoom.
Used by DashReels, StoryTV, Alright, QuickTV, DramaStory, RocketReels, KuKu (video),
SonyLIV and ZEE5 (all Lipat cloud platforms).

Engine order (per episode):
  HLS (.m3u8):  FastDL native parallel segments  ->  yt-dlp (16 frags)  ->  ffmpeg
  Direct file:  FastDL parallel HTTP ranges     ->  yt-dlp             ->  ffmpeg

* One shared connection pool for the whole bot (no CDN flooding, no CPU storms).
* Stall-based timeouts instead of whole-file timeouts.
* Real sliding-window speed + correct ETA, reported from a background task so
  Telegram edits never slow the download.
* Every fallback logs WHY the previous engine failed.
"""

import asyncio
import inspect
import json
import logging
import os
import re
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

import fastdl, task_stats
import progress as prog
import messages

logger = logging.getLogger("primex.dashreels.downloader")

PARALLEL_DOWNLOADS = int(os.environ.get("PARALLEL_EPISODE_DOWNLOADS", "16") or 16)
RETRIES_PER_EPISODE = 3
USER_AGENT = "Dart/3.5 (dart:io)"
MIN_VALID_DURATION_SEC = 0.5
YTDLP_STALL_SEC = 120  # kill yt-dlp only if it makes no progress for this long


# ─────────────────────────────────────────────────────────────────────────────
# Validation
# ─────────────────────────────────────────────────────────────────────────────
def _is_valid_ts_file(path: str) -> bool:
    """Sync validator (kept for backwards compatibility; prefer _is_valid_async)."""
    if not os.path.exists(path) or os.path.getsize(path) < 10 * 1024:
        return False
    try:
        cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type", "-of", "json", path]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=12)
        if r.returncode != 0:
            return os.path.getsize(path) > 30 * 1024
        data = json.loads(r.stdout)
        dur = float(data.get("format", {}).get("duration", 0) or 0)
        types = {s.get("codec_type") for s in data.get("streams", [])}
        return dur >= MIN_VALID_DURATION_SEC and ("video" in types or "audio" in types)
    except Exception:
        return os.path.exists(path) and os.path.getsize(path) > 30 * 1024


async def _is_valid_async(path: str) -> bool:
    return await fastdl.ffprobe_ok(path, min_dur=MIN_VALID_DURATION_SEC)


def _headers_for(url: str) -> Dict[str, str]:
    h = {"User-Agent": USER_AGENT}
    low = (url or "").lower()
    if "videostech" in low or "alright" in low:
        h["Referer"] = "https://alright.in/"
    return h


# ─────────────────────────────────────────────────────────────────────────────
# Fallback 1: yt-dlp (progress fed into the shared meter, stderr captured)
# ─────────────────────────────────────────────────────────────────────────────
def _ytdlp_format(quality: str) -> str:
    q_str = str(quality or "1080p").lower().strip()
    try:
        q_val = int(re.sub(r"[^\d]", "", q_str) or "1080")
    except ValueError:
        q_val = 1080
    if q_val >= 1080 or "org" in q_str or "best" in q_str:
        return "bestvideo+bestaudio/best"
    return (
        f"bestvideo[height<={q_val}][width>{q_val}]+bestaudio/"
        f"bestvideo[width<={q_val}][height>{q_val}]+bestaudio/"
        f"bestvideo[height<={q_val}]+bestaudio/"
        f"bestvideo[width<={q_val}]+bestaudio/"
        f"best[height<={q_val}][width>{q_val}]/"
        f"best[width<={q_val}][height>{q_val}]/"
        f"best[height<={q_val}]/"
        f"best[width<={q_val}]/"
        "best"
    )


async def _download_with_ytdlp(
    url: str,
    out_path: str,
    quality: str = "720p",
    meter: Optional[fastdl.SpeedMeter] = None,
    on_fraction: Optional[Callable[[float], None]] = None,
) -> bool:
    ytdl_bin = shutil.which("yt-dlp")
    if not ytdl_bin:
        return False
    container_ext = "mkv" if out_path.endswith(".mkv") else "mp4"
    cmd = [
        ytdl_bin, "-f", _ytdlp_format(quality),
        "--merge-output-format", container_ext,
        "-N", "16",
        "--no-playlist", "--no-part",
        "--retries", "10", "--fragment-retries", "10",
        "--socket-timeout", "25",
        "--no-warnings", "--newline",
        "--progress-template", "download:PRG %(progress.downloaded_bytes)s %(progress.total_bytes_estimate)s",
    ]
    for k, v in _headers_for(url).items():
        cmd += ["--add-header", f"{k}: {v}"]
    cmd += ["-o", out_path, url]

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    err_tail: List[str] = []

    async def _drain_err():
        while True:
            line = await proc.stderr.readline()
            if not line:
                return
            t = line.decode("utf-8", "ignore").strip()
            if t:
                err_tail.append(t)
                del err_tail[:-8]

    err_task = asyncio.create_task(_drain_err())
    last_bytes = 0
    last_progress_t = time.monotonic()
    try:
        while True:
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=YTDLP_STALL_SEC)
            except asyncio.TimeoutError:
                if time.monotonic() - last_progress_t >= YTDLP_STALL_SEC:
                    logger.warning(f"yt-dlp stalled {YTDLP_STALL_SEC}s, killing: {out_path}")
                    proc.kill()
                    break
                continue
            if not line:
                break
            t = line.decode("utf-8", "ignore").strip()
            if not t.startswith("PRG "):
                continue
            parts = t.split()
            try:
                cur = int(float(parts[1]))
            except (ValueError, IndexError):
                continue
            # yt-dlp restarts the counter for each format (video, then audio)
            delta = cur - last_bytes if cur >= last_bytes else cur
            last_bytes = cur
            if delta > 0:
                last_progress_t = time.monotonic()
                if meter:
                    meter.add(delta)
            try:
                tot = float(parts[2])
                if on_fraction and tot > 0:
                    on_fraction(min(0.99, cur / tot))
            except (ValueError, IndexError):
                pass
        await proc.wait()
    finally:
        try:
            await asyncio.wait_for(err_task, timeout=2)
        except Exception:
            err_task.cancel()

    if proc.returncode == 0:
        if not os.path.exists(out_path):
            base_no_ext, _ = os.path.splitext(out_path)
            for ext in (".mp4", ".mkv", ".webm"):
                alt = base_no_ext + ext
                if alt != out_path and os.path.exists(alt) and os.path.getsize(alt) > 10 * 1024:
                    os.rename(alt, out_path)
                    break
        return os.path.exists(out_path) and os.path.getsize(out_path) > 10 * 1024
    logger.warning(f"yt-dlp failed (exit {proc.returncode}) for {os.path.basename(out_path)}: {' | '.join(err_tail[-3:])}")
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Fallback 2: ffmpeg (single connection — last resort only)
# ─────────────────────────────────────────────────────────────────────────────
def resolve_hls_stream_target(url: str, quality: str = "720p") -> Tuple[str, Optional[str]]:
    """Picks the variant URL for `quality` from a master playlist (sync helper, run in a thread)."""
    if not url or ".m3u8" not in url.lower():
        return url, None
    try:
        req = urllib.request.Request(url, headers=_headers_for(url))
        with urllib.request.urlopen(req, timeout=10) as resp:
            text = resp.read().decode("utf-8", errors="ignore")
            final = resp.geturl()
    except Exception:
        return url, None
    if "#EXT-X-STREAM-INF" not in text:
        return url, None
    variants, audios = fastdl.parse_master(text, final)
    if not variants:
        return url, None
    v = fastdl.choose_variant(variants, quality)
    if v.audio_group and audios:
        # Separate audio: let ffmpeg read the master and map the chosen program
        idx = variants.index(v)
        return url, f"0:p:{idx}?"
    return v.url, None


def resolve_hls_stream_url(url: str, quality: str = "720p") -> str:
    return resolve_hls_stream_target(url, quality)[0]


async def _download_with_ffmpeg(
    url: str,
    out_path: str,
    quality: str = "720p",
    meter: Optional[fastdl.SpeedMeter] = None,
    on_fraction: Optional[Callable[[float], None]] = None,
    total_dur: float = 0.0,
) -> bool:
    target_url, map_arg = url, None
    if ".m3u8" in url.lower():
        try:
            target_url, map_arg = await asyncio.to_thread(resolve_hls_stream_target, url, quality)
        except Exception:
            pass
    hdr = "".join(f"{k}: {v}\r\n" for k, v in _headers_for(url).items())
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
        "-rw_timeout", "20000000",
        "-headers", hdr, "-i", target_url,
    ]
    if map_arg:
        cmd += ["-map", map_arg]
    cmd += ["-c", "copy"]
    if out_path.endswith(".mp4"):
        cmd += ["-movflags", "+faststart"]
    cmd += ["-progress", "pipe:1", "-nostats", out_path]

    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    last_size = 0
    last_progress_t = time.monotonic()
    err_buf = b""
    try:
        while True:
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=60)
            except asyncio.TimeoutError:
                if time.monotonic() - last_progress_t > 90:
                    proc.kill()
                    break
                continue
            if not line:
                break
            k, _, v = line.decode("utf-8", "ignore").strip().partition("=")
            if k == "total_size" and v.isdigit():
                cur = int(v)
                if cur > last_size:
                    if meter:
                        meter.add(cur - last_size)
                    last_size = cur
                    last_progress_t = time.monotonic()
            elif k == "out_time_us" and total_dur > 0 and on_fraction:
                try:
                    on_fraction(min(0.99, float(v) / 1e6 / total_dur))
                except ValueError:
                    pass
        await proc.wait()
        if proc.stderr:
            err_buf = await proc.stderr.read()
    except Exception as e:
        logger.error(f"ffmpeg fallback error: {e}")
        try:
            proc.kill()
        except Exception:
            pass
    if proc.returncode == 0 and os.path.exists(out_path):
        return True
    err = err_buf.decode("utf-8", "ignore").strip().splitlines()
    logger.warning(f"ffmpeg failed (exit {proc.returncode}) for {os.path.basename(out_path)}: {err[-1] if err else ''}")
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Single episode: engine ladder
# ─────────────────────────────────────────────────────────────────────────────
def _cleanup(path: str):
    for p in (path, path + ".parts", path + ".v.ts", path + ".v.mp4", path + ".a.ts", path + ".a.mp4"):
        try:
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
            elif os.path.exists(p):
                os.remove(p)
        except Exception:
            pass


async def _download_single_ep(
    url: str,
    out_path: str,
    quality: str = "720p",
    meter: Optional[fastdl.SpeedMeter] = None,
    on_fraction: Optional[Callable[[float], None]] = None,
    total_dur: float = 0.0,
    workers: int = fastdl.SEG_WORKERS,
    label: str = "",
) -> bool:
    _cleanup(out_path)
    headers = _headers_for(url)
    is_hls = fastdl.is_hls_url(url)
    tag = label or os.path.basename(out_path)

    def _seg_cb(done: int, total: int):
        if on_fraction and total > 0:
            on_fraction(min(0.99, done / total))

    # 1. Native FastDL
    t0 = time.monotonic()
    try:
        if is_hls:
            await fastdl.download_hls(url, out_path, quality, headers, meter, workers, on_progress=_seg_cb)
        else:
            await fastdl.download_direct(url, out_path, headers, meter, workers, on_progress=_seg_cb)
        if await _is_valid_async(out_path):
            el = max(0.01, time.monotonic() - t0)
            mb = os.path.getsize(out_path) / 1048576
            logger.info(f"⚡ [fastdl] {tag}: {mb:.1f} MB in {el:.1f}s ({mb / el:.1f} MB/s)")
            return True
        logger.warning(f"[fastdl] {tag}: output failed validation, trying yt-dlp")
    except fastdl.UnsupportedStream as e:
        logger.info(f"[fastdl] {tag}: {e} -> yt-dlp")
    except Exception as e:
        logger.warning(f"[fastdl] {tag} failed: {e!r} -> yt-dlp")
    _cleanup(out_path)

    # 2. yt-dlp
    if await _download_with_ytdlp(url, out_path, quality, meter, on_fraction) and await _is_valid_async(out_path):
        logger.info(f"✅ [yt-dlp] {tag} ok ({time.monotonic() - t0:.1f}s total)")
        return True
    _cleanup(out_path)

    # 3. ffmpeg (slow, last resort)
    logger.warning(f"⚠️ {tag}: falling back to single-connection ffmpeg (slow)")
    if await _download_with_ffmpeg(url, out_path, quality, meter, on_fraction, total_dur) and await _is_valid_async(out_path):
        return True
    _cleanup(out_path)
    return False


def _pick_url(ep: Dict[str, Any]) -> Optional[str]:
    return (
        ep.get("video_url")
        or ep.get("video_url_720p")
        or ep.get("video_url_1080p")
        or ep.get("video_url_480p")
        or ep.get("video_url_360p")
        or ep.get("master_stream_url")
    )


async def _maybe_refresh_alright(ep: Dict[str, Any], url: str, show_id: str, force: bool) -> str:
    ep_num = ep.get("episode_number", 0)
    if not force:
        m_exp = re.search(r"exp=(\d+)", url)
        if not m_exp or int(m_exp.group(1)) >= int(time.time()) + 60:
            return url
        logger.info(f"⚠️ Alright token expired for Ep #{ep_num}, refreshing...")
    try:
        from platforms.alright.api import AlrightAPI
        fresh = await AlrightAPI.get_instance().get_fresh_stream_url(
            show_id=show_id, episode_number=ep_num, season_number=ep.get("season_number", 1),
        )
        if fresh:
            ep["video_url"] = fresh
            ep["master_stream_url"] = fresh
            return fresh
    except Exception as e:
        logger.warning(f"Alright token refresh failed for Ep #{ep_num}: {e}")
    return url


async def download_episode_with_retry(
    ep: Dict[str, Any],
    out_dir: str,
    sem: asyncio.Semaphore,
    quality: str = "720p",
    status_cb: Optional[Callable] = None,
    title: str = "",
    label: str = "",
    total_dur: float = 0.0,
    meter: Optional[fastdl.SpeedMeter] = None,
    on_fraction: Optional[Callable[[float], None]] = None,
    workers: Optional[int] = None,
) -> Optional[str]:
    ep_num = ep.get("episode_number", 0)
    url = _pick_url(ep)
    if not url:
        return None

    show_id = str(ep.get("show_id") or "")
    if not show_id:
        m_sid = re.search(r"/([0-9a-fA-F]{24})\b", url)
        if m_sid:
            show_id = m_sid.group(1)
    low = url.lower()
    is_alright = (
        str(ep.get("platform", "")).lower() in ("alright", "alrighttv")
        or "alright" in low or "videostech" in low or "ruskmedia" in low
    )

    out_path = os.path.join(out_dir, f"EP_{int(ep_num):03d}.mp4")
    task_stats.set_phase("Download")
    own_reporter = None
    if status_cb is not None and meter is None:
        # Called directly (single episode, legacy path) -> provide our own live progress.
        meter = fastdl.SpeedMeter()
        frac = {"v": 0.0}
        on_fraction = lambda f: frac.__setitem__("v", f)

        def _render():
            pct = frac["v"] * 100
            est = int(meter.total / frac["v"]) if frac["v"] > 0.02 else 0
            spd = meter.rate_mb_s()
            eta = (est - meter.total) / (spd * 1048576) if (est and spd > 0.05) else 0
            return messages.msg_single_downloading_progress(
                title=title or "Video", label=label or f"Downloading Ep #{ep_num}", pct=pct,
                bytes_done=meter.total, total_bytes=est, speed_mb_s=spd, eta_sec=eta,
            )
        own_reporter = fastdl.ProgressReporter(status_cb, _render, interval=5.0).start()

    try:
        async with sem:
            if is_alright and show_id:
                url = await _maybe_refresh_alright(ep, url, show_id, force=False)
            for attempt in range(1, RETRIES_PER_EPISODE + 1):
                ok = await _download_single_ep(
                    url, out_path, quality=quality, meter=meter, on_fraction=on_fraction,
                    total_dur=total_dur, workers=workers or fastdl.SEG_WORKERS,
                    label=label or f"Ep #{ep_num}",
                )
                if ok:
                    return out_path
                if is_alright and show_id:
                    url = await _maybe_refresh_alright(ep, url, show_id, force=True)
                await asyncio.sleep(1.5 * attempt)
    finally:
        if own_reporter:
            await own_reporter.stop()

    logger.warning(f"Failed to download episode {ep_num} after {RETRIES_PER_EPISODE} attempts.")
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Many episodes
# ─────────────────────────────────────────────────────────────────────────────
def _select_quality_url(ep: Dict[str, Any], q_str: str, is_alright_plat: bool) -> Optional[str]:
    # QuickTV: exact real file per button (set by the parser; no other platform sets this)
    qurls = ep.get("quality_urls")
    if isinstance(qurls, dict) and qurls.get(q_str):
        return qurls[q_str]
    qual_d = ep.get("qualities") if isinstance(ep.get("qualities"), dict) else {}
    if is_alright_plat:
        order = ["master_stream_url", "video_url", "video_url_1080p", "video_url_720p", "video_url_480p", "video_url_360p"]
        return next((ep.get(k) for k in order if ep.get(k)), None)
    chains = {
        "1080p": ("video_url_1080p", "1080p", "master_stream_url", "video_url", "video_url_720p"),
        "720p": ("video_url_720p", "720p", "master_stream_url", "video_url", "video_url_1080p"),
        "540p": ("video_url_540p", "540p", "master_stream_url", "video_url", "video_url_720p"),
        "480p": ("video_url_480p", "480p", "master_stream_url", "video_url", "video_url_720p"),
        "360p": ("video_url_360p", "360p", "master_stream_url", "video_url", "video_url_480p"),
    }
    chain = chains.get(q_str, ("video_url_1080p", "video_url_720p", "master_stream_url", "video_url"))
    for k in chain:
        v = ep.get(k) if k.startswith(("video_", "master_")) else qual_d.get(k)
        if v:
            return v
    return None


async def download_all_dashreels_episodes(
    episodes: List[Dict[str, Any]],
    out_dir: str,
    title: str = "Show",
    quality: str = "720p",
    status_cb: Optional[Callable] = None,
    refetch_cb: Optional[Callable[[], Any]] = None,
    platform: str = "",
) -> List[str]:
    """Downloads all episodes concurrently with live speed/ETA and StoryTV auto-refetch retry."""
    os.makedirs(out_dir, exist_ok=True)
    total = len(episodes)
    if total == 0:
        return []
    task_stats.set_phase("Download")
    q_str = str(quality).lower().strip()
    is_alright_plat = str(platform or "").lower() in ("alright", "alrighttv")
    parallel = 1 if total == 1 else min(PARALLEL_DOWNLOADS, total)
    sem = asyncio.Semaphore(parallel)
    per_file_workers = fastdl.SEG_WORKERS if parallel <= 2 else fastdl.MULTI_SEG_WORKERS

    meter = fastdl.SpeedMeter()
    downloaded_map: Dict[int, str] = {}
    done_bytes = {"v": 0}
    fractions: Dict[int, float] = {}
    finished = {"v": 0}

    if status_cb:
        init_msg = messages.msg_progress_card(
            title=title, pct=0, status=f"Download ({quality})" if total == 1 else f"Download (0/{total} eps)")
        try:
            res = status_cb(init_msg)
            if inspect.isawaitable(res):
                await res
        except Exception:
            pass

    def _render() -> str:
        spd = meter.rate_mb_s()
        done = len(downloaded_map)
        pct = min(100.0, (finished["v"] + sum(fractions.values())) / total * 100.0) if total else 0.0
        bar_len = 12
        filled = int(round((pct / 100.0) * bar_len))
        bar_str = "█" * filled + "░" * (bar_len - filled)
        finalizing = bool(fractions) and all(f >= 0.99 for f in fractions.values()) and spd < 1.0
        status_label = f"Finalizing ({done}/{total} eps)" if finalizing else f"Downloading Episodes ({done}/{total} @ {quality})"
        return (
            f"⚡️ **Status:** {status_label}...\n\n"
            f"[{bar_str}] {pct:.1f}%\n"
            f"🚀 `{spd:.2f} MB/s`  •  `{done}/{total}` Downloaded\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )

    reporter = fastdl.ProgressReporter(status_cb, _render, interval=5.0).start()

    async def _worker(ep):
        ep_copy = dict(ep)
        ep_copy["platform"] = platform
        ep_copy["video_url"] = _select_quality_url(ep, q_str, is_alright_plat)
        ep_num = ep.get("episode_number", 1)
        dur = float(ep.get("duration_sec") or ep.get("duration") or 0.0)
        fractions[ep_num] = 0.0
        path = await download_episode_with_retry(
            ep_copy, out_dir, sem, quality=q_str, status_cb=None, title=title,
            label=f"Ep #{ep_num}", total_dur=dur, meter=meter,
            on_fraction=lambda f, n=ep_num: fractions.__setitem__(n, f),
            workers=per_file_workers,
        )
        fractions.pop(ep_num, None)
        finished["v"] += 1
        if path and os.path.exists(path):
            if ep_num not in downloaded_map:
                done_bytes["v"] += os.path.getsize(path)
            downloaded_map[ep_num] = path
        return path

    try:
        await asyncio.gather(*[_worker(ep) for ep in episodes])

        missing_eps = [ep for ep in episodes if ep.get("episode_number") not in downloaded_map]
        is_storytv = (platform or "").lower() in ("storytv",) or "storytv" in title.lower()

        if missing_eps and is_storytv and refetch_cb:
            for attempt in (2, 3):
                missing_nums = [e.get("episode_number") for e in missing_eps]
                logger.info(f"🔄 [StoryTV] {len(missing_eps)} missing {missing_nums}. Refetching from Lipat API (Attempt {attempt}/3)...")
                if attempt == 3:
                    await asyncio.sleep(3.0)
                try:
                    fresh = await refetch_cb()
                    if fresh:
                        fmap = {e.get("episode_number"): e for e in fresh if e.get("episode_number") is not None}
                        finished["v"] -= len(missing_eps)
                        await asyncio.gather(*[_worker(fmap.get(e.get("episode_number"), e)) for e in missing_eps])
                except Exception as e:
                    logger.warning(f"⚠️ [StoryTV] refetch attempt {attempt} error: {e}")
                missing_eps = [ep for ep in episodes if ep.get("episode_number") not in downloaded_map]
                if not missing_eps:
                    break
            final_missing = [ep.get("episode_number") for ep in missing_eps]
            if final_missing:
                logger.warning(f"⚠️ [StoryTV] Episodes {final_missing} failed after 3 attempts. Continuing with {len(downloaded_map)}.")
    finally:
        el = max(0.1, time.monotonic() - meter.t_start)
        await reporter.stop(final=bool(status_cb))

    logger.info(
        f"📊 [{platform or 'download'}] {title}: {len(downloaded_map)}/{total} eps, "
        f"{meter.total / 1048576:.1f} MB in {el:.1f}s (avg {meter.total / el / 1048576:.1f} MB/s)"
    )
    return [downloaded_map[k] for k in sorted(downloaded_map) if os.path.exists(downloaded_map[k])]
