"""Slow-CPU performance profile: fewer re-encodes, faster x264, tinier whisper."""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Sequence

_PROFILE: dict[str, Any] | None = None


def cpu_count() -> int:
    return int(os.cpu_count() or 4)


def detect_low_cpu(cfg: dict[str, Any] | None = None) -> bool:
    """True when the machine should use the slow-CPU profile.
    Override with env GTA_LOW_CPU=0/1 or performance.low_cpu: true|false|auto.
    """
    env = os.environ.get("GTA_LOW_CPU", "").strip().lower()
    if env in {"0", "false", "no", "off"}:
        return False
    if env in {"1", "true", "yes", "on"}:
        return True
    perf = (cfg or {}).get("performance") or {}
    mode = perf.get("low_cpu", "auto")
    if mode is True or str(mode).lower() in {"1", "true", "yes", "on", "force"}:
        return True
    if mode is False or str(mode).lower() in {"0", "false", "no", "off"}:
        return False
    # auto
    n = cpu_count()
    thresh = int(perf.get("auto_cpu_count", 8) or 8)
    if n <= thresh:
        return True
    # quick encode benchmark (~0.5s of solid colour) — >1.2s wall → slow
    try:
        t0 = time.time()
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=320x180:d=0.5",
             "-c:v", "libx264", "-preset", "veryfast", "-f", "null", "-"],
            capture_output=True, check=False, timeout=8,
        )
        return (time.time() - t0) > float(perf.get("auto_bench_seconds", 1.2) or 1.2)
    except Exception:
        return n <= thresh


def apply_performance(cfg: dict[str, Any]) -> dict[str, Any]:
    """Mutate cfg with a resolved performance profile. Safe to call repeatedly."""
    global _PROFILE
    perf = dict(cfg.get("performance") or {})
    low = detect_low_cpu(cfg)
    perf["low_cpu"] = low
    if low:
        preset = str(perf.get("preset") or "ultrafast")
        crf = int(perf.get("crf") or 23)
        width = int(perf.get("width") or 720)
        height = int(perf.get("height") or 1280)
        whisper = str(perf.get("whisper_model") or "tiny")
        skip_vocals = bool(perf.get("skip_vocals", True))
        skip_final_whisper = bool(perf.get("skip_final_whisper_if_captions", True))
        skip_final_review = bool(perf.get("skip_final_review", True))
        threads = str(perf.get("threads") or "0")  # 0 = ffmpeg auto
    else:
        preset = str(perf.get("preset_fast") or perf.get("preset") or "veryfast")
        crf = int(perf.get("crf_fast") or perf.get("crf") or 20)
        width = int((cfg.get("clips") or {}).get("width") or 1080)
        height = int((cfg.get("clips") or {}).get("height") or 1920)
        whisper = str(((cfg.get("analysis") or {}).get("whisper_model")) or "small")
        skip_vocals = bool(perf.get("skip_vocals", False))
        skip_final_whisper = bool(perf.get("skip_final_whisper_if_captions", False))
        skip_final_review = bool(perf.get("skip_final_review", False))
        threads = str(perf.get("threads") or "0")

    perf.update({
        "preset": preset, "crf": crf, "width": width, "height": height,
        "whisper_model": whisper, "skip_vocals": skip_vocals,
        "skip_final_whisper_if_captions": skip_final_whisper,
        "skip_final_review": skip_final_review, "threads": threads,
        "cpu_count": cpu_count(),
    })
    cfg["performance"] = perf
    clips = cfg.setdefault("clips", {})
    # Keep logical 9:16; low_cpu renders at 720x1280 (still full-screen Shorts)
    clips["width"] = width
    clips["height"] = height
    ana = cfg.setdefault("analysis", {})
    if low:
        ana["whisper_model"] = whisper
        ana["whisper_for_highlights"] = False
        # Soft-skip Gemini video review (score gate still available via ranking.final_review)
        clips["skip_final_review"] = clips.get("skip_final_review", True) or skip_final_review
    job = cfg.setdefault("job", {})
    # 90 min default so a slow CPU can finish 2 clips + ranking parts
    job.setdefault("overall_timeout_seconds", int(perf.get("overall_timeout_seconds") or 5400))
    job.setdefault("render_timeout_seconds", int(perf.get("render_timeout_seconds") or (1200 if low else 600)))
    job.setdefault("analyze_timeout_seconds", int(perf.get("analyze_timeout_seconds") or (600 if low else 300)))
    job.setdefault("watchdog_minutes", float(perf.get("watchdog_minutes") or (8 if low else 5)))
    job.setdefault("autopilot_timeout_seconds", int(perf.get("autopilot_timeout_seconds") or 5400))
    rk = cfg.setdefault("ranking", {})
    rk.setdefault("timeout_seconds", int(perf.get("ranking_timeout_seconds") or (90 * 60 if low else 1500)))
    _PROFILE = perf
    print(f"[perf] low_cpu={low} preset={preset} crf={crf} size={width}x{height} "
          f"whisper={whisper} cpus={cpu_count()} vocals={'off' if skip_vocals else 'on'}")
    return cfg


