#!/usr/bin/env python3
"""
================================================================================
🎬 PrimeX KuKu TV & KuKu FM Auto DL & Multi-Channel Upload Bot
================================================================================
Entrypoint: python3 main.py
Commands:
  /suru     - Starts the autonomous download & upload batch
  /stop     - Stops the batch immediately and clears active files
  /restart  - Pulls git updates, cleans up, and restarts bot process
  /status   - Displays real-time catalog & worker statistics
  /switch   - Manually toggle upload worker between Main & Helper bots
================================================================================
"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

# Fix Python 3.14 Pyrogram import event loop
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

from pyrogram import filters, idle
from pyrogram.types import Message

from bot_config import (
    ADMIN_ID,
    MAIN_BOT_NAME,
    HELPER_BOT_NAME,
    PRIMARY_CHANNEL_ID,
    FORWARD_CHANNEL_IDS,
    BOT_SWITCH_COUNT,
    ENABLE_WATERMARK,
    WATERMARK_TEXT,
    RELEASE_TAG,
)
from logger import logger
from bot_manager import bot_mgr
from engine import engine


batch_task: asyncio.Task = None


@bot_mgr.main_bot.on_message(filters.command(["start", "suru"]) & filters.private)
async def handle_start(client, message: Message):
    global batch_task
    if message.from_user.id != ADMIN_ID:
        return await message.reply_text("⛔️ Access Denied. Sirf Admin is bot ko chala sakta hai.")

    if engine.is_running:
        return await message.reply_text(
            f"⚠️ **Batch Already Running Hai!**\n\n"
            f"📖 **Current Show:** `{engine.current_show_title or 'Processing...'}`\n"
            f"💡 Rokne ke liye `/stop` likhein."
        )

    await message.reply_text(
        f"🚀 **KUKU TV AUTO DL SYSTEM STARTED!**\n\n"
        f"📡 **Primary Upload Channel:** `{PRIMARY_CHANNEL_ID}`\n"
        f"📢 **Forward Channels:** `{len(FORWARD_CHANNEL_IDS)} Channels` (Delay: 3s)\n"
        f"🤖 **Upload Worker:** {bot_mgr.get_active_bot()[1]} (Rotates every {BOT_SWITCH_COUNT} shows)\n"
        f"🏷 **Release Tag:** `{RELEASE_TAG}`\n"
        f"🎙 **Watermark:** `{'ON (' + WATERMARK_TEXT + ')' if ENABLE_WATERMARK else 'OFF'}`\n\n"
        f"Pending shows ka auto-download shuru ho raha hai..."
    )

    batch_task = asyncio.create_task(engine.run_batch_loop())


@bot_mgr.main_bot.on_message(filters.command(["stop"]) & filters.private)
async def handle_stop(client, message: Message):
    global batch_task
    if message.from_user.id != ADMIN_ID:
        return

    if not engine.is_running:
        return await message.reply_text("ℹ️ Batch abhi run nahi ho raha hai.")

    stopping_show = engine.current_show_title
    await message.reply_text(f"⏳ **Stopping Auto DL Engine...** (`{stopping_show}` ko abort karke cleanup kiya ja raha hai)")

    await engine.stop()
    if batch_task and not batch_task.done():
        batch_task.cancel()

    await message.reply_text(
        "🛑 **Batch Stopped Successfully!**\n\n"
        "✨ Local temporary files aur active downloads clear kar diye gaye hain.\n"
        "Aap kabhi bhi `/suru` karke agle pending show se resume kar sakte hain."
    )


@bot_mgr.main_bot.on_message(filters.command(["status"]) & filters.private)
async def handle_status(client, message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    catalog = engine.load_catalog()
    total = len(catalog)
    completed = sum(1 for v in catalog.values() if v.get("status") == "completed")
    pending = total - completed

    _, active_bot_name = bot_mgr.get_active_bot()
    status_str = "🟢 RUNNING" if engine.is_running else "🔴 STOPPED"

    msg = (
        f"📊 **AUTO DL SYSTEM STATUS**\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚡️ **Engine State:** `{status_str}`\n"
        f"📖 **Current Show:** `{engine.current_show_title or 'None'}`\n"
        f"📁 **Total Shows in DB:** `{total}`\n"
        f"✅ **Completed:** `{completed}`\n"
        f"⏳ **Pending:** `{pending}`\n\n"
        f"🤖 **Active Upload Bot:** `{active_bot_name}`\n"
        f"🔄 **Current Bot Batch:** `{bot_mgr.current_upload_count} / {BOT_SWITCH_COUNT}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )
    await message.reply_text(msg)


@bot_mgr.main_bot.on_message(filters.command(["switch"]) & filters.private)
async def handle_switch(client, message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    async with bot_mgr.lock:
        old_role = bot_mgr.active_bot_role
        bot_mgr.active_bot_role = "helper" if old_role == "main" else "main"
        bot_mgr.current_upload_count = 0
        _, new_name = bot_mgr.get_active_bot()

    await message.reply_text(f"🔄 **Bot Switched Manually!**\nUpload worker ab `{new_name}` par set ho gaya hai.")


RESTART_MSG_FILE = Path(__file__).resolve().parent / ".restartmsg"


def get_current_branch() -> str:
    """Dynamically returns the currently active git branch."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except Exception:
        pass
    return "main"


