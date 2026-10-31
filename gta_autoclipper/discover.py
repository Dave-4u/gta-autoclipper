"""Smart discovery: yt-dlp search + trending/new, velocity/freshness/engagement scoring."""

from __future__ import annotations

import hashlib
import json
import math
import random
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class Candidate:
    id: str
    title: str
    url: str
    duration: float | None
    source: str  # "search" | "links" | "trending"
    view_count: int = 0
    upload_date: str | None = None  # YYYYMMDD
    rank_score: float = 0.0
    like_count: int = 0
    comment_count: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


def _run_ytdlp_json(args: list[str], timeout: int = 180) -> list[dict[str, Any]]:
    cmd = ["yt-dlp", "--flat-playlist", "--dump-single-json", *args]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError as e:
        raise RuntimeError("yt-dlp not found. Install with: pip install yt-dlp") from e
    if proc.returncode != 0 and not proc.stdout.strip():
        return []
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    results: list[dict[str, Any]] = []
    for ln in lines:
        try:
            obj = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("_type") == "playlist" and "entries" in obj:
            for ent in obj["entries"] or []:
                if ent:
                    results.append(ent)
        else:
            results.append(obj)
    return results


def _parse_views(e: dict[str, Any]) -> int:
    for key in ("view_count", "views", "play_count"):
        v = e.get(key)
        if v is None:
            continue
        try:
            return int(v)
        except (TypeError, ValueError):
            pass
    return 0


def _parse_int(e: dict[str, Any], *keys: str) -> int:
    for key in keys:
        v = e.get(key)
        if v is None:
            continue
        try:
            return int(v)
        except (TypeError, ValueError):
            pass
    return 0


def _parse_upload(e: dict[str, Any]) -> str | None:
    ud = e.get("upload_date") or e.get("release_date")
    if ud and re.fullmatch(r"\d{8}", str(ud)):
        return str(ud)
    ts = e.get("timestamp") or e.get("release_timestamp")
    if ts:
        try:
            return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y%m%d")
        except (TypeError, ValueError, OSError):
            pass
    return None


def _age_days(upload_date: str | None) -> float | None:
    if not upload_date:
        return None
    try:
        dt = datetime.strptime(upload_date, "%Y%m%d").replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0
    except ValueError:
        return None


def _title_hook_score(title: str) -> float:
    t = (title or "").lower()
    score = 0.0
    hooks = (
        "wait", "insane", "crazy", "omg", "no way", "gone wrong", "vs", "fail",
        "clutch", "reaction", "leak", "trailer", "twist", "secret", "you won't",
        "pov", "caught", "exposed", "final", "boss", "edit", "new",
    )
    for h in hooks:
        if h in t:
            score += 0.35
    if "!" in (title or "") or "?" in (title or ""):
        score += 0.25
    if re.search(r"\d{4}", title or ""):
        score += 0.1
    # Length sweet spot
    n = len((title or "").split())
    if 4 <= n <= 14:
        score += 0.3
    return min(2.0, score)



def _gta_source_quality(title: str, channel: str = "", description: str = "") -> float:
    """Boost official trailers / gameplay / reactions; demote leak/meme commentary."""
    blob = f"{title} {channel} {description}".lower()
    score = 0.0
    # Strong positives
    if any(k in blob for k in ("official trailer", "rockstar games", "rockstar")):
        score += 2.2
    if "trailer" in blob and "leak" not in blob and "leaks" not in blob:
        score += 1.4
    if any(k in blob for k in ("gameplay", "walkthrough", "playthrough", "first look")):
        score += 1.3
    if any(k in blob for k in ("reaction", "reacts", "reacting", "watching")):
        score += 1.1
    # Mild positives
    if any(k in blob for k in ("lucia", "jason", "vice city", "leonida", "gta vi", "gta 6")):
        score += 0.3
    # Demote leak / meme / commentary farms
    if any(k in blob for k in ("leak", "leaks", "leaked", "datamine", "rumor")):
        score -= 1.6
    if any(k in blob for k in ("meme", "shitpost", "cats", "barber", "tiktok compilation", "ai generated")):
        score -= 1.4
    if any(k in blob for k in ("explained", "theory", "everything we know", "customization features")):
        score -= 0.8
    if "gta rp" in blob or "roleplay" in blob:
        score -= 0.6
    return score


