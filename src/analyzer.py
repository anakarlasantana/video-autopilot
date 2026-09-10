"""Inspiration mode — collect & analyze viral references from TikTok / Reels / Shorts.

All-free flow (nothing here needs a paid key):
  reference URL (or niche discovery)  →  yt-dlp download + metadata
  →  faster-whisper transcription (local)  →  LLM structural analysis → JSON
  →  saved to data/references/<channel>/<slug>.json  (private, git-ignored)

The analysis is injected into the scriptwriter as a PATTERN to adapt — keep the
narrative structure and pacing, replace ALL content. This keeps the output an
original work (see docs/COMPLIANCE.md) rather than a recycled copy.

Provider abstraction (config `inspiration:` section) — swap without code changes:
  transcriber: whisper_local    # whisper_local (free, default) | api (future stub)
  vision: none                  # none (default) | frames  → extracts visual style
                                #   from keyframes; works with any vision-capable
                                #   LLM already configured in cfg["llm"]
  cookies_from_browser: ""      # "" | firefox | chrome | ... (needed by Instagram,
                                #   helps TikTok rate limits)

CLI:
  python -m src.analyzer --channel gaming --url https://www.tiktok.com/@x/video/123
  python -m src.analyzer --channel gaming --discover "minha nicho" --source yt_shorts
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from .config import ROOT, load_prompt
from .llm import complete
from .utils import extract_json, ffprobe_duration, log, run_ffmpeg, save_json, slugify

REFERENCES_DIR = ROOT / "data" / "references"

_SOURCES = ("yt_shorts", "tiktok_tag", "tikwm", "direct")
_TIKWM_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}


# ── yt-dlp helpers (free, no key) ─────────────────────────────────────────────

def _ytdlp_base(cfg: dict) -> list[str]:
    """Common yt-dlp args — cookies only when configured (Instagram needs them)."""
    base = ["--no-playlist", "--no-warnings"]
    browser = (cfg.get("inspiration", {}) or {}).get("cookies_from_browser", "")
    if browser:
        base += ["--cookies-from-browser", browser]
    return base


# ── tikwm mirror (free third-party, no key) ───────────────────────────────────
# TikTok blocks unsigned tag/user feed APIs and rate-limits hard (yt-dlp marks
# its extractor broken). tikwm.com is a free mirror of the platform's own data:
# search feed, stats, and no-watermark mp4s. No key, no cookies, no signatures.
# Keep it free-first: `inspiration.tikwm_base` accepts any compatible instance
# (or a self-hosted one) — swap by config, never by code.

def _tikwm_base(cfg: dict) -> str:
    return (((cfg.get("inspiration", {}) or {}).get("tikwm_base"))
            or "https://www.tikwm.com").rstrip("/")


def _tikwm_get(path: str, cfg: dict, params: dict | None = None) -> dict:
    """tikwm GET with polite retry — it sits behind Cloudflare and rate-limits
    bursts (403 'Just a moment…'). Chrome TLS impersonation (curl_cffi) passes
    when the challenge window clears; plain requests is the fallback. One backoff
    + retry keeps daily automation smooth without hammering the free service."""
    url = f"{_tikwm_base(cfg)}{path}"
    wait = int(((cfg.get("inspiration", {}) or {}).get("tikwm_retry_wait")) or 90)
    last_exc: Exception | None = None
    for attempt in (1, 2):
        try:
            r = _tikwm_fetch(url, params or {})
            d = r.json()
            if d.get("code") != 0:
                raise RuntimeError(f"tikwm {path} failed: {d.get('msg')}")
            return d.get("data") or {}
        except Exception as e:  # noqa: BLE001 — curl_cffi/requests raise different types
            last_exc = e
            status = 0
            resp = getattr(e, "response", None)
            if resp is not None:
                status = getattr(resp, "status_code", 0) or 0
            retryable = (status in (403, 429)) or status == 0  # challenge or conn error
            if retryable and attempt == 1:
                log(f"tikwm unavailable ({status or type(e).__name__}) — "
                    f"waiting {wait}s and retrying", "warn")
                time.sleep(wait)
                continue
            raise
    raise last_exc if last_exc else RuntimeError(f"tikwm {path} failed")


def _tikwm_fetch(url: str, params: dict):
    """GET with Chrome TLS impersonation when available, plain requests otherwise."""
    try:
        from curl_cffi import requests as cr
        r = cr.get(url, params=params, impersonate="chrome", timeout=45)
        r.raise_for_status()
        return r
    except ImportError:
        r = requests.get(url, params=params, headers=_TIKWM_UA, timeout=45)
        r.raise_for_status()
        return r


def _tikwm_discover(query: str, n: int, cfg: dict) -> list[dict]:
    """TikTok discovery via the tikwm search feed — replaces the blocked tag API."""
    d = _tikwm_get("/api/feed/search", cfg,
                   {"keywords": query.strip().lstrip("#"), "count": n * 2})
    refs: list[dict] = []
    for v in d.get("videos") or []:
        vid = v.get("video_id") or ""
        author = (v.get("author") or {}).get("unique_id") or ""
        if not vid:
            continue
        refs.append({
            "url": f"https://www.tiktok.com/@{author}/video/{vid}",
            "platform": "tiktok",
            "title": (v.get("title") or "")[:120],
            "author": author,
            "duration": float(v.get("duration") or 0),
            "views": int(v.get("play_count") or 0),
        })
    return sorted(refs, key=lambda r: r["views"], reverse=True)[:n]


def _tikwm_metadata(url: str, cfg: dict) -> dict:
    """Single TikTok video metadata via tikwm (mirrors yt-dlp's dump shape)."""
    v = _tikwm_get("/api", cfg, {"url": url})
    title = v.get("title") or ""
    return {
        "url": url,
        "platform": "tiktok",
        "author": (v.get("author") or {}).get("unique_id") or "",
        "title": title,
        "description": title[:600],
        "duration": float(v.get("duration") or 0),
        "views": int(v.get("play_count") or 0),
        "likes": int(v.get("digg_count") or 0),
        "hashtags": re.findall(r"#(\w+)", title),
        "download_url": v.get("play") or "",   # no-watermark mp4 (direct or tikwm-hosted)
    }


def _tikwm_download(url: str, dest: Path, cfg: dict) -> Path:
    """No-watermark mp4 via tikwm — bypasses TikTok's per-IP video blocking."""
    dl = (_tikwm_metadata(url, cfg).get("download_url") or "")
    if not dl:
        raise RuntimeError("tikwm returned no download URL")
    full = dl if dl.startswith("http") else f"{_tikwm_base(cfg)}{dl}"
    out = dest.with_suffix(".mp4")
    with requests.get(full, headers=_TIKWM_UA, timeout=240, stream=True) as s:
        s.raise_for_status()
        with open(out, "wb") as f:
            for chunk in s.iter_content(1 << 20):
                f.write(chunk)
    if not out.exists() or out.stat().st_size < 50_000:
        raise RuntimeError(f"tikwm download too small: {out}")
    return out


def _platform(url: str) -> str:
    u = url.lower()
    if "tiktok" in u:
        return "tiktok"
    if "instagram" in u:
        return "instagram"
    return "youtube"


def _ytdlp_binary() -> list[str]:
    # Prefer the yt_dlp module of the RUNNING interpreter (matches requirements.txt
    # and stays updated with the venv) over a possibly stale system binary.
    try:
        import yt_dlp  # noqa: F401

        return [sys.executable, "-m", "yt_dlp"]
    except ImportError:
        return ["yt-dlp"] if shutil.which("yt-dlp") else [sys.executable, "-m", "yt_dlp"]


def _fetch_metadata(url: str, cfg: dict) -> dict:
    """yt-dlp metadata dump: title, channel, views, likes, duration, hashtags.

    Some YouTube sessions get the SABR-only experiment (yt-dlp #12482) — the
    default web client then fails; retrying with the android player client fixes it.
    TikTok: yt-dlp's extractor is broken and TikTok blocks per-IP — falling back
    to the tikwm mirror (free, no key) when yt-dlp fails.
    """
    if _platform(url) == "tiktok":
        try:
            return _ytdlp_metadata(url, cfg)
        except RuntimeError as e:
            log(f"yt-dlp TikTok metadata failed — mirroring via tikwm ({e})", "warn")
            return _tikwm_metadata(url, cfg)
    return _ytdlp_metadata(url, cfg)


def _ytdlp_metadata(url: str, cfg: dict) -> dict:
    for extra in ([], ["--extractor-args", "youtube:player_client=android"]):
        cmd = [*_ytdlp_binary(), *_ytdlp_base(cfg), *extra, "--dump-json", url]
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=int(cfg.get("visuals", {}).get("scraper_timeout", 120)))
        if proc.returncode == 0 and (proc.stdout or "").strip():
            break
    else:
        raise RuntimeError(
            f"yt-dlp metadata failed for {url}:\n{(proc.stderr or proc.stdout)[-500:]}")
    meta = json.loads(proc.stdout.strip().splitlines()[0])
    return {
        "url": url,
        "platform": _platform(url),
        "author": meta.get("uploader") or meta.get("channel") or "",
        "title": meta.get("title") or "",
        "description": (meta.get("description") or "")[:600],
        "duration": meta.get("duration") or 0,
        "views": meta.get("view_count") or 0,
        "likes": meta.get("like_count") or 0,
        "hashtags": meta.get("tags") or [],
    }


