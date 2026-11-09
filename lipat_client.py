#!/usr/bin/env python3
"""
================================================================================
🎬 Lipat Live API Client & Self-Healing Engine for Auto DL
================================================================================
Author: PrimeX / Deepak Bajwa
Description: Backed directly by Lipat Live Cloud Database (https://lipat.live).
Provides async API methods for:
  1. fetch_show()   -> GET  https://lipat.live/api/get-show-data
  2. delete_show()  -> POST https://lipat.live/api/delete-show-data
  3. upload_show()  -> POST https://lipat.live/api/upload-show-data
  4. check_episodes_locked() -> Detects locked episodes (PrimeXMasoom Standard)
================================================================================
"""

import aiohttp
import asyncio
import json
import logging
import re
import socket
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("primex.lipat_client")

LIPAT_BASE_URL = "https://lipat.live/api"
LIPAT_FETCH_ENDPOINT = f"{LIPAT_BASE_URL}/get-show-data"
LIPAT_DELETE_ENDPOINT = f"{LIPAT_BASE_URL}/delete-show-data"
LIPAT_UPLOAD_ENDPOINT = f"{LIPAT_BASE_URL}/upload-show-data"
LIPAT_ADMIN_TG_ID = "999888777"

LIPAT_HEADERS = {
    "User-Agent": "PrimeXBotUploader/1.0 (Macintosh; Intel Mac OS X 10_15_7)",
    "Accept": "application/json",
    "Content-Type": "application/json",
}

LIPAT_ALLOWED_COLUMNS = {
    "platform_name", "show_id", "show_url", "show_name", "show_slug",
    "show_format", "show_language", "show_genre", "show_tags",
    "show_description", "show_status", "show_release_date",
    "show_poster_url", "total_episodes", "show_duration_sec",
    "show_extra", "episodes_data"
}