def get_current_commit() -> str:
    """Returns the short commit hash of the current HEAD."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except Exception:
        pass
    return "HEAD"


async def check_restart_notification(client):
    """If .restartmsg exists on boot, edits message to confirm successful restart (PrimeXMasoom style)."""
    if not RESTART_MSG_FILE.exists():
        return
    try:
        with open(RESTART_MSG_FILE, "r", encoding="utf-8") as f:
            lines = [l.strip() for l in f if l.strip()]
        if len(lines) >= 2:
            chat_id = int(lines[0])
            msg_id = int(lines[1])
            branch = get_current_branch()
            commit = get_current_commit()

            import datetime
            tz_ist = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
            now = datetime.datetime.now(tz_ist)
            date_str = now.strftime("%d/%m/%y")
            time_str = now.strftime("%I:%M:%S %p")

            text = (
                f"⌬ **_Restarted Successfully!_**\n"
                f"┎ **Branch:** `{branch}` (`{commit}`)\n"
                f"┠ **Date:** {date_str}\n"
                f"┠ **Time:** {time_str}\n"
                f"┠ **TimeZone:** Asia/Kolkata\n"
                f"┖ **Status:** All systems operational 🟢"
            )
            await client.edit_message_text(chat_id=chat_id, message_id=msg_id, text=text)
            logger.info(f"Updated restart status message in chat {chat_id} (msg {msg_id})")
    except Exception as e:
        logger.error(f"Failed to send restart notification: {e}")
    finally:
        try:
            if RESTART_MSG_FILE.exists():
                RESTART_MSG_FILE.unlink()
        except Exception:
            pass


@bot_mgr.main_bot.on_message(filters.command(["restart", "r"]) & filters.private)
async def handle_restart(client, message: Message):
    global batch_task
    if message.from_user.id != ADMIN_ID:
        return

    branch = get_current_branch()
    status_msg = await message.reply_text(
        f"🔄 **_Restarting Bot..._**\n\n"
        f"📥 _Pulling latest updates for branch:_ `{branch}`...\n"
        f"⏳ _Please wait a few seconds..._"
    )

    # Stop active engine and clean files
    await engine.stop()
    if batch_task and not batch_task.done():
        batch_task.cancel()

    # Discard local changes to log.txt and pull updates
    try:
        subprocess.run(["git", "checkout", "--", "log.txt"], capture_output=True, text=True, timeout=5)
    except Exception:
        pass

    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        proc = subprocess.run(
            ["git", "pull", "origin", branch],
            capture_output=True,
            text=True,
            timeout=20,
            env=env,
        )
        git_res = proc.stdout.strip() or proc.stderr.strip()
        logger.info(f"Git pull on branch '{branch}': {git_res}")
    except Exception as ge:
        logger.warning(f"Git pull error: {ge}")

    # Write state file for startup notification edit
    try:
        with open(RESTART_MSG_FILE, "w", encoding="utf-8") as f:
            f.write(f"{status_msg.chat.id}\n{status_msg.id}\n")
    except Exception as e:
        logger.error(f"Failed to write {RESTART_MSG_FILE}: {e}")

    # Stop clients and re-exec process in-place
    await bot_mgr.stop()
    os.execl(sys.executable, sys.executable, "main.py")


async def main():
    logger.info("=" * 70)
    logger.info("🚀 INITIALIZING PRIMEX AUTO DL SYSTEM")
    logger.info("=" * 70)

    await bot_mgr.start()
    logger.info(f"👂 Main Bot online & listening for commands from Admin {ADMIN_ID}...")

    # Check if this startup was triggered by /restart
    await check_restart_notification(bot_mgr.main_bot)

    # Send startup message to Admin
    try:
        catalog = engine.load_catalog()
        completed = sum(1 for v in catalog.values() if v.get("status") == "completed")
        pending = len(catalog) - completed

        start_alert = (
            f"🤖 **Auto DL System Online!**\n\n"
            f"👑 **Main Bot:** @{MAIN_BOT_NAME}\n"
            f"🤝 **Helper Bot:** @{HELPER_BOT_NAME}\n"
            f"📊 **Catalog Loaded:** `{len(catalog)} Shows` (Done: `{completed}` | Pending: `{pending}`)\n\n"
            f"👉 Type `/suru` to begin auto-downloads!\n"
            f"👉 Type `/status` to view current progress.\n"
            f"👉 Type `/stop` to halt."
        )
        await bot_mgr.main_bot.send_message(ADMIN_ID, start_alert)
    except Exception as e:
        logger.warning(f"Could not send startup alert to Admin: {e}")

    await idle()
    await bot_mgr.stop()


if __name__ == "__main__":
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        logger.info("Bot manually stopped via Ctrl+C.")