def _download_video(url: str, dest: Path, cfg: dict) -> Path:
    """Download the full reference video (need real length for transcription).

    Retries with the android player client when the default one hits the SABR-only
    experiment (yt-dlp #12482) that strips downloadable formats.
    TikTok: falls back to the tikwm no-watermark mirror (free, no key) — TikTok
    blocks per-IP video access and yt-dlp's extractor is broken upstream.
    """
    if _platform(url) == "tiktok":
        try:
            return _ytdlp_download(url, dest, cfg)
        except RuntimeError as e:
            log(f"yt-dlp TikTok download failed — mirroring via tikwm ({e})", "warn")
            return _tikwm_download(url, dest, cfg)
    return _ytdlp_download(url, dest, cfg)


def _ytdlp_download(url: str, dest: Path, cfg: dict) -> Path:
    out = dest.with_suffix(".mp4")
    last_err = ""
    for extra in ([], ["--extractor-args", "youtube:player_client=android"]):
        cmd = [*_ytdlp_binary(), *_ytdlp_base(cfg), *extra,
               # AVOID the classic trap: `mp4[height<=...]` matches video-only
               # formats (AV1/VP9) with no audio track — transcription would get
               # silence. Always ask for video+audio and let yt-dlp merge.
               "--format", "bestvideo[height<=1080]+bestaudio/best",
               "--merge-output-format", "mp4",
               "-o", str(out), url]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
        if proc.returncode == 0 and out.exists():
            return out
        last_err = (proc.stderr or proc.stdout)[-500:]
    raise RuntimeError(f"yt-dlp download failed:\n{last_err}")


