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



def mean_trailer_motion(
    path: Path,
    start: float,
    end: float,
    game_box: tuple[float, float, float, float] = (0.28, 0.05, 0.50, 0.90),
    *,
    n_samples: int = 8,
) -> float:
    """Mean absolute frame-diff in the game/trailer crop (higher = more motion)."""
    import numpy as np
    from PIL import Image

    gx, gy, gw, gh = game_box
    dur = max(0.5, float(end) - float(start))
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        times = [start + dur * (i + 1) / (n_samples + 1) for i in range(n_samples)]
        for i, t in enumerate(times):
            vf = (
                f"crop=iw*{gw}:ih*{gh}:iw*{gx}:ih*{gy},scale=240:-2"
            )
            subprocess.run(
                ["ffmpeg", "-y", "-ss", f"{t:.2f}", "-i", str(path),
                 "-frames:v", "1", "-vf", vf, "-q:v", "5",
                 str(td_path / f"f{i:02d}.png")],
                capture_output=True, check=False,
            )
        frames = sorted(td_path.glob("f*.png"))
        if len(frames) < 3:
            return 0.0
        arrs = [np.array(Image.open(f)).astype(np.float32) for f in frames]
        diffs = [float(np.mean(np.abs(arrs[i] - arrs[i - 1]))) for i in range(1, len(arrs))]
        return float(sum(diffs) / len(diffs))



def trailer_content_ok(
    path: Path,
    start: float,
    end: float,
    game_box: tuple[float, float, float, float] = (0.28, 0.05, 0.50, 0.90),
    *,
    n_samples: int = 8,
    min_motion: float = 8.0,
) -> dict[str, Any]:
    """Require real trailer/gameplay in the game crop — reject static logos / grey cards.

    Uses frame-diff motion + per-sample mean. A Rockstar logo on dark grey has
    near-zero motion and flat means; real trailer has motion and/or scene cuts.
    """
    import numpy as np
    from PIL import Image

    gx, gy, gw, gh = game_box
    dur = max(0.5, float(end) - float(start))
    means: list[float] = []
    stds: list[float] = []
    arrs: list[Any] = []
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        times = [start + dur * (i + 0.5) / n_samples for i in range(n_samples)]
        for i, t in enumerate(times):
            vf = f"crop=iw*{gw}:ih*{gh}:iw*{gx}:ih*{gy},scale=240:-2"
            out = td_path / f"f{i:02d}.png"
            subprocess.run(
                ["ffmpeg", "-y", "-ss", f"{t:.2f}", "-i", str(path),
                 "-frames:v", "1", "-vf", vf, "-q:v", "5", str(out)],
                capture_output=True, check=False,
            )
            if out.is_file():
                im = np.array(Image.open(out)).astype(np.float32)
                means.append(float(im.mean()))
                stds.append(float(im.std()))
                arrs.append(im)
    if len(arrs) < 3:
        return {"ok": False, "reason": "no_frames", "motion": 0.0, "means": means}
    diffs = [float(np.mean(np.abs(arrs[i] - arrs[i - 1]))) for i in range(1, len(arrs))]
    motion = float(sum(diffs) / len(diffs))
    jump = float(max(means) - min(means)) if means else 0.0
    mean_avg = float(sum(means) / len(means))
    std_avg = float(sum(stds) / len(stds)) if stds else 0.0

    reasons = []
    # Static logo / title card: almost no motion, flat means, often dark
    if motion < 4.0 and jump < 10.0:
        reasons.append(f"static_logo_mot={motion:.1f}")
    # Near-solid grey/black card
    if std_avg < 18.0 and motion < 6.0:
        reasons.append(f"flat_panel_std={std_avg:.1f}")
    # Require measurable trailer motion overall
    if motion < min_motion and "static_logo_mot" not in "".join(reasons):
        # already flagged as logo, or genuinely low motion
        if motion < min_motion:
            reasons.append(f"low_trailer_motion={motion:.1f}")
    ok = not reasons
    return {
        "ok": ok,
        "motion": motion,
        "jump": jump,
        "mean_avg": mean_avg,
        "std_avg": std_avg,
        "means": means,
        "reasons": reasons,
        # keep old keys for callers
        "logo": any("static_logo" in r or "flat_panel" in r for r in reasons),
    }


def game_panel_stable(
    path: Path,
    start: float,
    end: float,
    game_box: tuple[float, float, float, float] = (0.28, 0.05, 0.50, 0.90),
    *,
    max_mean_jump: float = 35.0,
    n_samples: int = 6,
) -> dict[str, Any]:
    """Back-compat wrapper — prefer trailer_content_ok for new gates."""
    r = trailer_content_ok(path, start, end, game_box, n_samples=n_samples, min_motion=6.0)
    # For stable: logo fails; high jump with high motion is OK (trailer cuts)
    ok = r["ok"] or (r["motion"] >= 8.0 and not r.get("logo"))
    return {"ok": ok, "jump": r["jump"], "means": r["means"], "motion": r["motion"]}



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



