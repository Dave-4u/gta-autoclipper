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


def _cookie_args(cfg: dict[str, Any] | None = None) -> list[str]:
    """Build yt-dlp cookie flags from config (file path or browser)."""
    if not cfg:
        return []
    dl = cfg.get("download") or {}
    args: list[str] = []
    browser = str(dl.get("cookies_from_browser") or "").strip()
    if browser:
        args.extend(["--cookies-from-browser", browser])
    cookies = str(dl.get("cookies") or dl.get("cookies_file") or "").strip()
    if cookies:
        cp = Path(cookies)
        if cp.is_file():
            args.extend(["--cookies", str(cp)])
        else:
            print("[discover] configured cookies file missing (path not logged)")
    return args


def _run_ytdlp_json(
    args: list[str],
    timeout: int = 90,
    *,
    cookie_args: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Run yt-dlp JSON dump. Never raises on timeout — returns [] so callers can continue."""
    cmd = [
        "yt-dlp",
        "--flat-playlist",
        "--dump-single-json",
        "--ignore-no-formats-error",
        "--no-warnings",
        "--socket-timeout", "15",
        "--retries", "2",
        "--extractor-retries", "2",
        "--skip-unavailable-fragments",
        # Reduce bot/auth friction on search tabs
        "--extractor-args", "youtube:player_client=web,android",
        *(cookie_args or []),
        *args,
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=max(8, int(timeout)), check=False
        )
    except FileNotFoundError as e:
        raise RuntimeError("yt-dlp not found. Install with: pip install yt-dlp") from e
    except subprocess.TimeoutExpired:
        print(f"[discover] yt-dlp timed out after {timeout}s — skipping this query")
        return []
    except Exception as e:
        print(f"[discover] yt-dlp error_type={type(e).__name__}")
        return []
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if proc.returncode != 0 and not stdout:
        err = stderr[:300]
        if err:
            from .download_watch import diagnostic_reason
            print(f"[discover] status=failed reason={diagnostic_reason(stderr)}")
        return []
    if not stdout and stderr:
        # Sometimes yt-dlp prints only to stderr on block
        from .download_watch import diagnostic_reason
        print(f"[discover] status=failed reason={diagnostic_reason(stderr)}")
        return []
    lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
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
            return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y%m%d")
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


def _url_to_id(url: str) -> str:
    m = re.search(r"(?:v=|/shorts/|youtu\.be/)([\w-]{6,})", url)
    if m:
        return m.group(1)
    return hashlib.sha1(url.encode()).hexdigest()[:11]


def _entry_to_candidate(e: dict[str, Any], source: str) -> Candidate | None:
    vid = e.get("id") or e.get("url")
    if not vid:
        return None
    vid = str(vid)
    if len(vid) > 20 and "http" in vid:
        vid = _url_to_id(vid)
    title = (e.get("title") or e.get("fulltitle") or "untitled").strip()
    url = e.get("webpage_url") or e.get("url") or f"https://www.youtube.com/watch?v={vid}"
    if isinstance(url, str) and not url.startswith("http"):
        url = f"https://www.youtube.com/watch?v={vid}"
    dur = e.get("duration")
    try:
        dur_f = float(dur) if dur is not None else None
    except (TypeError, ValueError):
        dur_f = None
    return Candidate(
        id=vid,
        title=title,
        url=str(url),
        duration=dur_f,
        source=source,
        view_count=_parse_views(e),
        upload_date=_parse_upload(e),
        like_count=_parse_int(e, "like_count", "likes"),
        comment_count=_parse_int(e, "comment_count", "comments"),
        extra={
            "uploader_id": e.get("uploader_id") or "",
            "channel_id": e.get("channel_id") or "",
            "channel": e.get("channel") or e.get("uploader") or "",
            "description": (e.get("description") or "")[:500],
            "tags": list(e.get("tags") or [])[:30],
        },
    )


def search_youtube(
    keyword: str,
    n: int = 8,
    *,
    prefer_date: bool = True,
    timeout: int = 90,
    cookie_args: list[str] | None = None,
) -> list[Candidate]:
    """Search YouTube via yt-dlp. Soft-fails on hang; retries without cookies if needed."""
    n = max(1, min(int(n), 50))  # flat search: ytsearch30 is still one fast request
    t = max(12, min(int(timeout), 55))
    ca = cookie_args or []
    query = f"ytsearch{n}:{keyword}"
    entries = _run_ytdlp_json([query], timeout=t, cookie_args=ca)
    # Cookies can make ytsearch worse when stale — retry bare
    if not entries and ca:
        print(f"[discover] retry search without cookies for {keyword!r}")
        entries = _run_ytdlp_json([query], timeout=t, cookie_args=None)
    if not entries and n > 3:
        entries = _run_ytdlp_json([f"ytsearch{min(n, 10)}:{keyword}"], timeout=min(35, t), cookie_args=None)
    if prefer_date and entries:
        from urllib.parse import quote_plus

        url = (
            "https://www.youtube.com/results?search_query="
            + quote_plus(keyword)
            + "&sp=CAI%253D"
        )
        extra = _run_ytdlp_json(
            ["--playlist-end", str(min(n, 5)), url], timeout=min(30, t), cookie_args=ca or None
        )
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
    if not out:
        print(f"[discover] 0 results for query {keyword!r}")
    else:
        print(f"[discover] {len(out)} results for {keyword!r}")
    return out


def search_trending_related(
    keyword: str,
    n: int = 6,
    *,
    cookie_args: list[str] | None = None,
) -> list[Candidate]:
    """Approx 'new releases / trending' via date-sorted results + this week phrasing."""
    variants = [
        f"{keyword} this week",
        f"{keyword} new",
        f"trending {keyword}",
    ]
    out: list[Candidate] = []
    seen: set[str] = set()
    for kw in variants[:2]:
        for c in search_youtube(
            kw, n=max(3, n // 2), prefer_date=False, timeout=25, cookie_args=cookie_args
        ):
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
        local = Path(line).expanduser()
        if not local.is_absolute():
            local = p.parent / local
        if not line.startswith("http") and local.is_file():
            import hashlib
            out.append(Candidate(id="local_" + hashlib.sha256(str(local.resolve()).encode()).hexdigest()[:12],
                                 title=local.stem, url=str(local.resolve()), duration=None,
                                 source="local", extra={"source_kind": "local_supplied"}))
            continue
        vid = _url_to_id(line)
        out.append(
            Candidate(
                id=vid,
                title=f"link-{i+1}",
                url=line if line.startswith("http") else f"https://www.youtube.com/watch?v={vid}",
                duration=None,
                source="links",
            )
        )
    return out


def smart_rank_score(
    c: Candidate,
    *,
    sort_by: str = "recent_views",
    niche_id: str | None = None,
) -> float:
    views = float(c.view_count or 0)
    likes = float(c.like_count or 0)
    comments = float(c.comment_count or 0)
    age = _age_days(c.upload_date)
    age = age if age is not None else 30.0
    velocity = views / max(1.0, age)
    freshness = max(0.0, 1.0 - age / 60.0)
    engagement = 0.0
    if views > 0:
        engagement = min(1.0, (likes + comments * 3) / max(views * 0.05, 1.0))
    title = (c.title or "").lower()
    hook_words = (
        "wait", "insane", "crazy", "omg", "no way", "gone wrong", "vs", "fail",
        "reaction", "reacts", "trailer", "fight", "clutch", "meltdown",
    )
    hook = sum(1 for w in hook_words if w in title) / 4.0
    hook = min(1.0, hook)
    src_bonus = {"trending": 0.15, "links": 0.05, "search": 0.0}.get(c.source, 0.0)
    jitter = (int(hashlib.md5(c.id.encode()).hexdigest()[:6], 16) % 1000) / 10000.0

    if sort_by == "recent":
        score = freshness * 5 + hook + src_bonus + jitter
    elif sort_by == "views":
        score = math.log10(views + 10) + engagement + hook * 0.5 + jitter
    else:
        score = (
            math.log10(velocity + 10) * 1.4
            + freshness * 2.2
            + engagement * 1.5
            + hook * 1.2
            + src_bonus
            + jitter
        )
    return float(score)


def discover(cfg: dict[str, Any], history) -> list[Candidate]:
    from .download_watch import SourceHealth
    cfg.setdefault("_source_health", SourceHealth())
    cfg.setdefault("_provider_health", SourceHealth(limit=2))
    search_cfg = cfg.get("search", {})
    keywords = list(search_cfg.get("keywords") or [
        "GTA 6 trailer",
        "anime fight scene",
        "funny viral video",
    ])
    if cfg.get("niche_id") in {"anime", "animation"}:
        from .source_policy import source_queries
        keywords = source_queries(cfg["niche_id"], bool(cfg.get("_fan_sources")))
    random.shuffle(keywords)
    n = int(search_cfg.get("results_per_keyword", 8))
    min_d = float(search_cfg.get("min_source_duration", 15))
    max_d = float(search_cfg.get("max_source_duration", 900))
    sort_by = str(search_cfg.get("sort_by", "recent_views"))
    max_age = float(search_cfg.get("max_age_days", 45) or 0)
    prefer_date = bool(search_cfg.get(
        "prefer_date_search",
        sort_by in ("recent", "recent_views", "smart"),
    ))
    use_trending = bool(search_cfg.get("use_trending", True))
    search_timeout = int(search_cfg.get("search_timeout_seconds", 60) or 60)
    # ignore_seen: history never filters. seen_fallback_min: if fewer fresh results than this,
    # top up with already-seen ones (fresh first). 0 = strict (default behaviour).
    ignore_seen = bool(search_cfg.get("ignore_seen", False))
    seen_fallback_min = int(search_cfg.get("seen_fallback_min", 0) or 0)
    seen_pool: list[Candidate] = []
    cookie_args = _cookie_args(cfg)
    if cookie_args:
        print("[discover] configured authentication enabled (details not logged)")

    seen_ids: set[str] = set()
    candidates: list[Candidate] = []

    for c in read_links_file(cfg["download"]["links_file"]):
        if history.seen(c.id) or c.id in seen_ids:
            continue
        seen_ids.add(c.id)
        candidates.append(c)

    k_take = min(len(keywords), max(3, int(search_cfg.get("keywords_per_run", 5))))
    chosen_kw = keywords[:k_take]

    for kw in chosen_kw:
        print(f"[discover] searching: {kw}")
        print(f"[discover] search timeout={search_timeout}s prefer_date={prefer_date} trending={use_trending}")
        try:
            batch = search_youtube(
                kw, n=n, prefer_date=prefer_date, timeout=search_timeout, cookie_args=cookie_args
            )
        except Exception as e:
            print(f"[discover] search failed error_type={type(e).__name__}")
            batch = []
        if use_trending and batch is not None:
            try:
                batch.extend(
                    search_trending_related(kw, n=max(3, n // 2), cookie_args=cookie_args)
                )
            except Exception as e:
                print(f"[discover] trending search failed error_type={type(e).__name__}")
        skipped_dur = skipped_age = skipped_seen = 0
        for c in batch or []:
            if c.id in seen_ids:
                continue
            was_seen = (not ignore_seen) and history.seen(c.id)
            if c.duration is not None and (c.duration < min_d or c.duration > max_d):
                skipped_dur += 1
                continue
            if max_age > 0:
                age = _age_days(c.upload_date)
                if age is not None and age > max_age:
                    skipped_age += 1
                    continue
            c.rank_score = smart_rank_score(c, sort_by=sort_by, niche_id=str(cfg.get("niche_id") or "") or None)
            seen_ids.add(c.id)
            if was_seen:
                skipped_seen += 1
                seen_pool.append(c)
                continue
            candidates.append(c)
        if (batch or []) and not any(c.source != "links" for c in candidates[-len(batch or []):]):
            print(
                f"[discover] filtered all for {kw!r}: "
                f"seen={skipped_seen} duration={skipped_dur} age={skipped_age} "
                f"(min_d={min_d} max_d={max_d} max_age={max_age})"
            )

    links = [c for c in candidates if c.source == "links"]
    searched = [c for c in candidates if c.source != "links"]
    searched.sort(key=lambda c: c.rank_score, reverse=True)
    if seen_fallback_min and len(searched) < seen_fallback_min and seen_pool:
        seen_pool.sort(key=lambda c: c.rank_score, reverse=True)
        add = seen_pool[: seen_fallback_min - len(searched) + 5]
        print(f"[discover] only {len(searched)} fresh — adding {len(add)} previously-seen candidate(s)")
        fresh = searched
    else:
        add, fresh = [], None

    if len(searched) > 3:
        top = searched[: max(6, len(searched) // 2)]
        rest = searched[len(top) :]
        random.shuffle(top)
        searched = top + rest

    if fresh is not None:
        searched = searched + add  # fresh always ahead of seen
    out = links + searched
    # Manually supplied YouTube links need the same provenance metadata as search results.
    # Bound metadata-only extraction; never trust a raw URL's placeholder title.
    if cfg.get("niche_id") in {"anime", "animation"}:
        from urllib.parse import urlparse
        for c in [x for x in out if x.source == "links"][:3]:
            host = (urlparse(c.url).hostname or "").lower()
            if host not in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}:
                continue
            try:
                entries = _run_ytdlp_json([c.url], timeout=30, cookie_args=cookie_args)
                if entries:
                    resolved = _entry_to_candidate(entries[0], "links")
                    if resolved:
                        c.title, c.extra, c.duration = resolved.title, resolved.extra, resolved.duration
            except Exception:
                print("[source-policy] Link metadata unavailable; source will not bypass verification")
    from .source_policy import enforce_source
    out = [c for c in out if enforce_source(c, cfg)]
    if not out and cfg.get("niche_id") in {"anime", "animation"}:
        print("[source-policy] No eligible episode scenes. Supply a local original file or authorize an exact publisher channel in source_policy.authorized_channels; no edit fallback. Copyright matches can still occur.")
    print(f"[discover] {len(out)} candidates after smart rank")
    return out