def _scrape_tiktok_tag(tag: str, n: int, timeout: int = 45) -> list[dict]:
    """Scrape the public TikTok tag page. Requires curl_cffi (Chrome TLS
    impersonation — plain requests gets fingerprinted and served a shell
    without the video list). Returns [] when TikTok serves no SSR item list
    (the feed usually loads via a signed API after page load)."""
    try:
        from curl_cffi import requests as cr
    except ImportError:
        log("tiktok_tag needs curl_cffi (pip install curl-cffi) — skipping", "warn")
        return []
    try:
        r = cr.get(f"https://www.tiktok.com/tag/{tag}", impersonate="chrome",
                   timeout=timeout)
        r.raise_for_status()
        m = re.search(
            r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" '
            r'type="application/json"[^>]*>(.*?)</script>', r.text, re.S)
        if not m:
            return []
        payload = json.loads(m.group(1))
        modules: list = []

        def _walk(o, depth=0):
            if depth > 9:
                return
            if isinstance(o, dict):
                for k, v in o.items():
                    if k in ("ItemModule", "itemList", "ItemList") and \
                            isinstance(v, (dict, list)) and len(v) > 2:
                        modules.append(v)
                    _walk(v, depth + 1)
            elif isinstance(o, list):
                for v in o:
                    _walk(v, depth + 1)

        _walk(payload)
        refs: list[dict] = []
        for mod in modules:
            items = mod.values() if isinstance(mod, dict) else mod
            for it in items:
                if not isinstance(it, dict):
                    continue
                vid = it.get("id") or it.get("aweme_id") or ""
                if not vid:
                    continue
                stats = it.get("stats") or it.get("statsV2") or {}
                refs.append({
                    "url": f"https://www.tiktok.com/video/{vid}",
                    "platform": "tiktok",
                    "title": (it.get("desc") or "")[:120],
                    "author": (it.get("author") or {}).get("uniqueId", ""),
                    "duration": float(it.get("video", {}).get("duration", 0) or 0),
                    "views": int(stats.get("playCount", 0) or 0),
                })
        return refs
    except Exception as e:  # noqa: BLE001 — discovery must never break the flow
        log(f"tiktok_tag scrape failed: {e}", "warn")
        return []


