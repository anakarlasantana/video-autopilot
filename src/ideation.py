"""Stage 1 — pick a fresh, trend-anchored, viral-leaning idea for the channel's niche.

Ideas must ride REAL current demand: headlines from the last 7 days (Google News RSS,
DDG News) plus live YouTube autocomplete (what people actually search, ordered by
demand). Every idea carries a `trend_anchor` pointing at the seed it was built from;
strict mode rejects unanchored ideas. All sources are free (no API key) and fail-soft:
if every source is down the run falls back to evergreen LLM niche knowledge.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import requests

from .config import load_prompt
from .knowledge import strategy_notes
from .llm import complete
from .utils import extract_json, log, recent_topics

_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) video-autopilot/1.0"}


# ── Trend sources (all free, no key, fail-soft) ───────────────────────────────

def _fetch_google_news(query: str, cfg: dict, limit: int = 6) -> list[str]:
    """Real headlines from Google News RSS, localized, default last 7 days."""
    t = cfg["channel"].get("trends", {})
    q = requests.utils.quote(query)
    hl = t.get("locale", "pt-BR")
    gl = t.get("gl", "BR")
    ceid = t.get("ceid", "BR:pt-419")
    r = requests.get(
        f"https://news.google.com/rss/search?q={q}&hl={hl}&gl={gl}&ceid={ceid}",
        timeout=10, headers=_UA)
    r.raise_for_status()
    # only <item> titles — the feed also has <channel>/<image> titles ("Google Notícias")
    titles = re.findall(r"<item>.*?<title>(.*?)</title>", r.text, re.S)
    return [t.strip() for t in titles[:limit] if t.strip()]


def _fetch_yt_suggest(seed: str, limit: int = 6) -> list[str]:
    """What people search on YouTube right now (order ≈ search demand)."""
    r = requests.get(
        "https://suggestqueries.google.com/complete/search",
        params={"client": "firefox", "ds": "yt", "q": seed},
        timeout=10, headers=_UA)
    r.raise_for_status()
    data = r.json()
    return [s for s in (data[1] if len(data) > 1 else [])[:limit] if isinstance(s, str)]


def _fetch_ddg_news(query: str, max_days: int = 7, limit: int = 5) -> list[str]:
    """DDG News headlines, kept only if published within `max_days`."""
    from ddgs import DDGS
    with DDGS() as d:
        items = list(d.news(query, max_results=limit))
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_days)
    out: list[str] = []
    for it in items:
        title = (it.get("title") or "").strip()
        if not title:
            continue
        date = it.get("date") or ""
        if date:
            try:
                if datetime.fromisoformat(date.replace("Z", "+00:00")) < cutoff:
                    continue
            except ValueError:
                pass  # unparsable date → keep (better a stale seed than none)
        out.append(title)
    return out


def get_trends(cfg: dict) -> list[str]:
    """Real trend seeds for the channel niche (deduped, ≤12).

    Reads `channel.trends`: sources (google_news | yt_suggest | ddg_news),
    news_query (Google News/DDG, may use `when:7d` operator), seed (autocomplete).
    Falls back to legacy static `trend_seeds` when no dynamic source is configured.
    """
    t = cfg["channel"].get("trends") or {}
    sources = t.get("sources") or []
    if not sources:
        return cfg["channel"].get("trend_seeds", [])
    q = (t.get("news_query") or "").strip()
    ddg_q = re.sub(r"\s*when:\S+", "", q).strip()  # when:7d is a Google News operator
    seeds: list[str] = []
    for src in sources:
        try:
            if src == "google_news" and q:
                seeds += _fetch_google_news(q, cfg)
            elif src == "yt_suggest" and t.get("seed"):
                seeds += _fetch_yt_suggest(t["seed"])
            elif src == "ddg_news" and ddg_q:
                seeds += _fetch_ddg_news(ddg_q)
        except Exception as e:  # noqa: BLE001 — any source may die, never break ideation
            log(f"trend source {src} failed: {e}", "warn")
    seen: set[str] = set()
    out: list[str] = []
    for s in seeds:
        k = s.lower().strip()
        if k not in seen and len(s) > 3:
            seen.add(k)
            out.append(s)
    return out[:12]


# ── Anchor validation + trend re-ranking ──────────────────────────────────────

def _tokens(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9áéíóúâêôãõçüà]+", (s or "").lower()) if len(w) > 2}


def _anchor_match(anchor: str, seeds: list[str]) -> str | None:
    """Seed that best overlaps the anchor (token overlap ≥ 40%), else None."""
    a = _tokens(anchor)
    if not a:
        return None
    best, best_ov = None, 0.0
    for s in seeds:
        ov = len(a & _tokens(s)) / max(1, len(a))
        if ov > best_ov:
            best, best_ov = s, ov
    return best if best_ov >= 0.4 else None


def _rerank_by_trend(ideas: list[dict], seeds: list[str], cfg: dict) -> list[dict]:
    """Score each idea 0-10 on trend relevance via a cheap LLM call; reorder best-first.

    total = trend_relevance*0.6 + save_worthiness*0.4 (both on a 0-10 scale).
    On any failure the original order is kept (prompt already ranks best-first).
    """
    listing = "\n".join(
        f'{i}. "{it.get("title", "")}" — {it.get("concept", "")} '
        f'[anchor: {it.get("trend_anchor") or "(none)"}]'
        for i, it in enumerate(ideas))
    prompt = (
        "You are ranking faceless-video ideas by how well they exploit TODAY's "
        "trending topics and search demand.\n\nTrend seeds (real headlines from the "
        f"last 7 days / live YouTube search terms):\n"
        + "\n".join(f"- {s}" for s in seeds)
        + "\n\nIdeas:\n" + listing
        + '\n\nScore trend_relevance 0-10 for each idea: 10 = directly rides a '
          'specific seed above, 0 = unrelated to any seed. Judge anchoring, freshness '
          'and search demand only (not production quality).\n'
        'Return ONLY valid JSON: [{"index": 0, "trend_relevance": 8, "reason": "one short line"}]'
    )
    try:
        data = extract_json(complete(prompt, cfg, max_tokens=900))
        scores = {int(x["index"]): x for x in data if isinstance(x, dict) and "index" in x}
    except Exception as e:  # noqa: BLE001
        log(f"trend re-rank failed, keeping prompt order: {e}", "warn")
        return ideas
    for i, idea in enumerate(ideas):
        s = scores.get(i, {})
        try:
            trend = max(0.0, min(10.0, float(s.get("trend_relevance", 0))))
        except (TypeError, ValueError):
            trend = 0.0
        idea["trend_relevance"] = trend
        idea["trend_reason"] = str(s.get("reason", ""))[:120]
    ideas.sort(key=lambda x: x.get("trend_relevance", 0) * 0.6
               + float(x.get("save_worthiness", 0)) * 2 * 0.4, reverse=True)
    return ideas


def generate_idea(cfg: dict, _retry: bool = True) -> dict:
    ch = cfg["channel"]
    tcfg = ch.get("trends", {})
    seeds = get_trends(cfg)
    if not seeds:
        log("no trend seeds available — falling back to evergreen niche knowledge", "warn")
    notes = strategy_notes(ch["key"])
    lang = cfg.get("language", {}).get("code", "English")
    prompt = load_prompt("ideation").format(
        niche=ch["niche"],
        name=ch["name"],
        audience=ch["audience"],
        angle=ch["angle"],
        language=lang,
        trends="\n".join(f"- {s}" for s in seeds) or "(none — use your niche expertise)",
        recent_topics="\n".join(f"- {t}" for t in recent_topics(ch["key"])) or "(none yet)",
        creator_notes=notes or "(none provided)",
        n=6,
    )
    data = extract_json(complete(prompt, cfg, max_tokens=1200))
    ideas = data.get("ideas", [])
    if not ideas:
        raise RuntimeError(
            f"Ideation returned no ideas. Keys present: {sorted(data)}"
        )
    for it in ideas:
        try:
            it["save_worthiness"] = int(it.get("save_worthiness") or 0)
        except (TypeError, ValueError):
            it["save_worthiness"] = 0

    if seeds and len(ideas) > 1:
        ideas = _rerank_by_trend(ideas, seeds, cfg)

    strict = (tcfg.get("mode", "mixed") == "strict") and bool(seeds)
    if strict:
        anchored = [i for i in ideas if _anchor_match(i.get("trend_anchor", ""), seeds)]
        if not anchored and _retry:
            log("strict mode: no trend-anchored idea — regenerating once", "warn")
            return generate_idea(cfg, _retry=False)
        best = anchored[0] if anchored else ideas[0]
        if not anchored:
            log("strict mode: still unanchored after retry — using best-effort idea", "warn")
    else:
        best = ideas[0]

    anchor = _anchor_match(best.get("trend_anchor", ""), seeds) or ""
    trend = best.get("trend_relevance")
    extra = f" · trend {trend:.0f}/10" if isinstance(trend, (int, float)) else ""
    if anchor:
        extra += f" · anchor: \"{anchor[:70]}\""
    log(f"idea: \"{best['title']}\" (save-worthiness {best.get('save_worthiness', '?')}/5{extra})", "ok")
    return best
