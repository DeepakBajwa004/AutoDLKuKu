#!/usr/bin/env python3
"""
Naming and Video Metadata Probe Engine for PrimeXMasoom.
Uses ffprobe to truthfully detect stream parameters (resolution, codecs, channels).
"""

import html
import json
import logging
import re
import subprocess
from typing import Dict, Optional

logger = logging.getLogger("primex.naming")

_CLEAN_RE = re.compile(r'[\\/*?%*:|"<>~#]')
_MULTI_SPACE_RE = re.compile(r"[\s\-_.:|/]+")


def probe_video_metadata(path: str) -> Dict[str, float]:
    """
    Returns {'duration': int, 'width': int, 'height': int} of a video file via ffprobe.
    """
    out = {"duration": 0, "width": 1280, "height": 720}
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration:stream=width,height",
            "-of", "json", path,
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            return out
        data = json.loads(r.stdout)
        fmt = data.get("format", {})
        if fmt.get("duration"):
            out["duration"] = int(float(fmt["duration"]))
        for s in data.get("streams", []):
            if s.get("width") and s.get("height"):
                out["width"] = int(s["width"])
                out["height"] = int(s["height"])
                break
    except Exception as e:
        logger.warning(f"probe_video_metadata failed for {path}: {e}")
    return out


def detect_stream_tags(sample_path: str) -> Dict[str, str]:
    """
    Probes an episode or video file and returns truthful branding tags:
    {video_codec, width, height, audio_codec, channels}
    """
    out = {"video_codec": "H.264", "width": 1280, "height": 720, "audio_codec": "AAC", "channels": "2.0"}
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "stream=codec_type,codec_name,width,height,channels",
            "-of", "json", sample_path,
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            return out
        data = json.loads(r.stdout)
        for s in data.get("streams", []):
            if s.get("codec_type") == "video":
                cname = (s.get("codec_name") or "").lower()
                out["video_codec"] = {
                    "h264": "H.264", "hevc": "H.265", "h265": "H.265", "vp9": "VP9", "av1": "AV1"
                }.get(cname, cname.upper() or "H.264")
                out["width"] = int(s.get("width") or out["width"])
                out["height"] = int(s.get("height") or out["height"])
            elif s.get("codec_type") == "audio":
                cname = (s.get("codec_name") or "").lower()
                out["audio_codec"] = {
                    "aac": "AAC", "mp3": "MP3", "opus": "OPUS", "ac3": "AC3", "eac3": "EAC3"
                }.get(cname, cname.upper() or "AAC")
                ch = s.get("channels")
                out["channels"] = "5.1" if ch == 6 else ("2.0" if ch == 2 else ("1.0" if ch == 1 else f"{ch}ch" if ch else "2.0"))
    except Exception as e:
        logger.warning(f"detect_stream_tags failed for {sample_path}: {e}")
    return out


def clean_title(raw_title: str) -> str:
    """Sanitizes show titles for filenames and captions."""
    t = html.unescape(raw_title or "Show")
    t = _CLEAN_RE.sub("", t)
    t = _MULTI_SPACE_RE.sub(" ", t)
    return " ".join(t.split()).strip() or "Show"


def quality_label(width: int, height: int) -> str:
    short_side = min(width, height) if width and height else 720
    if short_side >= 1080:
        return "1080p"
    if short_side >= 720:
        return "720p"
    if short_side >= 480:
        return "480p"
    return f"{short_side}p"


def extract_title_and_season(raw_title: str, default_season: int = 1) -> tuple[str, int]:
    """
    Detects and cleanly extracts season tag from title:
    e.g. 'The Viral Scandal S01' -> ('The Viral Scandal', 1)
    Prevents duplicate season tags (e.g. 'The Viral Scandal S01 S01').
    """
    t = clean_title(raw_title)
    m = re.search(r"^(.*?)(?:[\s\-_.:]+[Ss](?:eason\s*)?0*(\d+))+$", t, re.I)
    if m:
        detected_title = m.group(1).strip()
        detected_season = int(m.group(2))
        final_season = detected_season if default_season == 1 else default_season
        return detected_title, final_season
    return t, default_season


def build_release_filename(
    show_title: str,
    source_tag: str,
    release_tag: str,
    tags: Dict[str, str],
    season_no: int = 1,
    first_ep: int = 1,
    last_ep: int = 1,
    lang: str = "English",
    part_idx: Optional[int] = None,
    total_parts: int = 1,
    custom_quality: Optional[str] = None,
    container_ext: str = "mp4",
    video_codec: Optional[str] = None,
) -> str:
    """
    Combined Release Filename:
    Example: Jinn Ki Dulhan S02 [E01-E395] COMBiNED 720p STORYTV WEB-DL Hindi AAC.2.0 H.264 - PrimeXOfficial.mp4
    """
    title, season_no = extract_title_and_season(show_title, season_no)
    title = re.sub(r'[/\\*?\"<>|:]', '', title).strip() or "Show"
    qlabel = custom_quality or quality_label(tags.get("width", 1280), tags.get("height", 720))
    part_str = f" - Part{part_idx}" if (total_parts > 1 and part_idx) else ""
    ext = re.sub(r'[/\\*?\"<>|:]', '', (container_ext or "mp4").lower().strip().lstrip(".")) or "mp4"
    rtag = (release_tag or "PrimeXOfficial").strip()
    if "http://" in rtag.lower() or "https://" in rtag.lower():
        m_domain = re.search(r"https?://(?:www\.)?([^/]+)", rtag, re.I)
        rtag = m_domain.group(1) if m_domain else "PrimeXOfficial"
    rtag = re.sub(r'[/\\*?\"<>|:]', '', rtag).strip() or "PrimeXOfficial"
    return (
        f"{title} S{season_no:02d} [E{first_ep:02d}-E{last_ep:02d}] COMBiNED "
        f"{qlabel} {source_tag.upper()} WEB-DL {lang} "
        f"AAC.2.0 {video_codec or 'H.264'} - {rtag}{part_str}.{ext}"
    )


