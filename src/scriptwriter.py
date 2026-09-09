"""Stage 2 — turn an idea into a tight, retention-optimized voiceover script."""
from __future__ import annotations

import json

from .config import load_prompt
from .knowledge import strategy_notes, style_examples
from .llm import complete
from .utils import extract_json, log


def _enrich_cues(script: dict, idea: dict) -> dict:
    """Enrich each visual_cue with the topic/keyword so the scraper searches
    for NICHE-SPECIFIC B-roll (e.g. 'GTA 6 gameplay trailer' not just
    'gameplay trailer'). Without this, generic cues return stock footage that
    has nothing to do with the channel's topic.

    Also ensures cues are specific and concrete — vague cues like 'person talking'
    or 'city skyline' are avoided because they return off-topic clips with people
    or generic footage unrelated to the script.

    IMPORTANT: Each cue should be UNIQUE and reflect the specific content of its beat.
    If all beats have the same cue, the video will have identical B-roll throughout."""
    topic = (idea.get("primary_keyword") or idea.get("title") or "").strip()
    if not topic:
        return script
    topic_l = topic.lower()
    # Generic/vague terms that lead to off-topic clips (people, random stock)
    GENERIC_TERMS = {
        "person", "people", "man", "woman", "guy", "girl", "boy", "kid",
        "talking", "speech", "interview", "portrait", "face", "headshot",
        "random", "various", "miscellaneous", "general", "stock",
    }
    # Extract key nouns from beat text to make cues diverse
    def _extract_key_visual_word(beat_text: str) -> str:
        """Extract a visual keyword from beat text for cue diversity."""
        # Common visual nouns that work well for B-roll
        visual_nouns = [
            "manhã", "sol", "nascer", "acordar", "despertar", "energia",
            "treino", "exercício", "academia", "corrida", "movimento",
            "café", "bebida", "alimentação", "comida", "saúde",
            "trabalho", "escritório", "computador", "foco", "concentração",
            "noite", "lua", "dormir", "sono", "descanso",
            "dinheiro", "investimento", "riqueza", "sucesso",
            "mente", "cérebro", "pensamento", "ideia", "estratégia",
            "tempo", "relógio", "calendário", "agenda", "rotina",
            "natureza", "paisagem", "montagem", "oceano", "floresta",
            "cidade", "rua", "trânsito", "carro", "viagem",
        ]
        text_lower = beat_text.lower()
        for noun in visual_nouns:
            if noun in text_lower:
                return noun
        # Fallback: return a hash-based word from the text
        words = [w for w in beat_text.split() if len(w) > 4]
        if words:
            return words[hash(beat_text) % len(words)]
        return ""

    for i, b in enumerate(script.get("beats", [])):
        cue = (b.get("visual_cue") or "").strip()
        beat_text = b.get("text", "")
        if not cue:
            continue
        # Always prepend the topic to ensure niche-specific results
        if topic_l not in cue.lower():
            cue = f"{topic} {cue}"
        # Remove generic terms that would lead to off-topic clips
        cue_words = cue.split()
        filtered_words = [w for w in cue_words if w.lower() not in GENERIC_TERMS]
        if filtered_words:
            cue = " ".join(filtered_words)
        # Ensure the cue still has the topic after filtering
        if topic_l not in cue.lower():
            cue = f"{topic} {cue}"
        # ADD DIVERSITY: incorporate beat-specific visual word if cue is too similar to topic
        if cue.lower().count(topic_l) > 1 or cue == topic:
            visual_word = _extract_key_visual_word(beat_text)
            if visual_word and visual_word.lower() not in cue.lower():
                cue = f"{topic} {visual_word}"
        b["visual_cue"] = cue
    return script


