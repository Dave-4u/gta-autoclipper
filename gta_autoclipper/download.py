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
            print("[download] configured cookies file missing (path not logged)")
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

def validate_download(path, *, minimum=720, section_seconds=0):
    """A completed/cache file must be readable, high quality and really section-sized."""
    import json
    try:
        result=subprocess.run(['ffprobe','-v','error','-select_streams','v:0',
            '-show_entries','stream=width,height:format=duration','-of','json',str(path)],
            capture_output=True,text=True,timeout=15,check=False)
        data=json.loads(result.stdout)
        stream=data.get('streams',[{}])[0]
        width,height=int(stream.get('width') or 0),int(stream.get('height') or 0)
        duration=float((data.get('format') or {}).get('duration') or 0)
        if result.returncode or min(width,height)<minimum or duration<=0:
            print('[download] status=failed reason=invalid_or_low_quality_media')
            return None
        if section_seconds and duration>section_seconds+20:
            print('[download] status=failed reason=unexpected_full_file_not_section')
            return None
        return path
    except (OSError,ValueError,TypeError,IndexError,subprocess.TimeoutExpired):
        print('[download] status=failed reason=media_probe_unavailable')
        return None


def download_video(candidate: Candidate, cfg: dict[str, Any]) -> Path | None:
    if procutil.cancelled():
        print("[download] cancelled")
        return None
    from .source_policy import enforce_source
    if not enforce_source(candidate, cfg):
        return None
    if candidate.source == "local" and not Path(candidate.url).is_file():
        return None
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
    if dl.get("section_start_seconds") is not None:
        sec_start = max(0.0, float(dl["section_start_seconds"]))
    suffix = (f"_r{int(max_s)}" + (f"s{int(sec_start)}" if sec_start else "")) if max_s > 0 else ""
    stem = f"{candidate.id}{suffix}"
    # Template: id.ext
    outtmpl = str(out_dir / f"{stem}.%(ext)s")

    if candidate.source == "local" and max_s <= 0:
        return Path(candidate.url)
    if candidate.source == "local":
        from .download_watch import run_download
        cmd = ["ffmpeg","-y","-ss",str(sec_start),"-i",candidate.url,"-t",str(max_s),
               "-map","0:v:0","-map","0:a?","-c","copy",str(out_dir/f"{stem}.mp4")]
        import time
        remaining=min(60,float(cfg.get("_source_deadline") or time.monotonic()+60)-time.monotonic())
        if remaining <= 1:return None
        ok,reason,_=run_download(cmd,out_dir=out_dir,stem=stem,total_seconds=remaining)
        print(f"[download] local_section status={'complete' if ok else 'failed'} reason={reason}")
        return validate_download(out_dir/f"{stem}.mp4",minimum=720 if cfg.get("niche_id") in {"anime","animation"} else 480,section_seconds=max_s) if ok else None

    # Already downloaded?
    for ext in ("mp4", "mkv", "webm", "mov"):
        existing = out_dir / f"{stem}.{ext}"
        if existing.is_file() and existing.stat().st_size > 10_000:
            checked=validate_download(existing,minimum=720 if cfg.get("niche_id") in {"anime","animation"} else 480,section_seconds=max_s)
            if checked:return checked
            existing.unlink(missing_ok=True)
        # Prefer section file; fall back to full only when not capping
        if not suffix:
            existing = out_dir / f"{candidate.id}.{ext}"
            if existing.is_file() and existing.stat().st_size > 10_000:
                return validate_download(existing,minimum=720 if cfg.get("niche_id") in {"anime","animation"} else 480)

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
        print("[download] status=skipped reason=playlist_or_channel_url")
        return None

    from .download_watch import run_download, SourceHealth
    import time
    health = cfg.setdefault("_source_health", SourceHealth())
    if not health.allowed(candidate.id):
        print("[download] status=skipped reason=failed_source_or_network_circuit_open")
        return None
    deadline = float(cfg.get("_source_deadline") or (time.monotonic()+240))
    remaining = min(240.0, deadline-time.monotonic())
    if remaining <= 1:
        print("[download] status=failed reason=source_budget")
        return None
    if max_s > 0:
        # Seek/copy sections rather than forced keyframe re-encoding. Keep >=720p.
        fmt = "bv*[height>=720][height<=1080][vcodec!~='av01|av1']+ba/b[height>=720][height<=1080][vcodec!~='av01|av1']"
    cmd = ["yt-dlp", "-f", fmt, "--merge-output-format", "mp4", "--no-playlist",
           "-o", outtmpl, "--no-warnings", "--newline", "--progress",
           "--progress-template", "download:gta_progress:%(progress.downloaded_bytes)s",
           "--socket-timeout", "15", "--retries", "0", "--fragment-retries", "0",
           "--extractor-retries", "0", "--concurrent-fragments", "2", *_cookie_args(cfg)]
    if max_s > 0:
        cmd += ["--download-sections", f"*{sec_start:.2f}-{sec_start+max_s:.2f}"]
    cmd.append(candidate.url)
    print(f"[download] window={sec_start:.1f}-{sec_start+max_s:.1f}s wall_remaining={remaining:.0f}s idle_limit=45s",flush=True)
    success = False
    reason = "extractor_error"
    # At most two total attempts, no reset of the per-source wall, never degrade below720p.
    for attempt in range(2):
        remaining = min(240.0,deadline-time.monotonic())
        if remaining <= 1:break
        try:
            success, reason, _ = run_download(cmd,out_dir=out_dir,stem=stem,
                total_seconds=remaining,idle_seconds=45)
        except (OSError, ValueError):
            reason = "download_tool_unavailable"
        print(f"[download] status={'complete' if success else 'failed'} reason={reason} attempt={attempt+1} remaining={max(0,deadline-time.monotonic()):.0f}s",flush=True)
        if success:break
        _cleanup_partials(out_dir,stem)
        if reason not in {"inactivity_stall", "transient_network"} or deadline-time.monotonic() < 30:
            break
    candidate.extra = dict(candidate.extra or {}, download_reason=reason)
    if not success:
        health.record(candidate.id,False)
        _cleanup_partials(out_dir,stem)
        return None
    for ext in ("mp4", "mkv", "webm", "mov"):
        pth = out_dir / f"{stem}.{ext}"
        if pth.is_file() and pth.stat().st_size > 10_000:
            checked=validate_download(pth,minimum=720 if cfg.get("niche_id") in {"anime","animation"} else 480,section_seconds=max_s)
            health.record(candidate.id,checked is not None)
            if not checked:
                pth.unlink(missing_ok=True)
            return checked
    health.record(candidate.id,False)
    print("[download] status=failed reason=missing_completed_file")
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
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=15)
    try:
        data = json.loads(proc.stdout or "{}")
        return float(data["format"]["duration"])
    except (KeyError, ValueError, json.JSONDecodeError, TypeError):
        return 0.0
