#!/usr/bin/env python3
"""
FFmpeg Episode Merger and Auto-Splitter for PrimeXMasoom.
Concatenates downloaded/watermarked segments into a single release MP4 file,
with live progress reporting for both watermarking and merging stages.
"""

import asyncio
import inspect
import logging
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import uuid
import bot_config as cfg
import watermark_config as wm_cfg
import json
import re
from collections import Counter
import naming, progress as prog, watermark, task_stats
from logger import logger


@dataclass
class MergeResult:
    parts: List[str]
    part_sizes_mb: List[float]
    watermark_time_sec: float
    merge_time_sec: float
    watermarked_episodes: Optional[List[int]] = None


_PIX_EQUIV = {"yuvj420p": "yuv420p", "yuvj422p": "yuv422p", "yuvj444p": "yuv444p"}
# 10/12-bit -> 8-bit equivalent. A single MP4 track cannot switch H.264 profile mid-stream, so mixed
# bit depths must be unified; 8-bit is the only depth every phone / Telegram player decodes in hardware
# (High10 often fails), and it is also the faster encode.
_PIX_TO_8BIT = {
    "yuv420p10le": "yuv420p", "yuv420p12le": "yuv420p", "yuv420p10be": "yuv420p",
    "yuv422p10le": "yuv422p", "yuv422p12le": "yuv422p",
    "yuv444p10le": "yuv444p", "yuv444p12le": "yuv444p",
    "p010le": "yuv420p", "p016le": "yuv420p",
}


def _is_high_bit(pix_fmt: str) -> bool:
    return pix_fmt in _PIX_TO_8BIT
CAP_RATIO = float(os.environ.get("MERGE_BITRATE_CAP_RATIO", "1.10") or 1.10)
# 0 = keep whatever the source gives (default): a re-encode is only ever capped at the source's own
# bitrate, scaled by the resolution change. Set e.g. 1.5 to also cap at 1.5x the show's own bitrate,
# which keeps releases small when a few episodes exist only as huge raw masters.
COHORT_CAP_RATIO = float(os.environ.get("MERGE_COHORT_CAP_RATIO", "0") or 0)
MIN_CAP_BPS = int(os.environ.get("MERGE_MIN_CAP_BPS", "400000") or 400000)


def probe_media_stream(path: str) -> Dict[str, Any]:
    """Probes the first video and first audio stream of a media file (everything the merge plan needs)."""
    cmd = ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", path]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            data = json.loads(r.stdout)
            info = {
                "width": 0, "height": 0, "fps": 0.0,
                "v_codec": "", "pix_fmt": "",
                "sample_rate": 0, "channels": 0,
                "a_codec": "", "a_profile": "",
                "has_audio": False,
                "timescale": 90000,
                "duration": 0.0,
                "v_bitrate": 0,
            }
            try:
                info["duration"] = float((data.get("format") or {}).get("duration") or 0)
            except (TypeError, ValueError):
                pass
            for s in data.get("streams", []):
                if s.get("codec_type") == "video" and not info["v_codec"]:
                    if (s.get("disposition") or {}).get("attached_pic"):
                        continue  # cover art, not the real video
                    info["v_codec"] = s.get("codec_name", "")
                    try:
                        info["v_bitrate"] = int(s.get("bit_rate") or 0)
                    except (TypeError, ValueError):
                        pass
                    info["width"] = int(s.get("width", 0) or 0)
                    info["height"] = int(s.get("height", 0) or 0)
                    pf = s.get("pix_fmt", "") or ""
                    info["pix_fmt"] = _PIX_EQUIV.get(pf, pf)
                    tb = s.get("time_base", "")
                    if "/" in tb:
                        try:
                            num, den = tb.split("/")
                            if float(num) > 0 and float(den) > 0:
                                info["timescale"] = int(round(float(den) / float(num)))
                        except (ValueError, ZeroDivisionError):
                            pass
                    for key in ("avg_frame_rate", "r_frame_rate"):
                        fr = s.get(key, "0/0") or "0/0"
                        try:
                            num, den = (fr.split("/") + ["1"])[:2]
                            val = float(num) / float(den) if float(den) > 0 else 0.0
                        except (ValueError, ZeroDivisionError):
                            val = 0.0
                        if val > 0:
                            info["fps"] = round(val, 3)
                            break
                elif s.get("codec_type") == "audio" and not info["has_audio"]:
                    info["has_audio"] = True
                    info["sample_rate"] = int(s.get("sample_rate", 0) or 0)
                    info["channels"] = int(s.get("channels", 0) or 0)
                    info["a_codec"] = s.get("codec_name", "") or ""
                    info["a_profile"] = s.get("profile", "") or ""
            if not info["v_bitrate"]:
                # TS / some MP4s carry no per-stream bitrate: derive from container size & duration
                try:
                    fmt = data.get("format") or {}
                    total = int(fmt.get("bit_rate") or 0)
                    if not total and info["duration"] > 0:
                        total = int(os.path.getsize(path) * 8 / info["duration"])
                    info["v_bitrate"] = max(0, total - 128_000) if total else 0
                except (TypeError, ValueError, OSError):
                    pass
            return info
    except Exception as e:
        logger.warning(f"probe_media_stream failed for {path}: {e}")
    return {}


def _video_key(info: Dict[str, Any]) -> tuple:
    """What MUST match for lossless stream-copy concat. Frame rate is deliberately NOT part of it:
    MP4/TS carry per-frame timestamps, so mixed 23.976/24/25/30/60 fps join cleanly (VFR) without re-encode."""
    return (info.get("v_codec") or "", int(info.get("width") or 0), int(info.get("height") or 0), info.get("pix_fmt") or "")


def _audio_key(info: Dict[str, Any]) -> tuple:
    """Audio params that cannot change inside one AAC/MP3 track without breaking playback."""
    if not info.get("has_audio"):
        return ("none",)
    return (info.get("a_codec") or "", int(info.get("sample_rate") or 0), int(info.get("channels") or 0), info.get("a_profile") or "")


def _dominant(items: List[tuple], weights: List[float], prefer_higher=None):
    """Most common key weighted by duration; ties broken by prefer_higher(key)."""
    score: Dict[tuple, float] = {}
    for k, w in zip(items, weights):
        score[k] = score.get(k, 0.0) + max(w, 0.001)
    if not score:
        return None
    return max(score, key=lambda k: (round(score[k], 3), prefer_higher(k) if prefer_higher else 0))


