"""Strict anime vs western-animation (cartoon) vs other classifier.

Used so the `anime` and `animation` niches never mix. Official vs fan-made is orthogonal.
"""

from __future__ import annotations

import re
from typing import Any

# --- Franchise / keyword banks ------------------------------------------------

ANIME_FRANCHISES = [
    "naruto", "boruto", "one piece", "demon slayer", "kimetsu", "jujutsu kaisen", "jjk",
    "attack on titan", "shingeki", "bleach", "dragon ball", "dbz", "dbs", "chainsaw man",
    "solo leveling", "my hero academia", "boku no hero", "tokyo ghoul", "evangelion",
    "spy x family", "spy×family", "frieren", "haikyuu", "hunter x hunter", "hxh",
    "code geass", "fullmetal", "fma", "death note", "jojo", "jojo's", "vinland saga",
    "mob psycho", "one punch", "one-punch", "berserk", "cowboy bebop", "gurren",
    "steins;gate", "re:zero", "konosuba", "overlord", "sword art online", "sao ",
    "fairy tail", "black clover", "blue lock", "dandadan", "kaiju no. 8", "kaiju 8",
    "oshi no ko", "bocchi", "chainsaw", "hell's paradise", "jigokuraku",
    "studio ghibli", "ghibli", "makoto shinkai", "your name", "suzume", "weathering with you",
    "princess mononoke", "spirited away", "howl's moving", "totoro",
    "pokémon", "pokemon", "digimon", "yugioh", "yu-gi-oh", "sailor moon",
    "neon genesis", "ghost in the shell", "akira", "devilman", "pluto",
    "wind breaker", "sakamoto days", "mashle", "dr. stone", "dr stone",
    "fire force", "promare", "cyberpunk edgerunners", "edgerunners",
    "arcane",  # NOTE: Arcane is western — listed in WESTERN below; do NOT put here
]

# Remove arcane if I accidentally left it - Arcane is western (Fortiche/Riot)

# Common character / fight labels (help fan-animation titles that omit the show name)
ANIME_CHAR_HINTS = [
    "itachi", "obito", "sasuke", "madara", "kakashi", "goku", "vegeta", "gojo", "sukuna",
    "tanjiro", "nezuko", "zenitsu", "luffy", "zoro", "sanji", "nami", "ichigo", "aizen",
    "eren yeager", "mikasa", "levi ackerman", "saitama", "genos", "deku", "bakugo",
    "jinwoo", "sung jinwoo", "thorfinn", "askeladd", "yuji itadori", "megumi",
    "tanjirou", "inosuke", "rengoku", "akaza", "muzan", "all might", "shigaraki",
]
ANIME_FRANCHISES = ANIME_FRANCHISES + ANIME_CHAR_HINTS

ANIME_FRANCHISES = [x for x in ANIME_FRANCHISES if x != "arcane"]

ANIME_MARKERS = [
    "anime", "manga", "shonen", "shounen", "seinen", "shoujo", "josei", "otaku", "waifu",
    "sakuga", "amv", "ova", "ona", "light novel", "seiyuu", "isekai", "mecha",
    "crunchyroll", "funimation", "aniplex", "mappa", "ufotable", "toei animation",
    "bones studio", "wit studio", "kyoto animation", "madhouse", "a-1 pictures",
    "muse asia", "ani-one", "anione", "gogoanime", "donghua", "manhwa", "manhua",
    "japanese animation", "nippon",
]

