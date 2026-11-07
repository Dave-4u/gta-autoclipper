"""Download videos with yt-dlp."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .discover import Candidate
from . import procutil

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



def _cleanup_partials(out_dir: Path, video_id: str) -> None:
    """Remove leftover yt-dlp .part / frag files after timeout or failure."""
    if not video_id:
        return
    for pth in out_dir.glob(f"{video_id}*"):
        name = pth.name.lower()
        if name.endswith(".part") or ".f" in name and name.endswith(".part") or name.endswith(".ytdl"):
            try:
                pth.unlink(missing_ok=True)
                print(f"[download] cleaned partial {pth.name}")
            except OSError:
                pass

def download_video(candidate: Candidate, cfg: dict[str, Any]) -> Path | None:
    if procutil.cancelled():
        print("[download] cancelled")
        return None
    from .source_policy import enforce_source
    if not enforce_source(candidate, cfg):
        return None
    if candidate.source == "local":
        local = Path(candidate.url)
        return local if local.is_file() else None
    out_dir = Path(cfg["download"]["output_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    fmt = cfg["download"].get("format", "bv*[height<=1080]+ba/b[height<=1080]/b")
    # Cap download length for ranking / fast CPU (seconds from start)
    max_s = float((cfg.get("download") or {}).get("max_download_seconds") or 0)
    # Long sources (episodes, compilations): don't grab the cold open / OP / channel intro.
    # Start the section ~35% in (configurable), keeping clear of the ending/credits.
    sec_start = 0.0
    dur = float(candidate.duration or 0)
    dl = cfg.get("download") or {}
    if max_s > 0 and dur > max_s * float(dl.get("section_long_factor", 3) or 3):
        frac = float(dl.get("section_start_frac", 0.35) or 0.35)
        sec_start = max(20.0, min(dur * frac, dur - max_s - 45.0))
        sec_start = max(0.0, sec_start)
    suffix = (f"_r{int(max_s)}" + (f"s{int(sec_start)}" if sec_start else "")) if max_s > 0 else ""
    stem = f"{candidate.id}{suffix}"
    # Template: id.ext
    outtmpl = str(out_dir / f"{stem}.%(ext)s")

    # Already downloaded?
    for ext in ("mp4", "mkv", "webm", "mov"):
        existing = out_dir / f"{stem}.{ext}"
        if existing.is_file() and existing.stat().st_size > 10_000:
            return existing
        # Prefer section file; fall back to full only when not capping
        if not suffix:
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
    # Ranking / section downloads need more wall-clock than media duration
    if max_s > 0:
        timeout = max(timeout, int(max_s * 2.5) + 45)
        # Prefer lighter progressive formats so section downloads finish in time
        fmt = (cfg.get("download") or {}).get(
            "format_fast",
            # Prefer real 720p+ when available; never grab sub-480p trash for ranking/clips
            "bv*[height>=720][height<=1080][ext=mp4]+ba[ext=m4a]/"
            "bv*[height>=720][height<=1080]+ba/"
            "b[height>=720][height<=1080]/"
            "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080]",
        )
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
        "--concurrent-fragments", "4",
        "--retries", "2",
        "--fragment-retries", "2",
        *_cookie_args(cfg),
    ]
    if max_s > 0:
        # Download only the first max_s seconds (yt-dlp section syntax)
        cmd.extend(["--download-sections", f"*{sec_start:.0f}-{sec_start + max_s:.0f}", "--force-keyframes-at-cuts"])
    cmd.append(candidate.url)
    print(f"[download] fetching {candidate.id}{suffix} (timeout {timeout}s, max_s={max_s or 'full'}"
          f"{f', from {sec_start:.0f}s of {dur:.0f}s' if sec_start else ''})…")
    def _try_run(run_cmd, run_timeout: int):
        try:
            return procutil.run_tracked(run_cmd, timeout=run_timeout, text=True, check=False), None
        except subprocess.TimeoutExpired:
            return None, "timeout"

    proc, err_kind = _try_run(cmd, timeout)
    if err_kind == "timeout":
        print(f"[download] timed out after {timeout}s — retrying lighter format…")
        _cleanup_partials(out_dir, stem)
        _cleanup_partials(out_dir, candidate.id)
        # Emergency fallback: progressive 360p/480p, no merge
        light = list(cmd)
        try:
            fi = light.index("-f")
            # Still refuse sub-480p — better to fail than ship mush
            light[fi + 1] = "b[height<=720][height>=480]/bv*[height<=720][height>=480]+ba/b[height<=720]"
        except ValueError:
            pass
        # Drop concurrent-fragments for flaky links
        while "--concurrent-fragments" in light:
            i = light.index("--concurrent-fragments")
            del light[i:i + 2]
        proc, err_kind = _try_run(light, min(timeout, 150))
        if err_kind == "timeout" or proc is None:
            print(f"[download] timed out again for {candidate.url}")
            _cleanup_partials(out_dir, stem)
            _cleanup_partials(out_dir, candidate.id)
            return None
        cmd = light  # for later path resolution context

    err_blob = ((proc.stderr or "") if proc else "") + "\n" + ((proc.stdout or "") if proc else "")
    if proc is not None and proc.returncode != 0 and _is_bot_wall(err_blob):
        print(
            "[download] YouTube bot check — skip (set download.cookies_from_browser: chrome|edge "
            "in config.yaml, or download.cookies: path\\to\\cookies.txt)"
        )
        return None

    if proc is not None and proc.returncode != 0:
        print("[download] retrying once…")
        proc, err_kind = _try_run(cmd, timeout)
        if err_kind == "timeout" or proc is None:
            print(f"[download] timed out after {timeout}s for {candidate.url}")
            _cleanup_partials(out_dir, stem)
            _cleanup_partials(out_dir, candidate.id)
            return None
        err_blob = (proc.stderr or "") + "\n" + (proc.stdout or "")
        if proc.returncode != 0 and _is_bot_wall(err_blob):
            print("[download] YouTube bot check on retry — skip")
            return None
    def _reject_lowres(pth: Path) -> Path | None:
        try:
            import json as _j
            pr = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height", "-of", "json", str(pth)],
                capture_output=True, text=True, check=False,
            )
            st = (_j.loads(pr.stdout).get("streams") or [{}])[0]
            w, h = int(st.get("width") or 0), int(st.get("height") or 0)
            short = min(w, h)
            if short and short < 480:
                print(f"[download] REJECT low-res {w}x{h} (<480p) for {pth.name}")
                try: pth.unlink(missing_ok=True)
                except Exception: pass
                return None
            if short and short < 720:
                print(f"[download] WARN sub-720p source {w}x{h} — keeping but crop quality limited")
        except Exception as e:
            print(f"[download] res probe skip: {e}")
        return pth

    # Prefer printed filepath
    path_line = (proc.stdout or "").strip().splitlines()
    if path_line:
        pth = Path(path_line[-1].strip())
        if pth.is_file():
            return _reject_lowres(pth)
    # Fallback scan
    for ext in ("mp4", "mkv", "webm", "mov"):
        pth = out_dir / f"{stem}.{ext}"
        if pth.is_file() and pth.stat().st_size > 10_000:
            return _reject_lowres(pth)
        pth = out_dir / f"{candidate.id}.{ext}"
        if not suffix and pth.is_file() and pth.stat().st_size > 10_000:
            return _reject_lowres(pth)
    # Only ever pick up files for THIS video. (The old "newest file in downloads" fallback
    # grabbed and renamed another worker's download during parallel ranking probes,
    # so that item's file vanished → "Error opening input".)
    videos = sorted(
        [p for p in out_dir.glob(f"{candidate.id}*")
         if p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"} and p.stat().st_size > 10_000],
        key=lambda x: x.stat().st_mtime,
        reverse=True,
    )
    if videos:
        return _reject_lowres(videos[0])
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