def discover_references(cfg: dict, query: str, source: str = "yt_shorts",
                        n: int = 8) -> list[dict]:
    """Find reference candidates by niche/hashtag. Returns metadata dicts, no downloads.

    Sources:
      yt_shorts  → YouTube search filtered to sub-60s verticals (stable, key-free)
      tiktok_tag → TikTok hashtag. TikTok blocks unsigned tag-feed APIs, so this
                   degrades: SSR scrape → tikwm mirror (free) → yt_shorts fallback
      tikwm      → tikwm search feed directly (free third-party TikTok mirror,
                   no key/cookies/signatures) — the reliable TikTok discovery path
      direct     → explicit --url reference (no discovery)
    """
    if source == "yt_shorts":
        # Over-fetch: generic search ranks long videos first; shorts get filtered
        # in Python below (falling back to the shortest found — their hook/structure
        # is still analyzable).
        cmd = [*_ytdlp_binary(), "--flat-playlist", "--print",
               "%(id)s\t%(title)s\t%(duration)s\t%(view_count)s\t%(channel)s",
               f"ytsearch{n * 4}:{query}"]
    elif source == "tiktok_tag":
        tag = query.strip().lstrip("#").replace(" ", "")
        # 1) SSR scrape (needs curl_cffi + TikTok serving an item list — usually empty)
        refs = _scrape_tiktok_tag(tag, n)
        if refs:
            log(f"tiktok_tag: {len(refs)} videos scraped from the tag page", "info")
            return sorted(refs, key=lambda r: r["views"], reverse=True)[:n]
        # 2) tikwm mirror (free, reliable) — TikTok blocks the unsigned tag API
        try:
            refs = _tikwm_discover(query, n, cfg)
            if refs:
                log(f"tiktok_tag blocked — {len(refs)} videos mirrored via tikwm", "info")
                return refs
        except RuntimeError as e:
            log(f"tikwm mirror failed ({e}) — falling back to yt_shorts", "warn")
        # 3) yt-dlp (extractor marked broken upstream; try anyway)
        cmd = [*_ytdlp_binary(), *_ytdlp_base(cfg), "--flat-playlist", "--print",
               "%(id)s\t%(title)s\t%(duration)s\t%(view_count)s\t%(channel)s",
               f"https://www.tiktok.com/tag/{tag}"]
    elif source == "tikwm":
        try:
            refs = _tikwm_discover(query, n, cfg)
            if refs:
                log(f"tikwm: {len(refs)} TikTok videos for '{query}'", "info")
                return refs
        except RuntimeError as e:
            log(f"tikwm mirror unavailable ({e}) — falling back to yt_shorts", "warn")
        cmd = [*_ytdlp_binary(), *_ytdlp_base(cfg), "--flat-playlist", "--print",
               "%(id)s\t%(title)s\t%(duration)s\t%(view_count)s\t%(channel)s",
               f"https://www.tiktok.com/tag/{query.strip().lstrip('#').replace(' ', '')}"]
    else:
        raise ValueError(f"unknown discovery source: {source} (use {_SOURCES})")

    proc = subprocess.run(cmd, capture_output=True, text=True,
                          timeout=int(cfg.get("visuals", {}).get("scraper_timeout", 120)))
    if proc.returncode != 0:
        if source in ("tiktok_tag", "tikwm"):
            # TikTok blocks unsigned tag feeds (yt-dlp #12482-family breakage).
            # Degrade gracefully instead of failing the whole discovery step.
            log("TikTok tag is unavailable (blocks unsigned feed APIs) — "
                "falling back to yt_shorts", "warn")
            return discover_references(cfg, query, "yt_shorts", n)
        raise RuntimeError(f"discovery failed: {(proc.stderr or proc.stdout)[-400:]}")

    refs: list[dict] = []
    for line in (proc.stdout or "").splitlines():
        parts = (line.split("\t") + [""] * 5)[:5]
        vid, title, dur, views, channel = (p.strip() for p in parts)
        if not vid or vid == "NA":
            continue
        try:
            dur = float(dur) if dur not in ("NA", "None", "") else 0.0
            views = int(float(views)) if views not in ("NA", "None", "") else 0
        except ValueError:
            dur, views = 0.0, 0
        url = (f"https://www.youtube.com/watch?v={vid}" if source == "yt_shorts"
               else f"https://www.tiktok.com/video/{vid}")
        refs.append({
            "url": url, "platform": _platform(url), "title": title,
            "author": channel, "duration": dur, "views": views,
        })
    # Dedupe + best-performing first. Prefer true Shorts (≤61s) when the search
    # surfaces any; otherwise keep the shortest high-view candidates — long-form
    # hooks/structure are still analyzable for inspiration.
    seen: set[str] = set()
    refs = [r for r in refs if not (r["url"] in seen or seen.add(r["url"]))]
    refs = sorted(refs, key=lambda r: r["views"], reverse=True)
    shorts = [r for r in refs if 0 < r["duration"] <= 61]
    if shorts:
        return shorts[:n]
    return sorted(refs, key=lambda r: (r["duration"], -r["views"]))[:n]


