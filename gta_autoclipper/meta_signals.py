"""YouTube Most-replayed heatmap + comment timestamps (free, via yt-dlp / innertube)."""
from __future__ import annotations

import json
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def _run_ytdlp_info(url: str, *, comments: bool = False, max_comments: int = 40) -> dict[str, Any]:
    out_dir = Path("/tmp/ytmeta")
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        "yt-dlp", "--skip-download", "--no-warnings",
        "--js-runtimes", "deno",
        "--write-info-json",
        "-o", str(out_dir / "%(id)s"),
        url,
    ]
    if comments:
        cmd += ["--write-comments", "--extractor-args", f"youtube:max_comments={max_comments},comment_sort=top"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180, check=False)
    # find info json
    vids = sorted(out_dir.glob("*.info.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not vids:
        return {}
    try:
        return json.loads(vids[0].read_text(encoding="utf-8"))
    except Exception:
        return {}


def heatmap_from_info(info: dict[str, Any]) -> list[dict[str, float]]:
    """Normalize yt-dlp heatmap to [{start, end, value}] with value in 0..1."""
    hm = info.get("heatmap") or []
    out: list[dict[str, float]] = []
    if not isinstance(hm, list):
        return out
    for pt in hm:
        if not isinstance(pt, dict):
            continue
        try:
            start = float(pt.get("start_time", pt.get("start", 0)) or 0)
            end = float(pt.get("end_time", pt.get("end", start + 1)) or (start + 1))
            val = float(pt.get("value", pt.get("intensity", 0)) or 0)
        except (TypeError, ValueError):
            continue
        out.append({"start": start, "end": end, "value": max(0.0, min(1.0, val))})
    return out


_TS_RE = re.compile(
    r"(?:(?:(\d{1,2}):)?(\d{1,2}):(\d{2})|^|\s)(\d{1,2}:\d{2}(?::\d{2})?)|"
    r"(?:^|\s)(\d{1,3}(?:\.\d+)?)\s*s(?:ec(?:onds?)?)?\b",
    re.I,
)


def _parse_ts_token(tok: str) -> float | None:
    tok = tok.strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", tok):
        return float(tok)
    parts = tok.split(":")
    try:
        parts_f = [float(p) for p in parts]
    except ValueError:
        return None
    if len(parts_f) == 3:
        return parts_f[0] * 3600 + parts_f[1] * 60 + parts_f[2]
    if len(parts_f) == 2:
        return parts_f[0] * 60 + parts_f[1]
    return None


def comment_timestamps(info: dict[str, Any], *, limit: int = 40) -> list[float]:
    """Extract timestamp mentions from top comments."""
    comments = info.get("comments") or []
    times: list[float] = []
    for c in comments[:limit]:
        text = ""
        if isinstance(c, dict):
            text = str(c.get("text") or c.get("content") or "")
        else:
            text = str(c)
        for m in re.finditer(r"\b(\d{1,2}:\d{2}(?::\d{2})?)\b", text):
            t = _parse_ts_token(m.group(1))
            if t is not None and t >= 0:
                times.append(t)
        for m in re.finditer(r"\b(\d{1,3}(?:\.\d+)?)\s*s(?:ec)?\b", text, re.I):
            try:
                times.append(float(m.group(1)))
            except ValueError:
                pass
    return times


def fetch_meta(video_id: str, *, want_comments: bool = True) -> dict[str, Any]:
    """Return {heatmap, comment_times, title, channel, raw_info} best-effort."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    info = _run_ytdlp_info(url, comments=want_comments, max_comments=40)
    hm = heatmap_from_info(info)
    cts = comment_timestamps(info) if info else []
    # Cache next to downloads if present
    root = Path("downloads")
    if root.is_dir() and info:
        try:
            (root / f"{video_id}.meta.json").write_text(
                json.dumps({"heatmap": hm, "comment_times": cts, "title": info.get("title")}, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass
    return {
        "heatmap": hm,
        "comment_times": cts,
        "title": info.get("title") or "",
        "channel": info.get("channel") or info.get("uploader") or "",
        "info": info,
    }


def load_cached_meta(video_id: str) -> dict[str, Any]:
    for p in (Path("downloads") / f"{video_id}.meta.json", Path("/tmp/ytmeta") / f"{video_id}.meta.json"):
        if p.is_file():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                pass
    return {}


def heatmap_series(heatmap: list[dict[str, float]], duration: float) -> list[float]:
    """Per-second intensity array from heatmap segments."""
    n = max(1, int(duration) + 1)
    arr = [0.0] * n
    if not heatmap:
        return arr
    for seg in heatmap:
        s = max(0, int(seg["start"]))
        e = min(n, max(s + 1, int(seg["end"]) + 1))
        v = float(seg["value"])
        for i in range(s, e):
            arr[i] = max(arr[i], v)
    return arr


def is_mashup_title(title: str) -> bool:
    t = (title or "").lower()
    return any(
        k in t
        for k in (
            "mashup", "compilation", "compilat", "mega reaction",
            "streamers react", "youtubers react", "reactors react",
            "everyone react", "best reactions", "reaction compilation",
        )
    )
