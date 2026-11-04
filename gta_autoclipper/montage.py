"""Multi-moment montage: pick several high-energy moments across a source and stitch them
with short, smooth transitions + light per-segment transforms (copyright-friendlier).

The result is a normal landscape/whatever-aspect mp4 that render_clip then treats as one
continuous segment, so 9:16 crop, captions (whisper on the montage), VO and watermark all
stay in sync automatically.
"""

from __future__ import annotations

import random
import subprocess
from pathlib import Path
from typing import Any

import numpy as np

_USED: dict[str, list[tuple[float, float]]] = {}  # source → moments already used by earlier clips

_TRANSITIONS = ["fadewhite", "zoomin", "smoothleft", "fadefast", "smoothright", "circleopen"]


def montage_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    m = dict((cfg.get("clips") or {}).get("montage") or cfg.get("montage") or {})
    m.setdefault("enabled", True)
    return m


def _probe(path: Path) -> tuple[float, int, int]:
    import json
    pr = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height:format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, check=False,
    )
    try:
        d = json.loads(pr.stdout or "{}")
        st = (d.get("streams") or [{}])[0]
        return float((d.get("format") or {}).get("duration") or 0), int(st.get("width") or 0), int(st.get("height") or 0)
    except Exception:
        return 0.0, 0, 0


def _has_audio(path: Path) -> bool:
    pr = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
                         "stream=index", "-of", "csv=p=0", str(path)], capture_output=True, text=True, check=False)
    return bool((pr.stdout or "").strip())


def energy_curve(path: Path, dur: float) -> tuple[np.ndarray, list[float]]:
    """Per-second energy = loudness + motion + scene-change density. One cheap pass each."""
    from .analyze import extract_loudness_peaks
    n = max(1, int(dur))
    loud = np.zeros(n)
    try:
        l = extract_loudness_peaks(path, window=1.0)
        loud[: min(n, len(l))] = l[:n]
    except Exception:
        pass
    fps = 4
    motion = np.zeros(n)
    cuts: list[float] = []
    try:
        pr = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(path), "-vf", f"fps={fps},scale=96:54,format=gray",
             "-f", "rawvideo", "-"], capture_output=True, check=False, timeout=240,
        )
        buf = np.frombuffer(pr.stdout or b"", dtype=np.uint8)
        k = buf.size // (96 * 54)
        if k > 2:
            fr = buf[: k * 96 * 54].reshape(k, 54, 96).astype(np.int16)
            d = np.abs(np.diff(fr, axis=0)).mean(axis=(1, 2))
            thr = max(28.0, float(np.percentile(d, 97)))
            for i, v in enumerate(d):
                sec = int((i + 1) / fps)
                if sec < n:
                    motion[sec] += float(min(v, thr * 0.8))
                if v >= thr:
                    cuts.append((i + 1) / fps)
    except Exception as e:
        print(f"[montage] motion pass skip: {e}")

    def norm(a):
        a = np.asarray(a, dtype=np.float64)
        rng = a.max() - a.min()
        return (a - a.min()) / rng if rng > 1e-9 else np.zeros_like(a)

    cut_density = np.zeros(n)
    for c in cuts:
        if int(c) < n:
            cut_density[int(c)] += 1
    e = 1.0 * norm(loud) + 0.8 * norm(motion) + 0.4 * norm(np.minimum(cut_density, 3))
    # Dead air / black: penalise near-silent seconds hard
    e[norm(loud) < 0.08] *= 0.3
    return e, cuts


def pick_moments(path: Path, cfg: dict[str, Any], *, n_seg: int, seg_len: float,
                 anchor: tuple[float, float] | None = None) -> list[tuple[float, float]]:
    dur, _, _ = _probe(path)
    if dur < seg_len * 2:
        return []
    m = montage_cfg(cfg)
    intro = float(m.get("skip_intro_frac", 0.06)) * dur if dur > 120 else min(2.0, dur * 0.04)
    outro = float(m.get("skip_outro_frac", 0.08)) * dur if dur > 120 else min(2.0, dur * 0.04)
    intro = max(intro, float(m.get("skip_intro_seconds", 0) or 0) if dur > 180 else 0)
    e, cuts = energy_curve(path, dur)
    L = max(1, int(round(seg_len)))
    win = np.convolve(e, np.ones(L) / L, mode="valid")  # score of segment starting at s
    used = _USED.get(str(path), [])
    min_gap = float(m.get("min_gap_seconds", 2.0))
    picks: list[tuple[float, float]] = []
    if anchor:  # keep the analyzer's best window as one of the moments
        a0 = max(intro, min(anchor[0], dur - outro - seg_len))
        picks.append((a0, a0 + seg_len))
    order = np.argsort(-win)
    for s in order:
        if len(picks) >= n_seg:
            break
        st = float(s)
        if st < intro or st + seg_len > dur - outro:
            continue
        # Start on a nearby hard cut if one is close (feels intentional, not mid-motion)
        near = [c for c in cuts if 0 <= st - c <= 1.5]
        if near:
            st = near[-1]
        en = st + seg_len
        if any(not (en + min_gap <= a or st >= b + min_gap) for a, b in picks + used):
            continue
        picks.append((round(st, 2), round(en, 2)))
    if len(picks) < max(2, n_seg - 1):
        # Fallback: evenly spread through the body
        picks = []
        body0, body1 = intro, dur - outro - seg_len
        for i in range(n_seg):
            st = body0 + (body1 - body0) * (i + 0.5) / n_seg
            picks.append((round(st, 2), round(st + seg_len, 2)))
    picks.sort()
    _USED.setdefault(str(path), []).extend(picks)
    return picks


