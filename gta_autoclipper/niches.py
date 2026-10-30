"""Multi-niche presets: search queries, header styles, voice, hashtags, boost words."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

NICHES: dict[str, dict[str, Any]] = {
    "gta6": {
        "label": "GTA 6",
        "keywords": [
            "GTA 6 official trailer",
            "GTA VI trailer Rockstar",
            "GTA 6 gameplay",
            "GTA 6 trailer reaction",
            "GTA VI trailer reaction",
            "GTA 6 streamer reaction",
            "GTA 6 news Rockstar",
            "GTA 6 leaks",
        ],
        "header_styles": ["pov", "hype", "hot_take", "question", "reaction", "when_you", "info_news"],
        "voice": "en_US-ryan-high",
        "voice_style": "hype",
        "hashtags": ["#GTA6", "#GTAVI", "#GTA", "#Shorts", "#FYP", "#Gaming", "#RockstarGames"],
        "boost_words": [
            "gta 6", "gta vi", "trailer", "leak", "rockstar", "lucia", "leonida",
            "vice city", "crazy", "insane", "oh my god", "chase", "police", "fail",
        ],
        "vocab": ["Los Santos", "Vice City", "Lucia", "Rockstar", "wanted level", "heist"],
    },
    "anime": {
        "label": "Anime",
        "keywords": [
            "anime fight scene 4k",
            "anime plot twist",
            "anime edit shorts",
            "best anime moments 2025",
            "anime trailer new",
            "anime reaction funny",
            "shonen fight scene",
            "anime goated moment",
        ],
        "header_styles": ["pov", "hype", "reaction", "question", "when_you", "hot_take"],
        "voice": "en_US-amy-medium",
        "voice_style": "hype",
        "hashtags": ["#Anime", "#AnimeEdit", "#Shorts", "#FYP", "#Manga", "#Otaku", "#AnimeMoments"],
        "boost_words": [
            "anime", "sensei", "power", "final", "form", "bankai", "nen", "chakra",
            "wait", "no way", "bro", "insane", "peak", "goat", "aura",
        ],
        "vocab": ["aura", "peak fiction", "goat", "shonen", "bankai", "plot armor"],
    },
    "animation": {
        "label": "Animation / Cartoons",
        "keywords": [
            "cartoon funny moments",
            "pixar trailer",
            "animated movie trailer",
            "cartoon edit shorts",
            "disney animation clip",
            "cartoon fail funny",
            "indie animation short",
        ],
        "header_styles": ["pov", "when_you", "hype", "question", "reaction", "hot_take"],
        "voice": "en_US-lessac-high",
        "voice_style": "commentary",
        "hashtags": ["#Animation", "#Cartoon", "#Shorts", "#FYP", "#Pixar", "#Disney", "#Animated"],
        "boost_words": ["cartoon", "animation", "funny", "wait", "look", "omg", "crazy", "trailer"],
        "vocab": ["toon force", "frame one", "cinema", "animated"],
    },
    "reactions": {
        "label": "Reactions / Streamers",
        "keywords": [
            "streamer reaction funny",
            "twitch funny moments",
            "youtuber reaction compilation",
            "streamer rage moment",
            "live reaction trailer",
            "kai cenat reaction",
            "ishowspeed reaction",
        ],
        "header_styles": ["reaction", "pov", "when_you", "hype", "question", "hot_take"],
        "voice": "en_US-joe-medium",
        "voice_style": "reaction",
        "hashtags": ["#Reaction", "#Streamer", "#Twitch", "#Shorts", "#FYP", "#Funny", "#Live"],
        "boost_words": [
            "oh my god", "bro", "what", "no way", "wait", "look", "insane", "crazy",
            "reaction", "chat", "dude",
        ],
        "vocab": ["chat", "W", "L", "ratio", "unhinged", "meltdown"],
    },
    "gaming": {
        "label": "Gaming",
        "keywords": [
            "gaming funny moments",
            "clutch play shorts",
            "game fail compilation",
            "new game trailer",
            "speedrun fail funny",
            "ranked rage moment",
            "best gaming clips",
        ],
        "header_styles": ["hype", "pov", "when_you", "hot_take", "question", "reaction"],
        "voice": "en_US-ryan-high",
        "voice_style": "hype",
        "hashtags": ["#Gaming", "#GamingClips", "#Shorts", "#FYP", "#Clutch", "#GameFail"],
        "boost_words": [
            "clutch", "noob", "rage", "fail", "win", "boss", "insane", "crazy", "gg", " Trailer",
        ],
        "vocab": ["clutch", "diff", "carry", "nerf", "buff", "boss fight"],
    },
    "funny": {
        "label": "Funny / Viral",
        "keywords": [
            "funny viral video",
            "unexpected moment funny",
            "fail compilation shorts",
            "comedy skit viral",
            "wholesome funny clip",
            "prank gone wrong funny",
        ],
        "header_styles": ["when_you", "pov", "reaction", "question", "hot_take", "hype"],
        "voice": "en_US-lessac-medium",
        "voice_style": "reaction",
        "hashtags": ["#Funny", "#Viral", "#Shorts", "#FYP", "#Comedy", "#Fail", "#Memes"],
        "boost_words": ["funny", "wait", "what", "bro", "no", "omg", "laugh", "fail", "prank"],
        "vocab": ["plot twist", "unhinged", "send it", "main character"],
    },
    "movies": {
        "label": "Movies / Trailers",
        "keywords": [
            "movie trailer new",
            "film trailer reaction",
            "best movie scenes",
            "cinema trailer 2025",
            "movie plot twist scene",
            "blockbuster trailer",
        ],
        "header_styles": ["hype", "question", "hot_take", "info_news", "pov", "reaction"],
        "voice": "en_US-libritts-high",
        "voice_style": "commentary",
        "hashtags": ["#Movies", "#Trailer", "#Cinema", "#Shorts", "#FYP", "#FilmTok", "#Hollywood"],
        "boost_words": [
            "trailer", "movie", "cinema", "scene", "director", "twist", "insane", "beautiful",
        ],
        "vocab": ["cinema", "frame", "score", "blockbuster", "director"],
    },
    "custom": {
        "label": "Custom",
        "keywords": [],  # filled from config / UI
        "header_styles": ["pov", "hype", "question", "when_you", "hot_take", "reaction", "info_news"],
        "voice": "en_US-ryan-high",
        "voice_style": "auto",
        "hashtags": ["#Shorts", "#FYP", "#Viral", "#Trending"],
        "boost_words": ["wait", "look", "crazy", "insane", "omg", "bro", "what"],
        "vocab": [],
    },
}


def list_niche_ids() -> list[str]:
    return list(NICHES.keys())


def get_niche(niche_id: str) -> dict[str, Any]:
    key = (niche_id or "gta6").strip().lower().replace(" ", "_")
    aliases = {
        "gta": "gta6",
        "gta_6": "gta6",
        "cartoons": "animation",
        "cartoon": "animation",
        "reaction": "reactions",
        "streamer": "reactions",
        "streamers": "reactions",
        "viral": "funny",
        "comedy": "funny",
        "movie": "movies",
        "trailer": "movies",
        "trailers": "movies",
    }
    key = aliases.get(key, key)
    if key not in NICHES:
        key = "gta6"
    return deepcopy(NICHES[key])


def apply_niche_to_config(cfg: dict[str, Any], niche_id: str, custom_keywords: list[str] | None = None) -> dict[str, Any]:
    """Return a shallow-copied cfg tuned for this niche (output dir, search, voice, tags)."""
    niche = get_niche(niche_id)
    cfg = dict(cfg)
    cfg["search"] = dict(cfg.get("search") or {})
    cfg["clips"] = dict(cfg.get("clips") or {})
    cfg["analysis"] = dict(cfg.get("analysis") or {})
    cfg["voiceover"] = dict(cfg.get("voiceover") or {})
    cfg["hooks"] = dict(cfg.get("hooks") or {})

    kws = list(niche.get("keywords") or [])
    if niche_id == "custom" or custom_keywords:
        extra = [k.strip() for k in (custom_keywords or []) if k and k.strip()]
        kws = extra + kws
    if not kws:
        kws = list((cfg.get("search") or {}).get("keywords") or ["viral shorts"])

    cfg["search"]["keywords"] = kws
    cfg["niche_id"] = niche_id if niche_id in NICHES or niche_id == "custom" else "gta6"
    if cfg["niche_id"] not in NICHES:
        cfg["niche_id"] = "gta6"
    cfg["niche"] = niche
    cfg["hooks"]["preferred_styles"] = list(niche.get("header_styles") or [])
    cfg["hooks"]["vocab"] = list(niche.get("vocab") or [])
    cfg["analysis"]["transcript_boost_words"] = list(
        dict.fromkeys(
            list(niche.get("boost_words") or [])
            + list(cfg["analysis"].get("transcript_boost_words") or [])
        )
    )
    cfg["hashtags"] = list(niche.get("hashtags") or cfg.get("hashtags") or [])
    if niche.get("voice"):
        cfg["voiceover"]["voice"] = niche["voice"]
    if niche.get("voice_style"):
        cfg["voiceover"]["script_style"] = niche["voice_style"]

    # Niche-specific output folder: <project>/outputs/<niche>
    from pathlib import Path
    root = Path(cfg.get("_root") or ".")
    cfg["clips"]["output_dir"] = str((root / "outputs" / cfg["niche_id"]).resolve())
    return cfg


# --- Niche relevance (keep each video strictly in one niche) ---
# must_any: at least one must appear in title/description/channel
# reject_any: if any appear (and must_any not strongly matched), reject
# hard_reject: always reject if present without a stronger must hit for THIS niche

NICHE_RELEVANCE: dict[str, dict[str, list[str]]] = {
    "gta6": {
        "must_any": [
            "gta 6", "gta6", "gta vi", "gtavi", "gta v", "gta 5", "gta rp", "gta online",
            "grand theft auto", "gta ", " gta", "rockstar", "lucia", "leonida", "vice city",
            "los santos", "jason and lucia",
        ],
        "hard_reject": [
            "anime", "manga", "naruto", "one piece", "demon slayer", "jujutsu", "aot ",
            "attack on titan", "pixar", "disney cartoon",
        ],
    },
    "anime": {
        "must_any": [
            "anime", "manga", "shonen", "seinen", "waifu", "otaku", "naruto", "one piece",
            "one-punch", "one punch", "demon slayer", "jujutsu", "jjk", "aot", "attack on titan",
            "bleach", "dbz", "dragon ball", "chainsaw", "solo leveling", "studio ghibli",
            "crunchyroll", "my hero", "tokyo ghoul", "evangelion", "sakuga", "amv",
            "code geass", "spy x family", "frieren", "bocchi", "haikyuu", "hxh",
        ],
        "hard_reject": [
            "gta 6", "gta6", "gta vi", "gta 5", "gta rp", "rockstar games", "lucia trailer",
            "fortnite", "call of duty", "minecraft",
        ],
    },
    "animation": {
        "must_any": [
            "cartoon", "animation", "animated", "pixar", "disney", "dreamworks", "illumination",
            "toon", "indie animation", "2d animation", "3d animation", "cartoon network",
        ],
        "hard_reject": [
            "anime fight", "manga", "gta 6", "gta vi", "live action trailer only",
        ],
    },
    "reactions": {
        "must_any": [
            "reaction", "reacts", "reacting", "streamer", "twitch", "live react",
            "watching", "first time watching", "kai cenat", "ishowspeed", "xqc",
            "rage", "chat said",
        ],
        "hard_reject": [],
    },
    "gaming": {
        "must_any": [
            "gameplay", "gaming", "gamer", "clutch", "speedrun", "boss fight", "ranked",
            "playstation", "xbox", "steam", "nintendo", "esports", "noob", "gg ",
            "call of duty", "fortnite", "valorant", "minecraft", "elden ring",
        ],
        "hard_reject": ["asmr cooking", "makeup tutorial"],
    },
    "funny": {
        "must_any": [
            "funny", "comedy", "fail", "prank", "meme", "hilarious", "viral", "skit",
            "unexpected", "try not to laugh",
        ],
        "hard_reject": [],
    },
    "movies": {
        "must_any": [
            "movie", "film", "cinema", "trailer", "blockbuster", "hollywood", "marvel",
            "dc comics", "hollywoodverse", "hollywood cut", "imdb", "hollywood scene",
        ],
        "hard_reject": ["anime fight", "gta 6 gameplay", "gta rp"],
    },
    "custom": {
        "must_any": [],
        "hard_reject": [],
    },
}


def niche_relevance(
    niche_id: str,
    *,
    title: str = "",
    description: str = "",
    channel: str = "",
    tags: list[str] | None = None,
    custom_keywords: list[str] | None = None,
) -> tuple[bool, float, str]:
    """
    Return (ok, score, reason). Rejects cross-niche pollution (e.g. GTA under anime).
    """
    key = (niche_id or "").strip().lower()
    aliases = {
        "gta": "gta6", "gta_6": "gta6", "reaction": "reactions", "streamer": "reactions",
        "cartoons": "animation", "cartoon": "animation", "viral": "funny", "movie": "movies",
    }
    key = aliases.get(key, key)
    rules = NICHE_RELEVANCE.get(key) or NICHE_RELEVANCE.get("custom") or {}
    blob = " ".join(
        [
            title or "",
            description or "",
            channel or "",
            " ".join(tags or []),
        ]
    ).lower()
    blob_pad = f" {blob} "

    # Custom niche: require at least one custom keyword if provided
    if key == "custom":
        kws = [k.lower() for k in (custom_keywords or []) if k.strip()]
        if not kws:
            return True, 0.5, "custom_no_filter"
        hits = [k for k in kws if k in blob]
        if hits:
            return True, min(2.0, 0.5 + 0.4 * len(hits)), f"custom_hit:{hits[0]}"
        return False, 0.0, "custom_no_keyword_match"

    must = [m.lower() for m in (rules.get("must_any") or [])]
    hard = [m.lower() for m in (rules.get("hard_reject") or [])]

    must_hits = [m for m in must if (m in blob_pad if m.startswith(" ") or m.endswith(" ") else m in blob)]
    hard_hits = [h for h in hard if h in blob]

    if hard_hits and not must_hits:
        return False, 0.0, f"hard_reject:{hard_hits[0]}"
    if hard_hits and must_hits:
        # Both present — only allow if must hit is clearer / longer than reject token noise
        best_must = max(len(m) for m in must_hits)
        best_hard = max(len(h) for h in hard_hits)
        if best_hard >= best_must and key in ("anime", "gta6", "animation", "movies"):
            return False, 0.0, f"cross_niche:{hard_hits[0]}"

    if must and not must_hits:
        return False, 0.0, "missing_must_keyword"

    score = 0.5 + 0.35 * len(must_hits)
    return True, min(2.5, score), f"ok:{must_hits[0] if must_hits else 'pass'}"


def filter_candidates_for_niche(
    niche_id: str,
    candidates: list[Any],
    *,
    custom_keywords: list[str] | None = None,
) -> list[Any]:
    """Filter discover candidates; attaches niche_relevance_score on survivors."""
    kept = []
    for c in candidates:
        title = getattr(c, "title", "") or ""
        extra = getattr(c, "extra", None) or {}
        ok, score, reason = niche_relevance(
            niche_id,
            title=title,
            description=str(extra.get("description") or ""),
            channel=str(extra.get("channel") or extra.get("uploader") or ""),
            tags=list(extra.get("tags") or []),
            custom_keywords=custom_keywords,
        )
        if not ok:
            print(f"[niche] reject [{niche_id}] {title[:70]!r} ({reason})")
            continue
        c.rank_score = float(getattr(c, "rank_score", 0) or 0) + score
        if isinstance(extra, dict):
            extra["niche_reason"] = reason
            c.extra = extra
        kept.append(c)
    print(f"[niche] {niche_id}: kept {len(kept)}/{len(candidates)} after relevance filter")
    return kept
