#!/usr/bin/env python3
"""
Central Message Templates for PrimeXMasoom.
Single source of truth for all bot notifications, errors, and status messages across all OTT platforms.
"""

import bot_config as cfg
import progress as prog

from typing import Any, Dict, List, Optional


def get_admin_handle() -> str:
    """Returns the formatted admin Telegram username from config/settings."""
    handle = (getattr(cfg, "ADMIN_USERNAME", "") or "").strip()
    if not handle:
        return "@Owner"
    if not handle.startswith("@"):
        handle = f"@{handle}"
    return handle


def msg_start_welcome(user_name: str = "User") -> str:
    bot_name = getattr(cfg, "BOT_NAME", "DramaBot").lstrip("@")
    return (
        f"Hey **{user_name}**,\n\n"
        f"Welcome to **{bot_name}**! 🎬\n\n"
        "Here you can download, watermark & merge shows from supported OTT platforms in high quality directly to your Telegram."
    )



def msg_otts_list(platforms: list) -> str:
    if not platforms:
        plat_lines = "• _No platforms registered currently._"
    else:
        lines = []
        for p in platforms:
            name = p.get("name", "Unknown")
            status = str(p.get("status", "Active")).strip().lower()
            icon = "✅" if status == "active" else "❌"
            lines.append(f"• **{name}** {icon}")
        plat_lines = "\n".join(lines)

    return (
        "📺 **Supported OTT Platforms:**\n\n"
        f"{plat_lines}"
    )


def msg_user_settings(profile: dict, uid: int) -> str:
    name = str(profile.get("name", "")).strip() or "User"
    sub_status = str(profile.get("sub_status", "Active")).strip()
    sub_icon = "🟢" if sub_status.lower() == "active" else "🔴"
    sub_validity = profile.get("sub_validity", 30)

    is_blocked = str(profile.get("block_status", "No")).strip().lower() in ("yes", "true", "1")
    block_display = "🚫 Blocked" if is_blocked else "✅ Normal"

    video_fmt = str(profile.get("video_format", "mp4")).strip().lower()
    if video_fmt.startswith("."):
        video_fmt = video_fmt[1:]

    bot_handle = getattr(cfg, "BOT_NAME", "DramaBot").lstrip("@")
    release_tag = str(profile.get("release_tag", "")).strip()
    if not release_tag or release_tag in ("@PrimeXDramaBot", "PrimeXDramaBot"):
        release_tag = bot_handle
    tag_display = f"`{release_tag}`" if release_tag else "_Default (None)_"

    wm_enabled = str(profile.get("water_mark_enabled", "No")).strip().lower() in ("yes", "true", "1")
    wm_status = "🟢 ON" if wm_enabled else "🔴 OFF"

    wm_name = str(profile.get("water_mark_name", "")).strip()
    if not wm_name or "@PrimeXDramaBot" in wm_name:
        wm_name = f"TG : @{bot_handle}"
    wm_name_display = f"`{wm_name}`" if wm_name else "_None_"

    downloads = profile.get("total_video_downloads", 0)
    limit = profile.get("total_video_limit", 0)

    raw_platforms = profile.get("bot_platform_access") or []
    if isinstance(raw_platforms, str):
        raw_platforms = [p.strip() for p in raw_platforms.replace("[", "").replace("]", "").replace("'", "").replace('"', '').split(",") if p.strip()]

    valid_platforms = [p for p in raw_platforms if p and str(p).strip().lower() != "tg"]
    if valid_platforms:
        platforms_display = "\n".join([f"• **{p}** ✅" for p in valid_platforms])
    else:
        platforms_display = "• _None assigned_"

    return (
        "⚙️ **User Configuration & Preferences**\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 **Account:** {name}\n"
        f"🆔 **Telegram ID:** `{uid}`\n"
        f"📋 **Subscription:** {sub_status} {sub_icon} • `{sub_validity} Days`\n"
        f"🛡️ **Status:** {block_display}\n"
        f"📊 **Usage:** `{downloads:,} / {limit:,}` videos\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎬 **Video Format:** `.{video_fmt.upper()}`\n"
        f"🏷️ **Release Tag:** {tag_display}\n"
        f"🎨 **Watermark:** {wm_status}\n"
        f"✍️ **Watermark Text:** {wm_name_display}\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "📺 **Allowed Platforms:**\n"
        f"{platforms_display}"
    )


