"""Stage 4 — gather one vertical visual per beat.

Providers: pexels (free stock), scraper (multi-source free, parallel+ranked),
AI images (fal/leonardo), youtube shorts (free).
"""
from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import requests

from .config import env
from .rank import Candidate, rank_candidates
from .utils import ffprobe_duration, log
from .image_generator import generate_scene_image
from .search_smart import smart_query_enrichment, extract_named_entities

UA = {"User-Agent": "video-autopilot/1.0 (B-roll scraper; +https://github.com/anakarlasantana/video-autopilot)"}


def _ytdlp_binary() -> list[str]:
    # Prefer the yt_dlp module of the RUNNING interpreter (matches requirements.txt
    # and stays updated with the venv) over a possibly stale system binary.
    try:
        import yt_dlp  # noqa: F401
        return [sys.executable, "-m", "yt_dlp"]
    except ImportError:
        return ["yt-dlp"] if shutil.which("yt-dlp") else [sys.executable, "-m", "yt_dlp"]


def gather_visuals(cfg: dict, script: dict, out_dir: Path, duration: float,
                   idea: dict | None = None) -> list[Path]:
    """Return an ordered list of media files (videos/images) to cover `duration`.

    `idea` (optional): the ideation dict (primary_keyword/title/concept). Used to
    derive topic words that bias B-roll ranking toward on-topic footage."""
    clips_dir = out_dir / "clips"
    clips_dir.mkdir(exist_ok=True)
    cut = cfg["video"]["cut_every_seconds"]
    needed = max(cfg["visuals"]["clips_per_video"], math.ceil(duration / cut))

    # Niche keywords: bias the scraper ranking so on-topic B-roll wins.
    topic_words = extract_topic_words(cfg, idea)
    # Gaming/entertainment niches: motion footage strongly preferred + youtube first.
    prefer_videos = bool(cfg["channel"].get("prefer_videos", False))
    source_priority = cfg["channel"].get("source_priority")  # optional custom order

        # Map each beat's visual cue across the segments it covers, so visuals track the
    # words being spoken. Pad/repeat to fill, fall back to the channel's visual style.
    cues = [b.get("visual_cue", "").strip() for b in script.get("beats", [])
            if b.get("visual_cue")]
    style = cfg["channel"].get("visual_style", "")
    if cues:
        # stretch the cue list to `needed` items, preserving order
        queries = [cues[int(i * len(cues) / needed)] for i in range(needed)]
    else:
        queries = [style] * needed

    # SMART SEARCH: Enriquecer queries com conteúdo oficial de marcas/jogos
    # Extrai entidades do texto completo do roteiro (não só visual_cues)
    script_text = script.get("full_script", "")
    if not script_text:
        # Construir texto do roteiro a partir dos beats
        script_text = " ".join(b.get("text", "") for b in script.get("beats", []))
    
    # Extrair entidades nomeadas (jogos, marcas, pessoas)
    named_entities = extract_named_entities(script_text)
    
    # Se entidades foram identificadas, enriquece as queries
    if named_entities:
        log(f"smart_search: detected entities: {', '.join(e[0] for e in named_entities[:3])}", "info")
        
        # Criar mapeamento de queries enriquecidas
        enriched_queries: list[str] = []
        unique_queries_original: set[str] = set()
        
        for q in queries:
            if q in unique_queries_original:
                # Query repetida - usar query original para diversidade
                enriched_queries.append(q)
            else:
                unique_queries_original.add(q)
                # Enriquecer query com conteúdo oficial
                if named_entities and q:
                    enriched = smart_query_enrichment(q, script_text, topic_words)
                    # Usar a primeira query enriquecida (prioridade oficial)
                    enriched_queries.append(enriched[0] if enriched else q)
                else:
                    enriched_queries.append(q)
        
        queries = enriched_queries

    provider = cfg["visuals"]["provider"]
    chain_map = {"auto": cfg["visuals"].get("fallback_chain", ["pexels", "scraper"])}
    chain_map = {k: [p for p in v if p not in ("fal", "replicate")] for k, v in chain_map.items()}

    # Deduplicate queries for parallel processing
    unique_queries = list(set(q for q in queries if q))
    query_to_path: dict[str, Path] = {}

    # Download unique queries in parallel
    workers = min(4, len(unique_queries)) if unique_queries else 1
    log(f"visuals: downloading {len(unique_queries)} unique queries ({workers} workers)...", "info")

    def _download_one(query: str) -> tuple[str, Optional[Path]]:
        """Download a single query. Returns (query, path) or (query, None) on failure."""
        dest = clips_dir / f"dl_{hash(query) % 10000:04d}"
        chain = chain_map.get(provider, [provider])
        for p in chain:
            try:
                if p == "pexels":
                    clip = _pexels_video(query, dest, cfg, set())
                elif p == "scraper":
                    clip = _scrape_visual(query, dest, cfg, set(), topic_words,
                                          prefer_videos, source_priority, 0)
                elif p in ("fal", "replicate"):
                    clip = _ai_image(query, dest, cfg, p)
                elif p == "ai_image":
                    # Use the new image generator module
                    clip = generate_scene_image(query, dest)
                else:
                    continue
                # Quick validation - just check file exists and has size
                if clip and clip.exists() and clip.stat().st_size > 10000:
                    return (query, clip)
            except Exception as e:
                log(f"visual '{query[:30]}' provider={p} failed: {e}", "warn")
                continue
        
        # Fallback: try AI image generation if all else fails
        if "ai_image" not in chain and "fal" not in chain and "replicate" not in chain:
            try:
                clip = generate_scene_image(query, dest)
                if clip and clip.exists() and clip.stat().st_size > 1000:
                    return (query, clip)
            except Exception as e:
                log(f"visual '{query[:30]}' AI generation failed: {e}", "warn")
        
        return (query, None)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_download_one, q): q for q in unique_queries}
        for future in as_completed(futures, timeout=120):
            query, path = future.result()
            if path:
                query_to_path[query] = path
                log(f"visual '{query[:30]}' downloaded", "info")

    # Assemble final paths in order, reusing downloaded clips
    paths: list[Path] = []
    for i, q in enumerate(queries):
        dest = clips_dir / f"{i:02d}"
        if q and q in query_to_path:
            src = query_to_path[q]
            clip = dest.with_suffix(src.suffix)
            if not clip.exists():
                shutil.copyfile(src, clip)
            paths.append(clip)
            log(f"visual {i} ('{q[:30]}') reused from cache", "info")
        elif paths:
            # Reuse previous clip
            src = paths[-1]
            clip = dest.with_suffix(src.suffix)
            if not clip.exists():
                shutil.copyfile(src, clip)
            paths.append(clip)
            log(f"visual {i} reusing previous", "warn")
        else:
            continue

    # Cleanup temp download files
    for p in query_to_path.values():
        try:
            if p.exists() and "dl_" in p.name:
                p.unlink()
        except Exception:
            pass

    if not paths:
        raise RuntimeError("No visuals could be gathered.")
    log(f"visuals: {len(paths)} clips ({provider})", "ok")
    return paths


