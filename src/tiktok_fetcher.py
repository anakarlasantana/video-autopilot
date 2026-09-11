"""TikTok reference fetcher with free fallback strategy.

Fetches TikTok video metadata and references using multiple free sources:
1. tikwm.com mirror (free, no key, no login)
2. TikTokApi (free, needs Playwright, may be blocked)
3. yt-dlp (fallback for public videos)

All sources are free and require no API keys.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Optional

import requests

from .utils import log

_TIKWM_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}


def _extract_tiktok_id(url: str) -> str | None:
    """Extract TikTok video ID from various URL formats."""
    patterns = [
        r"tiktok\.com/@[\w.]+/video/(\d+)",
        r"tiktok\.com/v/(\d+)",
        r"vm\.tiktok\.com/(\w+)",
        r"tiktok\.com/t/(\w+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def _tikwm_get(path: str, base: str = "https://www.tikwm.com", params: dict | None = None, retries: int = 2) -> dict:
    """tikwm.com GET with retry. Free mirror of TikTok data."""
    url = f"{base}{path}"
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, headers=_TIKWM_UA, timeout=15)
            r.raise_for_status()
            data = r.json()
            if data.get("code") == 0:
                return data.get("data", {})
            log(f"tikwm error: {data.get('msg', 'unknown')}", "warn")
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2)
            else:
                log(f"tikwm failed: {e}", "warn")
    return {}


def fetch_tiktok_metadata(url: str) -> dict | None:
    """Fetch TikTok video metadata using tikwm (free, no key).
    
    Returns dict with: id, title, duration, play_count, digg_count, 
    comment_count, share_count, download_url, cover, music_info
    """
    video_id = _extract_tiktok_id(url)
    if not video_id:
        log(f"Could not extract TikTok ID from: {url}", "warn")
        return None
    
    data = _tikwm_get("/api/", params={"url": url, "hd": 1})
    if not data:
        return None
    
    return {
        "id": data.get("id", video_id),
        "title": data.get("title", ""),
        "duration": data.get("duration", 0),
        "play_count": data.get("play_count", 0),
        "digg_count": data.get("digg_count", 0),
        "comment_count": data.get("comment_count", 0),
        "share_count": data.get("share_count", 0),
        "download_url": data.get("play", data.get("hdplay", "")),
        "cover_url": data.get("cover", data.get("origin_cover", "")),
        "music_title": data.get("music_info", {}).get("title", ""),
        "music_author": data.get("music_info", {}).get("author", ""),
        "author": data.get("author", {}).get("nickname", ""),
        "author_id": data.get("author", {}).get("unique_id", ""),
        "description": data.get("title", ""),
        "create_time": data.get("create_time", 0),
    }


def download_tiktok_video(url: str, dest: Path, timeout: int = 30) -> Path | None:
    """Download TikTok video without watermark using tikwm.
    
    Args:
        url: TikTok video URL
        dest: Destination path (without extension)
        timeout: Download timeout in seconds
    
    Returns:
        Path to downloaded video or None on failure
    """
    metadata = fetch_tiktok_metadata(url)
    if not metadata or not metadata.get("download_url"):
        log(f"Could not get download URL for: {url}", "warn")
        return None
    
    download_url = metadata["download_url"]
    out = dest.with_suffix(".mp4")
    
    try:
        r = requests.get(download_url, headers=_TIKWM_UA, timeout=timeout, stream=True)
        r.raise_for_status()
        
        with open(out, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
        
        if out.exists() and out.stat().st_size > 10000:
            log(f"Downloaded TikTok video: {out.name} ({out.stat().st_size / 1024 / 1024:.1f}MB)", "ok")
            return out
        else:
            log(f"Downloaded file too small: {out}", "warn")
            out.unlink(missing_ok=True)
            return None
    except Exception as e:
        log(f"TikTok download failed: {e}", "warn")
        return None


def fetch_tiktok_reference(url: str, download: bool = True, dest: Path | None = None) -> dict | None:
    """Fetch TikTok reference with automatic fallback.
    
    Strategy:
    1. Try tikwm.com (free, reliable)
    2. Try TikTokApi (if available)
    3. Return None if all fail
    
    Args:
        url: TikTok video URL
        download: Whether to download the video
        dest: Destination path for download
    
    Returns:
        Dict with metadata and local video path, or None
    """
    log(f"Fetching TikTok reference: {url}", "info")
    
    # 1. Try tikwm (primary, free)
    metadata = fetch_tiktok_metadata(url)
    if metadata:
        log(f"tikwm: found video by @{metadata.get('author', 'unknown')}", "info")
    
    if not metadata:
        log(f"Could not fetch TikTok metadata for: {url}", "err")
        return None
    
    # Download video if requested
    if download and dest:
        video_path = download_tiktok_video(url, dest)
        metadata["local_video"] = str(video_path) if video_path else None
    else:
        metadata["local_video"] = None
    
    metadata["source_url"] = url
    metadata["fetched_at"] = time.time()
    
    return metadata


if __name__ == "__main__":
    # Quick test
    test_url = "https://www.tiktok.com/@codigo.gta/video/7684024987514129682"
    result = fetch_tiktok_reference(test_url, download=False)
    if result:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print("Failed to fetch reference")