def msg_settings_no_profile(uid: int = 0) -> str:
    admin = get_admin_handle()
    return (
        "🔒 **Subscription Required**\n\n"
        f"Contact Owner To Buy: **{admin}**"
    )



def msg_watermark_locked_alert() -> str:
    admin = get_admin_handle()
    return (
        "⚠️ Watermark feature is not included in your subscription plan.\n\n"
        f"To enable watermarking, please contact owner to upgrade: {admin}"
    )


def msg_watermark_not_in_plan() -> str:
    admin = get_admin_handle()
    return (
        "🚫 **Watermark Not Included In Plan**\n\n"
        "Your current subscription plan does not include custom watermarking.\n\n"
        f"Please contact owner to upgrade your plan: **{admin}**"
    )


def msg_watermark_updated(is_on: bool) -> str:
    status_str = "🟢 **ON**" if is_on else "🔴 **OFF**"
    return f"🎨 Watermark setting updated: {status_str}"



def msg_subscription_required(user_id: int = 0) -> str:
    admin = get_admin_handle()
    return (
        "🔒 **Subscription Required**\n\n"
        f"Contact Owner To Buy: **{admin}**"
    )


def msg_platform_not_allowed(platform_name: str) -> str:
    admin = get_admin_handle()
    return (
        "🚫 **Platform Access Not Allowed!**\n\n"
        f"Aapke subscription plan me **{platform_name}** allowed nahi hai.\n\n"
        f"Contact Owner To Upgrade: **{admin}**"
    )


def msg_platform_not_supported(platform_name: str) -> str:
    admin = get_admin_handle()
    return (
        "⚠️ **Platform Currently Not Supported!**\n\n"
        f"**{platform_name}** currently under development / not available yet.\n\n"
        f"Contact Owner: **{admin}**"
    )


def msg_account_blocked() -> str:
    admin = get_admin_handle()
    return (
        "🚫 **Account Blocked!**\n\n"
        "Aapka bot account admin dwara block kar diya gaya hai.\n\n"
        f"Contact Owner: **{admin}**"
    )


def msg_download_limit_exceeded() -> str:
    admin = get_admin_handle()
    return (
        "⚠️ **Video Download Limit Exceeded!**\n\n"
        "Aapki video download limit khatam ho chuki hai.\n"
        "Kripya apna plan upgrade karne ke liye owner se contact karein.\n\n"
        f"Contact Owner To Upgrade: **{admin}**"
    )


def msg_subscription_expired() -> str:
    admin = get_admin_handle()
    return (
        "⏳ **Subscription Expired!**\n\n"
        "Aapka subscription plan expire ya inactive ho chuka hai.\n\n"
        f"Contact Owner To Renew: **{admin}**"
    )


def msg_unrecognized_link() -> str:
    return (
        "❌ **Unrecognized link or platform.**\n\n"
        "Please send a supported show link."
    )


def msg_active_task_running() -> str:
    """When user attempts to send another link while previous is processing."""
    return (
        "⚠️ **A task is already running!**\n\n"
        "You can only run 1 task at a time.\n"
        "Please wait until your current video is completely processed and uploaded."
    )


def msg_cooldown_wait(remaining_sec: int) -> str:
    """When user sends a link within the 5-second post-upload cooldown window."""
    return (
        f"⏳ **Please wait `{remaining_sec}` second(s) before starting a new task!**\n\n"
        "Your previous video was just delivered. Please wait 5 seconds before sending a new link."
    )


def msg_task_cancelled() -> str:
    """When a task is cancelled either at prompt or during processing."""
    return "❌ **Task Cancelled.**"





def msg_fetching_info() -> str:
    """Initial reply when user sends a link."""
    return "Please wait fetching info....."