def speech_coverage(
    path: Path,
    start: float,
    end: float,
    *,
    model_name: str = "tiny",
    niche_id: str = "gta6",
) -> dict[str, Any]:
    """Whisper the source window; return coverage fraction of real streamer words.

    Uses get_word_timestamps (with its filters) and also a plain faster-whisper
    fallback so music-heavy filters cannot zero out real speech.
    """
    dur = max(0.5, float(end) - float(start))

    def _cov(words):
        real = [w for w in (words or []) if str(w.get("word", "")).strip()
                and not str(w.get("word", "")).startswith("[")]
        if not real:
            return 0.0, 0, []
        spans = sorted((float(w["start"]), float(w["end"])) for w in real)
        covered = 0.0
        cs, ce = spans[0]
        for s, e in spans[1:]:
            if s <= ce:
                ce = max(ce, e)
            else:
                covered += max(0.0, ce - cs)
                cs, ce = s, e
        covered += max(0.0, ce - cs)
        return min(1.0, covered / dur), len(real), [str(w.get("word", "")) for w in real]

    best = (0.0, 0, [])
    try:
        from .transcribe import get_word_timestamps
    except Exception:
        from gta_autoclipper.transcribe import get_word_timestamps  # type: ignore
    try:
        words = get_word_timestamps(Path(path), float(start), float(end),
                                    model_name=model_name, niche_id="gta6")  # broad vocab for coverage
        best = max(best, _cov(words), key=lambda x: x[0])
    except Exception as e:
        print(f"[speech-cov] filtered whisper err: {e}")

    # Plain faster-whisper fallback (no niche/music drop) for coverage gating
    try:
        from faster_whisper import WhisperModel
        import tempfile, subprocess
        with tempfile.TemporaryDirectory() as td:
            wav = Path(td) / "a.wav"
            subprocess.run(
                ["ffmpeg", "-y", "-ss", f"{start:.2f}", "-t", f"{dur:.2f}",
                 "-i", str(path), "-ac", "1", "-ar", "16000", str(wav)],
                capture_output=True, check=False,
            )
            if wav.is_file():
                model = WhisperModel(model_name, device="cpu", compute_type="int8")
                segs, _ = model.transcribe(str(wav), word_timestamps=True, language="en")
                plain = []
                for seg in segs:
                    for w in (seg.words or []):
                        plain.append({"word": w.word.strip(), "start": float(w.start), "end": float(w.end)})
                best = max(best, _cov(plain), key=lambda x: x[0])
    except Exception as e:
        print(f"[speech-cov] plain whisper err: {e}")

    cov, n, toks = best
    return {"coverage": cov, "n_words": n, "words": toks}


def window_ok(
    path: Path,
    start: float,
    end: float,
    *,
    game_box: tuple[float, float, float, float] | None = None,
    max_freeze_s: float = 1.5,
    max_silence_s: float = 1.5,
    min_speech_coverage: float = 0.50,
    niche_id: str = "gta6",
    require_speech: bool = True,
) -> dict[str, Any]:
    """Gate a candidate window. Requires trailer motion + streamer speech coverage."""
    box = game_box or (0.28, 0.05, 0.50, 0.90)
    freeze = game_freeze_seconds(path, start, end, box)
    motion = mean_trailer_motion(path, start, end, box)
    sil = longest_silence(path, start, end, min_d=1.0)
    speech = {"coverage": 0.0, "n_words": 0, "words": []}
    if require_speech and min_speech_coverage > 0:
        try:
            speech = speech_coverage(path, start, end, niche_id=niche_id)
        except Exception as e:
            print(f"[window-gate] speech coverage error: {e}")
            speech = {"coverage": 0.0, "n_words": 0, "words": []}
    reasons = []
    stab: dict[str, Any] = {"jump": 0.0, "means": []}
    dur = max(0.5, float(end) - float(start))
    try:
        stab = trailer_content_ok(path, start, end, box, min_motion=8.0)
    except Exception as e:
        print(f"[window-gate] panel stability skip: {e}")
        stab = {"jump": 0.0, "means": [], "ok": True}
    # Trailer motion: streamers often pause to talk — allow near-static trailer
    # when speech is strong AND the game panel stays stable (not face-cam grey).
    min_motion = 6.0
    if speech["coverage"] >= min_speech_coverage and stab.get("ok", True):
        min_motion = 1.0
    if motion < min_motion:
        reasons.append(f"trailer_static_mot={motion:.1f}")
    freeze_limit = max_freeze_s
    if speech["coverage"] >= min_speech_coverage:
        freeze_limit = max(max_freeze_s, 0.70 * dur)  # talk-over-pause OK
    if freeze > freeze_limit:
        reasons.append(f"game_frozen_{freeze:.1f}s")
    if not stab.get("ok", True):
        reasons.extend(stab.get("reasons") or [f"trailer_bad_jump_{stab.get('jump', 0):.0f}"])
    if sil > max_silence_s and speech["coverage"] < 0.15:
        reasons.append(f"silence_{sil:.1f}s")
    if require_speech and speech["coverage"] < min_speech_coverage:
        reasons.append(f"speech_cov_{speech['coverage']:.0%}<{min_speech_coverage:.0%}")
    if require_speech and speech["n_words"] < 4:
        reasons.append(f"too_few_words_{speech['n_words']}")
    ok = not reasons
    print(
        f"[window-gate] {start:.1f}-{end:.1f} freeze={freeze:.1f}s mot={motion:.1f} "
        f"silence={sil:.1f}s trailer_mot={stab.get('motion', 0):.1f} jump={stab.get('jump', 0):.0f} speech={speech['coverage']:.0%}({speech['n_words']}w) "
        f"ok={ok} {reasons}"
    )
    return {
        "ok": ok,
        "freeze_s": freeze,
        "motion": motion,
        "silence_s": sil,
        "speech_coverage": speech["coverage"],
        "n_words": speech["n_words"],
        "sample_words": speech["words"][:12],
        "reasons": reasons,
    }



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
