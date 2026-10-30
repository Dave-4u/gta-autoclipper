"""Source grounding: detect franchise/show/game/streamer entities from metadata.

Franchise-specific slang (bankai, domain expansion, etc.) is ONLY allowed when
the matching franchise is detected in title/channel/tags/description/transcript.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

SAFE_SLANG = {
    "aura", "peak", "goat", "cinema", "insane", "crazy", "hype", "clutch",
    "chat", "streamer", "reaction", "trailer", "gameplay", "fight", "scene",
}

INTERNAL_SIGNAL_WORDS = {
    "volume spike", "the volume spike", "audio spike", "the cut", "the movement",
    "feel the movement", "feel that volume", "loud+cut", "react_peak",
    "face motion", "hype_speech", "audio_spike", "frame diff", "loudness",
    "scene_change", "keyword hit", "the spike", "that volume spike",
}

FRANCHISE_PACKS: dict[str, dict[str, Any]] = {
    "bleach": {
        "aliases": ["bleach", "ichigo", "aizen", "soul society", "zanpakuto", "thousand year blood war", "tybw"],
        "terms": ["bankai", "getsuga", "hollow", "shinigami", "zanpakuto"],
        "characters": ["ichigo", "aizen", "rukia", "renji", "byakuya"],
    },
    "jujutsu_kaisen": {
        "aliases": ["jujutsu kaisen", "jujutsu", "jjk", "gojo", "sukuna", "shibuya incident"],
        "terms": ["domain expansion", "cursed energy", "infinity"],
        "characters": ["gojo", "sukuna", "yuji", "megumi", "nobara"],
    },
    "naruto": {
        "aliases": ["naruto", "boruto", "sasuke", "akatsuki", "hokage"],
        "terms": ["rasengan", "chidori", "sharingan", "sage mode"],
        "characters": ["naruto", "sasuke", "sakura", "kakashi", "itachi"],
    },
    "one_piece": {
        "aliases": ["one piece", "luffy", "zoro", "gear 5", "gear five", "nika"],
        "terms": ["gear 5", "haki", "devil fruit"],
        "characters": ["luffy", "zoro", "nami", "sanji", "shanks"],
    },
    "demon_slayer": {
        "aliases": ["demon slayer", "kimetsu", "tanjiro", "nezuko", "muzan", "hashira"],
        "terms": ["breathing style", "hinokami", "hashira"],
        "characters": ["tanjiro", "nezuko", "zenitsu", "inosuke", "rengoku"],
    },
    "attack_on_titan": {
        "aliases": ["attack on titan", "shingeki", "aot", "eren yeager", "mikasa", "levi ackerman"],
        "terms": ["founding titan", "survey corps"],
        "characters": ["eren", "mikasa", "levi", "armer"],
    },
    "fate": {
        "aliases": ["fate/", "fate stay", "fate/stay", "saber alter", "heaven's feel", "unlimited blade"],
        "terms": ["noble phantasm", "servant", "holy grail"],
        "characters": ["saber", "berserker", "archer", "rin", "shirou"],
    },
    "dragon_ball": {
        "aliases": ["dragon ball", "dbz", "dbs", "goku", "vegeta", "super saiyan"],
        "terms": ["super saiyan", "kamehameha", "ultra instinct"],
        "characters": ["goku", "vegeta", "gohan", "frieza"],
    },
    "gta6": {
        "aliases": ["gta 6", "gta6", "gta vi", "gtavi", "grand theft auto vi", "leonida"],
        "terms": ["wanted level", "heist", "vice city", "leonida"],
        "characters": ["lucia", "jason"],
    },
    "gta5": {
        "aliases": ["gta 5", "gta v", "gta5", "gtav", "los santos gta"],
        "terms": ["los santos", "wanted level"],
        "characters": ["michael", "franklin", "trevor"],
    },
}

LOCKED_TERMS: set[str] = set()
for _pack in FRANCHISE_PACKS.values():
    for t in _pack.get("terms") or []:
        LOCKED_TERMS.add(t.lower())
    for t in _pack.get("characters") or []:
        LOCKED_TERMS.add(t.lower())

NEUTRAL_HEADERS = [
    "Watch what happens next",
    "Did you catch this moment?",
    "Wait for this part",
    "This is the moment",
    "Don't blink here",
]
NEUTRAL_VO = [
    "Watch what happens next.",
    "Stay for this part.",
    "This is the beat that matters.",
    "Don't scroll past this.",
]

PUBLISHERS = {
    "youtube", "music", "vevo", "crunchyroll", "netflix", "disney", "hulu",
    "amazon prime", "prime video", "hbo", "max", "rockstar games", "sony",
    "microsoft", "xbox", "playstation", "nintendo", "ign", "gamespot",
}

STREAMER_ALIASES = [
    ("kai cenat", "Kai Cenat"),
    ("ishowspeed", "iShowSpeed"),
    ("xqc", "xQc"),
    ("asmongold", "Asmongold"),
    ("moistcr1tikal", "MoistCr1TiKaL"),
    ("penguinz0", "MoistCr1TiKaL"),
    ("caseoh", "CaseOh"),
    ("yourrage", "YourRAGE"),
]


@dataclass
class EntitySet:
    franchises: list[str] = field(default_factory=list)
    shows: list[str] = field(default_factory=list)
    characters: list[str] = field(default_factory=list)
    games: list[str] = field(default_factory=list)
    streamers: list[str] = field(default_factory=list)
    allowed_terms: set[str] = field(default_factory=set)
    event_phrases: list[str] = field(default_factory=list)
    confidence: float = 0.0
    raw_blob: str = ""


def _norm_blob(title: str, channel: str = "", description: str = "",
               tags: list[str] | None = None, transcript: str = "") -> str:
    parts = [title or "", channel or "", description or "", " ".join(tags or []), transcript or ""]
    return re.sub(r"\s+", " ", " ".join(parts)).lower()


def extract_entities(
    *,
    title: str = "",
    channel: str = "",
    description: str = "",
    tags: list[str] | None = None,
    transcript: str = "",
    niche_id: str = "",
) -> EntitySet:
    blob = _norm_blob(title, channel, description, tags, transcript)
    es = EntitySet(raw_blob=blob)
    detected: list[str] = []
    allowed: set[str] = set(SAFE_SLANG)

    for fid, pack in FRANCHISE_PACKS.items():
        if any(a in blob for a in pack["aliases"]):
            detected.append(fid)
            for t in pack.get("terms") or []:
                allowed.add(t.lower())
            for c in pack.get("characters") or []:
                allowed.add(c.lower())
                if c not in es.characters:
                    es.characters.append(c)
            label = fid.replace("_", " ")
            es.shows.append(label)
            if fid.startswith("gta"):
                es.games.append(label)

    # Only add character names to allowed when franchise detected — already done above.
    # Do NOT put characters on es from undetected packs.

    for alias, name in STREAMER_ALIASES:
        if alias in blob:
            if name not in es.streamers:
                es.streamers.append(name)
            allowed.add(name.lower())
            allowed.add(alias)

    ch = (channel or "").strip()
    ch_l = ch.lower()
    if (
        ch
        and len(ch) > 2
        and ch_l not in PUBLISHERS
        and not any(
            p in ch_l
            for p in ("games", "official", "trailer", "records", "entertainment",
                       "animation", "studio", "crunchyroll", "network")
        )
    ):
        if ch not in es.streamers:
            es.streamers.append(ch)
        allowed.add(ch_l)

    title_clean = re.sub(r"[|\[\]\(\)#]", " ", title or "")
    title_clean = re.sub(r"\s+", " ", title_clean).strip()
    for pat in (
        r"(?i)\b(fight|battle|vs\.?|reaction|trailer|teaser|gameplay|chase|heist|clutch|fail)\b",
        r"(?i)(final form|power.?up|plot twist|boss fight)",
    ):
        m = re.search(pat, title_clean)
        if m:
            es.event_phrases.append(m.group(0).lower())

    stop = {"this", "that", "with", "from", "have", "been", "were", "they", "just", "like", "really"}
    for w in re.findall(r"[A-Za-z']{4,}", transcript or ""):
        lw = w.lower()
        if lw not in stop:
            allowed.add(lw)

    es.franchises = detected
    es.allowed_terms = allowed

    conf = 0.0
    if detected:
        conf += 0.55
    if es.streamers:
        conf += 0.25
    if es.event_phrases:
        conf += 0.15
    if transcript and len(transcript.split()) >= 6:
        conf += 0.15
    es.confidence = min(1.0, conf)

    niche = (niche_id or "").lower()
    if niche == "gta6" and "gta6" not in detected and "gta5" not in detected:
        if any(x in blob for x in ("gta", "rockstar", "lucia", "vice")):
            detected.append("gta6")
            es.franchises = detected
            for t in FRANCHISE_PACKS["gta6"]["terms"]:
                allowed.add(t.lower())
            for c in FRANCHISE_PACKS["gta6"]["characters"]:
                allowed.add(c.lower())
            es.confidence = max(es.confidence, 0.5)

    return es


def grounded_vocab(es: EntitySet, niche_id: str = "") -> list[str]:
    out: list[str] = []
    for fid in es.franchises:
        pack = FRANCHISE_PACKS.get(fid) or {}
        out.extend(pack.get("terms") or [])
        out.extend(pack.get("characters") or [])
    out.extend(es.streamers[:3])
    out.extend(es.event_phrases[:3])
    niche = (niche_id or "").lower()
    neutrals = {
        "gta6": ["trailer", "gameplay", "chase"],
        "anime": ["fight", "scene", "animation"],
        "reactions": ["reaction", "face cam", "chat"],
        "gaming": ["clutch", "gameplay"],
        "funny": ["fail", "bit"],
        "movies": ["trailer", "scene"],
        "animation": ["animation", "scene"],
    }
    out.extend(neutrals.get(niche, ["moment"]))
    seen: set[str] = set()
    clean: list[str] = []
    for v in out:
        k = v.lower()
        if k in seen:
            continue
        seen.add(k)
        clean.append(v)
    return clean[:12]


def event_summary(es: EntitySet, title: str = "", transcript: str = "", reason: str = "") -> str:
    """Human-readable event — never analyzer jargon or whisper garbage."""
    t = (title or "").lower()
    # Prefer reaction/reacts before trailer so "X Reacts to Trailer" → reaction
    action = None
    for word in ("reacts", "reaction", "fight", "battle", "vs", "teaser", "trailer",
                 "chase", "heist", "clutch", "fail", "gameplay", "meltdown"):
        if re.search(rf"\b{re.escape(word)}\b", t) or (word == "reacts" and "reacts" in t):
            action = "reaction" if word in ("reacts", "reaction") else word
            break
    # substring fallback for reacts
    if action is None and "react" in t:
        action = "reaction"

    proper = re.findall(r"\b([A-Z][a-zA-Z0-9']{2,})\b", title or "")
    skip = {
        "Top", "The", "Official", "New", "Best", "Part", "Watch", "Full",
        "Episode", "Live", "React", "Reacts", "Trailer", "Teaser", "Shorts",
        "Streamers", "Streamer", "Movie", "Film", "Anime", "GTA", "VI",
    }
    proper = [x for x in proper if x not in skip]

    if action == "reaction":
        if es.streamers:
            name = es.streamers[0]
            if "reaction" in name.lower():
                return name  # channel already says Reaction
            return f"{name} reaction"
        if proper:
            return f"{proper[0]} reaction"
        return "streamer reaction"

    if action:
        if es.characters and action in ("fight", "battle", "vs", "moment"):
            return f"{es.characters[0].title()} {action}"
        if proper:
            return f"{proper[0]} {action}"
        if es.franchises:
            return f"{es.franchises[0].replace('_', ' ')} {action}"
        if es.streamers:
            return f"{es.streamers[0]} {action}"
        return action

    if es.event_phrases:
        return es.event_phrases[0]
    if es.characters:
        return f"{es.characters[0].title()} moment"
    if es.streamers:
        return f"{es.streamers[0]} moment"
    if es.franchises:
        return es.franchises[0].replace("_", " ") + " moment"
    if proper:
        return " ".join(proper[:3])
    return "this moment"


def contains_locked_ungrounded(text: str, es: EntitySet) -> list[str]:
    blob = (text or "").lower()
    bad: list[str] = []
    for term in sorted(LOCKED_TERMS, key=len, reverse=True):
        if len(term) < 4:
            continue
        if term in es.allowed_terms:
            continue
        pat = r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])"
        if re.search(pat, blob):
            bad.append(term)
    return bad


def contains_internal_signals(text: str) -> list[str]:
    blob = (text or "").lower()
    return [w for w in INTERNAL_SIGNAL_WORDS if w in blob]


def validate_copy(text: str, es: EntitySet) -> tuple[bool, str]:
    if not (text or "").strip():
        return False, "empty"
    bad_locked = contains_locked_ungrounded(text, es)
    if bad_locked:
        return False, f"ungrounded_terms:{','.join(bad_locked[:4])}"
    bad_sig = contains_internal_signals(text)
    if bad_sig:
        return False, f"internal_signals:{','.join(bad_sig[:3])}"
    low = text.lower()
    if any(x in low for x in ("feel the", "feel that volume", "volume spike", "the movement")):
        return False, "analyzer_phrase"
    return True, "ok"