# ── Transcription (provider-abstracted) ───────────────────────────────────────

def _transcribe(audio: Path, cfg: dict) -> list[dict]:
    """Word-timed transcript. Provider: whisper_local (free) | api (future stub)."""
    provider = cfg.get("inspiration", {}).get("transcriber", "whisper_local")
    if provider == "api":
        raise NotImplementedError(
            "inspiration.transcriber=api not implemented yet — use whisper_local")
    from faster_whisper import WhisperModel

    model = WhisperModel(cfg.get("inspiration", {}).get(
        "whisper_model", cfg["captions"].get("model", "base")), compute_type="int8")
    segments, _ = model.transcribe(str(audio),
                                   language=cfg["captions"].get("language"))
    return [{"start": s.start, "end": s.end, "text": (s.text or "").strip()}
            for s in segments if (s.text or "").strip()]


def _extract_audio(video: Path, dest: Path) -> Path:
    run_ffmpeg(["-i", str(video), "-vn", "-ac", "1", "-ar", "16000", str(dest)])
    return dest


def _extract_frames(video: Path, frames_dir: Path, cfg: dict, n: int = 6) -> list[Path]:
    """Keyframes for the optional `vision: frames` analyzer provider."""
    dur = _probe_duration(video)
    if dur <= 0:
        return []
    frames: list[Path] = []
    step = max(1.0, dur / n)
    for i in range(n):
        ts = min(max(0.1, dur - 0.5), i * step + step / 2)
        f = frames_dir / f"frame_{i}.jpg"
        run_ffmpeg(["-ss", f"{ts:.2f}", "-i", str(video), "-frames:v", "1",
                    "-vf", "scale=540:-2", str(f)])
        if f.exists():
            frames.append(f)
    return frames


def _probe_duration(video: Path) -> float:
    try:
        return ffprobe_duration(video)
    except Exception:
        return 0.0


# ── LLM structural analysis ──────────────────────────────────────────────────

