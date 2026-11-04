"""Ranking mode: Top-N grounded niche moments ordered by view count (low → high = #N → #1)."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import json
import random
import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .analyze import Segment, find_highlights, find_highlights_light
from .config import ensure_dirs, load_config
from .discover import Candidate, discover
from .download import download_video
from . import procutil
from .edit import _escape_drawtext, _fit_header, render_clip
from .entities import extract_entities, event_summary, validate_copy
from .history import History
from .niches import apply_niche_to_config, niche_relevance
from .voiceover import make_intro_vo


def _fit_header_words(text: str, max_chars: int = 28) -> str:
    """Truncate on word boundaries only — never mid-name."""
    text = re.sub(r"\s+", " ", (text or "").strip())
    if len(text) <= max_chars:
        return text
    words = text.split()
    out: list[str] = []
    for w in words:
        trial = (" ".join(out + [w])).strip()
        if len(trial) > max_chars:
            break
        out.append(w)
    return " ".join(out) if out else text[:max_chars].rsplit(" ", 1)[0]



TOPIC_TEMPLATES = {
    "gta6": [
        "Top {n} wildest GTA 6 trailer moments",
        "Top {n} craziest GTA gameplay moments",
        "Top {n} GTA 6 chase clips",
        "Top {n} GTA 6 moments you missed",
    ],
    "anime": [
        "Top {n} hardest anime fight moments",
        "Top {n} anime scenes that hit different",
        "Top {n} anime fights going crazy",
        "Top {n} anime moments worth rewatching",
    ],
    "reactions": [  # experimental / disabled niche — kept for code compatibility
        "Top {n} funniest streamer reactions",
        "Top {n} trailer reactions that broke chat",
        "Top {n} streamer meltdown moments",
        "Top {n} reactions that went viral",
    ],
    "gaming": [
        "Top {n} cleanest gaming clutches",
        "Top {n} gaming fails that hurt",
        "Top {n} GTA gameplay moments",
    ],
    "funny": [
        "Top {n} funniest viral moments",
    ],
    "movies": [
        "Top {n} movie trailer moments",
    ],
    "animation": [
        "Top {n} funniest animation moments",
        "Top {n} animation scenes that slap",
        "Top {n} cartoon moments that hit",
    ],
}


@dataclass
class RankItem:
    rank: int  # display number (#5 .. #1)
    candidate: Candidate
    path: Path
    segment: Segment
    label: str
    views: int
    header: str = ""


def suggest_topic(niche_id: str, n: int = 5, cfg: dict[str, Any] | None = None) -> str:
    niche = (niche_id or "anime").lower()
    if niche == "reactions":
        niche = "anime"  # reactions paused — fall back to anime topics
    templates = TOPIC_TEMPLATES.get(niche) or [f"Top {{n}} {niche} moments"]
    # Prefer non-reaction wording
    templates = [x for x in templates if "reaction" not in x.lower()] or templates
    topic = random.choice(templates).format(n=n)
    if cfg:
        kws = list(((cfg.get("search") or {}).get("keywords")) or [])
        if kws and random.random() < 0.5:
            seed = random.choice(kws[:4])
            # Keep niche-locked
            topic = f"Top {n} {seed} moments"
            # Sanitize leak-heavy seeds for gta
            if niche == "gta6" and "leak" in topic.lower():
                topic = f"Top {n} GTA 6 trailer moments"
    return topic


def _format_views(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M views".replace(".0M", "M")
    if n >= 1_000:
        return f"{n/1_000:.1f}K views".replace(".0K", "K")
    return f"{n} views"



def detect_topic_type(topic: str) -> str:
    """Classify ranking topic into a strict filter type."""
    t = (topic or "").lower()
    if any(k in t for k in ("reaction", "reacts", "react ", "streamer meltdown", "broke chat")):
        return "reactions"
    if any(k in t for k in ("fight", "battle", "vs", "sakuga", "boss")):
        return "fights"
    if any(k in t for k in ("funny", "fail", "meme", "hilarious", "comedy")):
        return "funny"
    if any(k in t for k in ("gameplay", "clutch", "playthrough", "walkthrough")):
        return "gameplay"
    if any(k in t for k in ("trailer", "teaser", "cinematic")) and "reaction" not in t:
        return "trailers"
    if any(k in t for k in ("meltdown", "rage", "unhinged")):
        return "reactions"
    return "general"


def _has_face_cam(path: Path, sample_secs: float = 8.0) -> bool:
    """True if YuNet/upper-third face motion suggests a reaction face-cam."""
    try:
        from .analyze import extract_face_motion_scores
        import numpy as np
        scores = extract_face_motion_scores(path, sample_fps=2.0)
        if scores is None or len(scores) == 0:
            return False
        # Look at first ~sample_secs and overall — need sustained face presence
        head = scores[: max(1, int(sample_secs))]
        return float(np.mean(head)) > 0.35 or float(np.max(scores)) > 0.7
    except Exception as e:
        print(f"[ranking] face-cam check failed: {e}")
        return False


def topic_matches(
    topic_type: str,
    *,
    title: str,
    channel: str = "",
    description: str = "",
    tags: list[str] | None = None,
    has_face_cam: bool | None = None,
) -> tuple[bool, str]:
    """Strict topic relevance. Returns (ok, reason)."""
    blob = f"{title} {channel} {description} {' '.join(tags or [])}".lower()
    title_l = (title or "").lower()
    ch_l = (channel or "").lower()

    # Hard-reject official trailers/uploads for non-trailer topics
    official_signals = (
        "official trailer", "launch trailer", "rockstar games",
        "grand theft auto vi trailer", "gta vi trailer 1", "gta 6 trailer 1",
    )
    is_official = any(s in blob for s in official_signals) or (
        "official" in title_l and "trailer" in title_l and "react" not in title_l
    )
    # Channel is Rockstar / pure game publisher
    publisher_ch = any(p in ch_l for p in ("rockstar", "playstation", "xbox", "nintendo", "crunchyroll"))

    if topic_type == "reactions":
        if is_official and "react" not in title_l:
            return False, "official_trailer"
        if publisher_ch and "react" not in title_l:
            return False, "publisher_channel"
        react_sig = any(
            k in blob
            for k in ("react", "reaction", "reacts", "reacting", "watched", "watching")
        )
        # Known streamer aliases in entities
        from .entities import STREAMER_ALIASES
        streamer_sig = any(a in blob for a, _ in STREAMER_ALIASES) or any(
            k in ch_l for k in ("react", "live", "twitch", "yt ")
        )
        # Channel looks like a person (not a game brand)
        person_ch = bool(ch_l) and not publisher_ch and len(ch_l) > 2
        if not (react_sig or streamer_sig):
            return False, "no_reaction_signal"
        # Face-cam required when we have the file (post-download)
        if has_face_cam is False:
            return False, "no_face_cam"
        if has_face_cam is None and not react_sig:
            # Pre-download: require explicit react in title if no face yet
            if "react" not in title_l and "reaction" not in title_l:
                return False, "need_react_in_title"
        return True, "ok"

    if topic_type == "fights":
        if is_official and "fight" not in title_l:
            return False, "official_not_fight"
        if not any(k in blob for k in ("fight", "battle", "vs", "duel", "boss", "spar")):
            return False, "no_fight_signal"
        return True, "ok"

    if topic_type == "funny":
        if is_official:
            return False, "official_trailer"
        if not any(k in blob for k in ("funny", "fail", "meme", "hilarious", "comedy", "laugh", "prank")):
            return False, "no_funny_signal"
        return True, "ok"

    if topic_type == "gameplay":
        if not any(k in blob for k in ("gameplay", "playthrough", "walkthrough", "clutch", "ranked", "stream")):
            # allow if not clearly a trailer-only upload
            if "trailer" in title_l and "gameplay" not in title_l:
                return False, "trailer_not_gameplay"
        return True, "ok"

    if topic_type == "trailers":
        if "react" in title_l:
            return False, "reaction_not_trailer"
        if not any(k in blob for k in ("trailer", "teaser", "cinematic")):
            return False, "no_trailer_signal"
        return True, "ok"

    # general: reject pure official trailers unless topic asks for them
    if is_official and "trailer" not in (topic or "").lower():
        return False, "official_trailer"
    return True, "ok"


def smart_rank_label(
    *,
    title: str,
    channel: str = "",
    topic_type: str = "general",
    niche_id: str = "",
) -> str:
    """Short label that never truncates mid-name."""
    from .entities import extract_entities, STREAMER_ALIASES, detect_reactor

    es = extract_entities(title=title, channel=channel, niche_id=niche_id)
    title_l = (title or "").lower()

    # Prefer the actual reactor (title says X reacts) — never a random mentioned name
    if topic_type == "reactions" or "react" in title_l:
        # Mashups / multi-reactor comps first — never a single wrong name
        if any(k in title_l for k in (
            "mashup", "compilation", "streamers react", "youtubers react",
            "mega reaction", "reactors react", "everyone react",
        )):
            if es.franchises and any(f.startswith("gta") for f in es.franchises):
                return "GTA 6 reactions"
            return "Reaction mashup"
        reactor = detect_reactor(title, channel)
        if reactor and reactor.lower() not in ("youtubers", "streamers", "everyone"):
            return reactor[:28]
        m = re.search(r"^(.{3,28}?)\s+react", title or "", flags=re.I)
        if m:
            cand = m.group(1).strip(" -_|:")
            if cand.lower() not in ("youtubers", "streamers", "everyone", "big streamers"):
                return cand[:28]
        # Event-focused label if no clear reactor
        for kind in ("trailer", "gameplay", "fight", "ending"):
            if kind in title_l:
                if es.franchises:
                    show = es.franchises[0].replace("_", " ")
                    if show.startswith("gta"):
                        show = "GTA 6"
                    return f"{show} {kind}"[:28]
                return f"{kind} reaction"
        if channel and channel.strip() and channel.strip().lower() not in {
            "streamer", "reactor", "live", "gaming", "reacts", "reaction",
        }:
            return channel.strip()[:28]
        # Known alias scan in title/channel
        for alias, canon in STREAMER_ALIASES:
            if alias in title_l or alias in (channel or "").lower():
                return canon[:28]
        if es.franchises and any(f.startswith("gta") for f in es.franchises):
            return "GTA 6 reaction"
        return "Reaction"

    # "A vs B" straight from the title beats any guessed character
    m = re.search(r"([A-Z][\w'.]*(?:\s+[A-Z][\w'.]*)?)\s+(?:vs\.?|VS|Vs\.?|versus)\s+([A-Z][\w'.]*(?:\s+[A-Z][\w'.]*)?)", title or "")
    if m:
        b = re.sub(r"(?i)\s+(fight|scene|battle|amv|edit)$", "", m.group(2))
        lab = f"{m.group(1)} vs {b}"
        if len(lab) > 28:
            lab = f"{m.group(1).split()[0]} vs {m.group(2).split()[0]}"
        return lab[:28]
    # Only trust extracted characters that are literally named in the title
    es.characters = [c for c in (es.characters or []) if c.lower() in title_l]

    # Franchise + content type
    if es.franchises:
        show = es.franchises[0].replace("_", " ")
        if show.startswith("gta"):
            show = "GTA 6" if "gta6" in es.franchises or "gta 6" in title_l or "gta vi" in title_l else "GTA"
        else:
            show = show.title()
        for kind in ("trailer", "gameplay", "fight", "teaser", "clutch", "fail"):
            if kind in title_l:
                return f"{show} {kind}"
        if es.characters:
            return f"{es.characters[0].title()}"
        return show

    if es.characters:
        return es.characters[0].title()

    # Clean title: drop noise words, keep whole words only
    clean = re.sub(
        r"(?i)\b(official|hd|4k|shorts?|tiktok|part\s*\d+|finally+|crazy|insane)\b",
        " ",
        title or "",
    )
    clean = re.sub(r"[|\[\]\(\)#_]+", " ", clean)
    clean = re.sub(r"\s+", " ", clean).strip(" -")
    words = clean.split()
    # Take whole words up to ~28 chars — never cut a word
    out: list[str] = []
    for w in words[:6]:
        trial = (" ".join(out + [w])).strip()
        if len(trial) > 28:
            break
        out.append(w)
    label = " ".join(out).strip() or "viral moment"
    return label


_FAN_RX = re.compile(
    r"(?i)(fan[\s-]*(made|animation|animated|anim|project|film)|fanmade|\bamv\b|\bedit\b|"
    r"\bedits\b|animator|animated by|my animation|i animated|sakuga fan|parody|what if|"
    r"\bfan\b|blender|\bmmd\b|stick ?fight|\bdc2\b|remake by|re-?animated|sped up|reverb|"
    r"sound re-?design|\bremix\b)"
)
_OFFICIAL_RX = re.compile(
    r"(?i)(official|crunchyroll|netflix|toho|aniplex|mappa|ufotable|toei|funimation|"
    r"\bepisode\b|\bep\s*\d+|season\s*\d+|\bs\d+\b|full fight|fight scene|\bmovie\b|trailer|\bpv\b)"
)


def wants_fan_made(topic: str) -> bool:
    return bool(re.search(r"(?i)fan[\s-]*made|fanmade|fan animation|\bamv|\bedits?\b", topic or ""))


def classify_origin(cands: list[Candidate], cfg: dict[str, Any], log=None) -> dict[str, str]:
    """Return {id: 'fan'|'official'} from title/channel/description, Gemini for ambiguous ones."""
    out: dict[str, str] = {}
    unsure: list[Candidate] = []
    for c in cands:
        ex = c.extra or {}
        blob = " ".join([c.title or "", str(ex.get("channel") or ""), str(ex.get("description") or "")[:400],
                         " ".join(map(str, ex.get("tags") or []))])
        fan = bool(_FAN_RX.search(blob))
        off = bool(_OFFICIAL_RX.search(blob))
        if fan and not off:
            out[c.id] = "fan"
        elif off and not fan:
            out[c.id] = "official"
        else:
            unsure.append(c)
    if unsure:
        try:
            from .gemini_client import generate, has_gemini
            if has_gemini(cfg):
                lines = "\n".join(
                    f"{c.id}\t{(c.title or '')[:90]}\t{str((c.extra or {}).get('channel') or '')[:40]}"
                    for c in unsure[:25]
                )
                raw = generate(prompt=(
                    "Classify each YouTube video as FAN (fan animation, fan-made, AMV, edit, remix/sped-up edit, "
                    "animator/hobby project) or OFFICIAL (unaltered footage from the real anime/studio/streamer). "
                    "Reply ONLY with compact JSON {\"<id>\":\"fan\"|\"official\"}.\n" + lines
                ), cfg=cfg, max_retries=2)
                import json as _j
                m = re.search(r"\{.*\}", raw or "", re.S)
                data = _j.loads(m.group(0)) if m else {}
                for c in unsure:
                    v = str(data.get(c.id) or "").lower()
                    if v in ("fan", "official"):
                        out[c.id] = v
        except Exception as e:
            if log:
                log(f"[ranking] origin check via Gemini failed ({e}) — using title rules")
    for c in unsure:
        # Still unsure: an explicit "edit/AMV"-style hint wins, else treat as official
        out.setdefault(c.id, "fan" if _FAN_RX.search(c.title or "") else "official")
    return out


def _topic_search_queries(niche_id: str, topic: str) -> list[str]:
    base = re.sub(r"(?i)^top\s*\d+\s*", "", topic).strip()
    base = re.sub(r"\s+", " ", base)
    ttype = detect_topic_type(topic)
    qs = [base, topic]
    niche = (niche_id or "").lower()
    if ttype == "reactions":
        if niche == "gta6":
            qs += [
                "GTA 6 trailer reaction",
                "GTA VI trailer reaction",
                "streamer reacts GTA 6",
                "reacts to GTA 6 trailer",
            ]
        else:
            qs += ["streamer reaction funny", "trailer reaction", "twitch funny reaction"]
    elif ttype == "fights" or niche == "anime":
        if not wants_fan_made(topic):
            qs += [
                "anime episode fight scene", "Crunchyroll fight scene", "Muse Asia fight scene",
                "Ani-One fight scene", "anime full fight scene episode",
            ]
        qs += [
            "anime fight edit", "best anime fight scene", "anime fight scene 4k",
            "best anime fight moments", "anime fight shorts", "epic anime battle",
            "anime fight compilation", "most hype anime fight",
        ]
    elif ttype == "gameplay":
        qs += ["GTA 6 gameplay", "gameplay clutch", "gameplay funny"]
    elif ttype == "trailers":
        qs += ["GTA 6 official trailer", "GTA VI trailer"]
    elif niche == "gta6":
        qs += ["GTA 6 trailer reaction", "GTA 6 gameplay"]
    # Never add bare "official trailer" when topic is reactions
    out, seen = [], set()
    for q in qs:
        k = q.lower()
        if ttype == "reactions" and "official trailer" in k and "react" not in k:
            continue
        if k and k not in seen:
            seen.add(k)
            out.append(q)
    return out[:12]


_WIDE_QUERIES = {
    "anime": ["anime fight", "anime battle scene", "anime fight amv", "anime best fights ever",
              "anime fight edit 4k", "demon slayer fight scene", "jujutsu kaisen fight scene",
              "one piece fight scene", "naruto fight scene", "dragon ball fight scene"],
    "gta6": ["GTA 6", "GTA 6 trailer reaction", "GTA VI reaction", "GTA 6 gameplay leak"],
}



def _meta_prefilter_score(cand: Candidate, *, topic: str, niche_id: str, ttype: str) -> float:
    """Cheap score from metadata only (no download). Higher = better to probe."""
    title = (cand.title or "").lower()
    topic_l = (topic or "").lower()
    views = float(cand.view_count or 0)
    dur = float(cand.duration or 0)
    score = 0.0
    # Prefer mid-length clips (not 3h streams, not 5s shorts)
    if 20 <= dur <= 240:
        score += 3.0
    elif 12 <= dur <= 420:
        score += 1.5
    elif dur > 900:
        score -= 4.0
    elif dur and dur < 10:
        score -= 2.0
    # View band useful for Top-N diversity (avoid mega-viral only)
    if views > 0:
        score += min(4.0, __import__("math").log10(views + 10))
    # Title token overlap with topic
    stop = {"the", "a", "an", "to", "of", "and", "top", "best", "5", "10", "that", "hit", "different"}
    t_toks = {w for w in __import__("re").findall(r"[a-z0-9]+", topic_l) if w not in stop and len(w) > 2}
    title_toks = set(__import__("re").findall(r"[a-z0-9]+", title))
    if t_toks:
        score += 4.0 * (len(t_toks & title_toks) / max(1, len(t_toks)))
    # Official anime ranking: prefer real episode/scene uploads from official channels over
    # compilations / "Top 10" lists / re-uploads
    if (niche_id or "").lower() in {"anime", "animation"}:
        ch = str((cand.extra or {}).get("channel") or "").lower()
        if any(k in ch for k in ("crunchyroll", "muse", "ani-one", "aniplex", "toho", "netflix anime",
                                 "funimation", "mappa", "ufotable", "toei", "viz", "hidive", "bilibili")):
            score += 4.0
        if __import__("re").search(r"(episode|\bep\.?\s*\d+|season\s*\d+|\bs\d+\s*e\d+|full fight|fight scene)", title):
            score += 2.5
        if __import__("re").search(r"(top\s*\d+|compilation|best .* fights|every |all fights|ranking)", title):
            score -= 5.0
        if "re-upload" in title or "reupload" in title:
            score -= 3.0
    # Niche-ish keywords
    niche = (niche_id or "").lower()
    if niche == "anime" and any(k in title for k in ("anime", "fight", "vs", "amv", "edit", "manga")):
        score += 1.5
    if niche == "animation" and any(k in title for k in ("cartoon", "animation", "pixar", "disney")):
        score += 1.5
    if ttype == "fights" and any(k in title for k in ("fight", "vs", "battle", "clash", "duel")):
        score += 2.0
    if any(bad in title for bad in ("full episode", "full movie", "hour", "live stream", "livestream")):
        score -= 3.0
    return score


def collect_rank_items(
    niche_id: str,
    topic: str,
    *,
    n: int = 5,
    cfg: dict[str, Any] | None = None,
    log=None,
    deadline: float | None = None,
    cancel_event=None,
    max_probe: int | None = None,
) -> list[RankItem]:
    def _log(msg: str) -> None:
        print(msg, flush=True)
        if log:
            log(msg)

    def _timed_out() -> bool:
        if cancel_event is not None and cancel_event.is_set():
            return True
        if procutil.cancelled():
            return True
        if deadline is not None and time.time() >= deadline:
            return True
        return False

    cfg = apply_niche_to_config(cfg or load_config(), niche_id)
    root = Path(cfg.get("_root") or ".")
    rk = dict(cfg.get("ranking") or {})
    # Fast discovery knobs for ranking
    cfg.setdefault("search", {})
    cfg["search"]["keywords"] = _topic_search_queries(niche_id, topic)[: int(rk.get("keywords", 6) or 6)]
    cfg["search"]["results_per_keyword"] = int(rk.get("results_per_keyword", 25) or 25)
    cfg["search"]["keywords_per_run"] = len(cfg["search"]["keywords"])
    cfg["search"]["use_trending"] = bool(rk.get("use_trending", False))
    cfg["search"]["prefer_date_search"] = False
    cfg["search"]["search_timeout_seconds"] = int(rk.get("search_timeout_seconds", 45) or 45)
    # Cap source duration for ranking shortlist
    # Only a ~60–90s section is downloaded, so long sources are fine
    cfg["search"]["max_source_duration"] = int(rk.get("max_source_duration", 2400) or 2400)
    cfg["search"]["ignore_seen"] = bool(rk.get("ignore_seen", False))
    cfg["search"]["seen_fallback_min"] = int(rk.get("seen_fallback_min", 12) or 0)
    # Ranking is evergreen (best fights ever) — don't drop older uploads / short clips
    cfg["search"]["max_age_days"] = float(rk.get("max_age_days", 0) or 0)
    cfg["search"]["min_source_duration"] = float(rk.get("min_source_duration", 8) or 8)
    ttype = detect_topic_type(topic)
    _log(f"[ranking] topic_type={ttype} fast-discover kw={cfg['search']['keywords']}")

    hist = History(root / "history_ranking.json")
    cfg_rank = dict(cfg)
    cfg_rank.setdefault("history", {})
    cfg_rank["history"]["file"] = str((root / "history_ranking.json").resolve())
    cfg_rank["download"] = dict(cfg_rank.get("download") or {})
    # Only grab the first N seconds — huge win vs full episode downloads
    cfg_rank["download"]["max_download_seconds"] = float(rk.get("download_seconds", 90) or 90)
    cfg_rank["download"]["timeout_seconds"] = int(rk.get("download_timeout", 90) or 90)
    cfg_rank["analysis"] = dict(cfg_rank.get("analysis") or {})
    cfg_rank["analysis"]["max_analyze_seconds"] = float(rk.get("analyze_seconds", 75) or 75)
    cfg_rank["analysis"]["whisper_for_highlights"] = False
    cfg_rank["analysis"]["use_whisper"] = False

    def _prefilter(cands: list[Candidate]) -> list[Candidate]:
        filtered: list[Candidate] = []
        for cand in cands:
            if _timed_out():
                break
            channel = str((cand.extra or {}).get("channel") or "")
            desc = str((cand.extra or {}).get("description") or "")
            tags = list((cand.extra or {}).get("tags") or [])
            ok, rel, reason = niche_relevance(
                niche_id, title=cand.title, description=desc, channel=channel, tags=tags,
            )
            if not ok:
                continue
            ok_t, why_t = topic_matches(
                ttype, title=cand.title, channel=channel, description=desc, tags=tags, has_face_cam=None
            )
            if not ok_t:
                continue
            from .meta_signals import is_mashup_title
            if ttype == "reactions" and is_mashup_title(cand.title):
                continue
            cand.rank_score = _meta_prefilter_score(cand, topic=topic, niche_id=niche_id, ttype=ttype)
            filtered.append(cand)
        return filtered

    t_disc = time.time()
    cands = discover(cfg_rank, hist)
    _log(f"[ranking] {len(cands)} raw candidates in {time.time()-t_disc:.1f}s for topic={topic!r}")
    filtered = _prefilter(cands)
    min_parts = int(rk.get("min_parts", 3) or 3)
    want = max(min_parts, int(rk.get("second_pass_below", n) or n))
    if len(filtered) < want and not _timed_out() and bool(rk.get("second_pass", True)):
        _log(f"[ranking] only {len(filtered)} usable after pass 1 (<{want}) — wider pass 2")
        cfg2 = dict(cfg_rank)
        cfg2["search"] = dict(cfg_rank["search"])
        wide = _WIDE_QUERIES.get((niche_id or "").lower(), []) + list(rk.get("wide_queries") or [])
        kw2 = [q for q in wide + cfg_rank["search"]["keywords"] if q]
        kw2 = list(dict.fromkeys(kw2))[: int(rk.get("second_pass_keywords", 8) or 8)]
        cfg2["search"].update(
            keywords=kw2, keywords_per_run=len(kw2),
            results_per_keyword=int(rk.get("second_pass_results", 40) or 40),
            max_source_duration=int(rk.get("second_pass_max_duration", 7200) or 7200),
            min_source_duration=float(rk.get("second_pass_min_duration", 5) or 5),
            ignore_seen=True, max_age_days=0,
        )
        have = {c.id for c in filtered}
        t2 = time.time()
        extra = [c for c in discover(cfg2, hist) if c.id not in have]
        f2 = _prefilter(extra)
        _log(f"[ranking] pass 2: {len(extra)} raw → {len(f2)} usable in {time.time()-t2:.1f}s")
        filtered += f2

    filtered.sort(key=lambda c: c.rank_score, reverse=True)
    probe_n = int(max_probe or rk.get("max_probe", 7) or 7)
    # Never mix fan-made and official footage in one video
    origin = classify_origin(filtered, cfg, log=_log) if filtered else {}
    for c in filtered:
        c.extra = dict(c.extra or {})
        c.extra["origin"] = origin.get(c.id, "official")
        _log(f"[ranking] origin={c.extra['origin']:<8} {c.title[:70]}")
    fan_c = [c for c in filtered if c.extra["origin"] == "fan"]
    off_c = [c for c in filtered if c.extra["origin"] == "official"]
    need = int(rk.get("min_parts", 3) or 3) + 1
    if wants_fan_made(topic):
        group, pool = ("fan", fan_c) if len(fan_c) >= need or len(fan_c) >= len(off_c) else ("official", off_c)
    else:
        group, pool = ("official", off_c) if len(off_c) >= need or len(off_c) >= len(fan_c) else ("fan", fan_c)
    _log(f"[ranking] origin groups: official={len(off_c)} fan={len(fan_c)} → using {group.upper()} only")
    filtered = pool
    shortlist = filtered[: max(probe_n, n)]
    _log(f"[ranking] prefilter {len(filtered)} → probe top {len(shortlist)} (target n={n})")

    workers = max(1, min(3, int(rk.get("workers", 3) or 3)))
    picked_lock = __import__("threading").Lock()
    picked: list[tuple[Candidate, Path, Segment, str]] = []
    seen_ids: set[str] = set()

    def _process_one(cand: Candidate) -> tuple[Candidate, Path, Segment, str] | None:
        if _timed_out():
            return None
        try:
            path = download_video(cand, cfg_rank)
            if not path or _timed_out():
                return None
            face = None
            if ttype == "reactions":
                face = _has_face_cam(path)
                channel = str((cand.extra or {}).get("channel") or "")
                desc = str((cand.extra or {}).get("description") or "")
                tags = list((cand.extra or {}).get("tags") or [])
                ok_f, why_f = topic_matches(
                    ttype, title=cand.title, channel=channel, description=desc, tags=tags, has_face_cam=face
                )
                if not ok_f:
                    _log(f"[ranking] skip after face check ({why_f}): {cand.title[:70]}")
                    return None
            if _timed_out():
                return None
            # Lightweight analyze only for ranking
            segs = find_highlights_light(path, cfg_rank)
            if not segs:
                return None
            seg = segs[0]
            channel = str((cand.extra or {}).get("channel") or "")
            label = smart_rank_label(
                title=cand.title, channel=channel, topic_type=ttype, niche_id=niche_id
            )
            return (cand, path, seg, label)
        except Exception as e:
            _log(f"[ranking] error {cand.id}: {e}")
            return None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_process_one, c): c for c in shortlist}
        for fut in as_completed(futs):
            if _timed_out():
                _log("[ranking] deadline/cancel — stopping more probes")
                procutil.request_cancel()
                break
            with picked_lock:
                if len(picked) >= n:
                    break
            try:
                row = fut.result()
            except Exception as e:
                _log(f"[ranking] worker err: {e}")
                continue
            if not row:
                continue
            cand, path, seg, label = row
            if not path or not Path(path).is_file():
                _log(f"[ranking] skip {cand.id}: downloaded file vanished")
                continue
            with picked_lock:
                if cand.id in seen_ids or len(picked) >= n:
                    continue
                picked.append(row)
                seen_ids.add(cand.id)
                hist.mark(cand.id, {"status": "ranking", "niche": niche_id, "topic": topic, "topic_type": ttype})
                _log(f"[ranking] keep {cand.title[:60]} views={cand.view_count} label={label!r}")
                if len(picked) >= n:
                    _log("[ranking] target n reached — cancelling leftover probes")
                    procutil.request_cancel()

    # Ensure leftover worker downloads stop
    if len(picked) >= n or _timed_out():
        procutil.request_cancel()

    if not picked:
        return []

    picked.sort(key=lambda x: x[0].view_count)
    items: list[RankItem] = []
    total = len(picked)
    for i, (cand, path, seg, label) in enumerate(picked):
        rank_num = total - i
        items.append(
            RankItem(
                rank=rank_num,
                candidate=cand,
                path=path,
                segment=seg,
                label=label,
                views=int(cand.view_count or 0),
            )
        )
    _log(f"[ranking] collected {len(items)} item(s)")
    return items



def _render_title_card(
    topic: str,
    out_wav_dir: Path,
    cfg: dict[str, Any],
    *,
    seconds: float = 3.0,
    footage: Path | None = None,
    footage_t: float = 0.0,
) -> tuple[Path, Path | None]:
    """Title card over blurred #N footage (not a plain black screen)."""
    width = int((cfg.get("clips") or {}).get("width", 1080))
    height = int((cfg.get("clips") or {}).get("height", 1920))
    card = out_wav_dir / "title_card.mp4"
    topic_fit = _fit_header(topic, 56)
    topic_esc = _escape_drawtext(topic_fit)
    vo_wav = out_wav_dir / "topic_intro.wav"
    intro = make_intro_vo(topic_fit, cfg, vo_wav)
    vo_path = intro.wav_path if intro and intro.wav_path and intro.wav_path.is_file() else None
    dur = seconds
    if vo_path:
        try:
            import wave
            with wave.open(str(vo_path), "rb") as w:
                dur = max(2.5, min(4.5, w.getnframes() / float(w.getframerate() or 1) + 0.4))
        except Exception:
            pass
    # Huge centered title: fill ~60–70% of frame width, 2–3 bold outlined lines
    words = topic_fit.split()
    lines: list[str] = []
    cur: list[str] = []
    max_chars = 16  # ~3 lines for "Top 5 hardest anime fight moments"
    for w in words:
        trial = (" ".join(cur + [w])).strip()
        if cur and len(trial) > max_chars:
            lines.append(" ".join(cur))
            cur = [w]
        else:
            cur.append(w)
    if cur:
        lines.append(" ".join(cur))
    if not lines:
        lines = [topic_fit]
    lines = lines[:3]
    # Target text width ≈ 0.68 * 1080; glyph width ≈ 0.62*fs for bold
    longest = max(len(L) for L in lines) or 1
    fs = int(0.68 * width / max(0.62 * longest, 1))
    fs = max(96, min(160, fs))
    parts = []
    line_h = int(fs * 1.22)
    total_h = line_h * len(lines)
    base_y = int((height - total_h) / 2)
    for i, line in enumerate(lines):
        esc = _escape_drawtext(line)
        y = base_y + i * line_h
        parts.append(
            f"drawtext=text='{esc}':fontsize={fs}:fontcolor=yellow:borderw=16:bordercolor=black:"
            f"shadowx=4:shadowy=4:shadowcolor=black@0.9:x=(w-text_w)/2:y={y}"
        )
    draw = ",".join(parts)
    if footage and Path(footage).is_file():
        vf = (
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},gblur=sigma=18,eq=brightness=-0.08:saturation=0.85,{draw}"
        )
        # Always mute source footage on title card (avoid stray chat audio)
        cmd = ["ffmpeg", "-y", "-ss", f"{max(0.0, footage_t):.2f}", "-t", f"{dur:.2f}",
               "-i", str(footage)]
        if vo_path:
            cmd += ["-i", str(vo_path)]
            cmd += ["-map", "0:v:0", "-map", "1:a:0"]
        else:
            cmd += ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
            cmd += ["-map", "0:v:0", "-map", "1:a:0"]
        cmd += ["-vf", vf, "-t", f"{dur:.2f}",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30",
                "-c:a", "aac", "-shortest", str(card)]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode == 0 and card.is_file():
            return card, vo_path
        print(f"[ranking] footage title card failed: {(proc.stderr or '')[-200:]}")
    # Fallback black
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"color=c=black:s={width}x{height}:d={dur:.2f}",
    ]
    if vo_path:
        cmd += ["-i", str(vo_path)]
    cmd += ["-vf", draw, "-t", f"{dur:.2f}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30"]
    if vo_path:
        cmd += ["-c:a", "aac", "-shortest"]
    cmd.append(str(card))
    subprocess.run(cmd, capture_output=True, text=True, check=False)
    if not card.is_file():
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c=black:s={width}x{height}:d=3",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", str(card)],
            capture_output=True, check=False,
        )
    return card, vo_path