def profile(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    if cfg and cfg.get("performance"):
        return dict(cfg["performance"])
    if _PROFILE:
        return dict(_PROFILE)
    return {"low_cpu": True, "preset": "ultrafast", "crf": 23, "threads": "0"}


def encode_args(cfg: dict[str, Any] | None = None, *, audio_copy: bool = False) -> list[str]:
    p = profile(cfg)
    args = [
        "-c:v", "libx264",
        "-preset", str(p.get("preset") or "ultrafast"),
        "-crf", str(p.get("crf") or 23),
        "-pix_fmt", "yuv420p",
        "-threads", str(p.get("threads") or "0"),
        "-movflags", "+faststart",
    ]
    if audio_copy:
        args += ["-c:a", "copy"]
    else:
        args += ["-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "160k"]
    return args


def watermark_vf(cfg: dict[str, Any], *, width: int | None = None) -> str:
    """drawtext filter fragment for the drifting handle, or '' if disabled."""
    from .edit import _escape_drawtext, _find_font, _font_opt
    wm = dict(cfg.get("watermark") or {})
    if not wm.get("enabled", True):
        return ""
    text = _escape_drawtext(str(wm.get("text") or "@officialdevclips"))
    op = max(0.05, min(1.0, float(wm.get("opacity", 0.45) or 0.45)))
    pos = str(wm.get("position") or "drift").lower()
    size = float(wm.get("size", 0.024) or 0.024)
    font = _find_font(cfg)
    if pos == "drift":
        x = "(w-tw)/2-w*0.06+w*0.10*sin(2*PI*t/17)"
        y = "h*0.60+h*0.035*sin(2*PI*t/11)"
    elif pos in ("center-low", "center"):
        x, y = "(w-tw)/2-w*0.04", "h*0.60"
    elif pos == "top-center":
        x, y = "(w-tw)/2", "h*0.16"
    else:
        mx = float(wm.get("margin_x", 0.045) or 0.045)
        my_top = float(wm.get("margin_top", 0.095) or 0.095)
        my_bot = float(wm.get("margin_bottom", 0.20) or 0.20)
        x = f"w-tw-w*{max(mx, 0.12)}" if "right" in pos else f"w*{mx}"
        y = f"h-th-h*{max(my_bot, 0.36)}" if "bottom" in pos else f"h*{max(my_top, 0.16)}"
    return (
        f"drawtext=text='{text}'{_font_opt(font)}:fontsize=h*{size}:"
        f"fontcolor=white@{op:.2f}:borderw=1:bordercolor=black@{op * 0.7:.2f}:"
        f"shadowx=2:shadowy=2:shadowcolor=black@{op * 0.6:.2f}:x={x}:y={y}"
    )


_PROGRESS_RX = re.compile(r"out_time_ms=(\d+)|out_time=(\d+):(\d+):(\d+(?:\.\d+)?)|progress=end")


def run_ffmpeg(cmd: Sequence[str], *, label: str = "ffmpeg", duration_s: float | None = None,
               check: bool = False) -> subprocess.CompletedProcess:
    """Run ffmpeg with -progress on a pipe so long encodes log % and keep the UI watchdog alive."""
    cmd = list(cmd)
    # Insert progress flags after 'ffmpeg'
    if cmd and Path(cmd[0]).name.lower().startswith("ffmpeg"):
        if "-progress" not in cmd:
            # Use stderr for logs, stdout for -progress key=value lines
            insert_at = 1
            cmd[insert_at:insert_at] = ["-progress", "pipe:1", "-nostats", "-hide_banner"]
    t0 = time.time()
    last_pct = -1
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
    )

    def _drain_err():
        assert proc.stderr
        buf = []
        for line in proc.stderr:
            buf.append(line)
            if len(buf) > 80:
                del buf[:40]
        proc._err_tail = "".join(buf[-40:])  # type: ignore[attr-defined]

    err_t = threading.Thread(target=_drain_err, daemon=True)
    err_t.start()
    assert proc.stdout
    for raw in proc.stdout:
        line = raw.strip()
        if not line:
            continue
        m = _PROGRESS_RX.search(line)
        if not m:
            continue
        if line == "progress=end" or "progress=end" in line:
            continue
        secs = None
        if m.group(1):
            secs = int(m.group(1)) / 1_000_000.0
        elif m.group(2):
            secs = int(m.group(2)) * 3600 + int(m.group(3)) * 60 + float(m.group(4))
        if secs is None:
            continue
        if duration_s and duration_s > 0.5:
            pct = int(min(99, max(0, round(100.0 * secs / duration_s))))
            if pct >= last_pct + 5 or pct == 99:
                last_pct = pct
                print(f"[ffmpeg] {label} {pct}% ({secs:.1f}/{duration_s:.1f}s)", flush=True)
        else:
            # No duration known — still tick so the UI watchdog sees progress
            if int(secs) % 10 == 0:
                print(f"[ffmpeg] {label} t={secs:.0f}s", flush=True)
    rc = proc.wait()
    err_t.join(timeout=2)
    stderr = getattr(proc, "_err_tail", "") or ""
    elapsed = time.time() - t0
    if rc == 0:
        print(f"[ffmpeg] {label} done in {elapsed:.1f}s", flush=True)
    else:
        print(f"[ffmpeg] {label} FAILED rc={rc} in {elapsed:.1f}s: {stderr[-300:]}", flush=True)
    return subprocess.CompletedProcess(cmd, rc, "", stderr)