WESTERN_FRANCHISES = [
    "disney", "pixar", "dreamworks", "dream works", "illumination", "sony pictures animation",
    "cartoon network", "nickelodeon", "nicktoons", "disney channel", "disney+",
    "adventure time", "steven universe", "regular show", "the amazing world of gumball",
    "gumball", "gravity falls", "owl house", "amphibia", "phineas and ferb",
    "spongebob", "sponge bob", "avatar the last airbender", "legend of korra",
    "teen titans", "ben 10", "powerpuff", "dexter's lab", "foster's home",
    "rick and morty", "bojack", "arcane", "invincible", "the boys presents",
    "diabolical", "hazbin", "helluva", "smiling friends", "primal",
    "spider-verse", "spider verse", "into the spider", "across the spider",
    "frozen", "moana", "encanto", "tangled", "big hero 6", "zootopia", 
    "toy story", "incredibles", "inside out", "turning red", "elemental", "coco",
    "finding nemo", "finding dory", "wall-e",   "ratatouille",
    "shrek", "how to train your dragon", "kung fu panda", "trolls", "puss in boots",
    "despicable me", "minions",  "the bad guys", "wild robot",
    "family guy", "simpsons", "south park", "american dad", "futurama",
    "bluey", "peppar", "peppa pig", "paw patrol", "miraculous",
    "castlevania", "blood of zeus", "scavengers reign", "pantheon",
    "love death robots", "love, death", "klaus", "wolfwalkers", "song of the sea",
    "mitchells vs the machines",     ]

WESTERN_MARKERS = [
    "cartoon", "cartoons", "western animation", "american animation", "2d cartoon",
    "3d cartoon", "indie animation", "indie animatic", "animatic", "toon",
    "pixar", "dreamworks", "illumination", "disney animation", "walt disney",
    "cartoon network", "adult swim", "nickelodeon", "nick animation",
    "fortiche", "riot animation",
]

# Words that look like "animation" but often appear in anime titles — NOT enough alone for western
WEAK_ANIMATION_WORDS = ["animation", "animated", "animator", "animating"]


def _blob(title: str = "", channel: str = "", description: str = "", tags: list[str] | None = None) -> str:
    return " ".join([
        title or "", channel or "", (description or "")[:600],
        " ".join(map(str, tags or [])),
    ]).lower()


