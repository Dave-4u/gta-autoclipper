"""Download videos with yt-dlp."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .discover import Candidate


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
        candidate.url,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600, check=False)
    if proc.returncode != 0:
        print("[download] retrying once…")
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600, check=False)
    # Prefer printed filepath
    path_line = (proc.stdout or "").strip().splitlines()
    if path_line:
        p = Path(path_line[-1].strip())
        if p.is_file():
            return p
    # Fallback scan
    for ext in ("mp4", "mkv", "webm", "mov"):
        p = out_dir / f"{candidate.id}.{ext}"
        if p.is_file() and p.stat().st_size > 10_000:
            return p
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