# --------------------------------------------------------------- topic ---

def extract_topic_words(cfg: dict, idea: dict | None = None) -> list[str]:
    """Niche keywords used to bias B-roll ranking toward on-topic footage.

    Combines the idea's primary_keyword/title with the channel niche, dropping
    generic stopwords. Used by rank_candidates() to bonus candidates whose title
    actually contains the topic (so a GTA channel gets GTA clips, not generic
    "car driving" stock)."""
    STOP = {"the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
            "of", "with", "by", "from", "is", "are", "was", "were", "be", "this",
            "that", "it", "as", "do", "does", "did", "will", "can", "not", "how",
            "what", "why", "when", "de", "da", "do", "dos", "das", "em", "na", "no",
            "por", "com", "sem", "que", "uma", "uns", "para", "mais", "como", "mas",
            "ou", "se", "ao", "aos", "um", "uns", "ser", "tem", "foi", "video",
            "photo", "footage", "clip", "footages", "stock"}
    raw: list[str] = []
    if idea:
        for key in ("primary_keyword", "title", "concept"):
            v = (idea.get(key) or "").strip()
            if v:
                raw.append(v)
    niche = (cfg.get("channel", {}) or {}).get("niche", "")
    if niche:
        raw.append(niche)
    # split into words, dedupe, drop stopwords, keep tokens >= 3 chars
    seen: set[str] = set()
    out: list[str] = []
    for phrase in raw:
        for w in re.findall(r"[a-z0-9áéíóúâêôãõç]+", phrase.lower()):
            if len(w) >= 3 and w not in STOP and w not in seen:
                seen.add(w)
                out.append(w)
    return out


