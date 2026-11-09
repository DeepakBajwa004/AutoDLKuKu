#!/usr/bin/env python3
"""
================================================================================
🤖 Telegram Clients & Rotation Manager for Auto DL
================================================================================
"""

import asyncio
import os
import time
from typing import Callable, Optional, Tuple

import pyrogram
from pyrogram import Client
from pyrogram.errors import FloodWait
from pyrogram.types import Message

from bot_config import (
    API_ID,
    API_HASH,
    MAIN_BOT_TOKEN,
    MAIN_BOT_NAME,
    HELPER_BOT_TOKEN,
    HELPER_BOT_NAME,
    USER_SESSION_STRING,
    PRIMARY_CHANNEL_ID,
    FORWARD_CHANNEL_IDS,
    CHANNEL_DELAY_SECONDS,
    BOT_SWITCH_COUNT,
    MAX_BOT_FILE_SIZE_BYTES,
    ADMIN_ID,
    CUSTOM_CAPTION_CHANNELS,
    CUSTOM_CAPTION_TEXT,
    FORCE_PREMIUM_UPLOAD,
    WATERMARK_TEXT,
)
from logger import logger


class BotManager:
    def __init__(self):
        # Pyrogram Clients with high-speed concurrent transmission
        self.main_bot = Client(
            "auto_dl_main_session",
            api_id=API_ID,
            api_hash=API_HASH,
            bot_token=MAIN_BOT_TOKEN,
            in_memory=True,
            workers=20,
            max_concurrent_transmissions=12,
            sleep_threshold=30,
        )
        if HELPER_BOT_TOKEN and HELPER_BOT_TOKEN != MAIN_BOT_TOKEN:
            self.helper_bot = Client(
                "auto_dl_helper_session",
                api_id=API_ID,
                api_hash=API_HASH,
                bot_token=HELPER_BOT_TOKEN,
                in_memory=True,
                workers=20,
                max_concurrent_transmissions=12,
                sleep_threshold=30,
            )
        else:
            self.helper_bot = self.main_bot
        self.user_client: Optional[Client] = None
        if USER_SESSION_STRING and USER_SESSION_STRING.strip():
            self.user_client = Client(
                "auto_dl_user_session",
                api_id=API_ID,
                api_hash=API_HASH,
                session_string=USER_SESSION_STRING.strip(),
                in_memory=True,
                workers=20,
                max_concurrent_transmissions=12,
                sleep_threshold=30,
            )

        # Rotation State
        self.active_bot_role = "main"  # 'main' or 'helper'
        self.current_upload_count = 0
        self.total_completed_shows = 0
        self.lock = asyncio.Lock()

    async def start(self):
        """Starts all clients."""
        logger.info("🚀 Starting Telegram clients...")
        await self.main_bot.start()
        logger.info(f"✅ Main Bot started: @{MAIN_BOT_NAME}")

        if self.helper_bot is not self.main_bot:
            await self.helper_bot.start()
            logger.info(f"✅ Helper Bot started: @{HELPER_BOT_NAME}")
        else:
            logger.info("ℹ️ Helper Bot shares session with Main Bot.")

        if self.user_client:
            try:
                await self.user_client.start()
                me = await self.user_client.get_me()
                logger.info(f"✅ 4GB User Session ready: {me.first_name} (@{me.username or me.id})")
            except Exception as e:
                logger.error(f"❌ Failed to start 4GB User Client: {e}")

    async def stop(self):
        """Stops all clients."""
        logger.info("Stopping Telegram clients...")
        try:
            await self.main_bot.stop()
        except Exception:
            pass
        if self.helper_bot is not self.main_bot:
            try:
                await self.helper_bot.stop()
            except Exception:
                pass
        if self.user_client:
            try:
                await self.user_client.stop()
            except Exception:
                pass

    def get_active_bot(self) -> Tuple[Client, str]:
        """Returns the currently active bot for uploads based on rotation."""
        if self.active_bot_role == "main":
            return self.main_bot, f"@{MAIN_BOT_NAME}"
        else:
            return self.helper_bot, f"@{HELPER_BOT_NAME}"

    async def record_successful_upload(self) -> Tuple[bool, str, str]:
        """
        Increments upload count and triggers rotation when reaching threshold.
        Returns: (did_switch, old_bot_name, new_bot_name)
        """
        async with self.lock:
            self.current_upload_count += 1
            self.total_completed_shows += 1

            if self.current_upload_count >= BOT_SWITCH_COUNT:
                old_role = self.active_bot_role
                old_name = f"@{MAIN_BOT_NAME}" if old_role == "main" else f"@{HELPER_BOT_NAME}"

                # Switch role
                self.active_bot_role = "helper" if old_role == "main" else "main"
                new_name = f"@{MAIN_BOT_NAME}" if self.active_bot_role == "main" else f"@{HELPER_BOT_NAME}"
                self.current_upload_count = 0

                logger.info(f"🔄 Bot rotation triggered! {old_name} completed {BOT_SWITCH_COUNT} shows. Switching to {new_name}.")
                return True, old_name, new_name

            return False, "", ""

    async def upload_and_distribute(
        self,
        file_path: str,
        caption: str,
        is_audio: bool,
        thumbnail_path: Optional[str] = None,
        video_cover_path: Optional[str] = None,
        duration: int = 0,
        width: int = 0,
        height: int = 0,
        performer: Optional[str] = None,
        audio_title: Optional[str] = None,
        progress_cb: Optional[Callable] = None,
    ) -> bool:
        """
        Uploads file to PRIMARY_CHANNEL_ID and forwards to FORWARD_CHANNEL_IDS with 3s delay.
        Selects 4GB User client if > 1.89GB, else selects active rotation bot.
        """
        if not os.path.exists(file_path):
            logger.error(f"Upload error: File not found {file_path}")
            return False

        fsize = os.path.getsize(file_path)
        use_user_client = (
            (FORCE_PREMIUM_UPLOAD or fsize > MAX_BOT_FILE_SIZE_BYTES)
            and (self.user_client is not None)
        )

        if use_user_client:
            uploader = self.user_client
            uploader_tag = "4GB Premium User Session"
        else:
            uploader, uploader_tag = self.get_active_bot()

        logger.info(f"📤 Uploading ({fsize / (1024*1024):.1f} MB) via {uploader_tag} to Primary Channel {PRIMARY_CHANNEL_ID}...")

        # 1. Primary Upload
        sent_msg: Optional[Message] = None
        for attempt in range(1, 4):
            try:
                if is_audio:
                    sent_msg = await uploader.send_audio(
                        chat_id=PRIMARY_CHANNEL_ID,
                        audio=file_path,
                        caption=caption,
                        performer=performer or WATERMARK_TEXT or "@PrimeXOfficial",
                        title=audio_title or os.path.splitext(os.path.basename(file_path))[0],
                        thumb=thumbnail_path if (thumbnail_path and os.path.exists(thumbnail_path)) else None,
                        duration=duration,
                        progress=progress_cb,
                    )
                else:
                    sent_msg = await uploader.send_video(
                        chat_id=PRIMARY_CHANNEL_ID,
                        video=file_path,
                        caption=caption,
                        thumb=thumbnail_path if (thumbnail_path and os.path.exists(thumbnail_path)) else None,
                        video_cover=video_cover_path if (video_cover_path and os.path.exists(video_cover_path)) else None,
                        duration=duration,
                        width=width if width > 0 else None,
                        height=height if height > 0 else None,
                        supports_streaming=True,
                        progress=progress_cb,
                    )
                if sent_msg:
                    break
            except FloodWait as fw:
                logger.warning(f"⚠️ FloodWait of {fw.value}s encountered during upload. Sleeping...")
                await asyncio.sleep(fw.value + 1)
            except Exception as e:
                logger.warning(f"Upload attempt {attempt} failed via {uploader_tag}: {e}")
                if attempt < 3:
                    await asyncio.sleep(5)

        if not sent_msg:
            logger.error("❌ Failed to upload file to primary channel after 3 attempts.")
            return False

        logger.info(f"✅ Uploaded to Primary Channel {PRIMARY_CHANNEL_ID} (Msg ID: {sent_msg.id})")

        # 2. Distribute to Secondary Channels with 3-second delay
        base_name = os.path.basename(file_path)
        for fwd_channel_id in FORWARD_CHANNEL_IDS:
            try:
                await asyncio.sleep(CHANNEL_DELAY_SECONDS)

                # Check if this channel requires custom bold caption suffix
                if fwd_channel_id in CUSTOM_CAPTION_CHANNELS:
                    custom_cap = f"{caption}\n\n**{CUSTOM_CAPTION_TEXT}**"
                    await self.main_bot.copy_message(
                        chat_id=fwd_channel_id,
                        from_chat_id=PRIMARY_CHANNEL_ID,
                        message_id=sent_msg.id,
                        caption=custom_cap,
                    )
                    logger.info(f"➡️ Copied to Channel {fwd_channel_id} with custom caption (Waited {CHANNEL_DELAY_SECONDS}s)")
                else:
                    await self.main_bot.copy_message(
                        chat_id=fwd_channel_id,
                        from_chat_id=PRIMARY_CHANNEL_ID,
                        message_id=sent_msg.id,
                    )
                    logger.info(f"➡️ Copied to Channel {fwd_channel_id} (Waited {CHANNEL_DELAY_SECONDS}s)")
            except FloodWait as fw:
                logger.warning(f"⚠️ FloodWait of {fw.value}s encountered for channel {fwd_channel_id}. Sleeping...")
                await asyncio.sleep(fw.value + 1)
                try:
                    if fwd_channel_id in CUSTOM_CAPTION_CHANNELS:
                        custom_cap = f"{caption}\n\n**{CUSTOM_CAPTION_TEXT}**"
                        await self.main_bot.copy_message(
                            chat_id=fwd_channel_id,
                            from_chat_id=PRIMARY_CHANNEL_ID,
                            message_id=sent_msg.id,
                            caption=custom_cap,
                        )
                    else:
                        await self.main_bot.copy_message(
                            chat_id=fwd_channel_id,
                            from_chat_id=PRIMARY_CHANNEL_ID,
                            message_id=sent_msg.id,
                        )
                    logger.info(f"➡️ Copied to Channel {fwd_channel_id} after FloodWait sleep")
                except Exception as retry_err:
                    logger.warning(f"⚠️ Retry copy failed for channel {fwd_channel_id}: {retry_err}")
            except Exception as fwd_err:
                logger.warning(f"⚠️ Could not forward to channel {fwd_channel_id}: {fwd_err}")

        # 3. Record upload & check rotation
        did_switch, old_name, new_name = await self.record_successful_upload()
        if did_switch:
            switch_msg = (
                f"🔄 **#Bot_Rotation_Alert**\n\n"
                f"🤖 `{old_name}` ne `{BOT_SWITCH_COUNT}` shows upload kar diye hain.\n"
                f"👉 Agle `{BOT_SWITCH_COUNT}` shows ab `{new_name}` se upload honge!"
            )
            try:
                await self.main_bot.send_message(ADMIN_ID, switch_msg)
            except Exception:
                pass

        return True


# Global bot manager instance
bot_mgr = BotManager()
