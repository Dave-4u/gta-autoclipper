"""Download videos with yt-dlp."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .discover import Candidate

_BOT_MARKERS = (
    "sign in to confirm you",
    "confirm you're not a bot",
    "confirm you're not a bot",
    "not a bot",
)


def _is_bot_wall(err: str) -> bool:
    low = (err or "").lower()
    return any(m in low for m in ("sign in to confirm", "not a bot", "cookies-from-browser", "use --cookies"))


def _cookie_args(cfg: dict[str, Any]) -> list[str]:
    dl = cfg.get("download") or {}
    args: list[str] = []
    browser = str(dl.get("cookies_from_browser") or "").strip()
    if browser:
        # Allow "chrome" or "chrome:Default"
        args.extend(["--cookies-from-browser", browser])
    cookies = str(dl.get("cookies") or dl.get("cookies_file") or "").strip()
    if cookies:
        cp = Path(cookies)
        if cp.is_file():
            args.extend(["--cookies", str(cp)])
        else:
            print(f"[download] cookies file not found: {cookies}")
    return args


def download_video(candidate: Candidate, cfg: dict[str, Any]) -> Path | None:
    out_dir = Path(cfg["download"]["output_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    fmt = cfg["download"].get("format", "bv*[height<=1080]+ba/b[height<=1080]/b")
    # Template: id.ext
    outtmpl = str(out_dir / f"{candidate.id}.%(ext)s")

    # Already downloaded?
    for ext in ("mp4", "mkv", "webm", "mov"):
        existing = out_dir / f"{candidate.id}.{ext}"
        if existing.is_file() and existing.stat().st_size > 10_000:
            return existing

    # Skip playlist / channel URLs — they hang for minutes even with --no-playlist
    url = str(candidate.url or "")
    cid = str(candidate.id or "")
    if (
        "playlist?list=" in url
        or "/playlist" in url
        or "/channel/" in url
        or "/@" in url.split("?")[0]
        or cid.startswith("PL")
        or cid.startswith("UU")
    ):
        print(f"[download] skip playlist/channel: {cid or url}")
        return None

    timeout = int((cfg.get("download") or {}).get("timeout_seconds") or 180)
    cmd = [
        "yt-dlp",
        "-f",
        fmt,
        "--merge-output-format",
        "mp4",
        "--no-playlist",
        "-o",
        outtmpl,
        "--print",
        "after_move:filepath",
        "--no-warnings",
        *_cookie_args(cfg),
        candidate.url,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        print(f"[download] timed out after {timeout}s for {candidate.url}")
        return None

    err_blob = (proc.stderr or "") + "\n" + (proc.stdout or "")
    if proc.returncode != 0 and _is_bot_wall(err_blob):
        print(
            "[download] YouTube bot check — skip (set download.cookies_from_browser: chrome|edge "
            "in config.yaml, or download.cookies: path\\to\\cookies.txt)"
        )
        return None

    if proc.returncode != 0:
        print("[download] retrying once…")
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            print(f"[download] timed out after {timeout}s for {candidate.url}")
            return None
        err_blob = (proc.stderr or "") + "\n" + (proc.stdout or "")
        if proc.returncode != 0 and _is_bot_wall(err_blob):
            print("[download] YouTube bot check on retry — skip")
            return None
    # Prefer printed filepath
    path_line = (proc.stdout or "").strip().splitlines()
    if path_line:
        pth = Path(path_line[-1].strip())
        if pth.is_file():
            return pth
    # Fallback scan
    for ext in ("mp4", "mkv", "webm", "mov"):
        pth = out_dir / f"{candidate.id}.{ext}"
        if pth.is_file() and pth.stat().st_size > 10_000:
            return pth
    # Sometimes yt-dlp uses title-based names; find newest in out_dir
    videos = sorted(
        [p for p in out_dir.glob("*") if p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"}],
        key=lambda x: x.stat().st_mtime,
        reverse=True,
    )
    if videos and videos[0].stat().st_size > 10_000:
        # Rename to stable id
        dest = out_dir / f"{candidate.id}{videos[0].suffix}"
        if videos[0] != dest:
            try:
                videos[0].rename(dest)
                return dest
            except OSError:
                return videos[0]
        return videos[0]
    err = (proc.stderr or proc.stdout or "unknown error")[:500]
    print(f"[download] failed for {candidate.url}: {err}")
    return None


def probe_duration(path: Path) -> float:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    try:
        data = json.loads(proc.stdout or "{}")
        return float(data["format"]["duration"])
    except (KeyError, ValueError, json.JSONDecodeError, TypeError):
        return 0.0
