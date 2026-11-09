#!/usr/bin/env python3
"""
High-Speed Animated Moving Watermark Engine for PrimeXMasoom.
Direct port from dashreels_bot:
- Slow sine-wave drift moving watermark (x='(W-w)*abs(sin(0.035*t))', y='(H-h)*abs(sin(0.025*t))').
- Clean transparent PNG with crisp semi-transparent text (fill=(255, 255, 255, 230)).
- VBV bitrate capping to source bitrate to prevent player stutter.
- Matches source profile/level/pix_fmt for lossless MPEG-TS concat.
"""

import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

import watermark_config as wm_cfg
from logger import logger

_WM_SEMAPHORE = threading.Semaphore(4)

# Running watermark encodes, so a cancelled task can stop its ffmpeg processes immediately
# (threads cannot be cancelled; without this the encodes kept burning CPU after /cancel).
_ACTIVE: dict = {}
_KILLED: set = set()
_ACTIVE_LOCK = threading.Lock()


def _run_encode(cmd: list, timeout: float, output_path: str) -> subprocess.CompletedProcess:
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        preexec_fn=_lower_priority if os.name == "posix" else None,
    )
    with _ACTIVE_LOCK:
        _ACTIVE[output_path] = proc
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE.pop(output_path, None)
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def kill_watermark(output_path: str) -> None:
    """Stops the running watermark encode that writes `output_path` (task cancelled)."""
    with _ACTIVE_LOCK:
        proc = _ACTIVE.get(output_path)
        if proc is not None and proc.poll() is None:
            _KILLED.add(output_path)
            try:
                proc.kill()
            except Exception:
                pass


_TRC_ALIASES = {"bt470m": "gamma22", "bt470bg": "gamma28"}
_VALID = {
    "range": {"tv", "pc", "mpeg", "jpeg"},
    "space": {"bt709", "fcc", "bt470bg", "smpte170m", "smpte240m", "ycgco", "bt2020nc", "bt2020c", "gbr"},
    "trc": {"bt709", "gamma22", "gamma28", "smpte170m", "smpte240m", "linear", "log100", "log316",
            "iec61966-2-4", "bt1361e", "iec61966-2-1", "bt2020-10", "bt2020-12", "smpte2084", "arib-std-b67"},
    "prim": {"bt709", "bt470m", "bt470bg", "smpte170m", "smpte240m", "film", "bt2020", "smpte428", "smpte431", "smpte432"},
}


def _color_args(meta: dict) -> list:
    args = []
    rng = str(meta.get("color_range") or "")
    spc = str(meta.get("color_space") or "")
    trc = _TRC_ALIASES.get(str(meta.get("color_transfer") or ""), str(meta.get("color_transfer") or ""))
    prm = str(meta.get("color_primaries") or "")
    if rng in _VALID["range"]:
        args += ["-color_range", rng]
    if spc in _VALID["space"]:
        args += ["-colorspace", spc]
    if trc in _VALID["trc"]:
        args += ["-color_trc", trc]
    if prm in _VALID["prim"]:
        args += ["-color_primaries", prm]
    return args


def _lower_priority():
    """Run heavy encodes at lower CPU priority so downloads & the bot event loop never starve."""
    try:
        os.nice(5)
    except Exception:
        pass


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


def _find_font(size: int):
    """Finds available TrueType font on macOS and Linux systems."""
    from PIL import ImageFont

    candidates = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Supplemental/Verdana Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    ]
    for fp in candidates:
        if os.path.exists(fp):
            try:
                return ImageFont.truetype(fp, size)
            except Exception:
                continue
    try:
        return ImageFont.load_default(size=size)
    except Exception:
        return ImageFont.load_default()