def clean_episode_title(title: Optional[str], ep_no: int) -> str:
    """Cleans episode title to dot-separated format, excluding generic 'Episode 14' labels."""
    if not title:
        return ""
    t = str(title).strip()
    # Check if title is merely "Episode 14", "Ep 14", "EP_014", or just numbers
    clean_check = re.sub(r"^(?:ep|e|episode)?[_\s-]*0*(\d+)$", "", t, flags=re.I).strip()
    if not clean_check:
        return ""
    # Remove leading episode number if present, e.g. "14. Collab Week" or "14 Collab Week"
    t = re.sub(r"^(?:ep|e|episode)?[_\s-]*0*" + str(ep_no) + r"[\s.:_-]+", "", t, flags=re.I).strip()
    # Replace only strictly illegal filesystem characters (preserves ;, &, !, etc.)
    safe_chars = re.sub(r'[/\\*?\"<>|:]', ' ', t).strip(' .')
    dot_name = ".".join(safe_chars.split())
    dot_name = re.sub(r"\.{2,}", ".", dot_name).strip(".")
    return dot_name


def build_single_episode_filename(
    show_title: str,
    source_tag: str,
    release_tag: str,
    season_no: int = 1,
    ep_no: int = 1,
    quality: str = "720p",
    lang: str = "English",
    container_ext: str = "mp4",
    episode_title: Optional[str] = None,
    video_codec: Optional[str] = None,
) -> str:
    """
    Single Episode Dot-Separated Filename with optional episode name:
    Example without name: Jinn.Ki.Dulhan.S02E01.720p.STORYTV.WEB-DL.Hindi.AAC.2.0.H.264-PrimeXOfficial.mp4
    Example with name: I.Popstar.Vol.2.S01E14.Collab.Week;.The.Road.to.Finale.360p.ALRIGHT.WEB-DL.Hindi.AAC.2.0.H.264-PrimeXDramaBot.mp4
    """
    clean_t, season_no = extract_title_and_season(show_title, season_no)
    dot_title = ".".join(clean_t.split())
    ext = (container_ext or "mp4").lower().strip().lstrip(".") or "mp4"
    rtag = (release_tag or "PrimeXOfficial").strip()
    dot_ep_title = clean_episode_title(episode_title, ep_no)
    ep_suffix = f".{dot_ep_title}" if dot_ep_title else ""
    return (
        f"{dot_title}.S{season_no:02d}E{ep_no:02d}{ep_suffix}.{quality}.{source_tag.upper()}.WEB-DL.{lang}.AAC.2.0.{video_codec or 'H.264'}-{rtag}.{ext}"
    )


def build_movie_filename(
    movie_title: str,
    release_year: Optional[str] = None,
    source_tag: str = "STAGE",
    release_tag: str = "PrimeXOfficial",
    quality: str = "720p",
    lang: str = "Hindi",
    container_ext: str = "mp4",
) -> str:
    """
    Movie Dot-Separated Filename with release year:
    Example: Saankal.2026.1440p.STAGE.WEB-DL.haryanvi.AAC.2.0.H.264-PrimeXOfficial.mkv
    """
    clean_t = clean_title(movie_title)
    # Check if year is already inside movie_title (e.g. "Saankal 2026")
    m_yr = re.search(r"\b(19\d\d|20\d\d)\b", clean_t)
    if m_yr:
        year_str = m_yr.group(1)
        clean_t = re.sub(r"\b(19\d\d|20\d\d)\b", "", clean_t).strip(" .-_")
    else:
        y_val = str(release_year or "").strip()
        m_y2 = re.search(r"\b(19\d\d|20\d\d)\b", y_val)
        year_str = m_y2.group(1) if m_y2 else ""

    dot_title = ".".join(clean_t.split())
    ext = (container_ext or "mp4").lower().strip().lstrip(".") or "mp4"
    rtag = (release_tag or "PrimeXOfficial").strip()

    if year_str:
        return f"{dot_title}.{year_str}.{quality}.{source_tag.upper()}.WEB-DL.{lang}.AAC.2.0.H.264-{rtag}.{ext}"
    else:
        return f"{dot_title}.{quality}.{source_tag.upper()}.WEB-DL.{lang}.AAC.2.0.H.264-{rtag}.{ext}"


def format_ranges(nums: list) -> str:
    """
    Formats a list of episode numbers into human-readable compact ranges.
    e.g. [1, 2, 3, 4, 5, 8, 9, 10, 15] -> "1-5, 8-10, 15"
    """
    clean = []
    for x in (nums or []):
        try:
            clean.append(int(x))
        except (ValueError, TypeError):
            pass
    if not clean:
        return ""
    sorted_unique = sorted(set(clean))
    ranges = []
    start = sorted_unique[0]
    prev = sorted_unique[0]
    for n in sorted_unique[1:]:
        if n == prev + 1:
            prev = n
        else:
            ranges.append(f"{start}-{prev}" if start != prev else f"{start}")
            start = n
            prev = n
    ranges.append(f"{start}-{prev}" if start != prev else f"{start}")
    return ", ".join(ranges)