def _vision_notes(cfg: dict, video: Path, workdir: Path) -> str:
    """Optional visual-style description via the configured LLM's vision input.

    Kept behind config (`inspiration.vision: frames`) — free by default means
    `none`, so analysis runs text-only from the transcript + metadata.
    Returns '' when disabled or unavailable (never blocks the pipeline).
    """
    if cfg.get("inspiration", {}).get("vision", "none") != "frames":
        return ""
    try:
        import base64

        from .config import env
        frames = _extract_frames(video, workdir, cfg)
        if not frames:
            return ""
        prompt = load_prompt("analyze_vision")
        provider = cfg["llm"].get("provider", "")
        if provider == "anthropic":
            import anthropic

            content: list = [{"type": "text", "text": prompt}]
            for f in frames:
                content.append({"type": "image", "source": {
                    "type": "base64", "media_type": "image/jpeg",
                    "data": base64.b64encode(f.read_bytes()).decode(),
                }})
            client = anthropic.Anthropic(api_key=env("ANTHROPIC_API_KEY"))
            resp = client.messages.create(
                model=cfg["llm"].get("model", "claude-sonnet-4-6"),
                max_tokens=600, messages=[{"role": "user", "content": content}])
            return "".join(b.text for b in resp.content if b.type == "text").strip()
        # Other providers: add adapters here following the same pattern.
        log(f"vision provider '{provider}' not supported for frames; skipping", "warn")
        return ""
    except Exception as e:  # noqa: BLE001 — vision is an enhancement, never a blocker
        log(f"vision analysis skipped: {e}", "warn")
        return ""


def _transcript_text(segments: list[dict]) -> str:
    return "\n".join(f"[{s['start']:05.1f}-{s['end']:05.1f}s] {s['text']}"
                     for s in segments)


def analyze_reference(cfg: dict, url: str, keep_video: bool = False) -> dict:
    """Full analysis of one reference video. Returns (and persists) the analysis dict."""
    ch = cfg["channel"]
    meta = _fetch_metadata(url, cfg)
    slug = slugify(meta["title"] or url, max_len=40)
    ref_dir = REFERENCES_DIR / ch["key"] / slug
    ref_dir.mkdir(parents=True, exist_ok=True)
    save_json(ref_dir / "meta.json", meta)

    with tempfile.TemporaryDirectory(prefix="va_ref_") as tmp:
        workdir = Path(tmp)
        log(f"downloading reference ({meta['platform']}, {meta['duration']:.0f}s)…")
        video = _download_video(url, workdir / "reference", cfg)

        log("transcribing (whisper local)…")
        transcript = _transcribe(_extract_audio(video, workdir / "audio.wav"), cfg)
        if not transcript:
            raise RuntimeError("transcription returned no speech — "
                               "the video may have no dialogue (music only)")

        vision = _vision_notes(cfg, video, workdir)
        if keep_video:
            shutil.copyfile(video, ref_dir / "reference.mp4")

    log("LLM structural analysis…")
    prompt = load_prompt("analyze").format(
        niche=ch.get("niche", ""), language=cfg.get("language", {}).get("code", "English"),
        platform=meta["platform"], title=meta["title"], author=meta["author"],
        duration=meta["duration"], views=meta["views"],
        transcript=_transcript_text(transcript), vision_notes=vision or "(não disponível)",
    )
    analysis = extract_json(complete(prompt, cfg, max_tokens=2000))
    return _persist_analysis(cfg, url, meta, transcript, analysis, ref_dir)


def _persist_analysis(cfg: dict, url: str, meta: dict, transcript: list[dict],
                      analysis: dict, ref_dir: Path) -> dict:
    """Stamp provenance, save JSON + markdown digest (also to data/notes/)."""
    analysis.update({
        "reference_url": url,
        "reference_platform": meta["platform"],
        "reference_meta": meta,
        "transcript": transcript,
        "channel_key": cfg["channel"]["key"],
        "analyzed_at": datetime.now(timezone.utc).isoformat(),
    })
    save_json(ref_dir / "analysis.json", analysis)

    # Human-readable copy — also dropped in data/notes/ so the existing strategy-
    # notes ingestion (src/knowledge.py) picks it up on every future run for free.
    summary = _summary_md(analysis, meta)
    (ref_dir / "summary.md").write_text(summary, encoding="utf-8")
    notes_dir = ROOT / "data" / "notes" / cfg["channel"]["key"]
    notes_dir.mkdir(parents=True, exist_ok=True)
    (notes_dir / f"reference-{ref_dir.name}.md").write_text(summary, encoding="utf-8")
    log(f"reference analyzed → {ref_dir / 'analysis.json'}", "ok")
    return analysis