def msg_processing(show_name: Optional[str] = None, quality: Optional[str] = None) -> str:
    if show_name and quality:
        return f"⏳ **Preparing `{show_name}` ({str(quality).replace('_h264', '')})...**"
    elif show_name:
        return f"⏳ **Preparing `{show_name}`...**"
    return "⏳ **Preparing task...**"


def msg_show_card(
    platform_name: str,
    show_name: str,
    show_id: str,
    total_eps: int,
    lang: str = "English",
) -> str:
    return (
        f"📺 **Platform :** `{platform_name}`\n"
        f"🎬 **Show Name :** **{show_name}**\n"
        f"🆔 **Show Id :** `{show_id}`\n"
        f"📦 **Total Ep :** `{total_eps}`\n"
        f"🌐 **Language :** `{lang}`"
    )


def msg_custom_ep_prompt(show_name: str, total_eps: int) -> str:
    return (
        f"🎯 **{show_name}** (Total: `{total_eps}` episodes)\n\n"
        "Please type and send the episode numbers you want to download:\n\n"
        "• **Single Episode:** e.g. `4` or `10`\n"
        "• **Specific Episodes:** e.g. `4, 10, 15`\n"
        "• **Range of Episodes:** e.g. `4-10`\n\n"
        "_Send your input now or click Back._"
    )


def msg_choose_quality(show_name: str, ep_label: str = "All") -> str:
    return (
        f"🎬 **{show_name}**\n"
        f"📦 **Episodes :** `{ep_label}`\n\n"
        "✨ **Please Choose Your Quality:**"
    )


def msg_choose_format(show_name: str, quality: str, ep_label: str = "All") -> str:
    return (
        f"🎬 **{show_name}**\n"
        f"📦 **Episodes :** `{ep_label}`\n"
        f"🎯 **Quality :** `{str(quality).replace('_h264', '')}`\n\n"
        "📦 **Please Choose Output Format:**"
    )


def msg_choose_destination(show_name: str, fmt_label: str = "Combined", quality: str = "720p") -> str:
    display_fmt = "Combined Release" if fmt_label.lower() in ("combined", "movie") else "Episode-wise"
    return (
        f"🎬 **{show_name}**\n"
        f"⚙️ **Format :** `{display_fmt}`\n"
        f"🎯 **Quality :** `{str(quality).replace('_h264', '')}`\n\n"
        "🚀 **Choose Upload Destination:**\n"
        "Where would you like to receive your file?"
    )


def msg_show_not_available(show_ref: str = "") -> str:
    """When a show is not in local catalog and not found on live API."""
    admin = get_admin_handle()
    ref_part = f" (`{show_ref}`)" if show_ref else ""
    return (
        f"❌ **This show is currently Not Available / Or Not Released.**{ref_part}\n\n"
        f"If You think its our mistake Please contact {admin}"
    )


def msg_kuku_episodes_locked(
    show_name: str,
    total_eps: int,
    locked_count: int,
    unlocked_count: int,
    locked_range_str: str = "",
    unlocked_range_str: str = "",
    admin_handle: Optional[str] = None,
    show_id: Optional[str] = None,
) -> str:
    """Alert when a KuKu show contains locked episodes."""
    admin = admin_handle or get_admin_handle()
    locked_range = f" (`Ep {locked_range_str}`)" if locked_range_str else ""
    unlocked_range = f" (`Ep {unlocked_range_str}`)" if unlocked_range_str else ""
    id_line = f"🆔 **Show ID:** `{show_id}`\n" if show_id else ""
    return (
        f"🔒 **KuKu TV Episode Lock Alert!**\n\n"
        f"🎬 **Show:** `{show_name}`\n"
        f"{id_line}"
        f"📊 **Total Episodes:** `{total_eps}`\n"
        f"❌ **Locked Episodes:** `{locked_count}`{locked_range}\n"
        f"✅ **Unlocked Episodes:** `{unlocked_count}`{unlocked_range}\n\n"
        f"⚠️ *Is show ke kuch episodes KuKu par locked hain ya stream access unavailable hai. Incomplete file download na ho isliye task cancel kar diya gaya hai.*\n\n"
        f"👉 Kripya show ko unlock/fix karwane ke liye owner se contact karein:\n"
        f"👤 **Owner:** **{admin}**"
    )