# --------------------------------------------------------------- scraper ---
# 100% free sources, parallel metadata search -> rank -> download winner.

_FUNCS: dict = {}


def _register(name: str):
    def deco(fn):
        _FUNCS[name] = fn
        return fn
    return deco


def _scrape_visual(query: str, dest: Path, cfg: dict, used_urls: Optional[set] = None,
                    topic_words: list[str] | None = None,
                    prefer_videos: bool = False,
                    source_priority: list[str] | None = None,
                    query_uses: int = 0) -> Path:
    used_urls = used_urls if used_urls is not None else set()
    mode = cfg["visuals"].get("scraper_mode", "best")
    # reorder sources by niche priority (gaming -> youtube first), keep the rest
    default_sources = ["youtube", "ddg_video_ytdlp", "pixabay", "pexels_photo",
                       "unsplash", "google_cse", "giphy", "openverse_commons",
                       "archive", "ddg_image"]
    srcs = list(source_priority or cfg["visuals"].get("scraper_sources", default_sources))
    # ensure no source is lost if user listed a subset
    for s in default_sources:
        if s not in srcs:
            srcs.append(s)
    if mode == "first":
        last_err = None
        for s in srcs:
            try:
                p = _download_from_source(s, query, dest, cfg)
                _validate_clip(p, cfg, topic_words)
                return p
            except Exception as e:
                log(f"scraper:{s} '{query[:30]}' failed: {e}", "warn")
                last_err = e
        raise RuntimeError(f"all scraper_sources failed: {last_err}")
    per_source = int(cfg["visuals"].get("candidates_per_source", 3))
    workers = max(1, int(cfg["visuals"].get("max_workers", 6)))
    cands: list[Candidate] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_search_source, s, query, per_source, cfg): s for s in srcs}
        for f in as_completed(futs, timeout=60):
            try:
                cands += f.result() or []
            except Exception as e:
                log(f"scraper search {futs[f]} failed: {e}", "warn")
    if not cands:
        raise RuntimeError("no candidates from any source")
    ranked = rank_candidates(cands, query, cfg, used_urls, topic_words, prefer_videos)
    # WHEN prefer_videos=true (gaming/entertainment): if any video candidates
    # exist, filter to ONLY videos so we don't fall back to static images.
    # If no videos are available at all, allow images as fallback.
    if prefer_videos:
        video_cands = [c for c in ranked if c.kind == "video"]
        if video_cands:
            ranked = video_cands
            log(f"visual '{query[:30]}': prefer_videos active, filtered to {len(ranked)} video candidates", "info")
    # THEMATIC VALIDATION: filter out candidates that are completely off-topic.
    # When we have topic words, only keep candidates that match at least one topic word.
    # This ensures clips are actually related to the theme/script narration.
    topic_l = [t for t in (topic_words or []) if t]
    if topic_l:
        on_topic_cands = [c for c in ranked if any(t in (c.title or "").lower() for t in topic_l)]
        if on_topic_cands:
            ranked = on_topic_cands
            log(f"visual '{query[:30]}': filtered to {len(ranked)} on-topic candidates "
                f"(topic: {topic_l})", "info")
        else:
            log(f"visual '{query[:30]}': WARNING no on-topic B-roll found "
                f"(topic: {topic_l}) — results may be generic", "warn")
    log("rank '%s': %s" % (query[:40], " | ".join(
        f"{c.source}:{c.score:.0f}" for c in ranked[:3])), "info")
    try:
        from dataclasses import asdict as _asdict
        (dest.parent / (dest.name + ".json")).write_text(json.dumps(
            [_asdict(c) for c in ranked[:5]], indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    # VARIETY: for repeated queries, pick the Nth-best candidate (round-robin)
    # instead of always the winner -> consecutive clips actually differ.
    pick_idx = min(query_uses, len(ranked) - 1)
    last_err2 = None
    for offset in range(len(ranked)):
        c = ranked[(pick_idx + offset) % len(ranked)]
        try:
            p = _download_candidate(c, dest, cfg)
            _validate_clip(p, cfg, topic_words)
            used_urls.add(c.url)
            return p
        except Exception as e:
            last_err2 = e
            continue
    raise RuntimeError(f"all ranked downloads failed: {last_err2}")


def _download_from_source(source: str, query: str, dest: Path, cfg: dict) -> Path:
    cands = _search_source(source, query,
                           int(cfg["visuals"].get("candidates_per_source", 3)), cfg)
    if not cands:
        raise RuntimeError(f"{source}: no results")
    return _download_candidate(rank_candidates(cands, query, cfg, None)[0], dest, cfg)


def _search_source(source: str, query: str, n: int, cfg: dict) -> list[Candidate]:
    fn = _FUNCS.get(source)
    if fn is None:
        raise RuntimeError(f"unknown scraper source: {source}")
    return fn(query, n, cfg) or []


def _download_candidate(c: Candidate, dest: Path, cfg: dict) -> Path:
    """Download a candidate clip. Validates the file is valid after download."""
    if c.source in ("youtube", "vimeo_ytdlp"):
        return _download_ytdlp(c.url, dest, cfg)
    suffix = ".mp4" if c.kind == "video" else ".jpg"
    low = c.url.lower().split("?")[0]
    for ext in (".mp4", ".png", ".jpg", ".jpeg", ".webp"):
        if low.endswith(ext):
            suffix = ".jpg" if ext == ".jpeg" else ext
            break
    out = dest.with_suffix(suffix)
    r = requests.get(c.url, headers=UA, timeout=45)
    r.raise_for_status()
    if len(r.content) < 5000:
        raise RuntimeError(f"suspiciously small file from {c.source}")
    out.write_bytes(r.content)

    # Validate downloaded file is actually valid (not corrupted)
    _validate_downloaded_file(out)

    return out


def _validate_downloaded_file(path: Path) -> None:
    """Validate that a downloaded image/video file is not corrupted.

    Uses ffprobe for videos and PIL/imagesize check for images.
    Raises RuntimeError if file is invalid so the scraper can try next candidate."""
    if path.suffix.lower() == ".mp4":
        # Validate video with ffprobe
        try:
            dur = ffprobe_duration(path)
            if dur <= 0:
                raise RuntimeError(f"invalid video duration: {dur}")
        except Exception as e:
            raise RuntimeError(f"invalid video file: {e}")
    else:
        # Validate image - try to read header to verify it's a valid image
        try:
            with open(path, "rb") as f:
                header = f.read(32)
            # Check for valid image magic bytes
            if len(header) < 8:
                raise RuntimeError("file too small to be valid image")
            # JPEG: FF D8 FF
            # PNG: 89 50 4E 47
            # WEBP: RIFF....WEBP
            # GIF: 47 49 46 38
            is_jpeg = header[:3] == b'\xff\xd8\xff'
            is_png = header[:4] == b'\x89PNG'
            is_webp = header[:4] == b'RIFF' and header[8:12] == b'WEBP'
            is_gif = header[:4] == b'GIF8'
            if not (is_jpeg or is_png or is_webp or is_gif):
                raise RuntimeError(f"invalid image format (header: {header[:8].hex()})")
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"invalid image file: {e}")


def _download_ytdlp(page_url: str, dest: Path, cfg: dict) -> Path:
    out = dest.with_suffix(".mp4")
    try:
        out.unlink(missing_ok=True)
    except Exception:
        pass
    # Fast download: use lowest quality to get small files quickly.
    # Format 18 (360p) is usually ~10MB for a 3-4 min video.
    base = ["--no-playlist",
            "--format", "worst",
            "--extractor-args", "youtube:player_client=android",
            "--socket-timeout", "15",
            "-o", str(out), "--retries", "1", "--no-warnings", page_url]
    cmd = [*_ytdlp_binary(), *base]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    if proc.returncode != 0 or not out.exists() or out.stat().st_size < 10000:
        raise RuntimeError(f"yt-dlp download failed: {(proc.stderr or proc.stdout)[-200:]}")
    return out


def _validate_clip(path: Path, cfg: dict, topic_words: list[str] | None = None) -> None:
    """Validate a downloaded clip meets quality standards and is on-topic.

    When topic_words are provided, checks that the clip's metadata JSON (if present)
    contains at least one topic word. This ensures the clip is actually related
    to the theme/script narration."""
    min_s = float(cfg["visuals"].get("min_clip_seconds", 1.0))
    min_b = int(cfg["visuals"].get("min_clip_bytes", 100000))
    if path.suffix.lower() not in (".mp4", ".png", ".jpg", ".jpeg", ".webp"):
        raise RuntimeError(f"invalid format: {path.suffix}")
    if not path.exists() or path.stat().st_size < min_b:
        raise RuntimeError(f"file too small: {path}")
    if path.suffix.lower() == ".mp4":
        if ffprobe_duration(path) < min_s:
            raise RuntimeError("video shorter than minimum")
    # Topic validation: check if clip metadata indicates it's on-topic
    if topic_words:
        json_path = path.with_suffix(".json")
        if json_path.exists():
            try:
                meta = json.loads(json_path.read_text(encoding="utf-8"))
                # Check if any candidate in the metadata has a title matching the topic
                if isinstance(meta, list) and meta:
                    titles = " ".join(c.get("title", "") for c in meta).lower()
                    if not any(t.lower() in titles for t in topic_words):
                        log(f"clip {path.name}: metadata shows no topic match "
                            f"(topic: {topic_words}) — may be off-topic", "warn")
            except Exception:
                pass  # metadata check is best-effort


# -- YouTube via yt-dlp (free, no key) ---------------------------------------
@_register("youtube")
def _search_youtube(query: str, n: int, cfg: dict) -> list[Candidate]:
    n = max(1, min(n, 5))
    cmd = [*_ytdlp_binary(), "--no-playlist", "--flat-playlist",
           "--print", "%(id)s\t%(title)s\t%(duration)s",
           f"ytsearch{n}:{query}"]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          timeout=int(cfg["visuals"].get("scraper_timeout", 60)))
    if proc.returncode != 0:
        raise RuntimeError(f"yt-dlp search failed: {(proc.stderr or proc.stdout)[-300:]}")
    out: list[Candidate] = []
    for line in (proc.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        vid, title = parts[0].strip(), parts[1].strip()
        try:
            dur = float(parts[2]) if len(parts) > 2 and parts[2] not in ("NA", "None", "") else 0.0
        except Exception:
            dur = 0.0
        if not vid or vid == "NA":
            continue
        out.append(Candidate(source="youtube", kind="video",
                             url=f"https://www.youtube.com/watch?v={vid}",
                             title=title, duration=dur, license="unknown"))
    if not out:
        raise RuntimeError("no youtube results")
    return out


# -- DuckDuckGo video discoverer -> yt-dlp (free, no key) ---------------------
@_register("ddg_video_ytdlp")
def _search_ddg_video(query: str, n: int, cfg: dict) -> list[Candidate]:
    try:
        from ddgs import DDGS
    except ImportError:
        raise RuntimeError("ddgs not installed (pip install ddgs)")
    out: list[Candidate] = []
    domains = ("youtube.com", "youtu.be", "vimeo.com", "tiktok.com")
    with DDGS() as ddgs:
        # 1) native video search (ddgs<=9.16 videos() is broken upstream for all
        #    backends — errors are swallowed and we fall through to text search)
        try:
            for it in ddgs.videos(query, max_results=max(3, n)):
                url = it.get("content") or ""
                if not any(d in url for d in domains):
                    continue
                src = "vimeo_ytdlp" if "vimeo" in url else "youtube"
                out.append(Candidate(source=src, kind="video", url=url,
                                     title=it.get("title", ""), license="unknown"))
                if len(out) >= n:
                    break
        except Exception:
            pass
        # 2) text-search fallback: page URLs filtered to known video sites
        if not out:
            sites = " OR ".join(f"site:{d}" for d in domains)
            for it in ddgs.text(f"{query} {sites}", max_results=max(6, n * 3)):
                url = (it.get("href") or "").strip()
                is_yt = "watch?v=" in url
                is_vimeo = ("vimeo.com/" in url and "/event/" not in url
                            and url.rstrip("/").split("/")[-1].isdigit())
                is_tiktok = "/video/" in url
                if not (is_yt or is_vimeo or is_tiktok):
                    continue  # skip channel pages / plain domains / non-video links
                src = "vimeo_ytdlp" if is_vimeo else "youtube"
                out.append(Candidate(source=src, kind="video", url=url,
                                     title=it.get("title", ""), license="unknown"))
                if len(out) >= n:
                    break
    if not out:
        raise RuntimeError("no ddg video results")
    return out


@_register("vimeo_ytdlp")
def _search_vimeo(query: str, n: int, cfg: dict) -> list[Candidate]:
    cands = _search_ddg_video(query, n, cfg)
    v = [c for c in cands if c.source == "vimeo_ytdlp"]
    if not v:
        raise RuntimeError("no vimeo results")
    return v


# -- Pixabay video+image (free key) --------------------------------------------
def _env_key(name: str) -> str | None:
    """Read an API key, treating placeholder/comment values as missing.

    Guards against .env lines like `PIXABAY_API_KEY=# free: https://...`
    (comment pasted as value) so the source is skipped cleanly instead of
    burning a request on a 400/401.
    """
    val = (env(name) or "").strip()
    if not val or val.startswith("#") or "://" in val or val.lower().startswith("your_"):
        return None
    return val


@_register("pixabay")
def _search_pixabay(query: str, n: int, cfg: dict) -> list[Candidate]:
    key = _env_key("PIXABAY_API_KEY")
    if not key:
        raise RuntimeError("PIXABAY_API_KEY empty, skipping")
    out: list[Candidate] = []
    r = requests.get("https://pixabay.com/api/videos/",
                     params={"key": key, "q": query, "per_page": min(n, 5)}, timeout=30)
    r.raise_for_status()
    for h in (r.json().get("hits", []) or [])[:n]:
        v = (h.get("videos") or {}).get("medium") or {}
        if v.get("url"):
            out.append(Candidate(source="pixabay", kind="video", url=v["url"],
                                 title=h.get("tags", ""), width=v.get("width", 0),
                                 height=v.get("height", 0), duration=10.0,
                                 license="commercial-free"))
    if not out:
        r2 = requests.get("https://pixabay.com/api/",
                          params={"key": key, "q": query, "image_type": "photo",
                                  "orientation": "vertical", "per_page": min(n, 5)}, timeout=30)
        r2.raise_for_status()
        for h in (r2.json().get("hits", []) or [])[:n]:
            url = h.get("largeImageURL") or h.get("webformatURL")
            if url:
                out.append(Candidate(source="pixabay", kind="image", url=url,
                                     title=h.get("tags", ""), license="commercial-free"))
    if not out:
        raise RuntimeError("no pixabay results")
    return out


# -- Pexels photos (same free key) -------------------------------------------
@_register("pexels_photo")
def _search_pexels_photo(query: str, n: int, cfg: dict) -> list[Candidate]:
    key = env("PEXELS_API_KEY")
    if not key:
        raise RuntimeError("PEXELS_API_KEY empty, skipping")
    r = requests.get("https://api.pexels.com/v1/search", headers={"Authorization": key},
                     params={"query": query, "orientation": "portrait",
                             "per_page": min(n, 5)}, timeout=30)
    r.raise_for_status()
    out: list[Candidate] = []
    for p in (r.json().get("photos", []) or [])[:n]:
        url = (p.get("src") or {}).get("portrait") or (p.get("src") or {}).get("large")
        if url:
            out.append(Candidate(source="pexels_photo", kind="image", url=url,
                                 title=p.get("alt", ""), width=p.get("width", 0),
                                 height=p.get("height", 0), license="commercial-free"))
    if not out:
        raise RuntimeError("no pexels photo results")
    return out


# -- Unsplash (free key) -----------------------------------------------------
@_register("unsplash")
def _search_unsplash(query: str, n: int, cfg: dict) -> list[Candidate]:
    key = _env_key("UNSPLASH_ACCESS_KEY")
    if not key:
        raise RuntimeError("UNSPLASH_ACCESS_KEY empty, skipping")
    r = requests.get("https://api.unsplash.com/search/photos",
                     headers={"Authorization": f"Client-ID {key}"},
                     params={"query": query, "orientation": "portrait",
                             "per_page": min(n, 5)}, timeout=30)
    r.raise_for_status()
    out: list[Candidate] = []
    for p in (r.json().get("results", []) or [])[:n]:
        url = (p.get("urls") or {}).get("regular")
        if url:
            out.append(Candidate(source="unsplash", kind="image", url=url,
                                 title=p.get("alt_description") or "",
                                 width=p.get("width", 0), height=p.get("height", 0),
                                 license="commercial-free"))
    if not out:
        raise RuntimeError("no unsplash results")
    return out


# -- Google CSE images (official, 100/day free) -------------------------------
@_register("google_cse")
def _search_google_cse(query: str, n: int, cfg: dict) -> list[Candidate]:
    key = _env_key("GOOGLE_API_KEY")
    cx = _env_key("GOOGLE_CX")
    if not key or not cx:
        raise RuntimeError("GOOGLE_API_KEY/CX empty, skipping")
    try:
        from googleapiclient.discovery import build
    except ImportError:
        raise RuntimeError("google-api-python-client not installed")
    params: dict = {"q": query, "cx": cx, "searchType": "image",
                    "num": min(max(n, 1), 5), "imgSize": "XLARGE",
                    "imgType": "photo", "safe": "active"}
    lic = "unknown"
    if cfg["visuals"].get("scraper_cc_only"):
        params["rights"] = "cc_publicdomain|cc_attribute|cc_sharealike"
        lic = "cc"
    res = build("customsearch", "v1", developerKey=key).cse().list(**params).execute()
    out: list[Candidate] = []
    for it in (res.get("items", []) or [])[:n]:
        if it.get("link"):
            img = it.get("image") or {}
            out.append(Candidate(source="google_cse", kind="image", url=it["link"],
                                 title=it.get("title", ""), width=img.get("width", 0),
                                 height=img.get("height", 0), license=lic))
    if not out:
        raise RuntimeError("no google results")
    return out


# -- Giphy (free key, short vertical mp4) -------------------------------------
@_register("giphy")
def _search_giphy(query: str, n: int, cfg: dict) -> list[Candidate]:
    key = _env_key("GIPHY_API_KEY")
    if not key:
        raise RuntimeError("GIPHY_API_KEY empty, skipping")
    r = requests.get("https://api.giphy.com/v1/gifs/search",
                     params={"api_key": key, "q": query,
                             "limit": min(n, 5), "rating": "g"}, timeout=30)
    r.raise_for_status()
    out: list[Candidate] = []
    for g in (r.json().get("data", []) or [])[:n]:
        mp4 = ((g.get("images") or {}).get("original_mp4") or {}).get("mp4")
        if mp4:
            out.append(Candidate(source="giphy", kind="video", url=mp4,
                                 title=g.get("title", ""), width=480,
                                 height=852, duration=2.5, license="unknown"))
    if not out:
        raise RuntimeError("no giphy results")
    return out


# -- Openverse + Commons (100% CC, no key) --------------------------------------
@_register("openverse_commons")
def _search_openverse(query: str, n: int, cfg: dict) -> list[Candidate]:
    out: list[Candidate] = []
    try:
        r = requests.get("https://api.openverse.org/v1/images/",
                         params={"q": query, "license_type": "commercial",
                                 "page_size": min(n, 5)}, headers=UA, timeout=30)
        r.raise_for_status()
        for it in (r.json().get("results", []) or [])[:n]:
            if it.get("url"):
                out.append(Candidate(source="openverse_commons", kind="image",
                                     url=it["url"], title=it.get("title", ""),
                                     license="cc"))
    except Exception as e:
        log(f"openverse failed: {e}, trying commons", "warn")
    if out:
        return out
    r2 = requests.get("https://commons.wikimedia.org/w/api.php",
                      params={"action": "query", "format": "json", "generator": "search",
                              "gsrsearch": query, "gsrlimit": min(n, 5),
                              "prop": "imageinfo", "iiprop": "url|size|mime"},
                      headers=UA, timeout=30)
    r2.raise_for_status()
    for p in list((r2.json().get("query", {}) or {}).get("pages", {}).values())[:n]:
        info = (p.get("imageinfo") or [{}])[0]
        url = info.get("url", "")
        if not url:
            continue
        kind = "video" if "video" in str(info.get("mime", "")) else "image"
        out.append(Candidate(source="openverse_commons", kind=kind, url=url,
                             title=p.get("title", ""), license="cc"))
    if not out:
        raise RuntimeError("no openverse/commons results")
    return out


# -- Internet Archive (public domain, no key) ----------------------------------
@_register("archive")
def _search_archive(query: str, n: int, cfg: dict) -> list[Candidate]:
    r = requests.get("https://archive.org/advancedsearch.php",
                     params={"q": f"({query}) AND mediatype:movies",
                             "fl[]": ["identifier", "title"],
                             "rows": min(n, 5), "output": "json"}, timeout=30)
    r.raise_for_status()
    docs = ((r.json().get("response") or {}).get("docs")) or []
    out: list[Candidate] = []
    for d in docs:
        ident = d.get("identifier")
        if not ident:
            continue
        try:
            m = requests.get(f"https://archive.org/metadata/{ident}", timeout=30).json()
        except Exception:
            continue
        files = [f for f in (m.get("files") or [])
                 if str(f.get("name", "")).lower().endswith((".mp4", ".ogv", ".webm"))]
        if files:
            f0 = sorted(files, key=lambda f: int(f.get("size", 1 << 60) or (1 << 60)))[0]
            out.append(Candidate(source="archive", kind="video",
                                 url=f"https://archive.org/download/{ident}/{f0['name']}",
                                 title=d.get("title", ""), license="public-domain"))
    if not out:
        raise RuntimeError("no archive results")
    return out


# -- DuckDuckGo images (free, no key — guaranteed net) -------------------------
@_register("ddg_image")
def _search_ddg_image(query: str, n: int, cfg: dict) -> list[Candidate]:
    try:
        from ddgs import DDGS
    except ImportError:
        raise RuntimeError("ddgs not installed (pip install ddgs)")
    out: list[Candidate] = []
    with DDGS() as ddgs:
        for it in ddgs.images(query, max_results=max(3, n)):
            url = it.get("image", "")
            if not url:
                continue
            out.append(Candidate(source="ddg_image", kind="image", url=url,
                                 title=it.get("title", ""), thumb=it.get("thumbnail", ""),
                                 license="unknown"))
            if len(out) >= n:
                break
    if not out:
        raise RuntimeError("no ddg image results")
    return out


def _pexels_video(query: str, dest: Path, cfg: dict, used_ids: Optional[set] = None) -> Path:
    key = env("PEXELS_API_KEY")
    if not key:
        raise SystemExit("PEXELS_API_KEY missing in .env")
    used_ids = used_ids if used_ids is not None else set()
    r = requests.get(
        "https://api.pexels.com/videos/search",
        headers={"Authorization": key},
        params={"query": query, "orientation": "portrait", "per_page": 12, "size": "large"},
        timeout=30,
    )
    r.raise_for_status()
    videos = r.json().get("videos", [])
    if not videos:
        raise RuntimeError("no Pexels results")

    # Prefer a fresh result (not already used) so consecutive clips differ.
    fresh = [v for v in videos if v.get("id") not in used_ids] or videos
    video = fresh[0]
    used_ids.add(video.get("id"))

    # Pick a crisp portrait file: tall enough for 1080x1920 but not absurdly large.
    portrait = [f for f in video["video_files"]
                if (f.get("height") or 0) >= (f.get("width") or 0)]
    pool = portrait or video["video_files"]
    files = sorted(
        pool,
        key=lambda f: (abs((f.get("height") or 0) - 1920), -(f.get("width") or 0)),
    )
    url = files[0]["link"]
    out = dest.with_suffix(".mp4")
    out.write_bytes(requests.get(url, timeout=120).content)
    return out


def _ai_image(prompt: str, dest: Path, cfg: dict, provider: str) -> Path:
    style = cfg["visuals"].get("ai_image_style", "")
    full = f"{prompt}, {style}"
    out = dest.with_suffix(".png")
    if provider == "fal":
        key = env("FAL_KEY")
        if not key:
            raise SystemExit("FAL_KEY missing in .env")
        r = requests.post(
            "https://fal.run/fal-ai/flux/schnell",
            headers={"Authorization": f"Key {key}", "Content-Type": "application/json"},
            json={"prompt": full, "image_size": "portrait_16_9"},
            timeout=120,
        )
        r.raise_for_status()
        img_url = r.json()["images"][0]["url"]
    else:  # replicate
        key = env("REPLICATE_API_TOKEN")
        if not key:
            raise SystemExit("REPLICATE_API_TOKEN missing in .env")
        r = requests.post(
            "https://api.replicate.com/v1/models/black-forest-labs/flux-schnell/predictions",
            headers={"Authorization": f"Bearer {key}", "Prefer": "wait"},
            json={"input": {"prompt": full, "aspect_ratio": "9:16"}},
            timeout=180,
        )
        r.raise_for_status()
        out_field = r.json().get("output")
        img_url = out_field[0] if isinstance(out_field, list) else out_field
    out.write_bytes(requests.get(img_url, timeout=120).content)
    return out
