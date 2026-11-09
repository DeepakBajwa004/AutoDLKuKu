#!/usr/bin/env python3
"""
================================================================================
🚀 PrimeX Autonomous Batch Engine for KuKu TV & KuKu FM Auto DL
================================================================================
"""

import asyncio
import json
import math
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from pyrogram.types import Message

from lipat_client import lipat_mgr
from bot_config import (
    ADMIN_ID,
    CATALOG_PATH,
    TEMP_DIR,
    PROGRESS_UPDATE_INTERVAL,
    RELEASE_TAG,
    ENABLE_WATERMARK,
    WATERMARK_TEXT,
    FORCE_PREMIUM_UPLOAD,
    MAX_BOT_FILE_SIZE_BYTES,
    FETCH_SOURCE,
)
from logger import logger
from bot_manager import bot_mgr
from downloader import download_all_episodes
from merger import merge_audio_episodes, merge_video_episodes
from watermark import get_audio_duration
from thumbnail import optimize_telegram_thumb, extract_video_frame, process_kuku_style_poster, make_hd_cover
from naming import probe_video_metadata


def make_progress_bar(pct: float, length: int = 12) -> str:
    """Generates visual progress bar: [██████████░░] 83.3%"""
    pct = max(0.0, min(100.0, pct))
    filled = int(round((pct / 100.0) * length))
    bar = "█" * filled + "░" * (length - filled)
    return f"[{bar}] {pct:.1f}%"