def msg_download_failed(detail: str = "") -> str:
    admin = get_admin_handle()
    d_str = f"\n`Reason: {detail}`\n" if detail else "\n"
    return (
        f"❌ **Failed to download episodes.**{d_str}\n"
        f"Please try again or contact {admin}"
    )


def msg_merge_failed(detail: str = "") -> str:
    admin = get_admin_handle()
    d_str = f"\n`Reason: {detail}`\n" if detail else "\n"
    return (
        f"❌ **Video merge failed.**{d_str}\n"
        f"Please contact {admin}"
    )


def msg_upload_failed(detail: str = "") -> str:
    admin = get_admin_handle()
    d_str = f"\n`Reason: {detail}`\n" if detail else "\n"
    return (
        f"❌ **Upload failed after retries.**{d_str}\n"
        f"Please contact {admin}"
    )


def msg_something_went_wrong(extra_detail: str = "") -> str:
    """Generic error message with admin contact."""
    admin = get_admin_handle()
    err_line = f"\n`Details: {extra_detail}`\n" if extra_detail else ""
    return (
        f"⚠️ **Something went wrong while processing your request.**{err_line}\n"
        f"Please contact {admin}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Unified live progress card (Download / Merging / Watermark / Uploading)
# ─────────────────────────────────────────────────────────────────────────────
PROGRESS_MARKER = "Sᴛᴀᴛᴜs »"   # lets the status throttle recognise progress cards


def fmt_size(n: float) -> str:
    """94.38MB / 1.32GB / 512KB (trailing zeros trimmed)."""
    n = float(n or 0)
    for unit, div in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if n >= div:
            v = f"{n / div:.2f}".rstrip("0").rstrip(".")
            return f"{v}{unit}"
    return f"{int(n)}B"


def fmt_eta(sec: float) -> str:
    if not sec or sec <= 0 or sec > 86400 * 2:
        return "--"
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s_ = divmod(rem, 60)
    if h:
        return f"{h}h{m}m{s_}s"
    if m:
        return f"{m}m{s_}s"
    return f"{s_}s"


def circle_bar(pct: float, slots: int = 12) -> str:
    """[⬤⬤◕○○○○○○○○○] style bar with quarter-step partial slot."""
    pct = max(0.0, min(100.0, float(pct or 0)))
    exact = pct / 100.0 * slots
    full = int(exact)
    frac = exact - full
    parts = ["⬤"] * full
    if full < slots:
        if frac >= 0.75:
            parts.append("◕")
        elif frac >= 0.5:
            parts.append("◑")
        elif frac >= 0.25:
            parts.append("◔")
        else:
            parts.append("○")
        parts += ["○"] * (slots - len(parts))
    return "[" + "".join(parts[:slots]) + "]"


def _task_by_line() -> str:
    try:
        from core import task_stats
        st = task_stats.current()
    except Exception:
        st = None
    if not st or not st.user_id:
        return ""
    name = _md_safe(st.first_name or st.full_name or str(st.user_id), link_text=True)
    return f"┎ **Tᴀsᴋ Bʏ** {name} ( #ID{st.user_id} )\n"


def upload_status_label() -> str:
    try:
        from core import task_stats
        st = task_stats.current()
        dest = (st.upload_dest if st else "tg") or "tg"
    except Exception:
        dest = "tg"
    return "Uploading (GDrive)" if str(dest).lower() in ("drive", "gdrive") else "Uploading (Telegram)"


def msg_progress_card(
    title: str,
    pct: float,
    status: str,
    done_bytes: float = 0,
    total_bytes: float = 0,
    speed_mb_s: float = 0.0,
    eta_sec: float = 0.0,
    done_str: Optional[str] = None,
    total_str: Optional[str] = None,
    speed_str: Optional[str] = None,
) -> str:
    """
    {title}

    ┎ Tᴀsᴋ Bʏ Name ( #ID123 )
    ┠ [⬤◕○○○○○○○○○○] 16.23%
    ┠ Dᴏɴᴇ » 94.38MB
    ┠ Tᴏᴛᴀʟ » 581.54MB
    ┠ Sᴛᴀᴛᴜs » Download
    ┠ Sᴘᴇᴇᴅ » 3.58MB/s
    ┖ Eᴛᴀ » 2m15s
    """
    pct = max(0.0, min(100.0, float(pct or 0)))
    status_clean, eps_note = _clean_status(status)
    head = _task_by_line()
    first = "┠" if head else "┎"
    done_v = done_str if done_str is not None else fmt_size(done_bytes)
    total_v = total_str if total_str is not None else (fmt_size(total_bytes) if total_bytes else "--")
    speed_v = speed_str if speed_str is not None else (f"{fmt_size(speed_mb_s * 1048576)}/s" if speed_mb_s > 0.005 else "--")
    title_line = _md_safe(title) + (f" [{eps_note}]" if eps_note else "")
    return (
        f"**{title_line}**\n\n"
        f"{head}"
        f"{first} {circle_bar(pct)} {pct:.2f}%\n"
        f"┠ **Dᴏɴᴇ »** {done_v}\n"
        f"┠ **Tᴏᴛᴀʟ »** {total_v}\n"
        f"┠ **{PROGRESS_MARKER}** {status_clean}\n"
        f"┠ **Sᴘᴇᴇᴅ »** {speed_v}\n"
        f"┖ **Eᴛᴀ »** {fmt_eta(eta_sec)}"
    )


def _clean_status(status: str):
    """
    Status shows only the stage (Download / Merging / Watermark / Uploading (Telegram|GDrive)).
    Episode progress of a multi-episode download goes to the title instead: returns (status, eps_note).
    """
    st = str(status or "").strip()
    import re as _re
    m = _re.search(r"(\d+)/(\d+) eps", st)
    if st.startswith("Uploading"):
        return ("Uploading (GDrive)" if "GDrive" in st else "Uploading (Telegram)"), ""
    for base in ("Download", "Merging", "Watermark"):
        if st.startswith(base):
            note = f"{m.group(1)}/{m.group(2)} Eps" if (m and base == "Download") else ""
            return base, note
    return st, ""


def msg_stage_card(title: str, status: str, done: int, total: int, elapsed_sec: float = 0.0,
                   total_bytes: float = 0) -> str:
    """Card for count-based stages (aligning / watermark / merging): Done shows episodes."""
    pct = (done / total * 100.0) if total else 0.0
    eta = (elapsed_sec / done) * (total - done) if (done and elapsed_sec > 0 and done < total) else 0.0
    return msg_progress_card(
        title=title, pct=pct, status=status,
        done_str=f"{done}/{total} eps", total_str=(fmt_size(total_bytes) if total_bytes else f"{total} eps"),
        speed_str="--", eta_sec=eta,
    )


def msg_downloading_progress(
    title: str,
    done: int,
    total: int,
    bytes_so_far: int,
    speed_mb_s: float,
    elapsed_sec: float,
) -> str:
    """Multi-episode download progress (progress card)."""
    left = max(0, total - done)
    avg = (bytes_so_far / done) if done > 0 else 0
    eta = (avg * left) / (speed_mb_s * 1048576) if (speed_mb_s > 0 and avg) else 0
    return msg_progress_card(
        title=title, pct=(done / total * 100.0) if total else 0.0,
        status=f"Download ({done}/{total} eps)", done_bytes=bytes_so_far,
        total_bytes=int(avg * total) if avg else 0, speed_mb_s=speed_mb_s, eta_sec=eta,
    )

def msg_single_downloading_progress(
    title: str,
    label: str,
    pct: float,
    bytes_done: int,
    total_bytes: int = 0,
    speed_mb_s: float = 0.0,
    eta_sec: float = 0.0,
    emoji: str = "🎬",
) -> str:
    """Single file / episode download progress (progress card)."""
    low = (label or "").lower()
    status = "Download · Finalizing" if ("final" in low or "complet" in low) else "Download"
    return msg_progress_card(
        title=title, pct=pct, status=status, done_bytes=bytes_done,
        total_bytes=total_bytes, speed_mb_s=speed_mb_s, eta_sec=eta_sec,
    )

def msg_watermarking_progress(
    title: str,
    done: int,
    total: int,
    current_ep: int,
    elapsed_sec: float = 0.0,
) -> str:
    """Live watermarking progress (progress card)."""
    return msg_stage_card(title, f"Watermark ({done}/{total} eps)", done, total, elapsed_sec)

def msg_merging_progress(
    title: str,
    total_eps: int,
    pct: int = 50,
) -> str:
    """Live merging progress (progress card)."""
    return msg_progress_card(title=title, pct=pct, status="Merging",
                             done_str=f"{total_eps} eps", total_str=f"{total_eps} eps", speed_str="--")

def msg_merging_complete(
    title: str,
    size_str: str,
    merge_time_sec: float,
) -> str:
    """Merging finished (progress card at 100%, upload follows)."""
    size_str = str(size_str).replace(" MB", "MB").replace(" GB", "GB")
    return msg_progress_card(title=title, pct=100, status="Merging · Done",
                             done_str=size_str, total_str=size_str, speed_str="--")

def msg_uploading_progress(
    title: str,
    current_bytes: int,
    total_bytes: int,
    speed_mb_s: float,
    elapsed_sec: float,
) -> str:
    """Live upload progress (progress card; status shows Telegram or GDrive)."""
    rem = max(0, total_bytes - current_bytes)
    eta = rem / (speed_mb_s * 1048576) if speed_mb_s > 0.005 else 0
    return msg_progress_card(
        title=title, pct=(current_bytes / total_bytes * 100.0) if total_bytes else 0.0,
        status=upload_status_label(), done_bytes=current_bytes, total_bytes=total_bytes,
        speed_mb_s=speed_mb_s, eta_sec=eta,
    )

def msg_task_completed(title: str) -> str:
    """Final status banner."""
    return f"🎉 **{title}** — Task Completed! File delivered to your PM."


def msg_choose_codec(title: str, year: str = "", lang: str = "") -> str:
    """Step 1: Codec Selection card text."""
    meta = []
    if year:
        meta.append(f"({year})")
    yr_str = f" {meta[0]}" if meta else ""
    lang_line = f"🌐 **Language:** `{lang}`\n" if lang else ""
    return (
        f"**{title}**{yr_str}\n"
        f"{lang_line}"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⚙️ **Step 1/3: Choose Video Codec**\n\n"
        f"Select your preferred video compression standard:\n"
        f"• **H.264 (AVC):** Maximum universal playback compatibility\n"
        f"• **H.265 (HEVC):** Advanced compression, 40% smaller file size\n\n"
        f"💡 *Default: H.264. Tap Next when ready.*"
    )


def msg_choose_quality_multiselect(
    title: str,
    codec: str,
    selected_qualities: list,
    bitrate_mode: str = "low",
) -> str:
    """Step 2: Multi-quality Selection card text."""
    c_lbl = "H.264 (AVC)" if codec == "H264" else "H.265 (HEVC)"
    sel_str = ", ".join(selected_qualities) if selected_qualities else "None selected"
    return (
        f"**{title}**\n"
        f"⚙️ **Codec:** `{c_lbl}`\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🎞️ **Step 2/3: Choose Video Quality**\n\n"
        f"Tap quality buttons to toggle one or multiple resolutions.\n"
        f"Selected qualities will process in **Low ➔ High** order!\n\n"
        f"📌 **Current Selection:** `{sel_str}`"
    )


def msg_choose_audio_tracks(
    title: str,
    codec: str,
    selected_qualities: list,
    selected_count: int = 0,
    default_audio_label: str = "",
) -> str:
    """Step 3: Multi-audio Selection card text."""
    c_lbl = "H.264 (AVC)" if codec == "H264" else "H.265 (HEVC)"
    q_str = ", ".join(selected_qualities) if selected_qualities else "720p"
    def_line = f"⭐ **Default Playback Track:** `{default_audio_label}`\n" if default_audio_label else ""
    return (
        f"**{title}**\n"
        f"⚙️ **Codec:** `{c_lbl}` · 🎞️ **Qualities:** `{q_str}`\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🎵 **Step 3/3: Choose Audio Tracks**\n\n"
        f"Select audio languages & bitrates to embed.\n"
        f"{def_line}"
        f"💡 **Crucial:** The audio you choose **FIRST** is set as the default sound track on playback, and cached once across all chosen qualities!\n\n"
        f"📦 **Selected Tracks:** `{selected_count}`"
    )



# ─────────────────────────────────────────────────────────────────────────────
# Final task cards (user PM + dump/log channel) — leech-bot style
# ─────────────────────────────────────────────────────────────────────────────
def _md_safe(text: str, link_text: bool = False) -> str:
    """Strips markdown control sequences so titles/names never break formatting."""
    t = str(text or "")
    for seq in ("**", "__", "~~", "||", "`"):
        t = t.replace(seq, "")
    if link_text:
        t = t.replace("[", "(").replace("]", ")")
    else:
        t = t.replace("](", "] (")  # never let a title form a link
    return t.strip()


def platform_tag(platform_key: str) -> str:
    """'alright' -> '#AlrightTV', 'stage' -> '#StageOTT' (from the central platforms registry)."""
    key = str(platform_key or "").lower().strip()
    name = key
    try:
        from config.platforms import get_platform_by_key
        cfg_p = get_platform_by_key(key)
        if cfg_p:
            name = cfg_p.display_name
    except Exception:
        pass
    fallback = {"storytel": "Storytel", "kuku": "KukuTV"}
    name = fallback.get(key, name) if name == key else name
    return "#" + "".join(ch for ch in str(name) if ch.isalnum()) if name else "#OTT"


def out_mode_tag(upload_dest: str) -> str:
    return "#GDrive" if str(upload_dest or "").lower() in ("drive", "gdrive") else "#RIP"


def user_link(user_id: int, name: Optional[str] = None) -> str:
    return f"[{_md_safe(name, link_text=True) or user_id}](tg://user?id={user_id})"


def msg_task_done_card(
    title: str,
    task_size: str,
    time_taken: str,
    platform_key: str,
    upload_dest: str,
    total_files: int,
    user_id: int,
    user_name: Optional[str] = None,
) -> str:
    return (
        f"**{_md_safe(title)}**\n\n"
        f"┎ **Task Size:** {task_size}\n"
        f"┠ **Time Taken:** {time_taken}\n"
        f"┠ **Action:** {platform_tag(platform_key)} -> {out_mode_tag(upload_dest)}\n"
        f"┠ **Total Files:** {total_files}\n"
        f"┖ **Task By:** {user_link(user_id, user_name)}"
    )


STOP_HEADERS = {
    "download": "Download Stopped!",
    "merging": "Merging Stopped!",
    "watermark": "Watermark Stopped!",
    "uploading": "Uploading Stopped!",
}


def stop_header_for_phase(phase: str) -> str:
    return STOP_HEADERS.get(str(phase or "download").lower(), "Download Stopped!")


def msg_task_stopped_card(
    title: str,
    reason: str,
    task_size: str,
    time_taken: str,
    platform_key: str,
    upload_dest: str,
    user_id: int,
    user_name: Optional[str] = None,
    header: str = "Download Stopped!",
    username: Optional[str] = None,
    include_title: bool = False,
) -> str:
    by = f"@{username.lstrip('@')}" if username else user_link(user_id, user_name)
    title_line = f"**{_md_safe(title)}**\n\n" if (include_title and title) else ""
    return (
        f"{title_line}"
        f"〶 **{header}**\n\n"
        f"┎ **Due To:** {_md_safe(reason)}\n"
        f"┠ **Task Size:** {task_size}\n"
        f"┠ **Time Taken:** {time_taken}\n"
        f"┠ **Out Mode:** {out_mode_tag(upload_dest)}\n"
        f"┖ **Task By:** {by}"
    )
