# 🎬 PrimeX KuKu TV & KuKu FM Auto DL & Multi-Channel Upload System

Complete autonomous pipeline that scans pending KuKu shows, extracts metadata & episodes via **KuKu API Flow**, downloads, merges, watermarks, and uploads to **4 Telegram Channels** with **Dual-Bot Rotation & 4GB String Session** support.

---

## 📁 Directory Structure

```
kuku_apiflow/auto_dl/
├── bot_config.py            # Central configuration loader
├── config.env               # API credentials, bot tokens, channels & thresholds
├── bot_manager.py           # Pyrogram client lifecycle, dual-bot rotation & upload router
├── downloader.py            # High-speed parallel video/audio downloader
├── merger.py                # FFmpeg concat, ID3 tagging & release naming engine
├── watermark.py             # Voice watermark injection & moving video watermark badge
├── engine.py                # Master batch loop, catalog updater & card renderer
├── main.py                  # Entrypoint & Telegram command handlers
├── run.sh                   # Startup wrapper script
├── assets/
│   └── audio_watermark.mp3  # Voice watermark injected into audiobooks
├── all_kuku_shows.json      # Master database of all 18,631 shows
└── log.txt                  # Realtime execution log (reset on startup)
```

---

## ⚙️ Features & Architecture

### 1️⃣ Dual-Bot Rotation (Flood Prevention)
* **Main Bot:** `@DramaAutoDLBot` (Token: `9292048532:...`)
* **Helper Bot:** `@DramaAutoDL2Bot` (Token: `9076068614:...`)
* **Rotation:** Every `50` shows (configurable via `BOT_SWITCH_COUNT`), the upload worker switches between Main Bot and Helper Bot.
* **Notification:** When rotation occurs, an alert card is sent to Admin:
  `🔄 Bot rotation: @DramaAutoDLBot completed 50 shows. Next 50 will be uploaded via @DramaAutoDL2Bot!`

### 2️⃣ 4GB User Session Engine
* Files **<= 1.89 GB:** Uploaded via currently active bot (`Main` or `Helper`).
* Files **> 1.89 GB:** Automatically routed through the **4GB Premium User Session** (`USER_SESSION_STRING`).

### 3️⃣ Multi-Channel Distribution (4 Channels)
* **Primary Channel:** `-1004472483606`
* Uploads file to Primary Channel with live progress card (updated every 6s).
* Once uploaded, copies/forwards to 3 secondary channels with a **3-second delay** between each:
  1. `-1003714357910` (Wait 3s)
  2. `-1003851222588` (Wait 3s)
  3. `-1004367653974` (Wait 3s)

### 4️⃣ Release Naming & Watermarking
* **Audiobook:**
  `Show S01 [E01-E23] COMBiNED KUKU WEB-DL Malayalam AudioBook - @PrimeXDramaBot.mp3`
  * Voice watermark injected at middle episode boundary (`[HH:MM:SS]`).
* **Video Reel:**
  `Show S01 [E01-E110] COMBiNED 1080p KUKU WEB-DL Hindi AAC.2.0 H.264 - @PrimeXDramaBot.mp4`
  * Always selects **MAX available quality** (1080p > 720p > 540p).
  * Moving badge overlay with `TG : @PrimeXOfficial`.

### 5️⃣ Instant Cleanup & Catalog Update
* Show marked `"status": "completed"` in `all_kuku_shows.json` atomically.
* All downloaded episodes, temp files, and merged files deleted immediately upon upload.

---

## 🤖 Telegram Bot Commands (Admin `8544694038` Only)

| Command | Action |
| :--- | :--- |
| `/suru` or `/start` | Starts the autonomous download and upload loop from next pending show |
| `/stop` | Immediately halts current downloads, cleans local files, and pauses batch |
| `/status` | Shows real-time statistics (completed count, pending count, current bot, current show) |
| `/switch` | Manually toggles upload worker between Main Bot and Helper Bot |
| `/restart` | Safely aborts active tasks, cleans temp files, runs `git pull`, and reboots process cleanly |

---

## 🚀 How to Run

```bash
cd /Users/deepakbajwa/Desktop/PrimeXStage/kuku_apiflow/auto_dl

# Start the bot
python3 main.py
```
Or run in background with `screen`:
```bash
screen -S AutoDL
python3 main.py
# Detach with Ctrl + A, D
```