def probe_source_video(path: str) -> dict:
    """Probes resolution, profile, level, pix_fmt, color properties, fps, timescale, and audio codec to match encode properties."""
    params = {
        "width": 720,
        "height": 1280,
        "profile": "High",
        "level": "",
        "pix_fmt": "yuv420p",
        "fps": 24.0,
        "timescale": 90000,
        "color_range": "",
        "color_space": "",
        "color_primaries": "",
        "color_transfer": "",
        "a_codec": "",
        "v_bitrate": 0,
        "a_bitrate": 0,
        "duration": 0.0,
    }
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-show_streams", "-show_format",
            "-of", "json", path,
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            data = json.loads(r.stdout)
            for s in data.get("streams", []):
                if s.get("codec_type") == "video" and not params.get("_v_found"):
                    params["_v_found"] = True
                    params["v_codec"] = s.get("codec_name", "") or ""
                    try:
                        params["v_bitrate"] = int(s.get("bit_rate") or 0)
                    except (TypeError, ValueError):
                        pass
                    params["width"] = int(s.get("width", params["width"]))
                    params["height"] = int(s.get("height", params["height"]))
                    if s.get("profile"):
                        params["profile"] = s["profile"]
                    if s.get("level") is not None:
                        params["level"] = str(s["level"])
                    if s.get("pix_fmt"):
                        params["pix_fmt"] = s["pix_fmt"]
                    if s.get("color_range"):
                        params["color_range"] = s["color_range"]
                    if s.get("color_space"):
                        params["color_space"] = s["color_space"]
                    if s.get("color_primaries"):
                        params["color_primaries"] = s["color_primaries"]
                    if s.get("color_transfer"):
                        params["color_transfer"] = s["color_transfer"]
                    tb = s.get("time_base", "")
                    if "/" in tb:
                        try:
                            num, den = tb.split("/")
                            if float(num) > 0 and float(den) > 0:
                                params["timescale"] = int(round(float(den) / float(num)))
                        except (ValueError, ZeroDivisionError):
                            pass
                    r_fr = s.get("r_frame_rate", "0/0")
                    if "/" in r_fr:
                        try:
                            num, den = r_fr.split("/")
                            if float(den) > 0:
                                params["fps"] = round(float(num) / float(den), 2)
                        except (ValueError, ZeroDivisionError):
                            pass
                elif s.get("codec_type") == "audio" and not params["a_codec"]:
                    params["a_codec"] = s.get("codec_name", "")
                    try:
                        params["a_bitrate"] = int(s.get("bit_rate") or 0)
                    except (TypeError, ValueError):
                        pass
            fmt = data.get("format") or {}
            try:
                params["duration"] = float(fmt.get("duration") or 0)
            except (TypeError, ValueError):
                pass
            if not params["v_bitrate"]:
                # TS / some MP4s have no per-stream bitrate: derive from container
                try:
                    total_br = int(fmt.get("bit_rate") or 0)
                    if not total_br and params["duration"] > 0:
                        total_br = int(os.path.getsize(path) * 8 / params["duration"])
                    params["v_bitrate"] = max(0, total_br - (params["a_bitrate"] or 128000))
                except (TypeError, ValueError, OSError):
                    pass
    except Exception as e:
        logger.warning(f"probe_source_video failed for {path}: {e}")
    return params


def generate_watermark_png(text: str, video_width: int, video_height: int, out_png: str) -> bool:
    """
    Renders soft, anti-aliased semi-transparent PNG text overlay with
    Gaussian drop-shadow for buttery-smooth glide without pixel stepping.
    Font size dynamically adapts to the smaller video dimension (works for vertical & horizontal).
    """
    from PIL import Image, ImageDraw, ImageFilter

    base_dim = min(video_width, video_height) if (video_width and video_height) else (video_width or 720)
    fsize = max(18, min(48, int(base_dim * 0.040)))
    font = _find_font(fsize)

    dummy = Image.new("RGBA", (1, 1))
    bbox = ImageDraw.Draw(dummy).textbbox((0, 0), text, font=font)
    w = bbox[2] - bbox[0]
    h = bbox[3] - bbox[1]

    pad = 20
    img_w = w + pad * 2
    img_h = h + pad * 2

    # 1. Base shadow layer with gentle Gaussian blur for anti-aliased edges
    shadow = Image.new("RGBA", (img_w, img_h), (0, 0, 0, 0))
    s_draw = ImageDraw.Draw(shadow)
    ox = pad - bbox[0]
    oy = pad - bbox[1]

    s_draw.text((ox, oy), text, font=font, fill=(0, 0, 0, 190))
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=2.2))

    # 2. Main layer with semi-transparent crisp white core
    img = Image.new("RGBA", (img_w, img_h), (0, 0, 0, 0))
    img.alpha_composite(shadow)
    draw = ImageDraw.Draw(img)
    draw.text((ox, oy), text, font=font, fill=(255, 255, 255, 220))

    img.save(out_png, "PNG")
    return True


