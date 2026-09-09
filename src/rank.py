"""Candidate ranking for the multi-source scraper (100% free sources).

Fan-out (parallel metadata search) -> score -> download only the winner.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class Candidate:
    source: str       # youtube | pixabay | unsplash | google_cse | ...
    kind: str         # video | image
    url: str          # direct download URL (or page URL for yt-dlp)
    title: str = ""
    width: int = 0
    height: int = 0
    duration: float = 0.0
    license: str = "unknown"  # cc | commercial-free | unknown
    thumb: str = ""
    score: float = field(default=0.0, compare=False)

    def to_dict(self) -> dict:
        return asdict(self)


def rank_candidates(
    cands: list[Candidate],
    query: str,
    cfg: dict,
    used_urls: set | None = None,
    topic_words: list[str] | None = None,
    prefer_videos: bool = False,
) -> list[Candidate]:
    """Score candidates heuristically. Higher = better for 9:16 Shorts.

    `topic_words` (optional): niche keywords from the channel/idea. Candidates whose
    title contains any of them get a relevance boost, so on-topic B-roll (e.g. a GTA
    clip for a GTA channel) outranks a generic but higher-quality stock shot.

    `prefer_videos`: when True (gaming/entertainment niches), motion footage is
    strongly favored over static images — clips feel alive, not like a slideshow.
    """
    used_urls = used_urls or set()
    vcfg = cfg.get("visuals", {})
    weights = vcfg.get("rank_weights", {}) or {}
    w_rel = float(weights.get("relevance", 40))
    w_qual = float(weights.get("quality", 25))
    w_vert = float(weights.get("vertical", 15))
    w_dur = float(weights.get("duration_fit", 10))
    w_lic = float(weights.get("license", 10))
    cut = float(cfg.get("video", {}).get("cut_every_seconds", 2.3) or 2.3)
    penalty = float(vcfg.get("dedup_reuse_penalty", 25))

    qwords = set(query.lower().split())
    topic_l = [t.lower() for t in (topic_words or []) if t]
    for c in cands:
        title_l = (c.title or "").lower()
        twords = set(title_l.split())
        rel = (len(qwords & twords) / max(1, len(qwords)) * 100.0) if qwords else 50.0
        if "official" in title_l:
            rel += 15.0
        if "trailer" in title_l:
            rel += 10.0
                # MOTION matters: video keeps viewers far better than a still image.
        # For gaming/entertainment this is a very strong preference.
        if c.kind == "video":
            rel += 50.0 if prefer_videos else 12.0
        else:
            rel -= 30.0 if prefer_videos else 0.0  # strongly penalize stills when motion wanted
        # THEMATIC BONUS: candidate actually shows the channel's topic
        has_topic = topic_l and any(t in title_l for t in topic_l)
        if has_topic:
            rel += 35.0
        elif topic_l:
            # HARD PENALTY: candidate has NO topic relevance at all
            # This ensures off-topic clips (e.g. random people, generic stock)
            # are strongly deprioritized even if they have high quality
            rel -= 50.0
        rel = min(rel, 160.0)

        qual = (min(c.width or 0, 1080) / 1080.0 * 100.0) if c.width else 50.0
        vert = 100.0 if (c.height or 0) >= (c.width or 0) and c.height else 30.0
        if c.kind == "image":
            dur = 80.0 if prefer_videos else 100.0  # Ken Burns is weaker than real motion
        else:
            d = c.duration or 0.0
            dur = 100.0 if d >= cut else (d / cut * 100.0 if d > 0 else 40.0)
        lic = 100.0 if c.license in ("cc", "commercial-free", "public-domain") else 50.0

        c.score = (
            rel * w_rel / 100.0
            + qual * w_qual / 100.0
            + vert * w_vert / 100.0
            + dur * w_dur / 100.0
            + lic * w_lic / 100.0
        )
        if c.url in used_urls:
            c.score -= penalty
    return sorted(cands, key=lambda c: c.score, reverse=True)