class AutoDLEngine:
    def __init__(self):
        self.is_running = False
        self.stop_requested = False
        self.current_show_title = ""
        self.current_show_slug = ""
        self.current_workspace: Optional[Path] = None
        self.status_msg: Optional[Message] = None
        self.last_card_update_time = 0.0

    async def resolve_show_id_fallback(self, slug: str) -> Optional[str]:
        """Resolves show ID dynamically if missing from metadata."""
        try:
            cur_dir = Path(__file__).resolve().parent
            parent_dir = cur_dir.parent
            if str(parent_dir) not in sys.path:
                sys.path.insert(0, str(parent_dir))
            from client import KuKuClient
            c = KuKuClient()
            return str(c.resolve_show_id(slug))
        except Exception:
            return None

    async def _fetch_live_from_kuku_apiflow(self, show_id: str, slug: str) -> Optional[Dict[str, Any]]:
        """
        Dynamically imports and calls KuKu API Flow only on demand.
        Isolates external endpoints so auto_dl can be committed to git cleanly.
        """
        try:
            cur_dir = Path(__file__).resolve().parent
            parent_dir = cur_dir.parent
            if str(parent_dir) not in sys.path:
                sys.path.insert(0, str(parent_dir))
            from client import KuKuClient
            from extractor import format_kuku_show_data

            client = KuKuClient()
            raw_details = client.fetch_show_details(show_id)
            if not raw_details or not raw_details.get("show"):
                return None

            all_raw_eps = []
            page = 1
            while True:
                if self.stop_requested:
                    return None
                page_data = client.fetch_episodes_page(show_id, page=page, page_size=30)
                if not page_data or not page_data.get("episodes"):
                    break
                eps_batch = page_data.get("episodes", [])
                all_raw_eps.extend(eps_batch)
                if page_data.get("has_more") is False or len(eps_batch) < 30:
                    break
                page += 1

            if not all_raw_eps:
                return None

            formatted = format_kuku_show_data(raw_details, all_raw_eps)
            return formatted["data"][0] if formatted.get("data") else None
        except Exception as e:
            logger.warning(f"Error fetching live from KuKu API Flow: {e}")
            return None

    def load_catalog(self) -> Dict[str, Any]:
        """Loads master shows catalog."""
        if not CATALOG_PATH.exists():
            return {}
        try:
            with open(CATALOG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading catalog {CATALOG_PATH}: {e}")
            return {}

    def save_catalog_show_completed(self, slug: str) -> bool:
        """Atomically marks show as completed in all_kuku_shows.json."""
        try:
            catalog = self.load_catalog()
            if slug in catalog:
                catalog[slug]["status"] = "completed"
            temp_path = str(CATALOG_PATH) + ".tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(catalog, f, indent=2, ensure_ascii=False)
            os.replace(temp_path, str(CATALOG_PATH))
            logger.info(f"💾 Marked '{slug}' as 'completed' in {CATALOG_PATH.name}")
            return True
        except Exception as e:
            logger.error(f"Failed to save catalog status for {slug}: {e}")
            return False

    async def update_status_card(self, text: str, force: bool = False):
        """Updates the status card message on main_bot throttled by PROGRESS_UPDATE_INTERVAL (6s)."""
        now = time.time()
        if (not force) and (now - self.last_card_update_time < PROGRESS_UPDATE_INTERVAL):
            return

        self.last_card_update_time = now
        try:
            if not self.status_msg:
                self.status_msg = await bot_mgr.main_bot.send_message(ADMIN_ID, text)
            else:
                await self.status_msg.edit_text(text)
        except Exception as e:
            # If message was not modified or floodwait, silently log debug
            logger.debug(f"Status card update failed: {e}")

    async def send_fresh_card(self, text: str):
        """Sends a brand new message card at the bottom of the chat."""
        try:
            self.status_msg = await bot_mgr.main_bot.send_message(ADMIN_ID, text)
            self.last_card_update_time = time.time()
        except Exception as e:
            logger.warning(f"Failed to send fresh card: {e}")

    @staticmethod
    def get_hq_poster_url_candidates(poster_url: str) -> List[str]:
        """Builds high-resolution CDN and raw S3 candidate URLs from base image URL (PrimeXMasoom Method)."""
        candidates = []
        if not poster_url:
            return candidates
        # 1. Raw S3 master URL if embedded inside CDN wrapper
        s3_match = re.search(r'(https?://(?:s3\.[^/]+|kukufm\.s3|[a-zA-Z0-9.\-]+\.s3\.[a-zA-Z0-9.\-]+)[^\s"\'\?]+)', poster_url)
        if s3_match:
            raw_s3 = s3_match.group(1)
            candidates.append(raw_s3)
            # 2. High-res CDN request
            candidates.append(f"https://images.cdn.kukufm.com/w:1920/q:100/{raw_s3}")
        # 3. Cleaned CDN URL (strip any downscaling params)
        cleaned_url = re.sub(r'/(?:h:\d+|w:\d+|q:\d+|f:\w+)/', '/', poster_url)
        if cleaned_url not in candidates:
            candidates.append(cleaned_url)
        # 4. Original provided URL
        if poster_url not in candidates:
            candidates.append(poster_url)
        return candidates

    async def download_poster(self, poster_url: str, dest_path: str) -> bool:
        """
        Downloads high-resolution master poster art (PrimeXMasoom Method):
        - Resolves raw S3 master JPEG instead of compressed webp thumbnail
        - Normalizes RGBA / P alpha transparency over black background
        - Upscales to Full HD / UHD up to 1920p using Lanczos filter
        - Saves as crystal-clear JPEG (quality=95, optimize=True)
        """
        if not poster_url:
            return False
        candidates = self.get_hq_poster_url_candidates(poster_url)
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        raw_bytes = None

        async with aiohttp.ClientSession(headers=headers) as session:
            for cand in candidates:
                try:
                    async with session.get(cand, timeout=12) as resp:
                        if resp.status == 200:
                            data = await resp.read()
                            if len(data) > 1024:
                                raw_bytes = data
                                logger.info(f"🖼️ Downloaded HQ poster from: {cand} ({len(data)//1024} KB)")
                                break
                except Exception:
                    continue

        if not raw_bytes:
            logger.warning(f"Could not download poster from candidates for {poster_url}")
            return False

        try:
            from PIL import Image, ImageEnhance
            import io
            img = Image.open(io.BytesIO(raw_bytes))
            if img.mode in ("RGBA", "P"):
                bg = Image.new("RGBA", img.size, (0, 0, 0, 255))
                img = Image.alpha_composite(bg, img.convert("RGBA"))
            img = img.convert("RGB")
            w, h = img.size
            target_w, target_h = w, h
            if h > w and h < 1920:
                scale = 1920 / h
                target_h = 1920
                target_w = max(1, int(round(w * scale)))
            elif w >= h and w < 1920:
                scale = 1920 / w
                target_w = 1920
                target_h = max(1, int(round(h * scale)))

            if (target_w, target_h) != (w, h):
                img = img.resize((target_w, target_h), Image.Resampling.LANCZOS)

            # Subtle sharpness enhancement for crisp text and edges
            try:
                enhancer = ImageEnhance.Sharpness(img)
                img = enhancer.enhance(1.15)
            except Exception:
                pass

            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            img.save(dest_path, "JPEG", quality=95, optimize=True)
            return True
        except Exception as e:
            logger.warning(f"Failed to process HQ poster: {e}")
            try:
                with open(dest_path, "wb") as f:
                    f.write(raw_bytes)
                return True
            except Exception:
                return False

    async def process_single_show(
        self,
        slug: str,
        show_meta: Dict[str, Any],
        done_count: int,
        total_count: int,
    ) -> bool:
        """Processes a single pending show from API resolution through download and upload."""
        title = show_meta.get("title") or slug
        show_type = show_meta.get("type", "reel")
        language = show_meta.get("language", "Hindi").title()
        published_on = show_meta.get("published_on", "N/A")

        self.current_show_title = title
        self.current_show_slug = slug

        # Workspace creation
        clean_slug = re.sub(r'[^a-zA-Z0-9_\-]', '', slug) or "job"
        work_dir = TEMP_DIR / f"job_{clean_slug}"
        work_dir.mkdir(parents=True, exist_ok=True)
        self.current_workspace = work_dir

        is_audio = (show_type.lower() == "audio_book")
        fmt_label = "MP3 (AudioBook 🎙️)" if is_audio else "MP4 (Reel 🎬)"

        # Initial card
        fetch_src = str(FETCH_SOURCE).lower()
        src_label = "Lipat Live API" if fetch_src == "lipat" else "KuKu API Flow"
        header_card = (
            f"🎬 **BATCH PROGRESS: [ {done_count + 1} / {total_count} ]**\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📖 **Show:** `{title}`\n\n"
            f"🎞 **Total Episodes:** `{show_meta.get('total_episodes', '?')}` Episodes\n"
            f"🌐 **Language:** {language}\n"
            f"📅 **Published On:** {published_on}\n"
            f"📦 **Format:** {fmt_label}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⚡️ **Status:** ⏳ Fetching show data from {src_label}..."
        )
        await self.send_fresh_card(header_card)

        try:
            # 1. Resolve Show ID
            show_id = str(show_meta.get("id") or "").strip()
            if not show_id:
                show_id = await self.resolve_show_id_fallback(slug)
            if not show_id:
                logger.warning(f"Could not resolve Show ID for '{slug}'. Skipping.")
                return False

            episodes_data = []
            poster_url = show_meta.get("poster")
            show_obj = None

            # ─── MODE A: LIPAT LIVE CLOUD DATABASE (Default) ───
            if fetch_src == "lipat":
                logger.info(f"📡 Fetching show '{title}' (ID: {show_id}) from Lipat Live API...")
                lipat_data = await lipat_mgr.fetch_show(show_id=str(show_id), platform_name="KuKu")
                if lipat_data:
                    raw_eps = lipat_data.get("episodes_data") or lipat_data.get("episodes_list") or []
                    has_locked, locked_nums = lipat_mgr.check_episodes_locked(raw_eps)
                    if has_locked:
                        logger.warning(
                            f"⚠️ Lipat API returned {len(locked_nums)} locked episodes for '{title}'. "
                            f"Attempting self-healing: deleting from Lipat DB and refetching..."
                        )
                        await self.update_status_card(f"🔄 **Locked episodes detected on Lipat! Purging & refetching fresh streams...**")
                        # 1. Delete stale show from Lipat DB
                        await lipat_mgr.delete_show(show_id=str(show_id), platform_name="KuKu")
                        await asyncio.sleep(3)

                        # 2. Refetch directly from Lipat GET API (Lipat backend re-scrapes fresh unlocked streams)
                        refetched = await lipat_mgr.fetch_show(show_id=str(show_id), platform_name="KuKu")
                        if refetched:
                            ref_eps = refetched.get("episodes_data") or refetched.get("episodes_list") or []
                            ref_locked, _ = lipat_mgr.check_episodes_locked(ref_eps)
                            if not ref_locked and ref_eps:
                                logger.info(f"✅ Lipat successfully refetched unlocked episodes for '{title}'!")
                                show_obj = refetched
                                episodes_data = ref_eps
                                poster_url = refetched.get("show_poster_url") or poster_url
                    else:
                        show_obj = lipat_data
                        episodes_data = raw_eps
                        poster_url = lipat_data.get("show_poster_url") or poster_url

                # If Lipat still has locked eps or show was not found, fallback to local KuKu API Flow if present
                if not episodes_data:
                    logger.info(f"Attempting direct live fallback via KuKu API Flow for '{title}'...")
                    fresh_show = await self._fetch_live_from_kuku_apiflow(show_id, slug)
                    if fresh_show and fresh_show.get("episodes_data"):
                        show_obj = fresh_show
                        episodes_data = fresh_show.get("episodes_data", [])
                        poster_url = fresh_show.get("show_poster_url") or poster_url

            # ─── MODE B: DIRECT KUKU API FLOW ───
            if not episodes_data or fetch_src == "kuku":
                fresh_show = await self._fetch_live_from_kuku_apiflow(show_id, slug)
                if fresh_show and fresh_show.get("episodes_data"):
                    show_obj = fresh_show
                    episodes_data = fresh_show.get("episodes_data", [])
                    poster_url = fresh_show.get("show_poster_url") or poster_url

            if not episodes_data:
                logger.error(f"❌ Failed to obtain episode data for '{title}' (ID: {show_id}) from {src_label}. Skipping.")
                return False

            total_eps = len(episodes_data)
            poster_url = show_obj.get("show_poster_url") or show_meta.get("poster")

            # Determine Highest Available Quality for Video
            selected_quality = "1080p"
            if not is_audio:
                for ep in episodes_data:
                    q_list = ep.get("available_qualities", [])
                    if "1080p" in q_list:
                        selected_quality = "1080p"
                        break
                    elif "720p" in q_list:
                        selected_quality = "720p"
                        break
                    elif "540p" in q_list:
                        selected_quality = "540p"
                        break

            # 2. Download Poster Art
            cover_path = str(work_dir / "cover.jpg")
            has_cover = await self.download_poster(poster_url, cover_path)
            cover_art_arg = cover_path if has_cover else None

            # 3. Episode Download Progress Callback
            dl_start_time = time.time()

            async def _dl_progress(status_arg: Any = None, *args, **kwargs):
                if isinstance(status_arg, str):
                    status_text = (
                        f"🎬 **BATCH PROGRESS: [ {done_count + 1} / {total_count} ]**\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                        f"📖 **Show:** `{title}`\n\n"
                        f"🎞 **Total Episodes:** `{len(episodes_data)}` Episodes\n"
                        f"🌐 **Language:** {language}\n"
                        f"📅 **Published On:** {published_on}\n"
                        f"📦 **Format:** {fmt_label}\n\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"{status_arg}"
                    )
                    await self.update_status_card(status_text)
                elif isinstance(status_arg, (int, float)) and len(args) >= 1:
                    done_eps = int(status_arg)
                    tot_eps = int(args[0])
                    speed = float(args[1]) if len(args) > 1 else 0.0
                    pct = (done_eps / max(1, tot_eps)) * 100.0
                    bar_str = make_progress_bar(pct)
                    status_text = (
                        f"🎬 **BATCH PROGRESS: [ {done_count + 1} / {total_count} ]**\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                        f"📖 **Show:** `{title}`\n\n"
                        f"🎞 **Total Episodes:** `{tot_eps}` Episodes\n"
                        f"🌐 **Language:** {language}\n"
                        f"📅 **Published On:** {published_on}\n"
                        f"📦 **Format:** {fmt_label}\n\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"⚡️ **Status:** Downloading Episodes ({done_eps}/{tot_eps} @ {selected_quality if not is_audio else '48kHz'})...\n\n"
                        f"{bar_str}\n"
                        f"🚀 `{speed:.2f} MB/s`  •  `{done_eps}/{tot_eps}` Downloaded\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━"
                    )
                    await self.update_status_card(status_text)

            # 4. Download All Episodes
            downloaded_paths = await download_all_episodes(
                episodes=episodes_data,
                work_dir=str(work_dir),
                is_audio=is_audio,
                show_title=title,
                artist_name=RELEASE_TAG,
                cover_art_path=cover_art_arg,
                progress_cb=_dl_progress,
                selected_quality=selected_quality,
            )

            if not downloaded_paths or len(downloaded_paths) == 0:
                logger.error(f"Failed to download episodes for '{title}'.")
                return False

            if self.stop_requested:
                return False

            # 5. Merging
            merge_card = (
                f"🎬 **BATCH PROGRESS: [ {done_count + 1} / {total_count} ]**\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"📖 **Show:** `{title}`\n\n"
                f"🎞 **Total Episodes:** `{len(downloaded_paths)}` Episodes\n"
                f"🌐 **Language:** {language}\n"
                f"📅 **Published On:** {published_on}\n"
                f"📦 **Format:** {fmt_label}\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"⚡️ **Status:** ⚙️ Merging episodes into combined release file..."
            )
            await self.update_status_card(merge_card, force=True)

            wm_injected_time = None
            if is_audio:
                merged_file, wm_injected_time = await merge_audio_episodes(
                    ep_files=downloaded_paths,
                    out_dir=str(work_dir),
                    show_title=title,
                    language=language,
                    cover_art_path=cover_art_arg,
                )
                duration_sec = int(get_audio_duration(merged_file)) if merged_file else 0
            else:
                async def _merge_status_cb(msg: str):
                    await self.update_status_card(
                        f"🎬 **BATCH PROGRESS: [ {done_count + 1} / {total_count} ]**\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                        f"📖 **Show:** `{title}`\n\n"
                        f"🎞 **Total Episodes:** `{len(downloaded_paths)}` Episodes\n"
                        f"🌐 **Language:** {language}\n"
                        f"📅 **Published On:** {published_on}\n"
                        f"📦 **Format:** {fmt_label}\n\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"⚡️ **Status:** ⚙️ {msg}"
                    )

                merged_file, duration_sec = await merge_video_episodes(
                    ep_files=downloaded_paths,
                    out_dir=str(work_dir),
                    show_title=title,
                    language=language,
                    quality=selected_quality,
                    status_cb=_merge_status_cb,
                )

            if not merged_file or not os.path.exists(merged_file):
                logger.error(f"Merging failed for '{title}'.")
                return False

            if self.stop_requested:
                return False

            # 6. Upload
            _, active_bot_name = bot_mgr.get_active_bot()
            combined_filename = os.path.basename(merged_file)
            total_bytes = os.path.getsize(merged_file)
            total_mb = total_bytes / (1024 * 1024)
            if (FORCE_PREMIUM_UPLOAD or total_bytes > MAX_BOT_FILE_SIZE_BYTES) and bot_mgr.user_client:
                active_bot_name = "4GB Premium Session"

            upload_start_time = time.time()
            last_bytes = 0

            async def _upload_progress(current: int, total: int):
                nonlocal last_bytes
                now = time.time()
                elapsed = max(0.5, now - upload_start_time)
                speed_mb = (current / elapsed) / (1024 * 1024)
                pct = (current / max(1, total)) * 100.0
                bar_str = make_progress_bar(pct)
                cur_mb = current / (1024 * 1024)
                tot_mb = total / (1024 * 1024)

                card_text = (
                    f"🎬 **BATCH PROGRESS: [ {done_count + 1} / {total_count} ]**\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"📖 **Show:** `{title}`\n\n"
                    f"🎞 **Total Episodes:** `{len(downloaded_paths)}` Episodes\n"
                    f"🌐 **Language:** {language}\n"
                    f"📅 **Published On:** {published_on}\n"
                    f"📦 **Format:** {fmt_label}\n\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"⚡️ **Status:** Uploading Combined File via {active_bot_name}...\n\n"
                    f"{bar_str}\n"
                    f"🚀 `{speed_mb:.2f} MB/s`  •  `{cur_mb:.2f} MB / {tot_mb:.2f} MB`\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━"
                )
                await self.update_status_card(card_text)

            # Prepare optimal Telegram HD thumbnail & 1920p Full UHD Video Cover (PrimeXMasoom Standard)
            final_thumb_path = str(work_dir / "telegram_thumb.jpg")
            final_cover_path = str(work_dir / "uhd_cover.jpg")
            thumb_to_send = None
            cover_to_send = None

            if cover_art_arg and os.path.exists(cover_art_arg):
                thumb_to_send = optimize_telegram_thumb(cover_art_arg, final_thumb_path, max_side=1280)
                cover_to_send = process_kuku_style_poster(cover_art_arg, final_cover_path, target_max=1920) or make_hd_cover(cover_art_arg, final_cover_path, max_side=1920)

            v_width, v_height = 0, 0
            if not is_audio:
                meta = probe_video_metadata(merged_file)
                v_width = int(meta.get("width", 1080) or 1080)
                v_height = int(meta.get("height", 1920) or 1920)
                if not thumb_to_send or not os.path.exists(thumb_to_send):
                    thumb_to_send = extract_video_frame(merged_file, final_thumb_path, at_sec=5.0)

            caption = f"**{combined_filename}**"
            upload_ok = await bot_mgr.upload_and_distribute(
                file_path=merged_file,
                caption=caption,
                is_audio=is_audio,
                thumbnail_path=thumb_to_send or cover_art_arg,
                video_cover_path=cover_to_send,
                duration=duration_sec,
                width=v_width,
                height=v_height,
                performer=WATERMARK_TEXT,
                audio_title=title,
                progress_cb=_upload_progress,
            )

            if not upload_ok:
                logger.error(f"Upload distribution failed for '{title}'.")
                return False

            # 7. Mark Completed in Catalog & Clean up
            self.save_catalog_show_completed(slug)

            # Cleanup job workspace
            try:
                shutil.rmtree(work_dir, ignore_errors=True)
                self.current_workspace = None
            except Exception:
                pass

            # 8. Send Done Card
            wm_line = (
                f"🎙️ **Watermark:** Injected at `{wm_injected_time}` ✅\n"
                if wm_injected_time else
                (f"🎬 **Watermark:** Injected `{WATERMARK_TEXT}` badge ✅\n" if ENABLE_WATERMARK else "")
            )

            done_card = (
                f"🎬 **BATCH PROGRESS: [ {done_count + 1} / {total_count} ]**\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"📖 **Show:** `{title}`\n\n"
                f"🎞 **Total Episodes:** `{len(downloaded_paths)}` Episodes\n"
                f"🌐 **Language:** {language}\n"
                f"📅 **Published On:** {published_on}\n"
                f"📦 **Format:** {fmt_label}\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"⚡️ **Status:** ✅ Completed & Uploaded to Telegram!\n\n"
                f"📦 **File:** `{combined_filename}`\n"
                f"{wm_line}"
                f"🗑️ Local episode & combined files cleared ✨\n"
                f"💾 Marked completed in master catalog.\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━"
            )
            await self.send_fresh_card(done_card)
            return True

        except Exception as e:
            logger.error(f"Unexpected error processing show '{slug}': {e}", exc_info=True)
            return False
        finally:
            if self.current_workspace and self.current_workspace.exists():
                shutil.rmtree(self.current_workspace, ignore_errors=True)

    async def run_batch_loop(self):
        """Sequential loop: picks next pending show, downloads, merges, uploads, repeats."""
        if self.is_running:
            logger.warning("Batch is already running!")
            return

        self.is_running = True
        self.stop_requested = False
        logger.info("▶️ Auto DL Batch loop started!")

        try:
            while self.is_running and not self.stop_requested:
                catalog = self.load_catalog()
                if not catalog:
                    logger.error("Empty catalog. Stopping batch.")
                    break

                # Count statistics
                total_shows = len(catalog)
                completed_count = sum(1 for v in catalog.values() if v.get("status") == "completed")
                pending_shows = [(k, v) for k, v in catalog.items() if v.get("status") == "pending"]

                if not pending_shows:
                    logger.info("🎉 All shows in catalog are completed!")
                    try:
                        await bot_mgr.main_bot.send_message(
                            ADMIN_ID,
                            "🎉 **All Shows Complete!**\n\n"
                            f"Catalog ke saare `{total_shows}` shows successfully upload ho chuke hain! ✨"
                        )
                    except Exception:
                        pass
                    break

                # Pick the first pending show
                next_slug, next_meta = pending_shows[0]
                logger.info(f"👉 Next Pending Show: '{next_meta.get('title')}' ({next_slug}) [Done: {completed_count}/{total_shows}]")

                success = await self.process_single_show(
                    slug=next_slug,
                    show_meta=next_meta,
                    done_count=completed_count,
                    total_count=total_shows,
                )

                if self.stop_requested:
                    logger.info("🛑 Batch loop received stop request.")
                    break

                # Brief rest between shows
                await asyncio.sleep(2)

        finally:
            self.is_running = False
            logger.info("⏹ Auto DL Batch loop ended.")

    async def stop(self):
        """Immediately stops the batch engine and cleans workspace."""
        self.stop_requested = True
        self.is_running = False
        if self.current_workspace and self.current_workspace.exists():
            try:
                shutil.rmtree(self.current_workspace, ignore_errors=True)
                logger.info(f"🗑️ Cleaned active workspace: {self.current_workspace}")
            except Exception:
                pass


# Global engine instance
engine = AutoDLEngine()
