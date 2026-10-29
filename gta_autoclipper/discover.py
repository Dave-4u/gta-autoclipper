"""Discover GTA 6 / GTA RP content via yt-dlp search and links.txt."""

from __future__ import annotations

import hashlib
import json
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
    source: str  # "search" | "links"
    view_count: int = 0
    upload_date: str | None = None  # YYYYMMDD
    rank_score: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)


def _run_ytdlp_json(args: list[str], timeout: int = 180) -> list[dict[str, Any]]:
    cmd = ["yt-dlp", "--flat-playlist", "--dump-single-json", *args]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError as e:
        raise RuntimeError(
            "yt-dlp not found. Install with: pip install yt-dlp"
        ) from e
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


def _rank_score(
    views: int, upload_date: str | None, sort_by: str
) -> float:
    """Higher is better. Combines recency + views for Shorts clipping."""
    age = _age_days(upload_date)
    # Recency score: 1.0 if today, decays over ~90 days
    if age is None:
        recency = 0.35
    else:
        recency = max(0.0, 1.0 - (age / 90.0))
    # Views: log-scale
    import math

    view_score = math.log10(max(views, 1) + 1) / 8.0  # ~1.0 at 100M
    view_score = min(1.5, view_score)
    if sort_by == "views":
        return view_score * 2 + recency * 0.3
    if sort_by == "recent":
        return recency * 2 + view_score * 0.3
    # recent_views (default): both matter — good for clipping payouts
    return recency * 1.2 + view_score * 1.0


def search_youtube(
    keyword: str, n: int = 8, *, prefer_date: bool = True
) -> list[Candidate]:
    """Search YouTube via ytsearchN (yt-dlp). Recency applied in rank_score.

    Note: older `ytsearchdateN:` is not supported on all yt-dlp builds, so we
    always use ytsearchN and sort ourselves by upload_date + views.
    When prefer_date, also try a date-sorted YouTube results URL as a supplement.
    """
    query = f"ytsearch{n}:{keyword}"
    entries = _run_ytdlp_json([query])
    if prefer_date:
        # CAI%253D = sort by upload date (YouTube sp param)
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
        vid = e.get("id") or ""
        if not vid:
            continue
        url = e.get("url") or e.get("webpage_url") or ""
        if url and not url.startswith("http"):
            url = f"https://www.youtube.com/watch?v={vid}"
        if not url:
            url = f"https://www.youtube.com/watch?v={vid}"
        title = e.get("title") or vid
        dur = e.get("duration")
        try:
            dur_f = float(dur) if dur is not None else None
        except (TypeError, ValueError):
            dur_f = None
        views = _parse_views(e)
        upload = _parse_upload(e)
        out.append(
            Candidate(
                id=str(vid),
                title=str(title),
                url=url,
                duration=dur_f,
                source="search",
                view_count=views,
                upload_date=upload,
            )
        )
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
        out.append(
            Candidate(
                id=vid,
                title=f"link_{i+1}",
                url=line,
                duration=None,
                source="links",
                view_count=0,
                upload_date=None,
                rank_score=9999.0,  # user links always first
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
    keywords = search_cfg.get("keywords") or [
        "GTA 6 trailer",
        "GTA 6 news",
        "GTA RP funny moments",
    ]
    n = int(search_cfg.get("results_per_keyword", 8))
    min_d = float(search_cfg.get("min_source_duration", 15))
    max_d = float(search_cfg.get("max_source_duration", 900))
    sort_by = str(search_cfg.get("sort_by", "recent_views"))
    max_age = float(search_cfg.get("max_age_days", 90) or 0)
    prefer_date = sort_by in ("recent", "recent_views")

    seen_ids: set[str] = set()
    candidates: list[Candidate] = []

    for c in read_links_file(cfg["download"]["links_file"]):
        if history.seen(c.id) or c.id in seen_ids:
            continue
        seen_ids.add(c.id)
        candidates.append(c)

    for kw in keywords:
        print(f"[discover] searching: {kw}")
        for c in search_youtube(kw, n=n, prefer_date=prefer_date):
            if history.seen(c.id) or c.id in seen_ids:
                continue
            if c.duration is not None and (c.duration < min_d or c.duration > max_d):
                continue
            if max_age > 0:
                age = _age_days(c.upload_date)
                if age is not None and age > max_age:
                    continue
            c.rank_score = _rank_score(c.view_count, c.upload_date, sort_by)
            seen_ids.add(c.id)
            candidates.append(c)

    # links (score 9999) stay on top; rest by rank_score desc
    links = [c for c in candidates if c.source == "links"]
    searched = [c for c in candidates if c.source != "links"]
    searched.sort(key=lambda c: c.rank_score, reverse=True)
    return links + searched
