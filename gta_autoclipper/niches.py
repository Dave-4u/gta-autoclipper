"""Multi-niche presets: search queries, header styles, voice, hashtags, boost words."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

NICHES: dict[str, dict[str, Any]] = {
    "gta6": {
        "label": "GTA 6",
        "keywords": [
            "GTA 6 trailer",
            "GTA VI trailer reaction",
            "GTA 6 news",
            "GTA 6 leaks",
            "GTA 6 gameplay",
            "GTA 6 streamer reaction",
            "GTA RP funny moments",
            "GTA VI rockstar",
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