def _hits(blob: str, words: list[str]) -> list[str]:
    found = []
    for w in words:
        w = (w or "").strip().lower()
        if not w:
            continue
        # Always use word-ish boundaries so "sing" does not match inside "animation"
        if re.search(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", blob):
            found.append(w)
    return found


def classify_style_kind(
    *,
    title: str = "",
    channel: str = "",
    description: str = "",
    tags: list[str] | None = None,
) -> tuple[str, str, float]:
    """Return (kind, reason, confidence) where kind is anime|animation|other.

    Rules of thumb:
    - Explicit anime franchise/marker → anime (even if title also says 'animation')
    - Explicit western franchise/marker → animation
    - Bare 'animation/animated' without western OR anime cues → other (ambiguous)
    """
    blob = _blob(title, channel, description, tags)
    a_fr = _hits(blob, ANIME_FRANCHISES)
    a_mk = _hits(blob, ANIME_MARKERS)
    w_fr = _hits(blob, WESTERN_FRANCHISES)
    w_mk = _hits(blob, WESTERN_MARKERS)
    weak = _hits(blob, WEAK_ANIMATION_WORDS)

    anime_score = 3.0 * len(a_fr) + 2.0 * len(a_mk)
    west_score = 3.0 * len(w_fr) + 2.0 * len(w_mk)
    # Weak words only help western if no anime signal
    if weak and not (a_fr or a_mk):
        west_score += 0.4 * len(weak)

    # Special: "anime" token is decisive against western
    if "anime" in blob or "manga" in blob:
        anime_score += 4.0
    if any(x in blob for x in ("amv", "sakuga", "crunchyroll", "donghua", "manhwa")):
        anime_score += 2.5

    if anime_score >= 2.0 and anime_score > west_score + 0.5:
        why = (a_fr or a_mk or ["anime_token"])[0]
        return "anime", f"anime:{why}", min(0.99, 0.55 + 0.08 * anime_score)
    if west_score >= 2.0 and west_score > anime_score + 0.5:
        why = (w_fr or w_mk or ["western"])[0]
        return "animation", f"animation:{why}", min(0.99, 0.55 + 0.08 * west_score)

    if anime_score > 0 and west_score > 0:
        return "other", f"ambiguous:anime={anime_score:.1f},west={west_score:.1f}", 0.35
    if anime_score > 0:
        return "anime", f"weak_anime:{(a_fr or a_mk)[0]}", 0.55
    if west_score > 0:
        return "animation", f"weak_animation:{(w_fr or w_mk or weak)[0]}", 0.55
    return "other", "no_style_signal", 0.2


def fits_niche(niche_id: str, kind: str) -> bool:
    n = (niche_id or "").lower()
    if n == "anime":
        return kind == "anime"
    if n in {"animation", "cartoon", "cartoons"}:
        return kind == "animation"
    return True  # other niches don't use this gate


def classify_candidates(
    cands: list[Any],
    *,
    niche_id: str,
    cfg: dict[str, Any] | None = None,
    log=None,
) -> list[Any]:
    """Annotate + filter candidates for anime/animation niches. Logs [niche-check]."""
    def _log(msg: str) -> None:
        print(msg, flush=True)
        if log:
            log(msg)

    niche = (niche_id or "").lower()
    if niche not in {"anime", "animation", "cartoon", "cartoons"}:
        return list(cands)

    kept: list[Any] = []
    unsure: list[Any] = []
    for c in cands:
        title = getattr(c, "title", "") or ""
        extra = dict(getattr(c, "extra", None) or {})
        kind, reason, conf = classify_style_kind(
            title=title,
            channel=str(extra.get("channel") or extra.get("uploader") or ""),
            description=str(extra.get("description") or ""),
            tags=list(extra.get("tags") or []),
        )
        extra["style_kind"] = kind
        extra["style_reason"] = reason
        extra["style_conf"] = conf
        c.extra = extra
        _log(f"[niche-check] kind={kind:<10} conf={conf:.2f} reason={reason} | {title[:70]}")
        if fits_niche(niche, kind) and conf >= 0.5:
            kept.append(c)
        elif kind == "other" or conf < 0.5:
            unsure.append(c)
        else:
            _log(f"[niche-check] REJECT wrong-kind for niche={niche}: {title[:70]}")

    if unsure:
        _gemini_batch(unsure, niche_id=niche, cfg=cfg, log=_log)
        for c in unsure:
            kind = (c.extra or {}).get("style_kind") or "other"
            conf = float((c.extra or {}).get("style_conf") or 0)
            if fits_niche(niche, kind):
                kept.append(c)
            elif kind == "other" and conf < 0.5:
                # No decisive signal either way — keep if niche_relevance already passed
                # (must keywords matched). Final vision gate can still drop.
                _log(f"[niche-check] KEEP ambiguous for vision gate: {(c.title or '')[:70]}")
                kept.append(c)
            else:
                _log(f"[niche-check] REJECT after review for niche={niche}: {(c.title or '')[:70]}")

    return kept


def _gemini_batch(cands: list[Any], *, niche_id: str, cfg: dict[str, Any] | None, log) -> None:
    try:
        from .gemini_client import generate, has_gemini
        if not has_gemini(cfg):
            return
        lines = "\n".join(
            f"{getattr(c,'id','')}\t{(c.title or '')[:90]}\t{str((c.extra or {}).get('channel') or '')[:40]}"
            for c in cands[:30]
        )
        raw = generate(prompt=(
            "Classify each YouTube video's ON-SCREEN animation style as exactly one of:\n"
            "anime = Japanese/Korean/Chinese anime/donghua/manhwa adaptations (Naruto, One Piece, JJK, "
            "Demon Slayer, Ghibli, Solo Leveling, etc.)\n"
            "animation = Western cartoons / Disney / Pixar / DreamWorks / CN / Nick / Arcane / Invincible / "
            "Hazbin / indie western animatics — NOT anime\n"
            "other = live-action, game CG that isn't a cartoon series, unclear\n"
            "IMPORTANT: If the title says 'animation' but names an anime franchise, answer anime.\n"
            "If it names Disney/Pixar/Adventure Time/Arcane/etc., answer animation.\n"
            "Reply ONLY JSON {\"<id>\":\"anime\"|\"animation\"|\"other\"}.\n" + lines
        ), cfg=cfg, max_retries=2)
        import json as _j
        m = re.search(r"\{.*\}", raw or "", re.S)
        data = _j.loads(m.group(0)) if m else {}
        for c in cands:
            v = str(data.get(getattr(c, "id", ""), "")).lower().strip()
            if v in {"anime", "animation", "other"}:
                c.extra = dict(c.extra or {})
                c.extra["style_kind"] = v
                c.extra["style_reason"] = f"gemini:{v}"
                c.extra["style_conf"] = 0.85
                log(f"[niche-check] gemini kind={v} | {(c.title or '')[:70]}")
    except Exception as e:
        log(f"[niche-check] gemini batch failed: {e}")


def vision_style_check(
    path,
    *,
    niche_id: str,
    cfg: dict[str, Any] | None = None,
    title: str = "",
    header: str = "",
    start: float | None = None,
    end: float | None = None,
) -> dict[str, Any]:
    """Frame-level check: does the clip look like anime vs western animation for this niche?"""
    from pathlib import Path
    from .scene_vlm import grab_frame_files
    from .gemini_client import generate, has_gemini

    niche = (niche_id or "").lower()
    if niche not in {"anime", "animation", "cartoon", "cartoons"}:
        return {"ok": True, "skipped": "not_style_niche"}
    if not has_gemini(cfg):
        return {"ok": True, "skipped": "no_key"}
    path = Path(path)
    try:
        import json as _j
        import subprocess
        pr = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
            capture_output=True, text=True, check=False,
        )
        file_dur = float((_j.loads(pr.stdout).get("format") or {}).get("duration") or 8)
    except Exception:
        file_dur = 8.0
    t0 = float(start) if start is not None else 0.0
    t1 = float(end) if end is not None else file_dur
    if t1 <= t0:
        t1 = t0 + max(2.0, file_dur * 0.1)
    span = max(0.5, t1 - t0)
    frames = grab_frame_files(path, [t0 + span * x for x in (0.2, 0.5, 0.75)])
    want = "anime (Japanese/Korean/Chinese anime look: anime faces, sakuga, manga style)" if niche == "anime" \
        else "western cartoon / Disney / Pixar / DreamWorks / CN / Arcane-style (NOT Japanese anime)"
    prompt = (
        f"Niche required: {want}.\n"
        f"Title: {title[:120]}\nHeader: {header[:80]!r}\n"
        "From the frames answer exactly:\n"
        "KIND: anime OR animation OR other\n"
        "MATCH: yes OR no  (yes only if KIND fits the required niche)\n"
        "FRANCHISE: <best guess show/franchise or unknown>\n"
        "REASON: <short>\n"
        "Rules: Demon Slayer/Naruto/JJK/Ghibli = anime. Disney/Pixar/Adventure Time/Arcane/Invincible = animation. "
        "MATCH=no if the frames are the wrong tradition."
    )
    try:
        raw = generate(prompt=prompt, images=frames, cfg=cfg, max_retries=2)
    except Exception as e:
        return {"ok": True, "skipped": f"error:{e}"}
    finally:
        try:
            from .scene_vlm import _cleanup
            _cleanup(frames)
        except Exception:
            pass
    kind = match = franchise = reason = ""
    for ln in (raw or "").splitlines():
        low = ln.strip().lower()
        if low.startswith("kind:"):
            kind = ln.split(":", 1)[1].strip().lower()
        elif low.startswith("match:"):
            match = ln.split(":", 1)[1].strip().lower()
        elif low.startswith("franchise:"):
            franchise = ln.split(":", 1)[1].strip()
        elif low.startswith("reason:"):
            reason = ln.split(":", 1)[1].strip()
    mapped = "anime" if kind.startswith("anime") else ("animation" if "anim" in kind else "other")
    ok = match.startswith("yes") and fits_niche(niche, mapped)
    if niche == "anime" and mapped != "anime":
        ok = False
    if niche in {"animation", "cartoon", "cartoons"} and mapped != "animation":
        ok = False
    return {
        "ok": ok,
        "kind": kind,
        "franchise": franchise,
        "reasons": [] if ok else [reason or f"style_mismatch:{kind}"],
        "raw": (raw or "")[:400],
    }