def smart_rank_score(c: Candidate, *, sort_by: str = "recent_views", niche_id: str | None = None) -> float:
    """Combine view velocity, freshness, engagement, title hook + jitter."""
    age = _age_days(c.upload_date)
    if age is None:
        recency = 0.35
        age_safe = 14.0
    else:
        recency = max(0.0, 1.0 - (age / 45.0))  # stronger recent bias
        age_safe = max(0.5, age)

    views = max(c.view_count, 0)
    view_score = min(1.6, math.log10(views + 1) / 7.5)
    velocity = min(2.0, math.log10((views / age_safe) + 1) / 5.0)  # views per day

    likes = max(c.like_count, 0)
    comments = max(c.comment_count, 0)
    eng_raw = (likes + comments * 3) / max(views, 1)
    engagement = min(1.5, eng_raw * 80.0)  # typical YT ratios are small

    hook = _title_hook_score(c.title)
    jitter = random.uniform(0.0, 0.35)

    if sort_by == "views":
        base = view_score * 2.0 + velocity * 0.5 + hook * 0.4 + jitter
    elif sort_by == "recent":
        base = recency * 2.2 + velocity * 0.8 + hook * 0.5 + jitter
    else:
        base = None
    if base is not None:
        niche = (niche_id or "").lower()
        if niche in ("gta6", "gta", "gta_6"):
            extra = getattr(c, "extra", None) or {}
            ch = str((extra.get("channel") if isinstance(extra, dict) else "") or "")
            desc = str((extra.get("description") if isinstance(extra, dict) else "") or "")
            base += _gta_source_quality(c.title, ch, desc)
        return base
    # recent_views / smart default
    base = (
        velocity * 1.4
        + recency * 1.1
        + view_score * 0.9
        + engagement * 0.7
        + hook * 0.8
        + jitter
    )
    niche = (niche_id or "").lower()
    if niche in ("gta6", "gta", "gta_6"):
        ch = ""
        desc = ""
        extra = getattr(c, "extra", None) or {}
        if isinstance(extra, dict):
            ch = str(extra.get("channel") or extra.get("uploader") or "")
            desc = str(extra.get("description") or "")
        base += _gta_source_quality(c.title, ch, desc)
        # Also demote "leak" hook word that _title_hook_score rewarded
        if "leak" in (c.title or "").lower():
            base -= 0.35
    return base



def _is_playlist_or_channel(entry_or_url: dict | str, vid: str = "") -> bool:
    """Reject YouTube playlists/channels that hang yt-dlp for minutes."""
    if isinstance(entry_or_url, dict):
        e = entry_or_url
        vid = str(vid or e.get("id") or "")
        url = str(e.get("url") or e.get("webpage_url") or e.get("ie_key") or "")
        etype = str(e.get("_type") or e.get("ie_key") or "").lower()
        if etype in {"playlist", "channel", "youtube:tab"}:
            return True
    else:
        url = str(entry_or_url or "")
    low = (url or "").lower()
    if "playlist?list=" in low or "/playlist" in low:
        return True
    if "/channel/" in low or "/c/" in low or "/user/" in low:
        return True
    # bare @handle channel URLs
    if "youtube.com/@" in low and "watch" not in low:
        return True
    if vid.startswith(("PL", "UU", "OLAK5uy_")):
        return True
    return False

def _entry_to_candidate(e: dict[str, Any], source: str) -> Candidate | None:
    vid = e.get("id") or ""
    if not vid:
        return None
    if _is_playlist_or_channel(e, str(vid)):
        return None
    url = e.get("url") or e.get("webpage_url") or ""
    if url and not str(url).startswith("http"):
        url = f"https://www.youtube.com/watch?v={vid}"
    if not url:
        url = f"https://www.youtube.com/watch?v={vid}"
    title = e.get("title") or vid
    dur = e.get("duration")
    try:
        dur_f = float(dur) if dur is not None else None
    except (TypeError, ValueError):
        dur_f = None
    return Candidate(
        id=str(vid),
        title=str(title),
        url=str(url),
        duration=dur_f,
        source=source,
        view_count=_parse_views(e),
        upload_date=_parse_upload(e),
        like_count=_parse_int(e, "like_count", "likes"),
        comment_count=_parse_int(e, "comment_count", "comments"),
        extra={
            "channel": e.get("channel") or e.get("uploader") or "",
            "description": (e.get("description") or "")[:500],
            "tags": list(e.get("tags") or [])[:30],
        },
    )


def search_youtube(keyword: str, n: int = 8, *, prefer_date: bool = True) -> list[Candidate]:
    query = f"ytsearch{n}:{keyword}"
    entries = _run_ytdlp_json([query])
    if prefer_date:
        from urllib.parse import quote_plus

        url = (
            "https://www.youtube.com/results?search_query="
            + quote_plus(keyword)
            + "&sp=CAI%253D"
        )
        extra = _run_ytdlp_json(["--playlist-end", str(n), url])
        seen = {e.get("id") for e in entries if e.get("id")}
        for e in extra:
            if e.get("id") and e["id"] not in seen:
                entries.append(e)
                seen.add(e["id"])
    out: list[Candidate] = []
    for e in entries:
        c = _entry_to_candidate(e, "search")
        if c:
            out.append(c)
    return out


