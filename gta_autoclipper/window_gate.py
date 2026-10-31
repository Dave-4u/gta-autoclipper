"""Reject frozen-trailer / long-silence windows before render."""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Any


def game_freeze_seconds(
    path: Path,
    start: float,
    end: float,
    game_box: tuple[float, float, float, float] = (0.28, 0.05, 0.50, 0.90),
    *,
    fps: float = 2.0,
    diff_thresh: float = 3.0,
) -> float:
    """Longest contiguous freeze (sec) in the cropped game region via frame-diff."""
    import numpy as np
    from PIL import Image

    gx, gy, gw, gh = game_box
    dur = max(0.5, float(end) - float(start))
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        vf = (
            f"fps={fps},"
            f"crop=iw*{gw}:ih*{gh}:iw*{gx}:ih*{gy},"
            f"scale=320:-2"
        )
        subprocess.run(
            [
                "ffmpeg", "-y", "-ss", f"{start:.2f}", "-t", f"{dur:.2f}",
                "-i", str(path), "-vf", vf, "-q:v", "5",
                str(td_path / "f%03d.png"),
            ],
            capture_output=True, check=False,
        )
        frames = sorted(td_path.glob("f*.png"))
        if len(frames) < 3:
            return dur  # treat as fully frozen / unreadable
        prev = None
        run = 0
        max_run = 0
        for f in frames:
            im = np.array(Image.open(f)).astype(np.float32)
            if prev is not None:
                d = float(np.mean(np.abs(im - prev)))
                if d < diff_thresh:
                    run += 1
                    max_run = max(max_run, run)
                else:
                    run = 0
            prev = im
        return max_run / float(fps)


def silence_runs(
    path: Path,
    start: float,
    end: float,
    *,
    noise_db: float = -35.0,
    min_d: float = 1.5,
) -> list[tuple[float, float]]:
    """Return (silence_start, silence_end) pairs inside [start,end] longer than min_d."""
    import re
    dur = max(0.5, float(end) - float(start))
    cmd = [
        "ffmpeg", "-hide_banner",
        "-ss", f"{start:.2f}", "-t", f"{dur:.2f}", "-i", str(path),
        "-af", f"silencedetect=noise={noise_db}dB:d={min_d}",
        "-f", "null", "-",
    ]
    pr = subprocess.run(cmd, capture_output=True, text=True, check=False)
    text = (pr.stderr or "") + (pr.stdout or "")
    starts = [float(x) for x in re.findall(r"silence_start:\s*([0-9.]+)", text)]
    ends = [float(x) for x in re.findall(r"silence_end:\s*([0-9.]+)", text)]
    # silence_end lines also include silence_duration
    out: list[tuple[float, float]] = []
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else dur
        # ffmpeg reports times relative to the trimmed input
        out.append((float(start) + s, float(start) + e))
    return out


def longest_silence(path: Path, start: float, end: float, **kw: Any) -> float:
    runs = silence_runs(path, start, end, **kw)
    if not runs:
        return 0.0
    return max(e - s for s, e in runs)


def window_ok(
    path: Path,
    start: float,
    end: float,
    *,
    game_box: tuple[float, float, float, float] | None = None,
    max_freeze_s: float = 1.5,
    max_silence_s: float = 1.5,
) -> dict[str, Any]:
    """Gate a candidate window. ok=False if trailer frozen or long silence."""
    box = game_box or (0.28, 0.05, 0.50, 0.90)
    freeze = game_freeze_seconds(path, start, end, box)
    sil = longest_silence(path, start, end, min_d=1.0)
    reasons = []
    if freeze > max_freeze_s:
        reasons.append(f"game_frozen_{freeze:.1f}s")
    if sil > max_silence_s:
        reasons.append(f"silence_{sil:.1f}s")
    ok = not reasons
    print(f"[window-gate] {start:.1f}-{end:.1f} freeze={freeze:.1f}s silence={sil:.1f}s ok={ok} {reasons}")
    return {"ok": ok, "freeze_s": freeze, "silence_s": sil, "reasons": reasons}


def black_bar_ratios(
    path: Path,
    *,
    sample_t: float = 1.0,
    thresh: float = 16.0,
    min_run: int = 6,
) -> dict[str, Any]:
    """Check top/mid/bottom rows of a rendered frame for black bars/gaps."""
    import numpy as np
    from PIL import Image

    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "f.png"
        subprocess.run(
            ["ffmpeg", "-y", "-ss", f"{sample_t:.2f}", "-i", str(path),
             "-frames:v", "1", "-q:v", "2", str(out)],
            capture_output=True, check=False,
        )
        if not out.is_file():
            return {"ok": False, "reasons": ["no_frame"]}
        im = np.array(Image.open(out))
        H, W = im.shape[:2]
        row = im.mean(axis=(1, 2))
        def runs_in(a, b):
            total = 0
            run = 0
            for y in range(a, b):
                if row[y] < thresh:
                    run += 1
                    if run >= min_run:
                        total += 1
                else:
                    run = 0
            return total
        top = runs_in(0, max(1, H // 12))
        bot = runs_in(H - H // 12, H)
        # Mid gap: thin dark band with brighter content above AND below
        mid = 0
        for y in range(H // 2 - 30, H // 2 + 30):
            if row[y] < thresh and row[max(0, y - 40)] > 40 and row[min(H - 1, y + 40)] > 40:
                mid += 1
        reasons = []
        # Top/bottom bars only if majority of the band is black (true letterbox)
        if top > (H // 12) * 0.5:
            reasons.append("top_black_bar")
        if mid >= min_run:
            reasons.append("mid_gap")
        if bot > (H // 12) * 0.5:
            reasons.append("bottom_black_bar")
        return {"ok": not reasons, "top": top, "mid": mid, "bot": bot, "reasons": reasons}
