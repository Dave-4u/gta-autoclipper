"""Header + VO writing via Google Gemini, with template fallback."""
from __future__ import annotations

import re
from typing import Any

_BAD_GRAMMAR = [
    re.compile(r"\b\w+s\s+hits\b", re.I),
    re.compile(r"\bgonna\s+\w+", re.I),
    re.compile(r"\b(it|he|she|they)'?s\s+hits\b", re.I),
    re.compile(r"\bwait…?\s+did you catch \w+ reacts\b", re.I),
]


_DESCRIPTIVE_VO = re.compile(
    r"(?i)\b(fierce|melee|combat|visual effects?|action-packed|sequence features|"
    r"bloodlust|fatal rhythm|flashy combat|gameplay footage|cinematic sequence|"
    r"is shocked watching|watching the trailer|reacts with extreme|intense action sequence|"
    r"features? (an? |the )?(intense|fiery|flashy)|narrat)\b"
)


def _vo_is_hype(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if _DESCRIPTIVE_VO.search(t):
        return False
    # Narrator "Name is/was adjective ..." without conversational punch
    if re.match(r"(?i)^[A-Z][\w']+(?:\s+[A-Z][\w']+)?\s+is\s+\w+ing\b", t) and not re.search(r"[!?]", t):
        return False
    # Too many abstract nouns / no punch
    if len(t.split()) >= 7 and not re.search(r"[!?]|(?i)\b(no way|look|bro|she|he|they|cooked|insane|real)\b", t):
        return False
    return True


def grammar_ok(text: str) -> bool:

    t = (text or "").strip()
    if not t:
        return False
    if any(p.search(t) for p in _BAD_GRAMMAR):
        return False
    if re.search(r"\b(gonna|gotta|wanna|yeah)\b.*\bhits\b", t, re.I):
        return False
    if len(t.split()) > 14:
        return False
    if re.search(r"\b(and|or|the|a|to|for|with)\.?$", t, re.I):
        return False
    if len(re.findall(r"[A-Za-z]+", t)) < 2:
        return False
    return True


def _fit_six_words(text: str) -> str:
    return " ".join(re.findall(r"[A-Za-z0-9']+", text or "")[:6])


def _overlap(a: str, b: str) -> float:
    ta = {w for w in re.findall(r"[a-z0-9]+", (a or "").lower()) if len(w) > 2}
    tb = {w for w in re.findall(r"[a-z0-9]+", (b or "").lower()) if len(w) > 2}
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(1, len(ta))


def _clean_title_header(title: str) -> str:
    """Readable header from a YouTube title — keep names, drop noise tags."""
    t = title or ""
    t = re.sub(r"[\[\]\(\)【】「」『』]", " ", t)
    t = re.sub(r"[|#].*$", " ", t)  # drop trailing hashtags / pipe suffixes later carefully
    # Keep left side of first pipe / emdash if it looks like the real title
    t = re.split(r"\s*[|–—]\s*", t, maxsplit=1)[0]
    t = re.sub(
        r"(?i)\b("
        r"official\s+(?:teaser\s+)?(?:trailer|video)|teaser\s+trailer|launch\s+trailer|"
        r"4k|8k|hdr|60fps?|1080p|2160p|hd|full\s+fight|amv|edit|shorts?|tiktok|"
        r"compilation|reaction|reacts|free\s+pf|visuals?"
        r")\b",
        " ",
        t,
    )
    t = re.sub(r"\s+", " ", t).strip(" -_:.")
    # Prefer "X vs Y" intact
    mvs = re.search(
        r"(?i)\b([A-Za-z][A-Za-z0-9' ]{1,24}?)\s+vs\.?\s+([A-Za-z][A-Za-z0-9' ]{1,24})",
        title or "",
    )
    if mvs:
        return _fit_six_words(f"{mvs.group(1).strip().title()} vs {mvs.group(2).strip().title()}")
    if not t or len(t.split()) < 1:
        return ""
    return _fit_six_words(t)


def _template_fallback(*, niche_id, title, channel, vlm_desc, event, entities) -> dict[str, str]:
    from .entities import detect_reactor, NEUTRAL_HEADERS
    reactor = detect_reactor(title, channel)
    desc_l = (vlm_desc or "").lower()
    header = ""
    mvs = re.search(r"(?i)\b([A-Za-z][A-Za-z ]{1,20}?)\s+vs\.?\s+([A-Za-z][A-Za-z ]{1,20})", title or "")
    title_hdr = _clean_title_header(title or "")
    # Reject mangled event fragments like "Grand trailer", "Turning trailer", "DON'T clutch"
    bad_event = bool(
        event and re.search(
            r"(?i)^(grand|turning|learn|boxtoxtv|fight trill|oga sabinus)?\s*"
            r"(trailer|teaser|clutch|moment)$",
            (event or "").strip(),
        )
    ) or bool(event and re.search(r"(?i)\b(trailer|teaser|clutch)\b$", (event or "").strip()) and len((event or "").split()) <= 2)
    if mvs:
        header = _fit_six_words(f"{mvs.group(1).strip().title()} vs {mvs.group(2).strip().title()}")
    elif "tengen" in desc_l:
        header = "Tengen vs Gyutaro" if "gyutaro" in desc_l else "Tengen fights"
    elif title_hdr and len(title_hdr.split()) >= 2:
        header = title_hdr
    elif reactor:
        header = _fit_six_words(f"{reactor.split()[0]} loses it")
    elif event and (not bad_event) and event.lower() not in {"this moment", "that reaction", "moment", "trailer", "clutch"}:
        header = _fit_six_words(event)
    elif title_hdr:
        header = title_hdr
    else:
        header = _fit_six_words(NEUTRAL_HEADERS[0])
    vo = ""
    if any(k in desc_l for k in ("laugh", "shock", "excited")):
        vo = "Look at that face."
    elif any(k in desc_l for k in ("fight", "blade", "slash")):
        vo = "Blades everywhere."
    return {"header": header, "vo": vo, "via": "fallback", "raw": ""}

def write_header_vo(
    *, niche_id: str, title: str, channel: str, vlm_desc: str, transcript: str,
    event: str, entities: Any = None, cfg: dict[str, Any] | None = None,
    emotion: str = "",
) -> dict[str, str]:
    from .entities import validate_copy, extract_entities, detect_reactor
    from .gemini_client import generate, has_gemini

    es = entities or extract_entities(title=title, channel=channel, transcript=transcript, niche_id=niche_id)
    reactor = detect_reactor(title, channel)
    if not has_gemini(cfg):
        print("[llm] gemini skipped: no API key (set GEMINI_API_KEY env, .env, or gemini.api_key)")
        return _template_fallback(niche_id=niche_id, title=title, channel=channel,
                                  vlm_desc=vlm_desc, event=event, entities=es)

    emo = (emotion or "").strip().lower()
    prompt = (
        f"Niche: {niche_id}\nTitle: {title[:140]}\nChannel: {channel}\n"
        f"Reactor (only if title says X reacts): {reactor or 'unknown'}\n"
        f"Event hint: {event}\n"
        f"VISIBLE emotion label (trusted): {emo or 'unknown'}\n"
        f"Scene description from THIS clip's frames (trusted): {vlm_desc[:500]}\n"
        f"Transcript snippet (may be unrelated chatter — do NOT invent from it): {transcript[:180]}\n"
        f"Allowed names: {', '.join((es.streamers or [])[:3] + (es.characters or [])[:4]) or 'none'}\n\n"
        "Write exactly these lines:\n"
        "HEADER: <=6 words, catchy, factual, no ellipsis, no 'WAIT FOR', never truncate mid-word\n"
        "VO: <=8 words. Must sound like a NATURAL spoken hype line a creator would say on TikTok "
        "(punchy, conversational). Ground it ONLY in the visible scene/emotion.\n"
        "GOOD VO examples: 'No way that is real!', 'Look at his face!', 'Blades everywhere!', 'She is cooked.'\n"
        "BANNED descriptive/narrator style (never write like this): "
        "'Fierce melee combat with fiery visual effects', 'Flashy bloodlust meets a fatal rhythm', "
        "'Unstoppable flashy combat', 'The action-packed sequence features…', "
        "or any clinical description of what the footage contains.\n"
        "If emotion is blank/talking/none and nothing punchy is visible: VO: SKIP\n"
        "SAY: phonetic hints for proper nouns (e.g. Gyutaro=Gyoo-tah-roh), or SAY: none\n"
        "Use only names in Scene description or Reactor. Perfect grammar. No hashtags.\n"
        "Example:\nHEADER: Tengen vs Gyutaro\nVO: Blades everywhere!\nSAY: Gyutaro=Gyoo-tah-roh, Tengen=Ten-gen\n"
    )
    try:
        raw = generate(prompt=prompt, images=None, cfg=cfg)
    except Exception as e:
        print(f"[llm/gemini] write failed: {e}")
        return _template_fallback(niche_id=niche_id, title=title, channel=channel,
                                  vlm_desc=vlm_desc, event=event, entities=es)

    header = vo = say = ""
    for ln in (raw or "").splitlines():
        s = ln.strip().strip('"')
        low = s.lower()
        if low.startswith("header:"):
            header = s.split(":", 1)[1].strip().strip('"')
        elif low.startswith("vo:"):
            vo = s.split(":", 1)[1].strip().strip('"')
        elif low.startswith("say:"):
            say = s.split(":", 1)[1].strip().strip('"')
    header = _fit_six_words(header)
    if vo.upper() == "SKIP" or vo.lower() in {"none", "n/a", "-"}:
        vo = ""
    vo = re.sub(r"\s+", " ", vo).strip()
    # Block reaction-hallucination when emotion is not strong
    _hype = re.compile(r"(?i)\b(losing (his|her|their) mind|absolutely hilarious|goes crazy|meltdown|broke|cooked|unhinged)\b")
    if emo not in {"laughing", "shocked", "excited"} and vo and _hype.search(vo):
        vo = ""
    if emo in {"blank", "talking", "none"} and vo and re.search(r"(?i)\b(react|face|laugh|shock|scream)\b", vo):
        # Force neutral: keep only if clearly factual about footage
        if not re.search(r"(?i)\b(trailer|footage|game|fight|blade|scene|shows?)\b", vo):
            vo = ""
    ok_h, _ = validate_copy(header, es) if header else (False, "empty")
    if (not header) or (not ok_h) or (not grammar_ok(header)):
        fb = _template_fallback(niche_id=niche_id, title=title, channel=channel,
                                vlm_desc=vlm_desc, event=event, entities=es)
        fb["raw"] = (raw or "")[:300]
        return fb
    if vo and (not grammar_ok(vo) or _overlap(header, vo) >= 0.5):
        try:
            retry = generate(
                prompt=(f"Scene: {vlm_desc[:300]}\nHeader: {header}\n"
                        "Write ONE punchy VO line (<=8 words), perfect grammar, "
                        "does not restate the header. Or SKIP.\nVO:"),
                cfg=cfg,
            )
            m = re.search(r"(?i)(?:VO:)?\s*(.+)", (retry or "").strip())
            cand = (m.group(1) if m else "").strip().strip('"')
            vo = cand if cand.upper() != "SKIP" and grammar_ok(cand) and _overlap(header, cand) < 0.5 else ""
        except Exception:
            vo = ""
    if vo:
        ok_v, _ = validate_copy(vo, es)
        if not ok_v:
            vo = ""
    if vo and not _vo_is_hype(vo):
        vo = ""
    if vo and not vo.endswith((".", "!", "?")):
        vo += "!"
    return {"header": header, "vo": vo, "via": "gemini", "raw": (raw or "")[:300], "say": say, "emotion": emo}