def search_trending_related(keyword: str, n: int = 6) -> list[Candidate]:
    """Approx 'new releases / trending' via date-sorted results + this week phrasing."""
    variants = [
        f"{keyword} this week",
        f"{keyword} new",
        f"trending {keyword}",
    ]
    out: list[Candidate] = []
    seen: set[str] = set()
    for kw in variants[:2]:
        for c in search_youtube(kw, n=max(3, n // 2), prefer_date=True):
            if c.id in seen:
                continue
            seen.add(c.id)
            c.source = "trending"
            out.append(c)
    return out


def read_links_file(path: str | Path) -> list[Candidate]:
    p = Path(path)
    if not p.is_file():
        return []
    out: list[Candidate] = []
    for i, line in enumerate(p.read_text(encoding="utf-8").splitlines()):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        vid = _url_to_id(line)
        if _is_playlist_or_channel(line, vid):
            print(f"[discover] skip playlist/channel link: {line[:80]}")
            continue
        out.append(
            Candidate(
                id=vid,
                title=f"link_{i+1}",
                url=line,
                duration=None,
                source="links",
                view_count=0,
                upload_date=None,
                rank_score=9999.0,
            )
        )
    return out


def _url_to_id(url: str) -> str:
    m = re.search(r"(?:v=|youtu\.be/|shorts/)([A-Za-z0-9_-]{6,})", url)
    if m:
        return m.group(1)
    m = re.search(r"/video/(\d+)", url)
    if m:
        return f"tt_{m.group(1)}"
    m = re.search(r"facebook\.com/share/r/([A-Za-z0-9]+)", url)
    if m:
        return f"fb_{m.group(1)}"
    return "u_" + hashlib.sha1(url.encode()).hexdigest()[:12]


def discover(cfg: dict[str, Any], history) -> list[Candidate]:
    search_cfg = cfg.get("search", {})
    keywords = list(search_cfg.get("keywords") or [
        "GTA 6 trailer",
        "anime fight scene",
        "funny viral video",
    ])
    # Shuffle keywords so autopilot runs vary
    random.shuffle(keywords)
    n = int(search_cfg.get("results_per_keyword", 8))
    min_d = float(search_cfg.get("min_source_duration", 15))
    max_d = float(search_cfg.get("max_source_duration", 900))
    sort_by = str(search_cfg.get("sort_by", "recent_views"))
    max_age = float(search_cfg.get("max_age_days", 45) or 0)
    prefer_date = sort_by in ("recent", "recent_views", "smart")
    use_trending = bool(search_cfg.get("use_trending", True))

    seen_ids: set[str] = set()
    candidates: list[Candidate] = []

    for c in read_links_file(cfg["download"]["links_file"]):
        if history.seen(c.id) or c.id in seen_ids:
            continue
        seen_ids.add(c.id)
        candidates.append(c)

    # Sample a subset of keywords each run for variety
    k_take = min(len(keywords), max(3, int(search_cfg.get("keywords_per_run", 5))))
    chosen_kw = keywords[:k_take]

    for kw in chosen_kw:
        print(f"[discover] searching: {kw}")
        batch = search_youtube(kw, n=n, prefer_date=prefer_date)
        if use_trending:
            batch.extend(search_trending_related(kw, n=max(3, n // 2)))
        for c in batch:
            if history.seen(c.id) or c.id in seen_ids:
                continue
            if c.duration is not None and (c.duration < min_d or c.duration > max_d):
                continue
            if max_age > 0:
                age = _age_days(c.upload_date)
                if age is not None and age > max_age:
                    continue
            c.rank_score = smart_rank_score(c, sort_by=sort_by, niche_id=str(cfg.get("niche_id") or "") or None)
            seen_ids.add(c.id)
            candidates.append(c)

    links = [c for c in candidates if c.source == "links"]
    searched = [c for c in candidates if c.source != "links"]
    searched.sort(key=lambda c: c.rank_score, reverse=True)

    # Soft random among top tier so autopilot doesn't always pick #1
    if len(searched) > 3:
        top_n = min(12, len(searched))
        head = searched[:top_n]
        weights = [max(0.05, c.rank_score) for c in head]
        # Weighted shuffle: repeatedly pick without replacement
        picked: list[Candidate] = []
        pool = list(zip(head, weights))
        while pool:
            ws = [w for _, w in pool]
            choice = random.choices(range(len(pool)), weights=ws, k=1)[0]
            picked.append(pool.pop(choice)[0])
        searched = picked + searched[top_n:]

    print(f"[discover] {len(links)+len(searched)} candidates after smart rank")
    result = links + searched
    niche_id = cfg.get("niche_id")
    if niche_id:
        from .niches import filter_candidates_for_niche
        custom = list((cfg.get("search") or {}).get("custom_keywords") or [])
        # links from user bypass soft filter but still hard-reject cross-niche
        result = filter_candidates_for_niche(str(niche_id), result, custom_keywords=custom or None)
    return result
