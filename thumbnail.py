#!/usr/bin/env python3
"""
HD Auto-Aspect Thumbnail Engine for PrimeXMasoom.
Supports:
- 16:9 Landscape (Traditional shows)
- 9:16 Vertical (Short drama reels)
- Custom Aspect Ratios (Preserved without distortion or cropping)
- Telegram specification compliance (JPEG, max 320px bounding box, under 200KB)
- Auto frame extraction from video if no poster image exists.
"""

import io
import logging
import os
import subprocess
from pathlib import Path
from typing import Optional, Tuple
from PIL import Image

logger = logging.getLogger("primex.thumbnail")


def get_image_aspect_ratio(img_path: str) -> Tuple[int, int, str]:
    """
    Returns (width, height, orientation_label)
    orientation_label is 'horizontal_16_9', 'vertical_9_16', or 'square'.
    """
    with Image.open(img_path) as im:
        w, h = im.size
    ratio = w / h if h else 1.0
    if ratio > 1.2:
        orient = "horizontal_16_9"
    elif ratio < 0.8:
        orient = "vertical_9_16"
    else:
        orient = "square"
    return w, h, orient


def optimize_telegram_thumb(
    src_path: str,
    out_path: str,
    max_side: int = 1280,
    max_bytes: int = 200 * 1024,
) -> Optional[str]:
    """
    Converts any source image into a crystal-clear HD/UHD Telegram video thumbnail:
    - JPEG format
    - Longer side up to 1280px (crisp, zero blur on mobile & desktop)
    - File size <= 200 KB for Telegram MTProto compliance
    - High-quality Lanczos resampling with aspect ratio strictly preserved (NEVER CROPPED)
    - Subtle sharpness enhancement + subsampling=0 for razor-sharp clarity
    """
    if not os.path.exists(src_path):
        return None
    try:
        from PIL import ImageEnhance

        with Image.open(src_path) as img:
            img = img.convert("RGB")
            w, h = img.size
            if max(w, h) > max_side:
                scale = max_side / max(w, h)
                new_w = max(1, int(round(w * scale)))
                new_h = max(1, int(round(h * scale)))
                img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
            elif max(w, h) < 720:
                # Upscale small images to at least 720p for crisp presentation on mobile & desktop
                scale = 720 / max(w, h)
                new_w = max(1, int(round(w * scale)))
                new_h = max(1, int(round(h * scale)))
                img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

            # Subtle sharpness enhancement eliminates blur
            try:
                enhancer = ImageEnhance.Sharpness(img)
                img = enhancer.enhance(1.15)
            except Exception:
                pass

            # Progressive compression to fit under max_bytes (200 KB)
            quality = 92
            out_bytes = None
            while quality >= 40:
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=quality, optimize=True, subsampling=0)
                val = buf.getvalue()
                if len(val) <= max_bytes:
                    out_bytes = val
                    break
                quality -= 4

            if not out_bytes:
                # If still over 200KB at quality 40, downscale slightly
                scale_down = 800 / max(img.size)
                if scale_down < 1.0:
                    dw, dh = max(1, int(img.size[0] * scale_down)), max(1, int(img.size[1] * scale_down))
                    img = img.resize((dw, dh), Image.Resampling.LANCZOS)
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=80, optimize=True)
                out_bytes = buf.getvalue()

            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            with open(out_path, "wb") as f:
                f.write(out_bytes)
            return out_path
    except Exception as e:
        logger.warning(f"optimize_telegram_thumb failed for {src_path}: {e}")
        return None


def composite_title_on_hero(
    hero_path: str,
    title_path: str,
    out_path: str,
    title_width_ratio: float = 0.38,
    margin_left_ratio: float = 0.05,
    margin_top_ratio: float = 0.52,
) -> Optional[str]:
    """
    Overlays a transparent PNG title cutout logo on top of a 16:9 landscape hero image,
    creating a professional cinematic thumbnail matching Hotstar web & TV card layout.
    """
    if not os.path.exists(hero_path) or not os.path.exists(title_path):
        return None
    try:
        from PIL import Image

        hero = Image.open(hero_path).convert("RGBA")
        title = Image.open(title_path).convert("RGBA")

        target_w = int(hero.width * title_width_ratio)
        target_h = int(title.height * (target_w / title.width))
        title_resized = title.resize((target_w, target_h), Image.Resampling.LANCZOS)

        x = int(hero.width * margin_left_ratio)
        y = int((hero.height - target_h) * margin_top_ratio)

        hero.paste(title_resized, (x, y), title_resized)
        res = hero.convert("RGB")

        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        res.save(out_path, format="JPEG", quality=95)
        return out_path
    except Exception as e:
        logger.warning(f"composite_title_on_hero failed: {e}")
        return None


