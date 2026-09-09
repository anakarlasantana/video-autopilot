"""Smoke-test the multi-source scraper ranking (no pipeline needed).

Usage:
  python scripts/test_rank.py "GTA 6 trailer city chase"
  python scripts/test_rank.py --download "money city night"
"""
from __future__ import annotations

import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import channel_config
from src.rank import rank_candidates
from src.visuals import _download_candidate, _search_source, _validate_clip


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    do_download = "--download" in sys.argv
    query = args[0] if args else "city night cinematic"
    cfg = channel_config("money")
    sources = cfg["visuals"].get("scraper_sources", [])
    print(f"query: {query}\nsources: {sources}")
    cands = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(_search_source, s, query, 3, cfg): s for s in sources}
        for f in as_completed(futs, timeout=60):
            try:
                got = f.result() or []
                print(f"  {futs[f]}: {len(got)} candidates")
                cands += got
            except Exception as e:
                print(f"  {futs[f]}: FAILED {e}")
    if not cands:
        print("no candidates from any source")
        raise SystemExit(1)
    ranked = rank_candidates(cands, query, cfg, set())
    print("\ntop 5:")
    for c in ranked[:5]:
        print(f"  {c.score:6.1f} [{c.source}/{c.kind}] {c.title[:60]} | {c.url[:80]}")
    if do_download:
        dest = Path(tempfile.gettempdir()) / "rank_test_clip"
        for c in ranked:
            try:
                p = _download_candidate(c, dest, cfg)
                _validate_clip(p, cfg)
                print(f"\nwinner OK: {p} ({p.stat().st_size} bytes)")
                return
            except Exception as e:
                print(f"download {c.source} failed: {e}")
        raise SystemExit("all downloads failed")


if __name__ == "__main__":
    main()