def _render_rank_segment(
    item: RankItem,
    cfg: dict[str, Any],
    out_dir: Path,
    *,
    with_vo: bool = True,
) -> Path | None:
    """Render one ranked clip with #N overlay + label + views badge."""
    # First render base clip via existing pipeline
    base = render_clip(
        item.path,
        item.segment,
        cfg,
        title=item.candidate.title,
        index=item.rank,
        video_id=f"{item.candidate.id}_r{item.rank}",
        source_meta=dict(item.candidate.extra or {}),
    )
    if not base or not base.is_file():
        return None
    # Rank chrome (#N / label / views) is burned ONCE in the final pass with the final numbers.
    # Burning it here too stacked an interim rank under the final one (e.g. "#1" over "#-1" → "#11").
    if True:
        return base

    # Burn rank overlays on top
    width = int((cfg.get("clips") or {}).get("width", 1080))
    height = int((cfg.get("clips") or {}).get("height", 1920))
    rank_txt = _escape_drawtext(f"#{item.rank}")
    label = _escape_drawtext(_fit_header_words(item.label, 28))
    views = _escape_drawtext(_format_views(item.views))
    out = out_dir / f"rank_{item.rank:02d}_{item.candidate.id}.mp4"

    # Persistent rank chrome in upper ~12% only (never center)
    vf = (
        f"drawtext=text='{rank_txt}':fontsize=72:fontcolor=yellow:"
        f"borderw=5:bordercolor=black:x=36:y=48,"
        f"drawtext=text='{label}':fontsize=36:fontcolor=white:"
        f"borderw=3:bordercolor=black:x=36:y=205,"
        f"drawtext=text='{views}':fontsize=30:fontcolor=yellow:"
        f"borderw=3:bordercolor=black:x=36:y=252"
    )
    cmd = [
        "ffmpeg", "-y", "-i", str(base),
        "-vf", vf,
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0 or not out.is_file():
        print(f"[ranking] overlay failed: {(proc.stderr or '')[-300:]}")
        return base
    return out



_FAIL_MARKERS = ("rejected", "FAIL", "failed", "error", "discard", "Traceback", "timed out", "Invalid", "No such")


class _Tee:
    def __init__(self, real):
        self.real, self.lines, self._buf = real, [], ""

    def write(self, s):
        try:
            self.real.write(s)
        except Exception:
            pass
        self._buf += s
        while "\n" in self._buf:
            ln, self._buf = self._buf.split("\n", 1)
            self.lines.append(ln)
            if len(self.lines) > 400:
                del self.lines[:200]
        return len(s)

    def flush(self):
        try:
            self.real.flush()
        except Exception:
            pass


def _render_part_logged(item, cfg, work, *, with_vo=True):
    """Render one part; return (path|None, reason). Never raises."""
    import contextlib, sys as _sys, traceback as _tb
    src = Path(item.path) if item.path else None
    if not src or not src.is_file() or src.stat().st_size < 10000:
        return None, f"source file missing/empty after download ({src.name if src else 'none'})"
    tee = _Tee(_sys.stdout)
    try:
        with contextlib.redirect_stdout(tee):
            seg = _render_rank_segment(item, cfg, work, with_vo=with_vo)
    except Exception as e:
        tail = _tb.format_exc().strip().splitlines()[-1]
        return None, f"exception: {tail[:240]}"
    if tee._buf:
        tee.lines.append(tee._buf)
    if seg:
        return seg, ""
    hits = [ln.strip() for ln in tee.lines if any(m in ln for m in _FAIL_MARKERS)]
    why = " / ".join(hits[-3:]) if hits else (tee.lines[-1].strip() if tee.lines else "render_clip returned nothing")
    return None, why[:400]


def _ass_dialogue_count(ass_path: Path) -> int:
    if not ass_path.is_file():
        return 0
    return sum(1 for ln in ass_path.read_text(encoding="utf-8", errors="ignore").splitlines() if ln.startswith("Dialogue:"))


def _ensure_part_captions(
    clip: Path,
    *,
    cfg: dict[str, Any],
    niche_id: str,
    work: Path,
    label: str = "",
) -> Path:
    """Guarantee burned streamer captions on a ranking part (reuse ASS or re-whisper+burn)."""
    from .transcribe import get_word_timestamps
    from .edit import _write_ass_captions

    ass = clip.with_suffix(".ass")
    n = _ass_dialogue_count(ass)
    if n > 0:
        print(f"[ranking] captions ok {label or clip.name}: {n} Dialogue events")
        return clip
    try:
        import json as _json
        pr = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(clip)],
            capture_output=True, text=True, check=False,
        )
        dur = float((_json.loads(pr.stdout).get("format") or {}).get("duration") or 12)
    except Exception:
        dur = 12.0
    model = str((cfg.get("analysis") or {}).get("whisper_model") or "small")
    if model == "tiny":
        model = "small"
    # Skip title-card intro when present (~2s)
    words = get_word_timestamps(clip, 2.0, max(3.0, dur - 0.2), model_name=model, niche_id=niche_id)
    if len(words) < 3:
        words = get_word_timestamps(clip, 0.3, max(3.0, dur - 0.2), model_name=model, niche_id=niche_id)
    if len(words) < 3:
        print(f"[ranking] WARN {label or clip.name}: no caption words after whisper")
        return clip
    ass2 = work / f"{clip.stem}_rankcaps.ass"
    n2 = _write_ass_captions(words, ass2, 1080, 1920, mask_swears=bool((cfg.get("clips") or {}).get("mask_profanity", True)))
    print(f"[ranking] burned {n2} Dialogue events onto {label or clip.name} ({len(words)} words)")
    out = work / f"{clip.stem}_captioned.mp4"
    ass_esc = str(ass2).replace("\\", "/").replace(":", chr(92) + ":")
    pr = subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(clip),
            "-vf", f"ass='{ass_esc}'",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "copy", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            str(out),
        ],
        capture_output=True, text=True, check=False,
    )
    if pr.returncode == 0 and out.is_file():
        out.with_suffix(".ass").write_text(ass2.read_text(encoding="utf-8"), encoding="utf-8")
        return out
    print(f"[ranking] caption burn failed: {(pr.stderr or '')[-300:]}")
    return clip