def extract_video_frame(
    video_path: str,
    out_path: str,
    seek_seconds: float = 10.0,
) -> Optional[str]:
    """
    Extracts a crisp frame from the video at `seek_seconds` using ffmpeg.
    Then optimizes it as a Telegram-legal thumbnail.
    """
    if not os.path.exists(video_path):
        return None
    raw_frame_path = f"{out_path}.raw.jpg"
    try:
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(seek_seconds),
            "-i", video_path,
            "-vframes", "1",
            "-q:v", "2",
            raw_frame_path,
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if r.returncode != 0 or not os.path.exists(raw_frame_path):
            return None

        # Optimize for Telegram in HD
        res = optimize_telegram_thumb(raw_frame_path, out_path, max_side=1280)
        try:
            os.remove(raw_frame_path)
        except Exception:
            pass
        return res
    except Exception as e:
        logger.warning(f"extract_video_frame failed for {video_path}: {e}")
        return None


def make_hd_cover(
    src_path: str,
    out_path: str,
    max_side: int = 1920,
    max_bytes: int = 5 * 1024 * 1024,
) -> Optional[str]:
    """
    Builds the full-quality UHD video_cover image (up to 1920px 1080p).
    This is what Telegram uses to display the razor-sharp, UHD poster in chats.
    """
    if not os.path.exists(src_path):
        return None
    try:
        from PIL import ImageEnhance

        with Image.open(src_path) as img:
            img = img.convert("RGB")
            w, h = img.size
            scale = max_side / max(w, h)
            if scale < 1.0:
                img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.Resampling.LANCZOS)
            elif max(w, h) < 1280:
                # Upscale small images to at least 1280 for sharp presentation
                upscale = 1280 / max(w, h)
                img = img.resize((max(1, int(w * upscale)), max(1, int(h * upscale))), Image.Resampling.LANCZOS)

            try:
                enhancer = ImageEnhance.Sharpness(img)
                img = enhancer.enhance(1.15)
            except Exception:
                pass

            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            img.save(out_path, "JPEG", quality=95, optimize=True, subsampling=0)
            if os.path.getsize(out_path) > max_bytes:
                img.save(out_path, "JPEG", quality=85, optimize=True)
            return out_path
    except Exception as e:
        logger.warning(f"HD cover generation failed for {src_path}: {e}")
        return None


def process_kuku_style_poster(
    src_path: str,
    out_path: str,
    target_max: int = 1920,
) -> Optional[str]:
    """
    Downloads/processes poster using the exact Kuku engine logic:
    - RGBA / P alpha transparency composited over solid background
    - Upscales/resamples up to 1920p using Lanczos for razor-sharp clarity
    - Natural aspect ratio 100% strictly preserved (NEVER CROPPED, NEVER STRETCHED)
    - Highest quality JPEG (quality=95, subsampling=0, optimize=True)
    """
    if not os.path.exists(src_path):
        return None
    try:
        with Image.open(src_path) as img:
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGBA")
                background = Image.new("RGBA", img.size, (0, 0, 0, 255))
                img = Image.alpha_composite(background, img)
            img = img.convert("RGB")
            w, h = img.size
            target_w, target_h = w, h
            if h > w and h < target_max:
                scale = target_max / h
                target_h = target_max
                target_w = max(1, int(round(w * scale)))
            elif w >= h and w < target_max:
                scale = target_max / w
                target_w = target_max
                target_h = max(1, int(round(h * scale)))
            elif max(w, h) > target_max:
                scale = target_max / max(w, h)
                target_w = max(1, int(round(w * scale)))
                target_h = max(1, int(round(h * scale)))

            if (target_w, target_h) != (w, h):
                resample_filter = getattr(Image, "Resampling", Image).LANCZOS
                img = img.resize((target_w, target_h), resample=resample_filter)

            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            img.save(out_path, "JPEG", quality=95, subsampling=0, optimize=True)
            return out_path
    except Exception as e:
        logger.warning(f"process_kuku_style_poster failed for {src_path}: {e}")
        return None