def write_script(cfg: dict, idea: dict) -> dict:
    ch = cfg["channel"]
    target = cfg["video"]["target_seconds"]
    lang = cfg.get("language", {}).get("code", "English")
    visuals_lang = cfg.get("language", {}).get("visuals", "en")
    # ~2.4 spoken words/sec for English; Portuguese reads a touch slower (~2.2).
    lang_l = lang.lower()
    is_pt = "pt" in lang_l or "portug" in lang_l
    wps = 2.2 if is_pt else 2.4
    word_budget = int(target * wps)      # lower bound
    word_budget_max = int(target * (wps + 0.6))  # upper bound for a brisk read

    disclaimer_line = ""
    dkey = ch.get("inject_disclaimer")
    if dkey:
        text = cfg["compliance"]["disclaimers"].get(dkey, "")
        if text:
            disclaimer_line = f'- End the script with this exact disclaimer: "{text}"'

    voice_ref = style_examples(ch["key"])
    notes = strategy_notes(ch["key"])
    prompt = load_prompt("script").format(
        niche=ch["niche"], name=ch["name"], tone=ch["tone"], audience=ch["audience"],
        language=lang, visual_language=visuals_lang,
        title=idea["title"], concept=idea["concept"], hook_angle=idea.get("hook_angle", ""),
        primary_keyword=idea.get("primary_keyword", idea["title"]),
        search_question=idea.get("search_question", ""),
        style_reference=voice_ref or "(none provided — use the tone above)",
        creator_notes=notes or "(none provided)",
        target_seconds=target, word_budget=word_budget, word_budget_max=word_budget_max,
        disclaimer_line=disclaimer_line,
    )

    script = extract_json(complete(prompt, cfg, max_tokens=1500))
    script = _enrich_cues(script, idea)
    if not script.get("full_script"):
        raise RuntimeError(
            f"Scriptwriter returned no full_script. Keys present: {sorted(script)}"
        )

    # Small free models tend to under-write. Expand the draft toward the budget
    # (models hit a target far better when expanding existing text than writing cold).
    min_words = int(word_budget * 0.85)
    for _ in range(2):
        wc = len(script["full_script"].split())
        if wc >= min_words:
            break
        log(f"script short ({wc}w) — expanding toward {word_budget}w", "warn")
        expand = (
            f"This voiceover script is too short at {wc} words. Rewrite it to be "
            f"{word_budget}-{word_budget_max} words by deepening each beat with concrete, "
            f"vivid detail and complete spoken sentences. Keep the hook and the punchy tone. "
            f"Return the SAME JSON shape.\n\nCurrent script JSON:\n{json.dumps(script)}"
        )
        expanded = extract_json(complete(expand, cfg, max_tokens=1500))
        if expanded.get("full_script"):
            script = expanded
            script = _enrich_cues(script, idea)

    wc = len(script["full_script"].split())
    # Language guard: small reasoning models (gpt-oss etc.) sometimes drift to
    # English mid-script even when asked for pt-BR. Detect via stopword ratio
    # and rewrite once. Skipped when English is the target language.
    if not lang_l.startswith(("en", "eng")) and _is_mostly_english(script["full_script"]):
        log("script drifted to English — rewriting in target language", "warn")
        fix = (
            f"Rewrite this voiceover script ENTIRELY in {lang}. Every sentence must be "
            f"native, fluent {lang} — keep the same JSON shape, the same hook/beats/closer "
            f"structure and the punchy tone. Do NOT keep any English sentences. "
            f"Return ONLY the corrected JSON.\n\nCurrent script JSON:\n{json.dumps(script)}"
        )
        fixed = extract_json(complete(fix, cfg, max_tokens=1500))
        if fixed.get("full_script"):
            script = fixed

    wc = len(script["full_script"].split())
    log(f"script: hook + {len(script.get('beats', []))} beats, {wc} words", "ok")
    return script


_PT_STOP = {
    "que", "de", "não", "voce", "você", "para", "com", "uma", "seu", "sua", "mais",
    "isso", "seus", "suas", "como", "por", "os", "as", "é", "do", "da", "em", "no",
    "na", "ao", "aos", "se", "cada", "pode", "podem", "até", "muito", "muita", "sem",
    "mas", "ou", "então", "está", "estão", "tudo", "todo", "toda", "aqui", "ser",
    "tem", "neste", "nesse", "esse", "essa", "isso", "quem", "qual", "quanto",
}
_EN_STOP = {
    "the", "you", "your", "yours", "and", "for", "with", "that", "this", "these",
    "those", "of", "to", "in", "on", "it", "its", "is", "are", "was", "were", "by",
    "will", "can", "could", "would", "don't", "doesn't", "each", "next", "they",
    "them", "their", "how", "what", "when", "where", "why", "from", "have", "has",
    "get", "got", "just", "like", "make", "made", "there", "here", "about",
}


def _is_mostly_english(text: str) -> bool:
    """Heuristic: count PT vs EN stopword hits; English wins only if clearly ahead."""
    words = [w.strip(".,!?;:—–()\"'").lower() for w in text.split()]
    if len(words) < 8:
        return False
    pt = sum(1 for w in words if w in _PT_STOP)
    en = sum(1 for w in words if w in _EN_STOP)
    return en >= 5 and en > pt * 2