def build_ranking_video(
    niche_id: str,
    topic: str | None = None,
    *,
    n: int = 5,
    config_path: str | None = None,
    with_vo: bool = True,
    log=None,
    deadline_s: float | None = None,
    cancel_event=None,
) -> Path | None:
    def _log(msg: str) -> None:
        print(msg, flush=True)
        if log:
            log(msg)

    def _timed_out() -> bool:
        if cancel_event is not None and cancel_event.is_set():
            return True
        if procutil.cancelled():
            return True
        if deadline is not None and time.time() >= deadline:
            return True
        return False

    cfg = load_config(config_path)
    ensure_dirs(cfg)
    cfg = apply_niche_to_config(cfg, niche_id)
    rk = dict(cfg.get("ranking") or {})
    budget = float(deadline_s if deadline_s is not None else rk.get("timeout_seconds", 1500) or 1500)
    deadline = time.time() + budget
    min_on_timeout = int(rk.get("min_items_on_timeout", 3) or 3)
    _log(f"[ranking] budget={budget:.0f}s min_on_timeout={min_on_timeout}")
    # Ranking clips should be short
    cfg.setdefault("clips", {})
    cfg["clips"]["max_clips_per_video"] = 1
    # Anime/animation ranking items: ~12–18s continuous action (config override below)
    rank_dur = (cfg.get("clips") or {}).get("ranking_item_duration") or {}
    niche_l = (niche_id or "").lower()
    if niche_l in {"anime", "animation"}:
        cfg["clips"]["target_duration"] = int(rank_dur.get("target", 15))
        cfg["clips"]["min_duration"] = int(rank_dur.get("min", 12))
        cfg["clips"]["max_duration"] = int(rank_dur.get("max", 18))
    else:
        cfg["clips"]["target_duration"] = 12
        cfg["clips"]["min_duration"] = 8
        cfg["clips"]["max_duration"] = 16
    # Assembled ranking length = title + all items; do not inherit quality.yaml's 30s clip cap as a total.
    cfg["clips"]["max_ranking_duration"] = 180
    # A Gemini quota blip or a 7.5 review score must not delete a finished ranking part
    cfg["clips"]["review_soft"] = bool(rk.get("review_soft", True))
    cfg["clips"]["review_soft_min"] = float(rk.get("review_soft_min", 6.0) or 6.0)
    # Per-part Gemini video review is the slowest step (≈1–5 min under free-tier 503s)
    cfg["clips"]["skip_final_review"] = not bool(rk.get("final_review", False))
    cfg["clips"]["ranking_part"] = True  # montage: 2–4 moments, ~20–25s per part
    cfg["_no_watermark"] = True  # watermark the final assembled ranking once
    cs = cfg["clips"].setdefault("copyright_safe", {})
    cs["enabled"] = True

    topic = (topic or "").strip() or suggest_topic(niche_id, n, cfg)
    _log(f"[ranking] topic={topic!r} niche={niche_id} n={n}")

    spares = int(rk.get("spare_items", 2) or 0)
    min_parts = max(3, min(n, int(rk.get("min_parts", 3) or 3)))
    items = collect_rank_items(
        niche_id, topic, n=n + spares, cfg=cfg, log=log,
        deadline=deadline, cancel_event=cancel_event,
        max_probe=int(rk.get("max_probe", 7) or 7) + spares,
    )
    # collect_rank_items sets the GLOBAL procutil cancel flag to stop leftover probes once it has
    # enough. Left set, it made every later stage think the job was cancelled and killed tracked
    # child processes mid-render. Clear it unless the user actually stopped / the budget is gone.
    if not (cancel_event is not None and cancel_event.is_set()) and time.time() < deadline:
        procutil.clear_cancel()
    if _timed_out() and len(items) >= min_on_timeout:
        _log(f"[ranking] time budget hit with {len(items)} items — rendering partial Top {len(items)}")
        n = len(items)
    if items and all((it.candidate.extra or {}).get("origin") == "fan" for it in items) \
            and not re.search(r"(?i)fan[\s-]*made|fanmade", topic):
        topic = re.sub(r"(?i)^(top\s*\d+\s+)(anime\s+)?", lambda m: f"{m.group(1)}Fan-Made {m.group(2) or 'Anime '}", topic) \
            if re.match(r"(?i)top\s*\d+", topic) else f"Top {n} Fan-Made {topic}"
        _log(f"[ranking] fan-made set → topic={topic!r}")
    if False:
        pass
    elif len(items) < min_parts:
        _log(f"[ranking] not enough items ({len(items)}); need >={min_parts}")
        return None
    else:
        _log(f"[ranking] {len(items)} candidate(s) for Top {n} (spares={max(0, len(items) - n)})")

    out_root = Path(cfg["clips"]["output_dir"]) / "ranking"
    out_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    work = out_root / f"_work_{stamp}"
    work.mkdir(parents=True, exist_ok=True)

    # Fast path: trust light-analyze segments. Skip second full find_highlights (was 1–4 min each).
    from .scene_vlm import confirm_reaction_moment

    survivors: list[RankItem] = []
    topic_type = detect_topic_type(topic)
    need_reaction = niche_id == "reactions" or topic_type == "reactions"
    for item in items:
        if _timed_out():
            if len(survivors) >= min_on_timeout:
                _log(f"[ranking] budget hit during gate — keeping {len(survivors)} survivor(s)")
                break
            _log("[ranking] budget hit during gate with too few survivors")
            break
        _log(f"[ranking] gate {item.candidate.title[:50]} ({_format_views(item.views)})")
        if need_reaction:
            try:
                gate = confirm_reaction_moment(
                    item.path, item.segment.start, item.segment.end, cfg=cfg,
                    title=item.candidate.title, require_trailer=True,
                )
                _log(f"[ranking] gate @{item.segment.start:.1f}: ok={gate.get('ok')} emo={gate.get('emotion')}")
                if not gate.get("ok"):
                    _log(f"[ranking] DROPPED {item.candidate.id} (reaction gate)")
                    continue
            except Exception as e:
                _log(f"[ranking] gate err: {e}")
                continue
        else:
            _log(f"[ranking] soft-keep fill-crop @{item.segment.start:.1f}s ({item.segment.reason})")
        survivors.append(item)

    if len(survivors) < min_parts:
        _log(f"[ranking] too few survivors ({len(survivors)}); need >={min_parts}")
        return None
    if _timed_out() and len(survivors) >= min_on_timeout:
        _log(f"[ranking] rendering partial with {len(survivors)} survivors after budget")

    total = min(len(survivors), n)
    # Assign contiguous ranks: first survivor = #total (lowest), last = #1 (best)
    # Preserve existing order (collect_rank_items sorts ascending views → worst first)
    for i, item in enumerate(survivors):
        item.rank = total - i

    import re as _re
    topic = _re.sub(r"(?i)\btop\s*\d+\b", f"Top {total}", topic)
    if not _re.search(r"(?i)\btop\s*\d+", topic):
        topic = f"Top {total} {topic}" if topic else f"Top {total} moments"
    _log(f"[ranking] final topic={topic!r} count={total}")

    bg_item = survivors[-1]
    title_card, _ = _render_title_card(
        topic, work, cfg,
        footage=bg_item.path,
        footage_t=float(getattr(bg_item.segment, "start", 0.0) or 0.0),
    )
    rendered_items: list[RankItem] = []
    rendered_paths: list[Path] = []
    failures: list[str] = []
    assemble_reserve = float(rk.get("assemble_reserve_seconds", 150) or 150)
    part_times: list[float] = []
    # Best (most-viewed) first so spares replace the weakest picks, then try next candidate on failure
    queue = sorted(survivors, key=lambda it: it.views, reverse=True)
    for k, item in enumerate(queue):
        if len(rendered_items) >= n:
            break
        if cancel_event is not None and cancel_event.is_set() or procutil.cancelled():
            _log("[ranking] stopped — no more parts")
            break
        left = deadline - time.time()
        est = (sum(part_times) / len(part_times)) if part_times else 120.0
        if left < assemble_reserve + est:
            _log(f"[ranking] budget: {left:.0f}s left, part≈{est:.0f}s + assemble {assemble_reserve:.0f}s — "
                 f"stopping with {len(rendered_items)} part(s)")
            break
        _log(f"[ranking] render part {len(rendered_items) + 1}/{n} (candidate {k + 1}/{len(queue)}): "
             f"{item.candidate.title[:50]} ({_format_views(item.views)})")
        t0 = time.time()
        seg, why = _render_part_logged(item, cfg, work, with_vo=with_vo)
        part_times.append(time.time() - t0)
        if seg:
            rendered_items.append(item)
            rendered_paths.append(seg)
            _log(f"[ranking] part OK {item.candidate.id} in {part_times[-1]:.0f}s")
        else:
            failures.append(f"{item.candidate.id}: {why}")
            _log(f"[ranking] PART FAILED {item.candidate.id} ({part_times[-1]:.0f}s) — {why} — trying next candidate")

    if failures:
        _log("[ranking] part failures: " + " | ".join(failures))
    if len(rendered_items) < min_parts:
        _log(f"[ranking] too few rendered parts ({len(rendered_items)}); need >={min_parts}")
        return None
    # Back to ascending views (lowest first → #1 last)
    order = sorted(range(len(rendered_items)), key=lambda i: rendered_items[i].views)
    rendered_items = [rendered_items[i] for i in order]
    rendered_paths = [rendered_paths[i] for i in order]

    # Re-rank / retitle from what ACTUALLY rendered (no missing #N)
    total = len(rendered_items)
    for i, item in enumerate(rendered_items):
        item.rank = total - i
    topic = _re.sub(r"(?i)\btop\s*\d+\b", f"Top {total}", topic)
    if not _re.search(r"(?i)\btop\s*\d+", topic):
        topic = f"Top {total} {topic}"
    _log(f"[ranking] post-render topic={topic!r} count={total}")
    survivors = rendered_items

    # Rebuild title card + re-burn rank overlays with final numbers
    bg_item = survivors[-1]
    title_card, _ = _render_title_card(
        topic, work, cfg,
        footage=bg_item.path,
        footage_t=float(getattr(bg_item.segment, "start", 0.0) or 0.0),
    )
    parts: list[Path] = [title_card]
    for item, base in zip(survivors, rendered_paths):
        base = _ensure_part_captions(
            base, cfg=cfg, niche_id=niche_id, work=work, label=f"#{item.rank} {item.label}",
        )
        n_dlg = _ass_dialogue_count(base.with_suffix(".ass"))
        if n_dlg <= 0:
            # Music-only fight scenes legitimately have no dialogue — keep the part, just no captions
            _log(f"[ranking] note #{item.rank} {item.label}: no dialogue to caption (music/SFX only) — keeping part")
        fixed = work / f"rank_{item.rank:02d}_{item.candidate.id}_final.mp4"
        # Re-render overlay from original source clip via _render_rank_segment again would
        # re-call Gemini; instead redraw number/label on the already-rendered base.
        rank_txt = _escape_drawtext(f"#{item.rank}")
        label = _escape_drawtext(_fit_header_words(item.label, 28))
        views = _escape_drawtext(_format_views(item.views))
        # Outline-only rank labels (no black box over faces)
        vf = (
            f""
            f"drawtext=text='{rank_txt}':fontsize=72:fontcolor=yellow:"
            f"borderw=5:bordercolor=black:x=36:y=48,"
            f"drawtext=text='{label}':fontsize=36:fontcolor=white:"
            f"borderw=3:bordercolor=black:x=36:y=205,"
            f"drawtext=text='{views}':fontsize=30:fontcolor=yellow:"
            f"borderw=3:bordercolor=black:x=36:y=252"
        )
        pr = subprocess.run(
            ["ffmpeg", "-y", "-i", str(base), "-vf", vf,
             "-af", "aresample=48000,aformat=sample_rates=48000:channel_layouts=stereo",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30",
             "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "192k", str(fixed)],
            capture_output=True, text=True, check=False,
        )
        parts.append(fixed if fixed.is_file() else base)

    # Normalize EVERY part to 30fps + 48kHz stereo, then concat via filter
    # (demuxer -c copy with mixed sample rates = chipmunk / A-V desync).
    final = out_root / f"{niche_id}_top{total}_{stamp}.mp4"
    width = int((cfg.get("clips") or {}).get("width", 1080))
    height = int((cfg.get("clips") or {}).get("height", 1920))

    def _probe_dur(pp: Path) -> float:
        try:
            import json as _j
            pr = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "json", str(pp)],
                capture_output=True, text=True, check=False,
            )
            return float((_j.loads(pr.stdout).get("format") or {}).get("duration") or 0)
        except Exception:
            return 0.0

    def _probe_av(pp: Path) -> dict:
        try:
            import json as _j
            pr = subprocess.run(
                ["ffprobe", "-v", "error",
                 "-show_entries", "stream=codec_type,sample_rate,r_frame_rate,duration",
                 "-show_entries", "format=duration", "-of", "json", str(pp)],
                capture_output=True, text=True, check=False,
            )
            return _j.loads(pr.stdout or "{}")
        except Exception:
            return {}

    norm_parts: list[Path] = []
    for i, part in enumerate(parts):
        src_dur = _probe_dur(part)
        npth = work / f"norm_{i:02d}.mp4"
        # Force exact canvas, 30fps, 48kHz stereo — no pad color between panels (already 9:16)
        cmd_n = [
            "ffmpeg", "-y", "-i", str(part),
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
            "-filter_complex",
            f"[0:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"fps=30,setsar=1,format=yuv420p,setpts=PTS-STARTPTS[v];"
            f"[0:a]aformat=sample_rates=48000:channel_layouts=stereo,"
            f"aresample=48000,asetpts=PTS-STARTPTS[a0];"
            f"[a0][1:a]amix=inputs=2:duration=first:dropout_transition=0[a]",
            "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-r", "30",
            "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "192k",
            "-shortest", str(npth),
        ]
        pr = subprocess.run(cmd_n, capture_output=True, text=True, check=False)
        if pr.returncode != 0 or not npth.is_file():
            _log(f"[ranking] norm {i} failed: {(pr.stderr or '')[-300:]}")
            cmd_n2 = [
                "ffmpeg", "-y", "-i", str(part),
                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                "-vf", f"scale={width}:{height},fps=30,format=yuv420p,setsar=1",
                "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "libx264", "-preset", "veryfast", "-r", "30",
                "-c:a", "aac", "-ar", "48000", "-ac", "2", "-shortest",
                str(npth),
            ]
            subprocess.run(cmd_n2, capture_output=True, check=False)
        if not npth.is_file():
            continue
        nd = _probe_dur(npth)
        # Assert normalized duration ~= source part duration (±0.15s); title card is short
        if src_dur > 2.0 and abs(nd - src_dur) > 0.15:
            _log(f"[ranking] WARN dur mismatch part{i}: src={src_dur:.2f}s norm={nd:.2f}s — re-encode with -t")
            # Hard trim/pad to src_dur
            fixed = work / f"norm_{i:02d}_fixed.mp4"
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(npth), "-t", f"{src_dur:.3f}",
                 "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                 "-filter_complex",
                 f"[0:v]fps=30,format=yuv420p,setpts=PTS-STARTPTS[v];"
                 f"[0:a]aformat=sample_rates=48000:channel_layouts=stereo[a0];"
                 f"[a0][1:a]amix=inputs=2:duration=first[a]",
                 "-map", "[v]", "-map", "[a]",
                 "-c:v", "libx264", "-preset", "veryfast", "-r", "30",
                 "-c:a", "aac", "-ar", "48000", "-ac", "2", "-shortest",
                 str(fixed)],
                capture_output=True, check=False,
            )
            if fixed.is_file():
                npth = fixed
                nd = _probe_dur(npth)
        info = _probe_av(npth)
        _log(f"[ranking] norm part{i}: dur={nd:.3f}s (src={src_dur:.3f}) {info.get('streams', [{}])}")
        # Assert ±0.1s for clip-length parts
        if src_dur > 2.0 and abs(nd - src_dur) > 0.1:
            _log(f"[ranking] ASSERT FAIL part{i} duration {nd:.3f} vs window {src_dur:.3f}")
        norm_parts.append(npth)

    if len(norm_parts) < 2:
        _log("[ranking] not enough normalized parts")
        return None

    # concat FILTER (not demuxer copy) so timebases/sample rates stay aligned
    cmd = ["ffmpeg", "-y"]
    for pp in norm_parts:
        cmd += ["-i", str(pp)]
    n = len(norm_parts)
    fc = "".join(f"[{i}:v][{i}:a]" for i in range(n)) + f"concat=n={n}:v=1:a=1[v][a]"
    cmd += [
        "-filter_complex", fc,
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-r", "30",
        "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "192k",
        "-movflags", "+faststart",
        str(final),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0 or not final.is_file():
        _log(f"[ranking] concat filter failed: {(proc.stderr or '')[-600:]}")
        return None
    fin_info = _probe_av(final)
    _log(f"[ranking] final probe: {fin_info}")

    # Duration must equal sum of parts (title + every rank). Never apply clips.max_duration
    # (e.g. quality.yaml's 30s) to the assembled ranking — that cut mid-list and dropped #1.
    try:
        import json as _json
        part_durs = [_probe_dur(pp) for pp in norm_parts]
        expected = float(sum(part_durs))
        pr = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(final)],
            capture_output=True, text=True, check=False,
        )
        dur = float((_json.loads(pr.stdout).get("format") or {}).get("duration") or 0)
        _log(
            f"[ranking] duration check: final={dur:.3f}s expected_sum={expected:.3f}s "
            f"parts={[round(d,3) for d in part_durs]} ranks={[it.rank for it in survivors]}"
        )
        if expected > 1.0 and abs(dur - expected) > 0.75:
            _log(
                f"[ranking] ASSERT FAIL duration {dur:.3f} != sum(parts) {expected:.3f} "
                f"— refusing truncated ranking"
            )
            return None
        # Safety only for extreme length (never a 30s short-form cap)
        if dur > 180:
            trimmed = final.with_name(final.stem + "_180s.mp4")
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(final), "-t", "180", "-c", "copy", str(trimmed)],
                capture_output=True, check=False,
            )
            if trimmed.is_file():
                final.unlink(missing_ok=True)
                trimmed.rename(final)
                dur = min(dur, 180.0)
        _log(f"[ranking] final {final} ({dur:.1f}s) includes ranks {[it.rank for it in survivors]}")
    except Exception as e:
        _log(f"[ranking] duration verify skip: {e}")

    # Metadata
    hashtags = " ".join((cfg.get("hashtags") or [])[:8])
    meta = {
        "mode": "ranking",
        "niche": niche_id,
        "topic": topic,
        "items": [
            {
                "rank": it.rank,
                "title": it.candidate.title,
                "views": it.views,
                "views_label": _format_views(it.views),
                "label": it.label,
                "url": it.candidate.url,
                "id": it.candidate.id,
            }
            for it in survivors
        ],
        "hashtags": hashtags,
        "created": datetime.now().isoformat(),
    }
    final.with_suffix(".ranking.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    final.with_suffix(".meta.txt").write_text(
        (
            f"MODE: ranking\nNICHE: {niche_id}\nTOPIC: {topic}\n"
            + "\n".join(
                f"#{it.rank} {it.candidate.title} — {_format_views(it.views)} — {it.label}"
                for it in survivors
            )
            + f"\nHASHTAGS: {hashtags}\n"
        ),
        encoding="utf-8",
    )
    from .edit import apply_watermark
    apply_watermark(final, cfg)
    return final