def _summary_md(a: dict, meta: dict) -> str:
    """Markdown digest used both as review doc and as a channel strategy note."""
    scenes = "\n".join(
        f"- {s.get('timing', '')}: {s.get('descricao', s.get('description', ''))}"
        for s in a.get("cenas", []))
    return (
        f"# Reference analysis — {meta.get('title', '')}\n\n"
        f"- Source: {a['reference_platform']} ({a['reference_url']})\n"
        f"- Author: {meta.get('author', '')} · {meta.get('views', 0):,} views\n"
        f"- Hook: {a.get('gancho', '')}\n"
        f"- Theme: {a.get('tema', '')}\n"
        f"- Narrative: {a.get('estrutura_narrativa', '')}\n"
        f"- Visual style: {a.get('estilo_visual', '')}\n"
        f"- Pacing: {a.get('ritmo', '')}\n"
        f"- CTA: {a.get('cta', '')}\n"
        f"- Original elements: {', '.join(a.get('elementos_originais', []))}\n\n"
        f"## Scene structure (timing only — content is NOT copied)\n{scenes}\n"
    )


# ── Prompt-side helpers ───────────────────────────────────────────────────────

def reference_pattern_block(reference: dict | None) -> str:
    """Render the analysis as the scriptwriter's 'pattern to adapt' block ('' if none)."""
    if not reference:
        return ""
    scenes = "\n".join(
        f"  {i + 1}. [{s.get('timing', '')}] {s.get('descricao', s.get('description', ''))}"
        for i, s in enumerate(reference.get("cenas", [])))
    return (
        f'Hook mechanism the reference used (adapt the MECHANISM, never the words): '
        f'"{reference.get("gancho", "")}"\n'
        f"Narrative structure: {reference.get('estrutura_narrativa', '')}\n"
        f"Pacing: {reference.get('ritmo', '')}\n"
        f"CTA style: {reference.get('cta', '')}\n"
        f"Scene-by-scene structure (beat order + function only):\n{scenes}"
    )


def latest_reference(channel_key: str) -> dict | None:
    """Most recent analyzed reference for a channel (used by --use-latest)."""
    base = REFERENCES_DIR / channel_key
    if not base.exists():
        return None
    files = sorted(base.glob("*/analysis.json"), key=lambda p: p.stat().st_mtime)
    if not files:
        return None
    try:
        return json.loads(files[-1].read_text(encoding="utf-8"))
    except Exception:
        return None


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    from .config import channel_config

    p = argparse.ArgumentParser(description="Reference analyzer (inspiration mode)")
    p.add_argument("--channel", required=True, help="channel key from channels.yaml")
    p.add_argument("--url", help="TikTok / Reel / Short URL to analyze")
    p.add_argument("--discover", help="niche/hashtag query to discover candidates")
    p.add_argument("--source", default="yt_shorts", choices=list(_SOURCES),
                   help="discovery source (yt_shorts | tiktok_tag | tikwm | direct)")
    p.add_argument("--n", type=int, default=8, help="max candidates from discovery")
    p.add_argument("--keep-video", action="store_true", help="keep the downloaded video")
    args = p.parse_args()

    cfg = channel_config(args.channel)
    if args.discover and args.url:
        p.error("--url ignored when --discover is used "
                "(run again with the chosen URL)")
    try:
        if args.discover:
            refs = discover_references(cfg, args.discover, args.source, args.n)
            for r in refs:
                print(f"  {r['views']:>10,} views · {r['duration']:5.0f}s · "
                      f"{r['platform']:8s} {r['title'][:70]}\n"
                      f"    {'':>12}{r['url']}")
        elif args.url:
            analyze_reference(cfg, args.url, keep_video=args.keep_video)
        else:
            p.error("provide --url or --discover")
    except Exception as e:  # noqa: BLE001
        log(str(e), "err")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
