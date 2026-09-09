"""Shared helpers: logging, slugs, JSON parsing, run directories, history, ffprobe."""
from __future__ import annotations

import json
import re
import subprocess
import sys
import unicodedata
from datetime import date
from pathlib import Path

from .config import ROOT

HISTORY_FILE = ROOT / "output" / "history.json"

_ICONS = {"info": "•", "ok": "✓", "warn": "!", "err": "✗", "step": "▶"}


def log(msg: str, kind: str = "info") -> None:
    print(f"  {_ICONS.get(kind, '•')} {msg}", flush=True)


def banner(msg: str) -> None:
    print(f"\n\033[1m{msg}\033[0m", flush=True)


def die(msg: str) -> None:
    print(f"\033[31m✗ {msg}\033[0m", file=sys.stderr)
    raise SystemExit(1)


def slugify(text: str, max_len: int = 50) -> str:
    # Transliterate accents ("ação" → "acao", "você" → "voce") so non-ASCII
    # titles still produce clean, matching folder/file names.
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:max_len].strip("-") or "video"


def _repair_json(raw: str) -> str:
    """Escape literal control chars inside strings, drop trailing commas."""
    fixed = re.sub(r",\s*([}\]])", r"\1", raw)
    out: list[str] = []
    in_str = False
    esc = False
    for ch in fixed:
        if in_str:
            if esc:
                out.append(ch)
                esc = False
            elif ch == "\\":
                out.append(ch)
                esc = True
            elif ch == '"':
                out.append(ch)
                in_str = False
            elif ch == "\n":
                out.append("\\n")
            elif ch == "\r":
                out.append("\\r")
            elif ch == "\t":
                out.append("\\t")
            else:
                out.append(ch)
        else:
            out.append(ch)
            if ch == '"':
                in_str = True
    return "".join(out)


def extract_json(text: str) -> dict:
    """Pull the first JSON value (object or array) out of an LLM response.

    Handles ```json fences, raw control chars inside strings (gpt-oss habit),
    trailing commas, and trailing garbage after the first complete value
    ("Extra data" errors when models emit two objects back-to-back).
    """
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.MULTILINE).strip()
    spans: list[str] = []
    for opener, closer in (("{", "}"), ("[", "]")):
        i, j = t.find(opener), t.rfind(closer)
        if i != -1 and j > i:
            spans.append(t[i:j + 1])
    if not spans:
        raise ValueError(f"No JSON object found in model output:\n{t[:500]}")
    dec = json.JSONDecoder()
    # pass 1 — exact full parse (a complete array beats a partial first object)
    for raw in spans:
        for cand in (raw, _repair_json(raw)):
            try:
                return json.loads(cand)
            except json.JSONDecodeError:
                pass
    # pass 2 — tolerate trailing data after the first complete value
    for raw in spans:
        for cand in (raw, _repair_json(raw)):
            try:
                val, _ = dec.raw_decode(cand)
                return val
            except json.JSONDecodeError:
                continue
    return json.loads(spans[0], strict=False)  # last resort; raises if hopeless


def run_dir(output_dir: str, channel_key: str, slug: str) -> Path:
    d = ROOT / output_dir / date.today().isoformat() / channel_key / slug
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def ffprobe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def run_ffmpeg(args: list[str]) -> None:
    """Run ffmpeg quietly; raise with stderr tail on failure."""
    proc = subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed:\n{proc.stderr[-1500:]}")


# ── topic history (avoid repeats + power the feedback loop) ──
def load_history() -> dict:
    if HISTORY_FILE.exists():
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    return {}


def recent_topics(channel_key: str, limit: int = 40) -> list[str]:
    return load_history().get(channel_key, {}).get("topics", [])[-limit:]


def record_topic(channel_key: str, title: str) -> None:
    h = load_history()
    h.setdefault(channel_key, {}).setdefault("topics", []).append(title)
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_FILE.write_text(json.dumps(h, indent=2, ensure_ascii=False), encoding="utf-8")
