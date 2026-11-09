#!/usr/bin/env python3
"""
FastDL — shared high-speed download engine for PrimeXMasoom.

Why this exists
---------------
Earlier every platform used its own mix of yt-dlp / ffmpeg / aria2c / ad-hoc aiohttp.
When the primary engine failed, downloads silently fell back to single-connection
ffmpeg (1-8 MB/s from Germany -> Indian CDNs). This module gives one in-process
engine that:

* Downloads HLS (TS or fMP4, EXT-X-MAP, EXT-X-BYTERANGE, AES-128, separate audio
  renditions) with many parallel segment connections.
* Downloads direct files (.mp4/.mp3/...) with parallel HTTP Range pieces
  (work-stealing queue, per-piece resume + retry).
* Shares ONE connection pool across all users/jobs of this bot process so the CDN
  is never flooded (global + per-host caps), while each job keeps its own cookies.
* Uses stall timeouts (no data for N seconds) instead of whole-file timeouts, so
  big files never get killed just because they are big.
* Reports a real sliding-window speed (bytes actually received in the last few
  seconds), not "finished-episode bytes / elapsed".
* Never blocks the event loop (ffprobe/ffmpeg run as async subprocesses).

Tuning (optional env vars, safe defaults):
    FASTDL_MAX_CONN        total sockets for this bot process      (default 256)
    FASTDL_PER_HOST        sockets per CDN host                    (default 128)
    FASTDL_SEG_WORKERS     parallel segments/pieces, single file   (default 96)
    FASTDL_MULTI_SEG_WORKERS  per file when many episodes run      (default 24)
    FASTDL_STALL_SEC       abort a request if no bytes for N sec   (default 25)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import socket
import time
import urllib.parse
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

import aiohttp

import task_stats

logger = logging.getLogger("primex.fastdl")

MAX_CONN = int(os.environ.get("FASTDL_MAX_CONN", "256") or 256)
PER_HOST = int(os.environ.get("FASTDL_PER_HOST", "128") or 128)
SEG_WORKERS = int(os.environ.get("FASTDL_SEG_WORKERS", "96") or 96)          # single file / single episode
# Per file when many episodes run together. Workers beyond the socket cap simply queue on the shared
# pool, so the last episodes of a batch still get plenty of connections (no slow tail).
MULTI_SEG_WORKERS = int(os.environ.get("FASTDL_MULTI_SEG_WORKERS", "24") or 24)
STALL_SEC = float(os.environ.get("FASTDL_STALL_SEC", "25") or 25)
# 4 = IPv4 only (default, avoids broken v6 routes), 6 = IPv6 only, auto = let the resolver decide
_IP_FAMILY = {"4": socket.AF_INET, "6": socket.AF_INET6, "auto": 0}.get(
    str(os.environ.get("FASTDL_IP_FAMILY", "4")).strip().lower(), socket.AF_INET)

SEGMENT_RETRIES = 6
# Segments are small (1-5 MB): a connection that sends nothing for 12s is dead, retry/hedge it.
SEGMENT_TIMEOUT = aiohttp.ClientTimeout(total=None, connect=None, sock_connect=10, sock_read=12)
MAX_HEDGES_PER_SEGMENT = 2
# A single segment attempt may take at most max(15s, 6x median segment time) before it is retried.
SEGMENT_MIN_DEADLINE = float(os.environ.get("FASTDL_SEGMENT_MIN_DEADLINE", "15") or 15)
PIECE_MIN = 1 * 1024 * 1024           # direct-file range piece bounds
PIECE_MAX = 8 * 1024 * 1024
READ_CHUNK = 256 * 1024


class FastDLError(Exception):
    pass


class PermanentHTTPError(FastDLError):
    """404/410: retrying the same URL will not help."""


class UnsupportedStream(FastDLError):
    """Stream uses a feature we do not handle natively (e.g. SAMPLE-AES / DRM)."""


# ─────────────────────────────────────────────────────────────────────────────
# Speed meter (sliding window) — shared by a whole job so the UI shows real MB/s
# ─────────────────────────────────────────────────────────────────────────────
class SpeedMeter:
    def __init__(self, window_sec: float = 6.0):
        self.window = window_sec
        self.total = 0
        self.t_start = time.monotonic()
        self._samples: Deque[Tuple[float, int]] = deque()

    def add(self, n: int) -> None:
        if n <= 0:
            return
        now = time.monotonic()
        self.total += n
        task_stats.record_download(n)
        self._samples.append((now, n))
        cutoff = now - self.window
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    def rate_mb_s(self) -> float:
        now = time.monotonic()
        cutoff = now - self.window
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()
        if not self._samples:
            return 0.0
        span = max(1.0, min(self.window, now - self.t_start))
        return sum(n for _, n in self._samples) / span / (1024 * 1024)

    def avg_mb_s(self) -> float:
        el = max(0.1, time.monotonic() - self.t_start)
        return self.total / el / (1024 * 1024)


# ─────────────────────────────────────────────────────────────────────────────
# Shared connector (global socket cap). Each job gets its own ClientSession on top
# of it so cookies (e.g. Akamai hdntl) never leak between users.
# ─────────────────────────────────────────────────────────────────────────────
_connectors: Dict[int, aiohttp.TCPConnector] = {}
_connector_loop: Optional[asyncio.AbstractEventLoop] = None
# Hosts that reject this server's IPv4 address (e.g. RocketReels CloudFront answers 403 over IPv4,
# 200 over IPv6). Learned at runtime; such hosts are then always fetched over IPv6.
_HOST_FAMILY: Dict[str, int] = {}


def _get_connector(family: Optional[int] = None) -> aiohttp.TCPConnector:
    global _connector_loop
    loop = asyncio.get_running_loop()
    if _connector_loop is not loop:
        _connectors.clear()
        _connector_loop = loop
    fam = _IP_FAMILY if family is None else family
    conn = _connectors.get(fam)
    if conn is None or conn.closed:
        conn = aiohttp.TCPConnector(
            limit=MAX_CONN,
            limit_per_host=PER_HOST,
            ttl_dns_cache=300,
            keepalive_timeout=60,
            enable_cleanup_closed=True,
            family=fam,
        )
        _connectors[fam] = conn
    return conn


def _family_for(url: str) -> int:
    host = urllib.parse.urlsplit(url).hostname or ""
    return _HOST_FAMILY.get(host, _IP_FAMILY)


def _has_ipv6(host: str) -> bool:
    try:
        return bool(socket.getaddrinfo(host, 443, socket.AF_INET6, socket.SOCK_STREAM))
    except Exception:
        return False


_HOST_CHECKED: set = set()
_HOST_LOCKS: Dict[str, asyncio.Lock] = {}


async def _probe_status(url: str, headers: Optional[Dict[str, str]], family: int) -> int:
    try:
        timeout = aiohttp.ClientTimeout(total=12, sock_connect=8, sock_read=8)
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(family=family), timeout=timeout) as s:
            async with s.get(url, headers={**(headers or {}), "Range": "bytes=0-0"}, allow_redirects=True) as r:
                return r.status
    except Exception:
        return 0


async def _resolve_family(url: str, headers: Optional[Dict[str, str]]) -> int:
    """
    First request to an unknown host: ONE probe (serialised per host) decides IPv4 vs IPv6, so a batch of
    parallel episodes never fires a burst of IPv4 403s at a CDN that blocks this server's IPv4 address.
    """
    host = urllib.parse.urlsplit(url).hostname or ""
    if not host or host in _HOST_CHECKED or _IP_FAMILY != socket.AF_INET:
        return _HOST_FAMILY.get(host, _IP_FAMILY)
    lock = _HOST_LOCKS.setdefault(host, asyncio.Lock())
    async with lock:
        if host not in _HOST_CHECKED:
            st4 = await _probe_status(url, headers, socket.AF_INET)
            if st4 == 403 and await asyncio.to_thread(_has_ipv6, host):
                st6 = await _probe_status(url, headers, socket.AF_INET6)
                if st6 in (200, 206):
                    _HOST_FAMILY[host] = socket.AF_INET6
                    logger.info(f"[fastdl] {host}: IPv4 blocked (403), IPv6 OK -> using IPv6 for this host")
            _HOST_CHECKED.add(host)
    return _HOST_FAMILY.get(host, _IP_FAMILY)


async def _with_ipv6_fallback(url: str, run, headers: Optional[Dict[str, str]] = None):
    """Runs `run(family)`; on an HTTP 403 over IPv4, retries once over IPv6 and remembers the host."""
    await _resolve_family(url, headers)
    fam = _family_for(url)
    try:
        return await run(fam)
    except FastDLError as e:
        host = urllib.parse.urlsplit(url).hostname or ""
        if "HTTP 403" in str(e) and fam != socket.AF_INET6 and host and await asyncio.to_thread(_has_ipv6, host):
            logger.info(f"[fastdl] {host}: HTTP 403 over IPv4 -> retrying over IPv6 (remembered for this host)")
            _HOST_FAMILY[host] = socket.AF_INET6
            return await run(socket.AF_INET6)
        raise


def new_session(headers: Optional[Dict[str, str]] = None, family: Optional[int] = None) -> aiohttp.ClientSession:
    # connect=None: waiting for a free pooled socket must NOT count as a timeout.
    timeout = aiohttp.ClientTimeout(total=None, connect=None, sock_connect=15, sock_read=STALL_SEC)
    return aiohttp.ClientSession(
        connector=_get_connector(family),
        connector_owner=False,
        headers=headers or {},
        timeout=timeout,
        cookie_jar=aiohttp.CookieJar(unsafe=True),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Small helpers
# ─────────────────────────────────────────────────────────────────────────────
def _q_target(quality: str) -> int:
    q = str(quality or "").lower()
    if any(k in q for k in ("best", "org", "4k", "2160")):
        return 99999
    digits = re.sub(r"[^\d]", "", q)
    try:
        return int(digits) if digits else 720
    except ValueError:
        return 720


def _attr_list(line: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    body = line.split(":", 1)[1] if ":" in line else ""
    for m in re.finditer(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', body):
        out[m.group(1)] = m.group(2).strip('"')
    return out


def _join(base: str, uri: str) -> str:
    return urllib.parse.urljoin(base, uri.strip())


def _with_query(url: str, qs: str) -> str:
    if not qs or "?" in url:
        return url
    return f"{url}?{qs}"


async def ffprobe_ok(path: str, need_video: bool = False, need_audio: bool = False, min_dur: float = 0.5) -> bool:
    """Async (non-blocking) media sanity check."""
    try:
        if not os.path.exists(path) or os.path.getsize(path) < 4 * 1024:
            return False
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type",
            "-of", "json", path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        except asyncio.TimeoutError:
            proc.kill()
            return os.path.getsize(path) > 64 * 1024
        if proc.returncode != 0:
            return False
        data = json.loads(out.decode("utf-8", "ignore") or "{}")
        dur = float((data.get("format") or {}).get("duration") or 0)
        types = {s.get("codec_type") for s in data.get("streams", [])}
        if need_video and "video" not in types:
            return False
        if need_audio and "audio" not in types:
            return False
        if not types & {"video", "audio"}:
            return False
        return dur >= min_dur
    except Exception:
        return False


async def run_ffmpeg(args: List[str], timeout: float) -> Tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, "ffmpeg timeout"
    return proc.returncode, (err or b"").decode("utf-8", "ignore")[-1500:]


# ─────────────────────────────────────────────────────────────────────────────
# HLS parsing
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Variant:
    url: str
    bandwidth: int = 0
    width: int = 0
    height: int = 0
    audio_group: str = ""

    @property
    def res(self) -> int:
        if self.width and self.height:
            return min(self.width, self.height)  # portrait reels: 720x1280 -> 720p
        return self.height or self.width


@dataclass
class AudioRendition:
    url: str
    group: str = ""
    lang: str = ""
    name: str = ""
    default: bool = False


@dataclass
class Segment:
    url: str
    duration: float = 0.0
    byterange: Optional[Tuple[int, int]] = None  # (offset, length)
    key_uri: Optional[str] = None
    key_iv: Optional[bytes] = None
    seq: int = 0


@dataclass
class MediaPlaylist:
    url: str
    segments: List[Segment] = field(default_factory=list)
    init_url: Optional[str] = None
    init_range: Optional[Tuple[int, int]] = None
    total_duration: float = 0.0


def parse_master(text: str, base_url: str) -> Tuple[List[Variant], List[AudioRendition]]:
    variants: List[Variant] = []
    audios: List[AudioRendition] = []
    lines = [l.strip() for l in text.splitlines()]
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF:"):
            a = _attr_list(line)
            uri = ""
            for j in range(i + 1, min(i + 5, len(lines))):
                if lines[j] and not lines[j].startswith("#"):
                    uri = lines[j]
                    break
            if not uri:
                continue
            w = h = 0
            m = re.match(r"(\d+)x(\d+)", a.get("RESOLUTION", ""))
            if m:
                w, h = int(m.group(1)), int(m.group(2))
            try:
                bw = int(a.get("BANDWIDTH") or a.get("AVERAGE-BANDWIDTH") or 0)
            except ValueError:
                bw = 0
            variants.append(Variant(url=_join(base_url, uri), bandwidth=bw, width=w, height=h,
                                    audio_group=a.get("AUDIO", "")))
        elif line.startswith("#EXT-X-MEDIA:") and "TYPE=AUDIO" in line.upper():
            a = _attr_list(line)
            if a.get("URI"):
                audios.append(AudioRendition(
                    url=_join(base_url, a["URI"]), group=a.get("GROUP-ID", ""),
                    lang=a.get("LANGUAGE", ""), name=a.get("NAME", ""),
                    default=a.get("DEFAULT", "").upper() == "YES",
                ))
    return variants, audios


def parse_media(text: str, url: str, append_qs: str = "") -> MediaPlaylist:
    pl = MediaPlaylist(url=url)
    seq = 0
    key_uri: Optional[str] = None
    key_iv: Optional[bytes] = None
    pending_dur = 0.0
    pending_range: Optional[Tuple[int, int]] = None
    last_range_end = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                seq = int(line.split(":", 1)[1])
            except ValueError:
                pass
        elif line.startswith("#EXT-X-KEY:"):
            a = _attr_list(line)
            method = a.get("METHOD", "NONE").upper()
            if method == "NONE":
                key_uri, key_iv = None, None
            elif method == "AES-128" and a.get("KEYFORMAT", "identity").lower() in ("identity", ""):
                key_uri = _with_query(_join(url, a.get("URI", "")), append_qs)
                iv = a.get("IV")
                key_iv = bytes.fromhex(iv[2:] if iv.lower().startswith("0x") else iv) if iv else None
            else:
                raise UnsupportedStream(f"HLS encryption {method} not supported natively")
        elif line.startswith("#EXT-X-MAP:"):
            a = _attr_list(line)
            pl.init_url = _with_query(_join(url, a.get("URI", "")), append_qs)
            br = a.get("BYTERANGE")
            if br:
                ln, _, off = br.partition("@")
                pl.init_range = (int(off or 0), int(ln))
        elif line.startswith("#EXTINF:"):
            try:
                pending_dur = float(line.split(":", 1)[1].split(",", 1)[0])
            except ValueError:
                pending_dur = 0.0
        elif line.startswith("#EXT-X-BYTERANGE:"):
            ln, _, off = line.split(":", 1)[1].partition("@")
            start = int(off) if off else last_range_end
            pending_range = (start, int(ln))
            last_range_end = start + int(ln)
        elif not line.startswith("#"):
            pl.segments.append(Segment(
                url=_with_query(_join(url, line), append_qs), duration=pending_dur,
                byterange=pending_range, key_uri=key_uri, key_iv=key_iv, seq=seq,
            ))
            pl.total_duration += pending_dur
            seq += 1
            pending_dur = 0.0
            pending_range = None
    return pl


def choose_variant(variants: List[Variant], quality: str) -> Variant:
    target = _q_target(quality)
    ordered = sorted(variants, key=lambda v: (v.res, v.bandwidth), reverse=True)
    if target >= 99999:
        return ordered[0]
    fit = [v for v in ordered if v.res and v.res <= target]
    if fit:
        return fit[0]
    with_res = [v for v in ordered if v.res]
    if with_res:
        return with_res[-1]  # everything is above target -> smallest available
    return ordered[0]


AUDIO_FROM_VIDEO_MIN_BW = int(os.environ.get("FASTDL_AUDIO_FROM_VIDEO_MIN_BW", "500000") or 500000)


def _pick_audio_variant(variants: List[Variant]) -> Variant:
    """
    For audio extraction from a master playlist:
    - audio-only masters (no RESOLUTION): highest bandwidth (= best audio)
    - muxed video+audio (e.g. Kuku /video-trailer/ slides): the smallest variant whose total bandwidth
      suggests full-quality audio (~128 kbps AAC sits in the ~480p rung), instead of the 1080p video.
    """
    no_video = [v for v in variants if not (v.width or v.height)]
    if no_video:
        return max(no_video, key=lambda v: v.bandwidth)
    ok = sorted((v for v in variants if v.bandwidth >= AUDIO_FROM_VIDEO_MIN_BW), key=lambda v: v.bandwidth)
    return ok[0] if ok else max(variants, key=lambda v: v.bandwidth)


def choose_audio(audios: List[AudioRendition], group: str) -> Optional[AudioRendition]:
    cands = [a for a in audios if not group or a.group == group] or audios
    if not cands:
        return None
    for a in cands:
        if a.default:
            return a
    for a in cands:
        if a.lang.lower().startswith("hi"):
            return a
    return cands[0]


# ─────────────────────────────────────────────────────────────────────────────
# Network primitives
# ─────────────────────────────────────────────────────────────────────────────
async def fetch_text(session: aiohttp.ClientSession, url: str, headers: Optional[Dict[str, str]] = None,
                     retries: int = 4) -> Tuple[str, str]:
    """Returns (text, final_url). Raises FastDLError on failure."""
    last = ""
    for attempt in range(1, retries + 1):
        try:
            async with session.get(url, headers=headers, allow_redirects=True) as r:
                if r.status == 200:
                    body = await r.read()
                    return body.decode("utf-8", "ignore"), str(r.url)
                last = f"HTTP {r.status}"
                if r.status in (401, 403, 404, 410):
                    break
        except Exception as e:
            last = repr(e)
        await asyncio.sleep(min(4.0, 0.5 * attempt))
    raise FastDLError(f"playlist fetch failed ({last}): {url[:120]}")


async def _get_bytes(session: aiohttp.ClientSession, url: str, byterange: Optional[Tuple[int, int]],
                     meter: Optional[SpeedMeter], headers: Optional[Dict[str, str]] = None) -> bytes:
    hdrs = dict(headers or {})
    if byterange:
        off, ln = byterange
        hdrs["Range"] = f"bytes={off}-{off + ln - 1}"
    async with session.get(url, headers=hdrs, allow_redirects=True, timeout=SEGMENT_TIMEOUT) as r:
        if r.status in (404, 410):
            raise PermanentHTTPError(f"HTTP {r.status}")
        if r.status not in (200, 206):
            raise FastDLError(f"HTTP {r.status}")
        buf = bytearray()
        async for chunk in r.content.iter_chunked(READ_CHUNK):
            buf += chunk
            if meter:
                meter.add(len(chunk))
        cl = r.headers.get("Content-Length")
        if cl and cl.isdigit() and int(cl) != len(buf) and "Content-Encoding" not in r.headers:
            raise FastDLError(f"short read {len(buf)}/{cl}")
        if byterange and r.status == 200:  # server ignored Range
            off, ln = byterange
            buf = buf[off:off + ln]
        return bytes(buf)


def _aes_decrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    from Crypto.Cipher import AES  # pycryptodome (already in requirements)
    out = AES.new(key, AES.MODE_CBC, iv).decrypt(data)
    pad = out[-1] if out else 0
    if 0 < pad <= 16 and out.endswith(bytes([pad]) * pad):
        out = out[:-pad]
    return out


# ─────────────────────────────────────────────────────────────────────────────
# HLS media playlist -> single file
# ─────────────────────────────────────────────────────────────────────────────
def _backoff(attempt: int) -> float:
    return min(4.0, 0.3 * attempt ** 1.5)


async def _download_media_playlist(
    session: aiohttp.ClientSession,
    pl: MediaPlaylist,
    out_path: str,
    meter: Optional[SpeedMeter],
    workers: int,
    headers: Optional[Dict[str, str]],
    on_segment: Optional[Callable[[int, int], None]] = None,
) -> None:
    """
    Parallel segment download with straggler hedging: once the queue is empty, idle
    workers re-request segments that are taking much longer than usual; whichever copy
    finishes first wins. This removes the "stuck at 97% for several seconds" tail.
    """
    if not pl.segments:
        raise FastDLError("playlist has no segments")
    tmp_dir = out_path + ".parts"
    os.makedirs(tmp_dir, exist_ok=True)
    keys: Dict[str, bytes] = {}
    key_lock = asyncio.Lock()
    total = len(pl.segments)
    done_set: set = set()
    inflight: Dict[int, float] = {}
    hedged: Dict[int, int] = {}
    seg_times: List[float] = []
    last_err: Dict[int, str] = {}

    async def _key(uri: str) -> bytes:
        async with key_lock:
            if uri not in keys:
                keys[uri] = await _get_bytes(session, uri, None, None, headers)
            return keys[uri]

    async def _fetch(i: int, attempts: int) -> None:
        seg = pl.segments[i]
        t_start = time.monotonic()
        for attempt in range(1, attempts + 1):
            if i in done_set:
                return
            try:
                # Adaptive per-attempt deadline: a CDN edge that trickles bytes (never "stalled", so the
                # socket read timeout never fires) is cut and retried on a fresh connection.
                data = await asyncio.wait_for(
                    _get_bytes(session, seg.url, seg.byterange, meter, headers),
                    timeout=_attempt_deadline(),
                )
                if seg.key_uri:
                    key = await _key(seg.key_uri)
                    iv = seg.key_iv or seg.seq.to_bytes(16, "big")
                    data = _aes_decrypt(data, key, iv)
                if not data:
                    raise FastDLError("empty segment")
                if i in done_set:  # the other copy already won
                    return
                with open(os.path.join(tmp_dir, f"{i:06d}"), "wb") as f:
                    f.write(data)
                done_set.add(i)
                me = asyncio.current_task()
                for t in list(seg_tasks.get(i, ())):
                    if t is not me and not t.done():
                        t.cancel()  # stop the slower duplicate
                took = time.monotonic() - t_start
                seg_times.append(took)
                if took > 8:
                    logger.info(f"[fastdl] slow segment {i}/{total}: {took:.1f}s ({len(data) / 1048576:.2f} MB, attempt {attempt})"
                                + (f", earlier error {last_err[i]}" if i in last_err else ""))
                if on_segment:
                    on_segment(len(done_set), total)
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                last_err[i] = repr(e)
                logger.debug(f"[fastdl] segment {i} attempt {attempt} failed at {time.monotonic() - t_start:.1f}s: {e!r}")
                if isinstance(e, PermanentHTTPError) and attempt >= 2:
                    return
                if attempt < attempts:
                    await asyncio.sleep(_backoff(attempt))

    def _attempt_deadline() -> float:
        if len(seg_times) >= 5:
            st = sorted(seg_times)
            return max(SEGMENT_MIN_DEADLINE, 6.0 * st[len(st) // 2])
        return SEGMENT_MIN_DEADLINE * 2

    def _hedge_threshold() -> float:
        if len(seg_times) >= 5:
            st = sorted(seg_times)
            return max(2.5, 2.5 * st[len(st) // 2])
        return 6.0

    q: asyncio.Queue = asyncio.Queue()
    for i in range(total):
        q.put_nowait(i)
    running: Dict[int, int] = {}
    seg_tasks: Dict[int, set] = {}

    async def _run_copy(i: int, attempts: int) -> None:
        running[i] = running.get(i, 0) + 1
        t = asyncio.create_task(_fetch(i, attempts))
        seg_tasks.setdefault(i, set()).add(t)
        try:
            await asyncio.wait({t})  # does not raise if the copy was cancelled
        finally:
            seg_tasks[i].discard(t)
            _release(i)

    def _release(i: int) -> None:
        running[i] = running.get(i, 1) - 1
        if running[i] <= 0:
            running.pop(i, None)
            inflight.pop(i, None)  # nobody is working on it any more (done or failed)

    async def _worker():
        while True:
            try:
                i = q.get_nowait()
            except asyncio.QueueEmpty:
                pending = [j for j in inflight if j not in done_set]
                if not pending:
                    return
                now = time.monotonic()
                thr = _hedge_threshold()
                cand = next((j for j in sorted(pending, key=lambda j: inflight[j])
                             if hedged.get(j, 0) < MAX_HEDGES_PER_SEGMENT
                             and now - inflight[j] > thr * (1 + hedged.get(j, 0))), None)
                if cand is None:
                    await asyncio.sleep(0.25)
                    continue
                hedged[cand] = hedged.get(cand, 0) + 1
                logger.debug(f"[fastdl] hedging slow segment {cand} ({now - inflight[cand]:.1f}s > {thr:.1f}s)")
                await _run_copy(cand, 3)
                continue
            inflight[i] = time.monotonic()
            await _run_copy(i, SEGMENT_RETRIES)

    try:
        n = max(1, min(workers, total))
        await asyncio.gather(*[_worker() for _ in range(n)])
        failed = [i for i in range(total) if i not in done_set]
        if failed:
            i0 = failed[0]
            logger.warning(f"[fastdl] {len(failed)}/{total} segments failed, e.g. #{i0}: {last_err.get(i0, '?')} | {pl.segments[i0].url[:100]}")
            raise FastDLError(f"{len(failed)}/{total} segments failed")

        init_bytes = b""
        if pl.init_url:
            init_bytes = await _get_bytes(session, pl.init_url, pl.init_range, meter, headers)

        def _assemble():
            with open(out_path, "wb") as out:
                if init_bytes:
                    out.write(init_bytes)
                for i in range(total):
                    with open(os.path.join(tmp_dir, f"{i:06d}"), "rb") as f:
                        shutil.copyfileobj(f, out, 4 * 1024 * 1024)

        await asyncio.to_thread(_assemble)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def _probe_segment(session, seg: Segment, headers) -> bool:
    try:
        off = seg.byterange[0] if seg.byterange else 0
        await _get_bytes(session, seg.url, (off, 1), None, headers)
        return True
    except Exception:
        return False


async def _resolve_and_parse(session, url: str, headers, append_qs_mode: str, parent_qs: str = "",
                             prefetched: Optional[Tuple[str, str]] = None) -> MediaPlaylist:
    """
    Fetches (or reuses) a media playlist and decides whether child URLs need a signed
    query string (CloudFront/Akamai style). Modes: always | never | auto.
    """
    if prefetched:
        text, final_url = prefetched
    else:
        try:
            text, final_url = await fetch_text(session, url, headers)
        except FastDLError:
            if append_qs_mode == "auto" and parent_qs and "?" not in url:
                text, final_url = await fetch_text(session, _with_query(url, parent_qs), headers)
            else:
                raise
    qs = urllib.parse.urlsplit(final_url).query or parent_qs
    if append_qs_mode == "always":
        return parse_media(text, final_url, qs)
    pl = parse_media(text, final_url, "")
    if append_qs_mode == "auto" and qs and pl.segments and "?" not in pl.segments[0].url:
        if not await _probe_segment(session, pl.segments[0], headers):
            pl_q = parse_media(text, final_url, qs)
            if await _probe_segment(session, pl_q.segments[0], headers):
                logger.info("[fastdl] segments need signed query string -> appending")
                return pl_q
    return pl


async def download_hls(url: str, out_path: str, quality: str = "720p", *args, **kwargs) -> str:
    hdrs = kwargs.get("headers") if "headers" in kwargs else (args[0] if args else None)
    return await _with_ipv6_fallback(
        url, lambda fam: _download_hls_impl(url, out_path, quality, *args, _family=fam, **kwargs), hdrs)


async def _download_hls_impl(
    url: str,
    out_path: str,
    quality: str = "720p",
    headers: Optional[Dict[str, str]] = None,
    meter: Optional[SpeedMeter] = None,
    workers: int = SEG_WORKERS,
    append_qs: str = "auto",           # "auto" | "always" | "never"
    audio_url: Optional[str] = None,   # explicit separate audio playlist (e.g. Stage)
    on_progress: Optional[Callable[[int, int], None]] = None,
    remux_timeout: float = 900,
    audio_only: bool = False,          # keep only the audio track (audiobooks / audio-story trailers)
    _family: Optional[int] = None,
) -> str:
    """
    Downloads an HLS master or media playlist to `out_path` (.mp4/.mkv/.m4a) with stream copy.
    Raises FastDLError / UnsupportedStream on failure (caller falls back).
    """
    headers = dict(headers or {})
    async with new_session(headers, _family) as session:
        text, final_url = await fetch_text(session, url, headers)
        master_qs = urllib.parse.urlsplit(final_url).query
        video_url = final_url
        a_url = audio_url
        if "#EXT-X-STREAM-INF" in text:
            variants, audios = parse_master(text, final_url)
            if not variants:
                raise FastDLError("master playlist without variants")
            if audio_only and audios:
                # Separate audio renditions: fetch only the audio playlist
                ar = choose_audio(audios, "")
                logger.info(f"[fastdl] audio-only: separate rendition '{ar.name or ar.lang}'")
                a_pl_only = await _resolve_and_parse(session, ar.url, headers, append_qs, master_qs)
                variants = []  # skip video variant loop below
            first = (_pick_audio_variant(variants) if audio_only else choose_variant(variants, quality)) if variants else None
            # Try the best match first, then the nearest other qualities if its playlist is broken.
            target = first.res if first else 0
            others = sorted((x for x in variants if x is not first), key=lambda x: x.bandwidth) if audio_only else sorted((x for x in variants if x is not first),
                            key=lambda x: (abs((x.res or 0) - (target or 0)), -(x.bandwidth or 0)))
            v_pl = a_pl_only if (audio_only and not variants) else None
            last_err: Optional[Exception] = None
            for v in ([first] + others if first else []):
                video_url = _with_query(v.url, master_qs) if append_qs == "always" else v.url
                try:
                    v_pl = await _resolve_and_parse(session, video_url, headers, append_qs, master_qs)
                except FastDLError as e:
                    last_err = e
                    logger.warning(f"[fastdl] variant {v.width}x{v.height} unavailable ({e}); trying next")
                    continue
                a_url = audio_url
                if not a_url and v.audio_group:
                    ar = choose_audio(audios, v.audio_group)
                    if ar:
                        a_url = _with_query(ar.url, master_qs) if append_qs == "always" else ar.url
                logger.info(f"[fastdl] variant {v.width}x{v.height} bw={v.bandwidth} (target {quality}) audio={'separate' if a_url else 'muxed'}")
                break
            if v_pl is None:
                raise last_err or FastDLError("no playable variant")
        else:
            v_pl = await _resolve_and_parse(session, final_url, headers, append_qs, "", (text, final_url))

        a_pl = None
        if a_url and a_url != video_url:
            a_pl = await _resolve_and_parse(session, a_url, headers, append_qs, master_qs)

        total_segs = len(v_pl.segments) + (len(a_pl.segments) if a_pl else 0)
        seg_done = {"v": 0, "a": 0}

        def _prog(kind):
            def _cb(done, _total):
                seg_done[kind] = done
                if on_progress:
                    on_progress(seg_done["v"] + seg_done["a"], total_segs)
            return _cb

        v_ext = ".mp4" if v_pl.init_url else ".ts"
        v_raw = out_path + ".v" + v_ext
        jobs = [_download_media_playlist(session, v_pl, v_raw, meter, workers, headers, _prog("v"))]
        a_raw = None
        if a_pl:
            a_raw = out_path + ".a" + (".mp4" if a_pl.init_url else ".ts")
            jobs.append(_download_media_playlist(session, a_pl, a_raw, meter, max(4, workers // 3), headers, _prog("a")))
        try:
            await asyncio.gather(*jobs)
        except Exception:
            for p in (v_raw, a_raw):
                if p and os.path.exists(p):
                    os.remove(p)
            raise

    # Lossless remux to the final container.
    try:
        args = ["-fflags", "+genpts", "-i", v_raw]
        if a_raw:
            args += ["-i", a_raw] + (["-map", "1:a:0"] if audio_only else ["-map", "0:v:0?", "-map", "1:a:0?"])
        elif audio_only:
            args += ["-map", "0:a:0", "-vn"]
        else:
            args += ["-map", "0:v?", "-map", "0:a?"]
        args += ["-c", "copy"]
        if out_path.lower().endswith(".mp4"):
            args += ["-movflags", "+faststart"]
        rc, err = await run_ffmpeg(args + [out_path], timeout=remux_timeout)
        if rc != 0:
            # Retry with explicit ADTS->ASC fix (AAC from TS into MP4)
            rc, err = await run_ffmpeg(args + ["-bsf:a", "aac_adtstoasc", out_path], timeout=remux_timeout)
        if rc != 0 or not os.path.exists(out_path):
            raise FastDLError(f"remux failed: {err.strip().splitlines()[-1] if err.strip() else rc}")
    finally:
        for p in (v_raw, a_raw):
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
    return out_path


# ─────────────────────────────────────────────────────────────────────────────
# Direct file (parallel HTTP Range)
# ─────────────────────────────────────────────────────────────────────────────
async def download_direct(url: str, out_path: str, *args, **kwargs) -> str:
    hdrs = kwargs.get("headers") if "headers" in kwargs else (args[0] if args else None)
    return await _with_ipv6_fallback(
        url, lambda fam: _download_direct_impl(url, out_path, *args, _family=fam, **kwargs), hdrs)


async def _download_direct_impl(
    url: str,
    out_path: str,
    headers: Optional[Dict[str, str]] = None,
    meter: Optional[SpeedMeter] = None,
    workers: int = SEG_WORKERS,
    on_progress: Optional[Callable[[int, int], None]] = None,
    _family: Optional[int] = None,
) -> str:
    """Parallel ranged download with per-piece resume. Falls back to a single stream if no Range support."""
    headers = dict(headers or {})
    async with new_session(headers, _family) as session:
        total = 0
        ranged = False
        final_url = url
        for attempt in range(1, 4):
            try:
                async with session.get(url, headers={**headers, "Range": "bytes=0-0"}, allow_redirects=True) as r:
                    final_url = str(r.url)
                    if r.status == 206:
                        cr = r.headers.get("Content-Range", "")
                        m = re.search(r"/(\d+)$", cr)
                        if m:
                            total = int(m.group(1))
                            ranged = total > 0
                    elif r.status == 200:
                        cl = r.headers.get("Content-Length", "")
                        total = int(cl) if cl.isdigit() else 0
                    else:
                        raise FastDLError(f"HTTP {r.status}")
                break
            except FastDLError:
                if attempt == 3:
                    raise
            except Exception as e:
                if attempt == 3:
                    raise FastDLError(f"probe failed: {e!r}")
            await asyncio.sleep(0.5 * attempt)

        done_bytes = 0

        def _tick(n):
            nonlocal done_bytes
            done_bytes += n
            if meter:
                meter.add(n)
            if on_progress:
                on_progress(done_bytes, total)

        if not ranged or total < 2 * 1024 * 1024:
            # Single stream (with retries from scratch)
            for attempt in range(1, 4):
                try:
                    done_bytes = 0
                    async with session.get(final_url, headers=headers) as r:
                        if r.status != 200:
                            raise FastDLError(f"HTTP {r.status}")
                        with open(out_path, "wb") as f:
                            async for chunk in r.content.iter_chunked(READ_CHUNK):
                                f.write(chunk)
                                _tick(len(chunk))
                    if total and os.path.getsize(out_path) != total:
                        raise FastDLError("size mismatch")
                    return out_path
                except Exception as e:
                    if attempt == 3:
                        raise FastDLError(f"single-stream download failed: {e!r}")
                    await asyncio.sleep(attempt)

        # Parallel ranged with work stealing: when the queue runs dry, idle workers split the
        # largest remaining piece in half so one slow connection can never hold up the file.
        with open(out_path, "wb") as f:
            f.truncate(total)
        fd = os.open(out_path, os.O_WRONLY)
        piece = max(PIECE_MIN, min(PIECE_MAX, total // max(1, workers * 2)))
        queue: Deque[Dict[str, int]] = deque(
            {"start": st, "pos": st, "end": min(total, st + piece) - 1} for st in range(0, total, piece)
        )
        active: List[Dict[str, Any]] = []
        errors: List[str] = []
        min_split = 2 * PIECE_MIN
        piece_rates: List[float] = []

        async def _run(pc: Dict[str, Any]) -> None:
            pc["t0"] = pc["t_last"] = time.monotonic()
            got = 0
            for attempt in range(1, SEGMENT_RETRIES + 1):
                if pc["pos"] > pc["end"]:
                    break
                try:
                    async with session.get(final_url, headers={**headers, "Range": f"bytes={pc['pos']}-{pc['end']}"}) as r:
                        if r.status != 206:
                            raise FastDLError(f"HTTP {r.status}")
                        async for chunk in r.content.iter_chunked(READ_CHUNK):
                            lim = pc["end"] - pc["pos"] + 1   # end may shrink if another worker took our tail
                            if lim <= 0:
                                break
                            if len(chunk) > lim:
                                chunk = chunk[:lim]
                            os.pwrite(fd, chunk, pc["pos"])
                            pc["pos"] += len(chunk)
                            got += len(chunk)
                            pc["t_last"] = time.monotonic()
                            _tick(len(chunk))
                    if pc["pos"] > pc["end"]:
                        break
                    raise FastDLError("short piece")
                except Exception as e:
                    if attempt == SEGMENT_RETRIES:
                        errors.append(f"piece {pc['start']}-{pc['end']}: {e!r}")
                        return
                    await asyncio.sleep(_backoff(attempt))
            el = time.monotonic() - pc["t0"]
            if got > 256 * 1024 and el > 0.05:
                piece_rates.append(got / el)

        def _is_slow(p: Dict[str, Any], now: float) -> bool:
            if now - p.get("t_last", now) > 3.0:
                return True  # no bytes for 3s
            if len(piece_rates) >= 4 and now - p.get("t0", now) > 2.0:
                med = sorted(piece_rates)[len(piece_rates) // 2]
                rate = (p["pos"] - p["start"]) / max(0.05, now - p["t0"])
                return rate < 0.25 * med
            return False

        async def _worker():
            while not errors:
                if queue:
                    pc = queue.popleft()
                else:
                    if not active:
                        return
                    now = time.monotonic()
                    cands = [p for p in active if p["end"] >= p["pos"] and not p.get("taken")]
                    slow = [p for p in cands if _is_slow(p, now)]
                    big = [p for p in cands if p["end"] - p["pos"] + 1 >= min_split]
                    if slow:
                        v = max(slow, key=lambda p: p["end"] - p["pos"])
                        # Take over everything the slow connection has not written yet.
                        pc = {"start": v["pos"], "pos": v["pos"], "end": v["end"]}
                        v["end"] = v["pos"] - 1
                        v["taken"] = True
                        vt = v.get("task")
                        if vt is not None and not vt.done():
                            vt.cancel()  # its connection is too slow; free it immediately
                    elif big:
                        v = max(big, key=lambda p: p["end"] - p["pos"])
                        mid = v["pos"] + (v["end"] - v["pos"] + 1) // 2
                        pc = {"start": mid, "pos": mid, "end": v["end"]}
                        v["end"] = mid - 1
                    else:
                        await asyncio.sleep(0.3)
                        continue
                active.append(pc)
                t = asyncio.create_task(_run(pc))
                pc["task"] = t
                try:
                    await asyncio.wait({t})
                finally:
                    active.remove(pc)

        try:
            n = max(1, min(workers, len(queue)))
            await asyncio.gather(*[_worker() for _ in range(n)])
        finally:
            os.close(fd)
        if errors:
            raise FastDLError(errors[0])
        return out_path


def is_hls_url(url: str) -> bool:
    u = (url or "").lower().split("?", 1)[0]
    return u.endswith(".m3u8") or ".m3u8" in (url or "").lower()


async def download_any(url: str, out_path: str, quality: str = "720p", headers: Optional[Dict[str, str]] = None,
                       meter: Optional[SpeedMeter] = None, workers: int = SEG_WORKERS,
                       on_progress: Optional[Callable[[int, int], None]] = None) -> str:
    """HLS -> download_hls, everything else -> download_direct (direct .mp4 is remuxed only if needed)."""
    if is_hls_url(url):
        return await download_hls(url, out_path, quality, headers, meter, workers, on_progress=on_progress)
    return await download_direct(url, out_path, headers, meter, workers, on_progress=on_progress)


# ─────────────────────────────────────────────────────────────────────────────
# Non-blocking progress reporter: downloads never wait for Telegram edits
# ─────────────────────────────────────────────────────────────────────────────
class ProgressReporter:
    """Calls `status_cb(render())` every `interval` seconds in a background task."""

    def __init__(self, status_cb: Optional[Callable], render: Callable[[], Optional[str]], interval: float = 3.0):
        self.status_cb = status_cb
        self.render = render
        self.interval = interval
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    async def _loop(self):
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
                break
            except asyncio.TimeoutError:
                pass
            await self.push()

    async def push(self):
        if not self.status_cb:
            return
        try:
            text = self.render()
            if text:
                res = self.status_cb(text)
                if asyncio.iscoroutine(res) or isinstance(res, asyncio.Future):
                    # Bound the edit so a Telegram FloodWait never freezes reporting forever.
                    await asyncio.wait_for(res, timeout=15)
        except Exception as e:
            logger.debug(f"[fastdl] status update skipped: {e!r}")

    def start(self):
        if self.status_cb and self._task is None:
            self._task = asyncio.create_task(self._loop())
        return self

    async def stop(self, final: bool = False):
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=16)
            except Exception:
                self._task.cancel()
        if final:
            await self.push()