def apply_watermark_to_file(
    input_path: str,
    output_path: str,
    text: Optional[str] = None,
    font_path: Optional[str] = None,
    target_fps: Optional[float] = None,
    target_timescale: Optional[int] = None,
    progress_file: Optional[str] = None,
) -> bool:
    """
    Applies calm, cinematic harmonic Lissajous drift watermark with eval=frame
    for seamless, buttery-smooth floating motion without harsh pixel jumps or playback stutter.
    Maintains 100% concat merge compatibility (matching timescale, profile, level, color space, and b-frames).
    """
    text = text or wm_cfg.DEFAULT_WATERMARK_TEXT
    meta = probe_source_video(input_path)

    w_vid = meta.get("width", 720)
    h_vid = meta.get("height", 1280)
    timescale = target_timescale or meta.get("timescale") or 90000

    png_path = f"{output_path}.wm_{os.getpid()}_{int(time.time()*1000)}.png"
    try:
        generate_watermark_png(text, w_vid, h_vid, png_path)
    except Exception as e:
        logger.exception(f"❌ Failed to generate watermark PNG for '{text}': {e}")
        return False

    # Fully dynamic margins: 5% of width and 7% of height, clamped safely
    margin_x = max(16, int(w_vid * 0.05))
    margin_y = max(30, int(h_vid * 0.07))

    # Smooth harmonic sinusoidal drift (eval=frame calculates per frame for 100% fluid glide)
    filter_expr = (
        f"[0:v][1:v]overlay="
        f"x='max({margin_x}, (W-w-{margin_x*2})*(sin(0.022*t)+1)/2 + {margin_x})':"
        f"y='max({margin_y}, (H-h-{margin_y*2})*(cos(0.016*t)+1)/2 + {margin_y})':"
        f"eval=frame:format=yuv420[v_wm]"
    )

    preset_val = getattr(wm_cfg, "WATERMARK_X264_PRESET", "superfast")
    crf_val = str(getattr(wm_cfg, "WATERMARK_CRF", 20))
    # Size guard: cap the encode at ~source bitrate so a watermarked episode is about the same
    # size as the original (previously CRF-only output could be 2-5x bigger than the source).
    src_vbr = int(meta.get("v_bitrate") or 0)
    cap_ratio = float(getattr(wm_cfg, "WATERMARK_BITRATE_CAP_RATIO", 1.05))
    rate_args: list = []
    if src_vbr > 100_000 and cap_ratio > 0:
        rate_args = ["-maxrate", str(int(src_vbr * cap_ratio)), "-bufsize", str(int(src_vbr * 2))]

    cmd = [
        "ffmpeg", "-y",
        *(["-progress", progress_file, "-nostats"] if progress_file else []),
        "-fflags", "+genpts",
        "-i", input_path,
        "-i", png_path,
        "-filter_complex", filter_expr,
        "-map", "[v_wm]",
        "-map", "0:a?",
    ]

    # Encode in the SAME codec as the source episode: a watermarked episode is joined with the
    # untouched ones by stream copy, so an HEVC show must stay HEVC (an H.264 episode inside an HEVC
    # track breaks playback). HEVC sources previously failed outright (their level, e.g. 93 = 3.1,
    # was passed to x264 as "9.3") and the watermark was silently skipped.
    is_hevc = str(meta.get("v_codec", "")).lower() in ("hevc", "h265")
    if is_hevc:
        cmd += [
            "-c:v", "libx265",
            "-preset", preset_val,
            "-crf", str(int(crf_val) + 4),       # x265 CRF ~ x264 CRF + 4 for similar quality
            *rate_args,
            "-tag:v", "hvc1",
            "-x265-params", "log-level=error",
            "-pix_fmt", meta.get("pix_fmt", "yuv420p"),
            "-vsync", "0",
        ]
    else:
        cmd += [
            "-c:v", "libx264",
            "-preset", preset_val,
            "-crf", crf_val,
            *rate_args,
            "-threads", "0",
            "-pix_fmt", meta.get("pix_fmt", "yuv420p"),
            "-vsync", "0",
        ]
        # Match H.264 profile / level (H.264-style levels only: "40" -> 4.0)
        raw_profile = str(meta.get("profile", "")).lower().replace(" ", "")
        if "high" in raw_profile:
            cmd += ["-profile:v", "high10" if "10" in raw_profile else "high"]
        elif "main" in raw_profile:
            cmd += ["-profile:v", "main"]
        elif "baseline" in raw_profile:
            cmd += ["-profile:v", "baseline"]
        else:
            cmd += ["-profile:v", "high"]
        raw_level = str(meta.get("level", "")).strip()
        if raw_level.isdigit():
            if len(raw_level) == 2 and raw_level != "0":
                cmd += ["-level", f"{raw_level[0]}.{raw_level[1]}"]
            elif len(raw_level) == 1:
                cmd += ["-level", f"{raw_level}.0"]

    # Match color space, range, and transfer if available (only values libx264 accepts;
    # e.g. ffprobe reports transfer "bt470m" but the encoder option name is "gamma22").
    color_args = _color_args(meta)
    cmd += color_args

    # Container & Audio matching:
    is_ts = output_path.endswith(".ts")
    if is_ts:
        cmd += [
            "-c:a", "copy",
            "-bsf:v", "hevc_mp4toannexb" if is_hevc else "h264_mp4toannexb",
            "-f", "mpegts",
            output_path,
        ]
    else:
        cmd += ["-c:a", "copy"]
        if meta.get("a_codec") == "aac":
            cmd += ["-bsf:a", "aac_adtstoasc"]
        cmd += [
            "-video_track_timescale", str(timescale),
            "-avoid_negative_ts", "make_zero",
            "-movflags", "+faststart",
            output_path,
        ]

    with _WM_SEMAPHORE:
        t0_wm = time.time()
        logger.info(f"🎨 Watermarking start: {os.path.basename(input_path)} -> {os.path.basename(output_path)} ({'x265' if is_hevc else 'x264'}, CRF={int(crf_val) + 4 if is_hevc else crf_val}, preset={preset_val}, cap={int(src_vbr * cap_ratio / 1000) if rate_args else 'none'}kbps, {w_vid}x{h_vid})")
        try:
            dur = float(meta.get("duration") or 0)
            enc_timeout = max(600, int(dur * 4)) if dur > 0 else 3600
            r = _run_encode(cmd, enc_timeout, output_path)
            if output_path in _KILLED:
                _KILLED.discard(output_path)
                logger.info(f"🛑 Watermark for {os.path.basename(input_path)} stopped (task cancelled)")
                return False
            color_rejected = "color" in (r.stderr or "").lower() and "error" in (r.stderr or "").lower()
            if r.returncode != 0 and color_args and color_rejected:
                # Some sources carry color tags the encoder rejects: retry without them.
                logger.warning(f"⚠️ Watermark encode rejected color tags {color_args} for {os.path.basename(input_path)}; retrying without them")
                cmd_nc = [a for a in cmd]
                for i in range(0, len(color_args), 2):
                    k = color_args[i]
                    if k in cmd_nc:
                        j = cmd_nc.index(k)
                        del cmd_nc[j:j + 2]
                r = _run_encode(cmd_nc, enc_timeout, output_path)
                if output_path in _KILLED:
                    _KILLED.discard(output_path)
                    return False
            if os.path.exists(png_path):
                try:
                    os.remove(png_path)
                except Exception:
                    pass

            if r.returncode != 0 or not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
                err_snip = _clean_ffmpeg_error(r.stderr) if r and r.stderr else "Unknown FFmpeg error"
                logger.error(f"❌ Watermark overlay failed for {input_path} (returncode: {r.returncode if r else 'None'}):\n{err_snip}")
                return False

            dur_sec = time.time() - t0_wm
            out_mb = os.path.getsize(output_path) / (1024 * 1024)
            in_mb = os.path.getsize(input_path) / (1024 * 1024)
            logger.info(f"✅ Watermark finished: {os.path.basename(output_path)} ({in_mb:.1f} MB -> {out_mb:.1f} MB) in {dur_sec:.2f}s")
            return True
        except Exception as e:
            logger.exception(f"❌ Watermark exception for {input_path}: {e}")
            if os.path.exists(png_path):
                try:
                    os.remove(png_path)
                except Exception:
                    pass
            return False


def apply_video_watermark(input_mp4: str, output_mp4: str, watermark_text: Optional[str] = None) -> bool:
    """Wrapper that calls PrimeXMasoom's apply_watermark_to_file engine."""
    from bot_config import WATERMARK_TEXT
    txt = watermark_text or WATERMARK_TEXT or wm_cfg.DEFAULT_WATERMARK_TEXT
    return apply_watermark_to_file(input_path=input_mp4, output_path=output_mp4, watermark_text=txt)


def get_audio_duration(file_path: str) -> float:
    """Returns duration in seconds using ffprobe."""
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "csv=p=0", file_path
        ]
        out = subprocess.check_output(cmd, text=True, timeout=10).strip()
        return float(out) if out else 0.0
    except Exception:
        return 0.0


def format_seconds_to_hhmmss(seconds: float) -> str:
    """Formats seconds into [HH:MM:SS]."""
    total_sec = int(round(seconds))
    h = total_sec // 3600
    m = (total_sec % 3600) // 60
    s = total_sec % 60
    return f"[{h:02d}:{m:02d}:{s:02d}]"