def build_merge_plan(infos: List[Dict[str, Any]], prefer: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    prefer (optional, platform hint e.g. QuickTV 720p): {"codec": "hevc"|"h264"|None, "width", "height"}.
      The baseline is picked only among layouts that some episode ALREADY has and that match the
      hint, so a minority of raw originals is aligned to the show's real stream format instead of
      the whole show being re-encoded towards the originals. Re-encoded episodes are also capped
      at the bitrate of the episodes that already are the baseline (cohort cap).

    Decides, fully dynamically, the baseline stream layout and per-episode action:
      video: 'copy' | 'encode'      (encode only if codec / resolution / pixel format differ)
      audio: 'copy' | 'encode' | 'silent'  (encode only if codec / rate / channels / profile differ;
                                            'silent' adds a silent track to episodes that have none)
    """
    durs = [float(i.get("duration") or 1.0) for i in infos]
    vkeys = [_video_key(i) for i in infos]
    valid_v = [(k, d) for k, d in zip(vkeys, durs) if k[1] and k[2]]
    v_target = _dominant([k for k, _ in valid_v], [d for _, d in valid_v],
                         prefer_higher=lambda k: (not _is_high_bit(k[3]), k[1] * k[2], k[0] == "h264")) if valid_v else None
    cohort_ratio = COHORT_CAP_RATIO
    if prefer and valid_v:
        def _match(k):
            return ((not prefer.get("codec") or k[0] == prefer["codec"])
                    and (not prefer.get("width") or k[1] == int(prefer["width"]))
                    and (not prefer.get("height") or k[2] == int(prefer["height"])))
        hinted = [(k, d) for k, d in valid_v if _match(k)]
        if hinted:
            v_target = _dominant([k for k, _ in hinted], [d for _, d in hinted],
                                 prefer_higher=lambda k: (not _is_high_bit(k[3]), k[0] == (prefer.get("codec") or k[0])))
            cohort_ratio = float(prefer.get("cohort_cap_ratio") or 1.15)
    if v_target and _is_high_bit(v_target[3]) and any(not _is_high_bit(k[3]) for k, _ in valid_v):
        # Mixed bit depths: aim at 8-bit instead of re-encoding 8-bit episodes up to 10-bit
        # (pointless, slower, and High10 output breaks hardware decoders on many phones).
        v_target = (v_target[0], v_target[1], v_target[2], _PIX_TO_8BIT[v_target[3]])

    akeys = [_audio_key(i) for i in infos]
    real_a = [(k, d) for k, d in zip(akeys, durs) if k != ("none",)]
    a_target = _dominant([k for k, _ in real_a], [d for _, d in real_a],
                         prefer_higher=lambda k: (k[2], k[1])) if real_a else None
    if a_target and a_target[0] not in ("aac", "mp3", "ac3", "eac3", "opus"):
        a_target = ("aac", a_target[1] or 48000, a_target[2] or 2, "LC")

    # Re-encode ceiling: episodes that already ARE the baseline define what this show's bitrate
    # normally looks like. Without this, a raw 15 Mbps master downscaled to 720p kept a 15 Mbps
    # ceiling and stayed ~400 MB per episode.
    cohort = sorted(int(i.get("v_bitrate") or 0) for i, k in zip(infos, vkeys)
                    if k == v_target and int(i.get("v_bitrate") or 0) > 100_000)
    cohort_vbr = cohort[len(cohort) // 2] if cohort else 0

    actions = []
    for info, vk, ak in zip(infos, vkeys, akeys):
        v_act = "copy" if (v_target is None or vk == v_target or not (vk[1] and vk[2])) else "encode"
        if a_target is None:
            a_act = "copy"            # no episode has audio -> nothing to align
        elif ak == ("none",):
            a_act = "silent"
        elif ak == a_target:
            a_act = "copy"
        else:
            a_act = "encode"
        reasons = []
        if v_act == "encode":
            if vk[0] != v_target[0]:
                reasons.append(f"codec {vk[0]}->{v_target[0]}")
            if (vk[1], vk[2]) != (v_target[1], v_target[2]):
                reasons.append(f"res {vk[1]}x{vk[2]}->{v_target[1]}x{v_target[2]}")
            if vk[3] != v_target[3]:
                depth = "10-bit->8-bit" if (_is_high_bit(vk[3]) and not _is_high_bit(v_target[3])) else f"{vk[3]}->{v_target[3]}"
                reasons.append(f"pix {depth}")
        if a_act == "encode":
            reasons.append(f"audio {ak[0]} {ak[1]}Hz {ak[2]}ch -> {a_target[0]} {a_target[1]}Hz {a_target[2]}ch")
        elif a_act == "silent":
            reasons.append("no audio -> silent track")
        cap_bps = 0
        if v_act == "encode" and v_target:
            src_vbr = int(info.get("v_bitrate") or 0)
            src_px = max(1, vk[1] * vk[2])
            tgt_px = max(1, v_target[1] * v_target[2])
            caps = []
            if src_vbr > 100_000:
                caps.append(src_vbr * (tgt_px / src_px) * CAP_RATIO)   # scale with pixel count
            if cohort_vbr and cohort_ratio > 0:
                caps.append(cohort_vbr * cohort_ratio)                 # optional: stay in line with the show
            if caps:
                cap_bps = int(max(MIN_CAP_BPS, min(caps)))
                reasons.append(f"cap {cap_bps // 1000}kbps")
        actions.append({"video": v_act, "audio": a_act, "reasons": reasons, "cap_bps": cap_bps})
    return {"video_target": v_target, "audio_target": a_target, "actions": actions,
            "cohort_vbr": cohort_vbr}


def _encoder_args_for(codec: str, cap_bps: int, pix_fmt: str) -> List[str]:
    """CRF encode with an explicit bitrate ceiling (cap_bps already includes any ratio)."""
    preset = getattr(wm_cfg, "WATERMARK_X264_PRESET", "superfast")
    crf = str(getattr(wm_cfg, "WATERMARK_CRF", 20))
    if codec in ("hevc", "h265"):
        args = ["-c:v", "libx265", "-preset", preset, "-crf", str(int(crf) + 4), "-tag:v", "hvc1"]
    else:
        args = ["-c:v", "libx264", "-preset", preset, "-crf", crf]
    args += ["-pix_fmt", pix_fmt or "yuv420p"]
    if cap_bps > 100_000:
        args += ["-maxrate", str(int(cap_bps)), "-bufsize", str(int(cap_bps * 2))]
    return args


def _audio_encoder_args(a_target: tuple) -> List[str]:
    codec, sr, ch = a_target[0], int(a_target[1] or 48000), int(a_target[2] or 2)
    enc = {"aac": "aac", "mp3": "libmp3lame", "ac3": "ac3", "eac3": "eac3", "opus": "libopus"}.get(codec, "aac")
    args = ["-c:a", enc, "-ar", str(sr), "-ac", str(ch)]
    args += ["-b:a", "192k" if ch <= 2 else "384k"]
    return args


def normalize_episode(
    input_path: str,
    output_path: str,
    action: Dict[str, Any],
    video_target: Optional[tuple],
    audio_target: Optional[tuple],
) -> bool:
    """Applies exactly the needed fix: video copy/encode x audio copy/encode/silent. Fps is always preserved."""
    v_act, a_act = action.get("video", "copy"), action.get("audio", "copy")
    src_meta = watermark.probe_source_video(input_path)
    src_dur = float(src_meta.get("duration") or 0)
    timeout = (max(600, int(src_dur * 4)) if src_dur > 0 else 3600) if v_act == "encode" else 600

    cmd = ["ffmpeg", "-y", "-i", input_path]
    if a_act == "silent" and audio_target:
        layout = "mono" if int(audio_target[2] or 2) == 1 else ("stereo" if int(audio_target[2] or 2) == 2 else f"{int(audio_target[2])}c")
        cmd += ["-f", "lavfi", "-i", f"anullsrc=r={int(audio_target[1] or 48000)}:cl={layout}"]
        cmd += ["-map", "0:v:0", "-map", "1:a:0", "-shortest"]
    else:
        cmd += ["-map", "0:v:0", "-map", "0:a:0?"]

    if v_act == "encode" and video_target:
        _, tw, th, tpix = video_target
        # Only scale when the resolution really differs: a bit-depth / codec-only fix needs no
        # filter graph at all, and dropping it makes those encodes noticeably cheaper.
        if (int(src_meta.get("width") or 0), int(src_meta.get("height") or 0)) != (tw, th):
            cmd += ["-vf", (f"scale={tw}:{th}:force_original_aspect_ratio=decrease,"
                            f"pad={tw}:{th}:'trunc((ow-iw)/2/2)*2':'trunc((oh-ih)/2/2)*2',setsar=1")]
        cap = int(action.get("cap_bps") or 0)
        if cap <= 0:   # direct callers without a plan: fall back to the source bitrate
            cap = int(int(src_meta.get("v_bitrate") or 0) * CAP_RATIO)
        cmd += _encoder_args_for(video_target[0], cap, tpix)
    else:
        cmd += ["-c:v", "copy"]

    if a_act in ("encode", "silent") and audio_target:
        cmd += _audio_encoder_args(audio_target)
    else:
        cmd += ["-c:a", "copy"]

    cmd += ["-avoid_negative_ts", "make_zero", "-movflags", "+faststart", output_path]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           preexec_fn=watermark._lower_priority if os.name == "posix" else None)
        if r.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 1024:
            return True
        # Copying a damaged audio stream can fail; retry once with audio re-encoded
        if a_act == "copy" and audio_target:
            logger.warning(f"Normalize copy failed for {os.path.basename(input_path)}, retrying with audio re-encode")
            return normalize_episode(input_path, output_path, {**action, "audio": "encode"}, video_target, audio_target)
        logger.error(f"Normalization failed for {input_path} (exit {r.returncode}): {_clean_ffmpeg_error(r.stderr)}")
        return False
    except Exception as e:
        logger.error(f"Normalization exception for {input_path}: {e}")
        return False


def normalize_media_stream(
    input_path: str,
    output_path: str,
    target_w: int,
    target_h: int,
    target_fps: float = 0.0,
    target_sr: int = 44100,
    target_ch: int = 2,
    target_timescale: int = 90000,
) -> bool:
    """Backwards-compatible wrapper (fps is ignored on purpose: never re-encode just for frame rate)."""
    info = probe_media_stream(input_path)
    plan_v = (info.get("v_codec") or "h264", target_w, target_h, info.get("pix_fmt") or "yuv420p")
    plan_a = (info.get("a_codec") or "aac", target_sr, target_ch, info.get("a_profile") or "")
    action = {
        "video": "copy" if _video_key(info) == plan_v else "encode",
        "audio": "copy" if _audio_key(info) == plan_a else ("silent" if not info.get("has_audio") else "encode"),
    }
    return normalize_episode(input_path, output_path, action, plan_v, plan_a)


def fmt_dur(sec: float) -> str:
    sec = int(max(0, sec))
    h, rem = divmod(sec, 3600)
    m, s_ = divmod(rem, 60)
    return f"{h}h{m}m" if h else (f"{m}m{s_}s" if m else f"{s_}s")


async def _maybe_await(cb: Optional[Callable], *args):
    if not cb:
        return
    res = cb(*args)
    if inspect.isawaitable(res):
        await res


def extract_ep_num_from_path(file_path: str, default: int = 1) -> int:
    """
    Extracts the episode number from a file name such as EP_001.ts, 'Show - Ep 07 - Tag.mp3',
    'Show.S01E05.mp4' or 'wm_ep_4_EP_014.mp4'.
    Explicit episode markers win over bare numbers, so digits inside a title ('Transform Your 24 Hours',
    'Mission 2') are never mistaken for the episode; the LAST marker is used because processed files
    are prefixed (wm_ep_<idx>_ / norm_ep_<idx>_) with a position index before the real name.
    """
    fname = os.path.splitext(os.path.basename(file_path))[0]
    patterns = (
        r"S\d{1,3}E0*(\d{1,5})(?!\d)",                         # S01E05
        r"(?:^|[^A-Za-z])(?:ep|episode)[\s._-]*0*(\d{1,5})(?!\d)",  # Ep 05 / EP_005 / episode-5
        r"(?:^|[^A-Za-z])E0*(\d{1,5})(?!\d)",                    # E05
    )
    for pat in patterns:
        found = re.findall(pat, fname, re.IGNORECASE)
        if found:
            try:
                return int(found[-1])
            except ValueError:
                pass
    m = re.search(r"(\d+)", fname)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    return default


def format_chunk_filename(base_name: str, first_ep: int, last_ep: int) -> str:
    """
    Updates the episode range in base_name (e.g. [E01-E95] -> [E01-E55]).
    CRITICAL: Does NOT include Part001, Part002, or - Part1.
    Preserves exact release formatting.
    """
    clean_name = re.sub(r'[\s\-_.]+part\s*0*\d+', '', base_name, flags=re.IGNORECASE)
    new_range = f"[E{first_ep:02d}-E{last_ep:02d}]"
    if re.search(r'\[\s*E\d+\s*-\s*E\d+\s*\]', clean_name, flags=re.IGNORECASE):
        return re.sub(r'\[\s*E\d+\s*-\s*E\d+\s*\]', new_range, clean_name, flags=re.IGNORECASE)
    elif re.search(r'\bE\d+\s*-\s*E\d+\b', clean_name, flags=re.IGNORECASE):
        return re.sub(r'\bE\d+\s*-\s*E\d+\b', f"E{first_ep:02d}-E{last_ep:02d}", clean_name, flags=re.IGNORECASE)
    else:
        root, ext = os.path.splitext(clean_name)
        return f"{root} {new_range}{ext}"


def build_chunk_filename(
    base_filename: str,
    first_ep: int,
    last_ep: int,
    naming_params: Optional[Dict[str, Any]] = None,
) -> str:
    """Builds clean release filename for chunk without any part001/part002 labels."""
    if naming_params:
        try:
            return naming.build_release_filename(
                show_title=naming_params.get("show_title", "Show"),
                source_tag=naming_params.get("source_tag", "OTT"),
                release_tag=naming_params.get("release_tag", cfg.RELEASE_TAG),
                tags=naming_params.get("tags", {}),
                season_no=naming_params.get("season_no", 1),
                first_ep=first_ep,
                last_ep=last_ep,
                lang=naming_params.get("lang", "Hindi"),
                part_idx=None,   # CRITICAL: User requirement - NO Part001/Part002!
                total_parts=1,   # CRITICAL: User requirement - NO Part001/Part002!
                custom_quality=naming_params.get("custom_quality"),
                container_ext=naming_params.get("container_ext", "mp4"),
                video_codec=naming_params.get("video_codec"),
            )
        except Exception as e:
            logger.warning(f"Error building chunk filename via naming_params: {e}")
    return format_chunk_filename(base_filename, first_ep, last_ep)


def _clean_ffmpeg_error(stderr: str, max_lines: int = 15) -> str:
    """Extracts actual meaningful error lines from FFmpeg stderr, filtering out noisy progress lines."""
    if not stderr:
        return "No stderr recorded."
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    meaningful = [
        l for l in lines
        if not (l.startswith("frame=") or l.startswith("size=") or l.startswith("video:") or l.startswith("Stream mapping:"))
    ]
    if not meaningful:
        meaningful = lines
    return "\n".join(meaningful[-max_lines:])


async def _execute_seamless_concat(
    input_files: List[str],
    output_path: str,
    temp_dir: str,
    loop: asyncio.AbstractEventLoop,
) -> bool:
    """
    Concatenates input files into output_path using TS-mediated Annex-B stream-copy.
    Guarantees 100% exact duration, zero PTS/DTS timeline skew, and zero quality loss across all OTTs.
    Falls back gracefully to direct container concat if TS remuxing is not supported.
    All actions and any errors are logged with full detail to log.txt.
    """
    created_ts: List[str] = []
    remux_sem = asyncio.Semaphore(16)
    uid = uuid.uuid4().hex[:8]

    async def _ensure_ts(idx: int, p: str) -> str:
        if p.lower().endswith(".ts") and os.path.exists(p):
            return p
        ts_p = os.path.join(temp_dir, f"remux_{uid}_{idx}.ts")
        codec = ((await loop.run_in_executor(None, probe_media_stream, p)).get("v_codec") or "").lower()
        bsf_filter = "hevc_mp4toannexb" if codec in ("hevc", "h265") else "h264_mp4toannexb"
        cmd_ts = [
            "ffmpeg", "-y", "-i", p,
            "-c", "copy",
            "-bsf:v", bsf_filter,
            "-f", "mpegts",
            ts_p,
        ]
        try:
            async with remux_sem:
                r = await loop.run_in_executor(
                    None, lambda: subprocess.run(cmd_ts, capture_output=True, text=True, timeout=600)
                )
            if r.returncode == 0 and os.path.exists(ts_p) and os.path.getsize(ts_p) > 0:
                created_ts.append(ts_p)
                return ts_p
            err_snip = _clean_ffmpeg_error(r.stderr) if r and r.stderr else ""
            logger.warning(f"⚠️ TS remux with {bsf_filter} failed for {os.path.basename(p)}: {err_snip}. Trying fallback without filter...")
        except Exception as e:
            logger.exception(f"❌ Exception during TS remux with filter for {p}: {e}")

        # Fallback without bsf filter
        cmd_ts_fb = [
            "ffmpeg", "-y", "-i", p,
            "-c", "copy",
            "-f", "mpegts",
            ts_p,
        ]
        try:
            async with remux_sem:
                r_fb = await loop.run_in_executor(
                    None, lambda: subprocess.run(cmd_ts_fb, capture_output=True, text=True, timeout=600)
                )
            if r_fb.returncode == 0 and os.path.exists(ts_p) and os.path.getsize(ts_p) > 0:
                created_ts.append(ts_p)
                return ts_p
            err_fb = _clean_ffmpeg_error(r_fb.stderr) if r_fb and r_fb.stderr else ""
            logger.warning(f"⚠️ Fallback TS remux also failed for {os.path.basename(p)}: {err_fb}")
        except Exception as e:
            logger.exception(f"❌ Exception in fallback TS remux for {p}: {e}")

        return p

    concat_list_path = os.path.join(temp_dir, f"concat_list_{uid}.txt")
    t0_concat = time.time()
    try:
        logger.info(f"⚙️ Remuxing {len(input_files)} segments to Annex-B TS for seamless merge...")
        ts_files = await asyncio.gather(*[_ensure_ts(i, f) for i, f in enumerate(input_files)])

        with open(concat_list_path, "w", encoding="utf-8") as f:
            for p in ts_files:
                f.write(f"file '{os.path.abspath(p)}'\n")

        cmd = [
            "ffmpeg", "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", concat_list_path,
            "-c", "copy",
        ]
        # NOTE: HEVC is deliberately left as "hev1": watermarked episodes carry their own parameter
        # sets in-band, which "hvc1" forbids (forcing hvc1 produced decode errors at those joins).
        if output_path.lower().endswith(".mp4"):
            cmd.extend(["-movflags", "+faststart"])
        cmd.append(output_path)

        logger.info(f"🚀 Running FFmpeg seamless concat -> '{os.path.basename(output_path)}'...")
        r = await loop.run_in_executor(
            None, lambda: subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        )

        if r.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 10 * 1024:
            dur_concat = time.time() - t0_concat
            out_sz_mb = os.path.getsize(output_path) / (1024 * 1024)
            logger.info(f"✅ Seamless concat succeeded: '{os.path.basename(output_path)}' ({out_sz_mb:.1f} MB) in {dur_concat:.2f}s")
            return True

        err_snip = _clean_ffmpeg_error(r.stderr) if r and r.stderr else "Unknown error"
        logger.warning(f"⚠️ Seamless TS concat failed (exit {r.returncode if r else 'None'}):\n{err_snip}\nFalling back to direct container concat...")

        # Fallback: direct concat using original files
        with open(concat_list_path, "w", encoding="utf-8") as f:
            for p in input_files:
                f.write(f"file '{os.path.abspath(p)}'\n")

        cmd_fb = [
            "ffmpeg", "-y",
            "-fflags", "+genpts+igndts",
            "-f", "concat",
            "-safe", "0",
            "-i", concat_list_path,
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
        ]
        if output_path.lower().endswith(".mp4"):
            cmd_fb.extend(["-movflags", "+faststart"])
        cmd_fb.append(output_path)

        logger.info(f"🔄 Running direct concat fallback -> '{os.path.basename(output_path)}'...")
        r_fb = await loop.run_in_executor(
            None, lambda: subprocess.run(cmd_fb, capture_output=True, text=True, timeout=3600)
        )
        if r_fb.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 10 * 1024:
            dur_fb = time.time() - t0_concat
            out_sz_mb = os.path.getsize(output_path) / (1024 * 1024)
            logger.info(f"✅ Fallback direct concat succeeded: '{os.path.basename(output_path)}' ({out_sz_mb:.1f} MB) in {dur_fb:.2f}s")
            return True

        err_fb = _clean_ffmpeg_error(r_fb.stderr) if r_fb and r_fb.stderr else "Unknown error"
        logger.error(f"❌ Fallback direct concat also failed (exit {r_fb.returncode if r_fb else 'None'}):\n{err_fb}")
        return False
    except Exception as e:
        logger.exception(f"❌ Critical exception in _execute_seamless_concat: {e}")
        return False
    finally:
        if os.path.exists(concat_list_path):
            try:
                os.remove(concat_list_path)
            except Exception:
                pass
        for p in created_ts:
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass


async def merge_and_split(
    episode_files: List[str],
    output_dir: str,
    base_filename: str,
    temp_dir: str,
    title: str = "Show",
    watermark_enabled: bool = True,
    watermark_text: Optional[str] = None,
    status_cb: Optional[Callable] = None,
    episode_numbers: Optional[List[int]] = None,
    naming_params: Optional[Dict[str, Any]] = None,
    max_split_bytes: Optional[int] = None,
    merge_hint: Optional[Dict[str, Any]] = None,
) -> Optional[MergeResult]:
    """
    1. Probes all episodes to calculate show-wide baseline parameters (resolution, fps, audio).
    2. Watermarks middle episodes with target baseline fps.
    3. Harmonizes / normalizes any episodes that deviate from baseline (e.g. 24fps vs 60fps).
    4. Auto-groups episodes into chunks if total size exceeds 4GB (~3.91GB safe threshold):
       - File 1 fits as many episodes as possible under ~3.91GB.
       - File 2 (and subsequent files) takes remaining episodes.
       - Each file displays its accurate episode count/range [E01-E55], [E56-E95] without Part001/Part002.
    5. Concat-merges each chunk via FFmpeg copy.
    """
    if not episode_files:
        return None

    try:
        import messages

        # Ensure base_filename is strictly a safe single file name without slashes/paths
        clean_base = os.path.basename(str(base_filename or "release.mp4"))
        clean_base = re.sub(r'[/\\*?\"<>|:]', '', clean_base).strip() or "release.mp4"
        base_filename = clean_base

        os.makedirs(output_dir, exist_ok=True)
        os.makedirs(temp_dir, exist_ok=True)

        total_eps = len(episode_files)
        wm_indices = []
        wm_time = 0.0

        if status_cb:
            task_stats.set_phase("Merging")
            await _maybe_await(status_cb, messages.msg_progress_card(
                title=title, pct=0, status="Merging (preparing)",
                done_str=f"{total_eps} eps", total_str=f"{total_eps} eps", speed_str="--"))

        # 1. Probe all episodes in parallel and build a fully dynamic merge plan
        loop = asyncio.get_event_loop()
        probe_sem = asyncio.Semaphore(16)

        async def _safe_probe(p: str):
            async with probe_sem:
                return await loop.run_in_executor(None, probe_media_stream, p)

        probed_infos = await asyncio.gather(*[_safe_probe(p) for p in episode_files])
        plan = build_merge_plan(probed_infos, prefer=merge_hint)
        v_target, a_target = plan["video_target"], plan["audio_target"]
        actions = plan["actions"]

        fps_set = sorted({round(i.get("fps") or 0, 3) for i in probed_infos if i.get("fps")})
        target_fps = max(((i.get("fps") or 0) for i in probed_infos), default=0.0) or 25.0  # informational only
        target_timescale = 90000
        n_enc = sum(1 for x in actions if x["video"] == "encode")
        caps = [x.get("cap_bps") or 0 for x in actions if x["video"] == "encode"]
        logger.info(
            f"🎯 Merge plan for '{title}': video={v_target} audio={a_target} | fps seen={fps_set} (kept as-is, VFR) | "
            f"video encode: {n_enc}, audio fix: {sum(1 for x in actions if x['audio'] != 'copy')}, of {total_eps}"
            + (f" | show bitrate {plan['cohort_vbr'] // 1000}kbps, encode cap {min(caps) // 1000}-{max(caps) // 1000}kbps"
               if n_enc and caps and plan.get("cohort_vbr") else "")
        )

        # 2. Only the episodes that really need it are touched
        norm_sem = asyncio.Semaphore(4)       # CPU heavy (video encode)
        light_sem = asyncio.Semaphore(8)      # audio-only / remux (video copy)
        to_fix = [i for i, x in enumerate(actions) if x["video"] == "encode" or x["audio"] != "copy"]
        n_fix = len(to_fix)
        fix_done = 0
        t0_fix = time.time()
        fix_lock = asyncio.Lock()
        last_ui = 0.0

        def _fix_summary() -> str:
            parts = []
            nv = sum(1 for x in actions if x["video"] == "encode")
            na = sum(1 for x in actions if x["audio"] == "encode")
            ns = sum(1 for x in actions if x["audio"] == "silent")
            if nv:
                parts.append(f"{nv} video ({v_target[1]}x{v_target[2]} {v_target[0]})")
            if na:
                parts.append(f"{na} audio ({a_target[1]}Hz {a_target[2]}ch)")
            if ns:
                parts.append(f"{ns} silent-audio")
            return ", ".join(parts)

        if n_fix and status_cb:
            try:
                await _maybe_await(status_cb, messages.msg_stage_card(
                    title, f"Merging (aligning {_fix_summary()})", 0, n_fix))
            except Exception:
                pass

        async def _harmonize_ep(idx: int, p: str) -> str:
            nonlocal fix_done, last_ui
            act = actions[idx - 1]
            if act["video"] == "copy" and act["audio"] == "copy":
                return p
            base_p, _ = os.path.splitext(os.path.basename(p))
            norm_out = os.path.join(temp_dir, f"norm_ep_{idx}_{base_p}.mp4")
            logger.info(f"⚙️ Ep {idx}: {', '.join(act['reasons'])} (video {act['video']}, audio {act['audio']})")
            sem = norm_sem if act["video"] == "encode" else light_sem
            async with sem:
                ok = await loop.run_in_executor(None, normalize_episode, p, norm_out, act, v_target, a_target)
            async with fix_lock:
                fix_done += 1
                now = time.time()
                if status_cb and (now - last_ui >= 5.0 or fix_done == n_fix):
                    last_ui = now
                    try:
                        await _maybe_await(status_cb, messages.msg_stage_card(
                            title, f"Merging (aligning {fix_done}/{n_fix})", fix_done, n_fix, now - t0_fix))
                    except Exception:
                        pass
            if ok and os.path.exists(norm_out) and os.path.getsize(norm_out) > 1024:
                return norm_out
            logger.error(f"❌ Failed to align Ep {idx} ({p}); using original")
            return p

        harmonized_tasks = [_harmonize_ep(i + 1, p) for i, p in enumerate(episode_files)]
        harmonized_inputs = await asyncio.gather(*harmonized_tasks)

        # 3. Apply watermark on harmonized episodes if enabled (with live progress bar)
        final_inputs = []
        wm_indices = []
        wm_time = 0.0
        if watermark_enabled and total_eps > 0:
            wm_indices = wm_cfg.select_episodes_to_watermark(total_eps)
            total_wm = len(wm_indices)
            logger.info(f"🎨 Watermarking {total_wm} episodes in parallel: {wm_indices} of {total_eps}")

            t0_wm = time.time()
            wm_done = 0
            wm_sem = asyncio.Semaphore(4)
            wm_lock = asyncio.Lock()

            task_stats.set_phase("Watermark")
            # Live encode progress: each ffmpeg writes -progress to its own file; the card shows the share of
            # total episode duration already encoded (smooth %, real ETA, encode speed in x realtime).
            wm_prog_files = {i: os.path.join(temp_dir, f"wm_prog_{i}.txt") for i in wm_indices}
            wm_durs = {i: float((probed_infos[i - 1] if i - 1 < len(probed_infos) else {}).get("duration") or 0) for i in wm_indices}
            wm_total_dur = sum(wm_durs.values()) or 0.0
            wm_finished: set = set()

            def _encoded_seconds(i: int) -> float:
                if i in wm_finished:
                    return wm_durs.get(i, 0.0)
                try:
                    with open(wm_prog_files[i], "rb") as fh:
                        fh.seek(0, 2)
                        fh.seek(max(0, fh.tell() - 2048))
                        tail = fh.read().decode("utf-8", "ignore")
                    vals = [ln.split("=", 1)[1] for ln in tail.splitlines() if ln.startswith("out_time_us=")]
                    for v in reversed(vals):
                        if v.strip().isdigit():
                            return min(wm_durs.get(i, 0.0) or 1e12, int(v) / 1e6)
                except Exception:
                    pass
                return 0.0

            def _wm_card() -> str:
                el = max(0.1, time.time() - t0_wm)
                enc = sum(_encoded_seconds(i) for i in wm_indices)
                if wm_total_dur > 0:
                    pct = min(100.0, enc / wm_total_dur * 100.0)
                    xrt = enc / el
                    eta = (wm_total_dur - enc) / xrt if xrt > 0.01 else 0.0
                else:
                    pct, xrt, eta = (wm_done / total_wm * 100.0 if total_wm else 0.0), 0.0, 0.0
                return messages.msg_progress_card(
                    title=title, pct=pct, status=f"Watermark ({wm_done}/{total_wm} eps)",
                    done_str=f"{wm_done}/{total_wm} eps",
                    total_str=f"{fmt_dur(wm_total_dur)} video" if wm_total_dur else f"{total_wm} eps",
                    speed_str=f"{xrt:.1f}x" if xrt > 0 else "--", eta_sec=eta,
                )

            if status_cb:
                await _maybe_await(status_cb, _wm_card())

            stop_wm_ticker = asyncio.Event()

            async def _wm_ticker():
                while not stop_wm_ticker.is_set():
                    try:
                        await asyncio.wait_for(stop_wm_ticker.wait(), timeout=5.0)
                        break
                    except asyncio.TimeoutError:
                        pass
                    if stop_wm_ticker.is_set():
                        break
                    if status_cb:
                        try:
                            await _maybe_await(status_cb, _wm_card())
                        except Exception:
                            pass

            ticker_task = asyncio.create_task(_wm_ticker())

            async def _watermark_task(idx: int, ep_path: str):
                nonlocal wm_done
                base_name_no_ext, _ = os.path.splitext(os.path.basename(ep_path))
                wm_out = os.path.join(temp_dir, f"wm_ep_{idx}_{base_name_no_ext}.mp4")
                ok = False
                try:
                    async with wm_sem:
                        ok = await loop.run_in_executor(
                            None,
                            watermark.apply_watermark_to_file,
                            ep_path, wm_out, watermark_text, None, target_fps, target_timescale,
                            wm_prog_files.get(idx),
                        )
                except asyncio.CancelledError:
                    watermark.kill_watermark(wm_out)  # stop the ffmpeg process, not just the await
                    raise
                except Exception as e:
                    logger.exception(f"❌ Exception in watermarking episode {idx} ({ep_path}): {e}")
                    ok = False

                if not ok or not os.path.exists(wm_out) or os.path.getsize(wm_out) == 0:
                    logger.warning(f"⚠️ Watermark failed for Ep {idx} ({ep_path}). Using original unwatermarked episode to prevent merge failure.")
                    chosen_path = ep_path
                else:
                    chosen_path = wm_out

                async with wm_lock:
                    wm_done += 1
                    wm_finished.add(idx)
                    try:
                        os.remove(wm_prog_files[idx])
                    except Exception:
                        pass
                    if status_cb:
                        await _maybe_await(status_cb, _wm_card())
                return idx, chosen_path

            try:
                wm_tasks = [_watermark_task(idx, harmonized_inputs[idx - 1]) for idx in wm_indices]
                wm_results = await asyncio.gather(*wm_tasks)
            finally:
                stop_wm_ticker.set()
                ticker_task.cancel()
                try:
                    await ticker_task
                except (asyncio.CancelledError, Exception):
                    pass

            wm_map = dict(wm_results)

            final_inputs = [
                wm_map.get(i + 1, p) for i, p in enumerate(harmonized_inputs)
            ]
            wm_time = time.time() - t0_wm
        else:
            final_inputs = list(harmonized_inputs)

        # 4. Smart Single-File Verification & Chunking
        # Telegram Premium hard limit is 4000 MiB (~3.91 GiB).
        # We set safe limit to 3900 MiB (~3.91 GB / 4089 MB).
        if max_split_bytes is None:
            env_limit = os.environ.get("MAX_SPLIT_BYTES")
            if env_limit and env_limit.strip().isdigit():
                max_split_bytes = int(env_limit.strip())
            elif getattr(cfg, "HAS_PREMIUM_SESSION", bool(getattr(cfg, "USER_SESSION_STRING", False))):
                max_split_bytes = 3900 * 1024 * 1024  # 3900 MiB (~3.91 GB)
            else:
                max_split_bytes = 1900 * 1024 * 1024  # 1900 MiB (~1.95 GB)

        t0_merge = time.time()

        # Step 4a: Fast attempt at unified single file merge
        single_output = os.path.join(output_dir, base_filename)
        task_stats.set_phase("Merging")
        if status_cb:
            await _maybe_await(status_cb, messages.msg_merging_progress(title, total_eps, 50))

        ok_single = await _execute_seamless_concat(final_inputs, single_output, temp_dir, loop)
        if ok_single and os.path.exists(single_output):
            actual_size = os.path.getsize(single_output)
            if actual_size <= max_split_bytes and actual_size > 50 * 1024:
                # Succeeded in 1 file! No split needed!
                merge_time = time.time() - t0_merge
                actual_mb = actual_size / (1024 * 1024)
                logger.info(
                    f"✅ Unified merge successful in 1 single file: {base_filename} "
                    f"({actual_mb:.1f} MB <= limit {max_split_bytes / (1024*1024):.1f} MB)"
                )
                await _maybe_await(
                    status_cb,
                    messages.msg_merging_complete(title, f"{actual_mb:.1f} MB", merge_time)
                )
                return MergeResult(
                    parts=[single_output],
                    part_sizes_mb=[actual_mb],
                    watermark_time_sec=wm_time,
                    merge_time_sec=merge_time,
                    watermarked_episodes=wm_indices,
                )
            else:
                logger.info(
                    f"Unified file ({actual_size / (1024*1024):.1f} MB) exceeds Telegram limit "
                    f"({max_split_bytes / (1024*1024):.1f} MB). Proceeding with multi-chunk split..."
                )
                try:
                    os.remove(single_output)
                except Exception:
                    pass

        # Step 4b: Multi-file chunking fallback if unified file genuinely exceeds limit
        input_sizes = [os.path.getsize(p) if os.path.exists(p) else 0 for p in final_inputs]
        total_size = sum(input_sizes)

        chunks = []
        chunk_indices = []

        curr_chunk = []
        curr_idxs = []
        curr_bytes = 0

        for idx, (path, sz) in enumerate(zip(final_inputs, input_sizes)):
            if curr_chunk and (curr_bytes + sz > max_split_bytes):
                chunks.append(curr_chunk)
                chunk_indices.append(curr_idxs)
                curr_chunk = [path]
                curr_idxs = [idx]
                curr_bytes = sz
            else:
                curr_chunk.append(path)
                curr_idxs.append(idx)
                curr_bytes += sz

        if curr_chunk:
            chunks.append(curr_chunk)
            chunk_indices.append(curr_idxs)

        total_chunks = len(chunks)
        logger.info(
            f"📦 Splitting into {total_chunks} chunk(s) (Limit: {max_split_bytes / (1024*1024):.1f} MB)"
        )

        # 5. Concat-merges each chunk
        parts = []
        part_sizes = []

        for c_idx, (chunk_files, chunk_idxs) in enumerate(zip(chunks, chunk_indices), 1):
            if episode_numbers and len(episode_numbers) == total_eps:
                chunk_first_ep = episode_numbers[chunk_idxs[0]]
                chunk_last_ep = episode_numbers[chunk_idxs[-1]]
            else:
                chunk_first_ep = extract_ep_num_from_path(chunk_files[0], default=chunk_idxs[0] + 1)
                chunk_last_ep = extract_ep_num_from_path(chunk_files[-1], default=chunk_idxs[-1] + 1)

            if total_chunks == 1:
                chunk_filename = base_filename
            else:
                chunk_filename = build_chunk_filename(base_filename, chunk_first_ep, chunk_last_ep, naming_params)

            chunk_output = os.path.join(output_dir, chunk_filename)

            if status_cb:
                if total_chunks > 1:
                    status_msg = messages.msg_progress_card(
                        title=title, pct=(c_idx - 1) / total_chunks * 100.0,
                        status=f"Merging (file {c_idx}/{total_chunks}: E{chunk_first_ep:02d}-E{chunk_last_ep:02d})",
                        done_str=f"{c_idx - 1}/{total_chunks} files", total_str=f"{total_chunks} files", speed_str="--")
                else:
                    status_msg = messages.msg_merging_progress(title, total_eps, 50)
                await _maybe_await(status_cb, status_msg)

            ok_chunk = await _execute_seamless_concat(chunk_files, chunk_output, temp_dir, loop)
            if not ok_chunk or not os.path.exists(chunk_output):
                logger.error(f"FFmpeg concat failed for chunk {c_idx}")
                return None

            file_sz = os.path.getsize(chunk_output)
            parts.append(chunk_output)
            part_sizes.append(file_sz / (1024 * 1024))
            logger.info(f"✅ Created merged chunk {c_idx}/{total_chunks}: {chunk_filename} ({file_sz / (1024*1024):.1f} MB)")

        merge_time = time.time() - t0_merge

        total_mb_str = f"{sum(part_sizes):.1f} MB"
        if total_chunks > 1:
            total_mb_str += f" ({total_chunks} files)"

        await _maybe_await(
            status_cb,
            messages.msg_merging_complete(title, total_mb_str, merge_time)
        )

        return MergeResult(
            parts=parts,
            part_sizes_mb=part_sizes,
            watermark_time_sec=wm_time,
            merge_time_sec=merge_time,
            watermarked_episodes=wm_indices,
        )
    except Exception as e:
        logger.exception(f"❌ CRITICAL FATAL ERROR in merge_and_split for '{title}': {e}")
        if status_cb:
            try:
                err_text = str(e)[:150]
                await _maybe_await(status_cb, f"❌ **Merge failed:** `{err_text}`")
            except Exception:
                pass
        return None


# ─────────────────────────────────────────────────────────────────────────────
# High-Level Adapters for PrimeX Auto-DL Engine
# ─────────────────────────────────────────────────────────────────────────────
async def merge_video_episodes(
    ep_files: List[str],
    out_dir: str,
    show_title: str,
    language: str = "Hindi",
    quality: str = "1080p",
    watermark_enabled: bool = True,
    watermark_text: Optional[str] = None,
    status_cb: Optional[Callable] = None,
    source_tag: str = "KUKU",
) -> Tuple[Optional[str], int]:
    """Combines video episodes into master release using PrimeXMasoom's merge_and_split engine."""
    if not ep_files:
        return None, 0
    first_ep = 1
    last_ep = len(ep_files)
    client_tag = getattr(cfg, "RELEASE_TAG", "@PrimeXDramaBot")
    source_tag = (source_tag or "KUKU").upper()

    tags = naming.detect_stream_tags(ep_files[0])
    codec = tags.get("video_codec", "H.264")
    real_quality = naming.quality_label(tags.get("width", 0), tags.get("height", 0)) if (tags.get("width") and tags.get("height")) else quality

    release_filename = naming.build_release_filename(
        show_title=show_title,
        source_tag=source_tag,
        release_tag=client_tag,
        tags=tags,
        season_no=1,
        first_ep=first_ep,
        last_ep=last_ep,
        lang=language.title(),
        custom_quality=real_quality,
        container_ext="mp4",
        video_codec=codec,
    )

    naming_params = {
        "show_title": show_title,
        "source_tag": source_tag,
        "release_tag": client_tag,
        "tags": tags,
        "season_no": 1,
        "lang": language.title(),
        "custom_quality": real_quality,
        "container_ext": "mp4",
        "video_codec": codec,
    }

    temp_dir = os.path.join(out_dir, "temp_merge")
    os.makedirs(temp_dir, exist_ok=True)

    wm_active = watermark_enabled and getattr(cfg, "ENABLE_WATERMARK", True)
    wm_txt = watermark_text or getattr(cfg, "WATERMARK_TEXT", None)

    merge_res = await merge_and_split(
        episode_files=ep_files,
        output_dir=out_dir,
        base_filename=release_filename,
        temp_dir=temp_dir,
        title=show_title,
        watermark_enabled=wm_active,
        watermark_text=wm_txt,
        status_cb=status_cb,
        naming_params=naming_params,
    )
    if merge_res and merge_res.parts:
        part = merge_res.parts[0]
        meta = naming.probe_video_metadata(part)
        dur = int(meta.get("duration", 0))
        return part, dur
    return None, 0


async def merge_audio_episodes(
    ep_files: List[str],
    out_dir: str,
    show_title: str,
    language: str = "Hindi",
    cover_art_path: Optional[str] = None,
    inject_watermark: bool = True,
) -> Tuple[Optional[str], Optional[str]]:
    """Combines audiobook episodes with embedded cover art and audio watermark injection."""
    from kuku_audio_downloader import merge_kuku_audio_episodes
    if not ep_files:
        return None, None
    first_ep = 1
    last_ep = len(ep_files)
    client_tag = getattr(cfg, "RELEASE_TAG", "PrimeXOfficial")
    chunk_range = f"E{first_ep:02d}-E{last_ep:02d}"
    out_filename = f"{show_title} S01 [{chunk_range}] COMBiNED KUKU WEB-DL {language.title()} AudioBook - {client_tag}.mp3"
    clean_filename = "".join(c for c in out_filename if c.isalnum() or c in (" ", "_", "-", ".", "[", "]", "(", ")")).strip()
    out_path = os.path.join(out_dir, clean_filename)

    wm_author = getattr(cfg, "WATERMARK_TEXT", None) or client_tag
    merged_path, wm_time = await merge_kuku_audio_episodes(
        ep_files=ep_files,
        out_path=out_path,
        title=f"{show_title} S01 [{chunk_range}]",
        artist=wm_author,
        album=show_title,
        cover_art_path=cover_art_path,
        inject_watermark=inject_watermark,
    )
    return merged_path, wm_time