def build_moment_montage(source: Path, cfg: dict[str, Any], *, ranking_part: bool = False,
                         anchor: tuple[float, float] | None = None, tag: str = "") -> Path | None:
    m = montage_cfg(cfg)
    if not m.get("enabled", True):
        return None
    if ranking_part:
        lo, hi = m.get("ranking_total_seconds", [20, 25])
        n_lo, n_hi = m.get("ranking_segments", [2, 4])
    else:
        lo, hi = m.get("total_seconds", [45, 75])
        n_lo, n_hi = m.get("segments", [4, 7])
    s_lo, s_hi = m.get("segment_seconds", [4, 12])
    dur, w, h = _probe(source)
    if dur <= 0 or w <= 0:
        return None
    td = float(m.get("transition_seconds", 0.3))
    # Fit to what the source can give (short sources → shorter montage)
    target = min(float(hi), max(float(lo), (float(lo) + float(hi)) / 2))
    target = min(target, dur * 0.7)
    n_seg = int(max(n_lo, min(n_hi, round(target / 9.0))))
    seg_len = max(float(s_lo), min(float(s_hi), target / n_seg + td))
    while n_seg > n_lo and seg_len * n_seg > dur * 0.8:
        n_seg -= 1
    if seg_len * n_seg > dur * 0.85 or n_seg < 2:
        print(f"[montage] source too short ({dur:.0f}s) for {n_seg}×{seg_len:.1f}s — single stretch")
        return None
    moments = pick_moments(source, cfg, n_seg=n_seg, seg_len=seg_len, anchor=anchor)
    if len(moments) < 2:
        return None
    rng = random.Random(hash((str(source), tag)) & 0xFFFF)
    # Even output dims, keep source aspect (render_clip does the 9:16 crop afterwards)
    ow = min(1920, w - (w % 2))
    oh = int(round(h * ow / w)) // 2 * 2
    audio = _has_audio(source)
    mirror_ok = bool(m.get("mirror", False))
    inputs: list[str] = []
    fc: list[str] = []
    seg_out: list[float] = []
    for i, (st, en) in enumerate(moments):
        inputs += ["-ss", f"{st:.2f}", "-t", f"{en - st:.2f}", "-i", str(source)]
        sp = rng.uniform(*m.get("speed_range", [1.03, 1.08]))
        z = rng.uniform(1.03, 1.09)
        bx = rng.uniform(-0.03, 0.03)
        flip = ",hflip" if (mirror_ok and i % 2 == 1) else ""
        sat = rng.uniform(1.04, 1.14)
        con = rng.uniform(1.02, 1.08)
        hue = rng.uniform(-4, 4)
        fc.append(
            f"[{i}:v]scale={ow}:{oh},setsar=1,fps=30,"
            f"scale=iw*{z:.3f}:ih*{z:.3f},crop={ow}:{oh}:(iw-{ow})/2+iw*{bx:.3f}:(ih-{oh})/2{flip},"
            f"eq=contrast={con:.3f}:saturation={sat:.3f},hue=h={hue:.1f},"
            f"setpts=(PTS-STARTPTS)/{sp:.3f},fps=30,settb=1/30,format=yuv420p[v{i}]"
        )
        if audio:
            fc.append(f"[{i}:a]aresample=48000,aformat=sample_rates=48000:channel_layouts=stereo,"
                      f"asetpts=PTS-STARTPTS,atempo={sp:.3f}[a{i}]")
        seg_out.append((en - st) / sp)
    # Chain xfade/acrossfade
    vlab, alab, off = "v0", "a0", seg_out[0]
    for i in range(1, len(moments)):
        tr = _TRANSITIONS[(i - 1 + rng.randint(0, 5)) % len(_TRANSITIONS)]
        off_i = off - td
        fc.append(f"[{vlab}][v{i}]xfade=transition={tr}:duration={td:.2f}:offset={off_i:.3f}[vx{i}]")
        vlab = f"vx{i}"
        if audio:
            fc.append(f"[{alab}][a{i}]acrossfade=d={td:.2f}:c1=tri:c2=tri[ax{i}]")
            alab = f"ax{i}"
        off = off_i + seg_out[i]
    out_dir = Path((cfg.get("clips") or {}).get("output_dir") or "outputs") / "_montage"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{source.stem}_{tag or 'm'}_montage.mp4"
    script = out.with_suffix(".fc.txt")
    script.write_text(";\n".join(fc), encoding="utf-8", newline="\n")
    cmd = ["ffmpeg", "-y", "-v", "warning", *inputs, "-filter_complex_script", script.resolve().as_posix(),
           "-map", f"[{vlab}]"]
    if audio:
        cmd += ["-map", f"[{alab}]", "-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", str(out)]
    pr = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if pr.returncode != 0 or not out.is_file() or out.stat().st_size < 5000:
        print(f"[montage] build failed: {(pr.stderr or '')[-400:]}")
        return None
    md, _, _ = _probe(out)
    print(f"[montage] {len(moments)} moments "
          + ", ".join(f"{a:.0f}-{b:.0f}s" for a, b in moments)
          + f" → {md:.1f}s ({'ranking part' if ranking_part else 'clip'}, xfade {td:.2f}s)")
    return out