class LipatClient:
    def __init__(self, admin_tg_id: str = LIPAT_ADMIN_TG_ID, user_tg_id: str = "8544694038"):
        self.admin_tg_id = str(admin_tg_id or LIPAT_ADMIN_TG_ID).strip()
        self.user_tg_id = str(user_tg_id or "8544694038").strip()

    def _get_connector(self) -> aiohttp.TCPConnector:
        """Forces AF_INET (IPv4) to avoid Cloudflare IPv6 timeout on macOS."""
        return aiohttp.TCPConnector(family=socket.AF_INET)

    async def fetch_show(
        self,
        show_id: str,
        platform_name: str = "KuKu",
        tg_id: Optional[str] = None,
        show_url: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Fetches show metadata and complete multi-quality episodes list from Lipat Live API.
        Endpoint: GET https://lipat.live/api/get-show-data
        """
        clean_id = str(show_id or "").strip()
        user_id = str(tg_id or self.user_tg_id).strip()
        params = {
            "tg_id": user_id,
            "platform_name": platform_name,
            "show_id": clean_id,
        }
        if show_url:
            params["show_url"] = show_url

        logger.info(f"📡 Fetching '{clean_id}' ({platform_name}) from Lipat API (tg_id: {user_id})...")
        timeout = aiohttp.ClientTimeout(total=45, connect=15, sock_read=45)
        try:
            async with aiohttp.ClientSession(connector=self._get_connector(), headers=LIPAT_HEADERS, timeout=timeout) as session:
                async with session.get(LIPAT_FETCH_ENDPOINT, params=params) as resp:
                    if resp.status != 200:
                        logger.warning(f"Lipat API returned HTTP {resp.status} for show {clean_id}")
                        return None
                    data = await resp.json()
                    status = data.get("status")
                    if status == "success":
                        show_data = data.get("show_data") or data.get("data") or {}
                        if isinstance(show_data, list) and show_data:
                            show_data = show_data[0]
                        eps = show_data.get("episodes_data") or show_data.get("episodes_list") or []
                        title = show_data.get("show_name") or show_data.get("title") or clean_id
                        logger.info(f"✅ Lipat API returned show '{title}' with {len(eps)} episodes.")
                        return show_data
                    else:
                        logger.warning(f"Lipat API response: {data.get('message', data)}")
                        return None
        except Exception as e:
            logger.error(f"❌ Failed to fetch show {clean_id} from Lipat API: {e}")
            return None

    async def delete_show(
        self,
        show_id: str,
        platform_name: str = "KuKu",
        admin_tg_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Deletes a show from Lipat DB using Admin ID ('999888777') so stale or locked episodes
        are purged and fresh unlocked data can be updated.
        Endpoint: POST https://lipat.live/api/delete-show-data
        """
        clean_id = str(show_id or "").strip()
        admin_id = str(admin_tg_id or self.admin_tg_id).strip()
        payload = {
            "tg_id": admin_id,
            "platform_name": platform_name,
            "show_id": clean_id,
        }
        logger.info(f"🗑️ Deleting show '{clean_id}' ({platform_name}) from Lipat DB (Admin tg_id: {admin_id})...")
        timeout = aiohttp.ClientTimeout(total=20)
        try:
            async with aiohttp.ClientSession(connector=self._get_connector(), headers=LIPAT_HEADERS, timeout=timeout) as session:
                async with session.post(LIPAT_DELETE_ENDPOINT, json=payload) as resp:
                    data = await resp.json()
                    logger.info(f"🗑️ Lipat delete response for {clean_id}: HTTP {resp.status} - {data}")
                    return data
        except Exception as e:
            logger.error(f"❌ Failed to delete show {clean_id} from Lipat: {e}")
            return {"status": "error", "message": str(e)}

    async def upload_show(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Uploads fresh show metadata and episode stream manifests to Lipat Database.
        Endpoint: POST https://lipat.live/api/upload-show-data
        """
        sanitized = {k: v for k, v in payload.items() if k in LIPAT_ALLOWED_COLUMNS}
        if "show_status" in sanitized and sanitized["show_status"] not in ("pending", "active", "completed"):
            sanitized["show_status"] = "pending"
        elif "show_status" not in sanitized:
            sanitized["show_status"] = "pending"

        extra = dict(sanitized.get("show_extra") or {})
        for k, v in payload.items():
            if k not in LIPAT_ALLOWED_COLUMNS and k != "id":
                extra[k] = v
        sanitized["show_extra"] = extra

        show_title = sanitized.get("show_name") or sanitized.get("title") or sanitized.get("show_id")
        logger.info(f"📤 Uploading fresh show '{show_title}' to Lipat API...")
        timeout = aiohttp.ClientTimeout(total=60)
        try:
            async with aiohttp.ClientSession(connector=self._get_connector(), headers=LIPAT_HEADERS, timeout=timeout) as session:
                async with session.post(LIPAT_UPLOAD_ENDPOINT, json=sanitized) as resp:
                    data = await resp.json()
                    if data.get("status") == "success":
                        rec_id = data.get("data", {}).get("id") or data.get("id") or "N/A"
                        logger.info(f"✅ Show uploaded to Lipat! Record ID: {rec_id}")
                    else:
                        logger.warning(f"⚠️ Lipat upload returned: {data}")
                    return data
        except Exception as e:
            logger.error(f"❌ Failed to upload show to Lipat API: {e}")
            return {"status": "error", "message": str(e)}

    @staticmethod
    def check_episodes_locked(episodes_data: List[Dict[str, Any]]) -> Tuple[bool, List[int]]:
        """
        Locked Episodes Guard (Exact PrimeXMasoom Standard).
        Detects whether any episode is DRM/play-locked or missing a valid stream URL.
        Returns: (has_locked, list_of_locked_episode_numbers)
        """
        locked_eps = []
        for idx, ep in enumerate(episodes_data, 1):
            ep_no = ep.get("episode_number") or ep.get("index") or idx
            is_locked = (
                bool(ep.get("locked"))
                or bool(ep.get("is_locked"))
                or bool(ep.get("is_play_locked"))
                or str(ep.get("locked", "")).strip().lower() in ("true", "1")
                or not bool(
                    ep.get("video_url")
                    or ep.get("master_stream_url")
                    or ep.get("stream_url")
                    or ep.get("audio_url")
                    or ep.get("video_url_1080p")
                    or ep.get("video_url_720p")
                )
            )
            if is_locked:
                locked_eps.append(int(ep_no))

        return (len(locked_eps) > 0), locked_eps


# Singleton instance
lipat_mgr = LipatClient()

