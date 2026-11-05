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


def _snap_to_silence(path: Path, t: float, *, window: float = 0.6, prefer: str = "near") -> float:
    """Nudge a cut point to the quietest nearby sample so we don't chop mid-word."""
    import subprocess as _sp
    import numpy as np
    t0 = max(0.0, t - window)
    dur = window * 2
    try:
        pr = _sp.run(
            ["ffmpeg", "-v", "error", "-ss", f"{t0:.3f}", "-t", f"{dur:.3f}", "-i", str(path),
             "-vn", "-ac", "1", "-ar", "8000", "-f", "f32le", "-"],
            capture_output=True, check=False, timeout=20,
        )
        a = np.frombuffer(pr.stdout or b"", dtype=np.float32)
        if a.size < 200:
            return t
        # 20ms hop energy
        hop = 160
        energies = [float(np.sqrt(np.mean(a[i:i+hop] ** 2) + 1e-12)) for i in range(0, len(a) - hop, hop)]
        if not energies:
            return t
        # Quietest sample near the middle (original t)
        mid = int((t - t0) / (hop / 8000.0))
        lo = max(0, mid - 8); hi = min(len(energies), mid + 9)
        local = energies[lo:hi]
        if not local:
            return t
        best = lo + int(np.argmin(local))
        return t0 + best * (hop / 8000.0)
    except Exception:
        return t


def _mean_luma(path: Path, t: float) -> float:
    """0–255 mean luma of one frame — used to reject black intro freezes."""
    import subprocess as _sp, numpy as np
    try:
        pr = _sp.run(
            ["ffmpeg", "-v", "error", "-ss", f"{max(0,t):.2f}", "-i", str(path), "-frames:v", "1",
             "-vf", "scale=160:90,format=gray", "-f", "rawvideo", "-"],
            capture_output=True, check=False, timeout=15,
        )
        buf = np.frombuffer(pr.stdout or b"", dtype=np.uint8)
        return float(buf.mean()) if buf.size else 0.0
    except Exception:
        return 0.0


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
        lo, hi = m.get("ranking_total_seconds", [18, 25])
        n_lo, n_hi = m.get("ranking_segments", [2, 4])
        min_total = float(m.get("ranking_min_seconds", 18) or 18)
    else:
        lo, hi = m.get("total_seconds", [45, 75])
        n_lo, n_hi = m.get("segments", [4, 7])
        min_total = float(m.get("min_seconds", 30) or 30)
    s_lo, s_hi = m.get("segment_seconds", [5, 12])
    dur, w, h = _probe(source)
    if dur <= 0 or w <= 0:
        return None
    if h < 480 and w < 480:
        print(f"[montage] reject low-res source {w}x{h} (<480p)")
        return None
    td = float(m.get("transition_seconds", 0.3))
    # Never shrink below the hard floor — reject the source instead of outputting a 6s stub
    if dur < min_total + 2:
        print(f"[montage] source too short ({dur:.0f}s < min {min_total:.0f}s) — skip")
        return None
    target = min(float(hi), max(float(lo), float(min_total), (float(lo) + float(hi)) / 2))
    target = min(target, max(min_total, dur * 0.75))
    n_seg = int(max(n_lo, min(n_hi, round(target / 9.0))))
    seg_len = max(float(s_lo), min(float(s_hi), target / n_seg + td))
    # Prefer more shorter segments over fewer that leave us under the floor
    while n_seg * (seg_len - td) < min_total and n_seg < n_hi:
        n_seg += 1
        seg_len = max(float(s_lo), min(float(s_hi), target / n_seg + td))
    if n_seg * (seg_len - td) < min_total * 0.9 or n_seg < 2:
        print(f"[montage] cannot hit min {min_total:.0f}s from {dur:.0f}s source — skip")
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
    # Snap cut points to nearby silence so we don't chop mid-word
    snapped = []
    for st, en in moments:
        st2 = _snap_to_silence(source, st)
        en2 = _snap_to_silence(source, en)
        if en2 - st2 < float(s_lo) * 0.7:
            en2 = st2 + (en - st)
        snapped.append((round(st2, 2), round(en2, 2)))
    moments = snapped
    # Drop moments that are mostly black / blank (causes black VO freezes and trashy cuts)
    kept = []
    for st, en in moments:
        mid = (st + en) / 2
        lu = _mean_luma(source, mid)
        if lu < 14:
            print(f"[montage] drop blackish moment {st:.0f}-{en:.0f}s (luma={lu:.0f})")
            continue
        kept.append((st, en))
    if len(kept) < 2:
        print(f"[montage] too few non-black moments ({len(kept)}) — skip")
        return None
    moments = kept

    from .face_layout import smart_frame
    fade = float(m.get("audio_fade_seconds", 0.15) or 0.15)
    for i, (st, en) in enumerate(moments):
        inputs += ["-ss", f"{st:.2f}", "-t", f"{en - st:.2f}", "-i", str(source)]
        sp = rng.uniform(*m.get("speed_range", [1.03, 1.08]))
        z = rng.uniform(1.02, 1.06)  # mild zoom — big zooms + random bias cut faces off
        # Subject-aware horizontal crop (face > motion > centre); NO random edge shove
        try:
            _pre, cx = smart_frame(source, st, en)
        except Exception:
            cx = 0.5
        bx = (cx - 0.5) * 0.35  # map face centre into a gentle crop bias
        bx = max(-0.08, min(0.08, bx))
        flip = ",hflip" if (mirror_ok and i % 2 == 1) else ""
        sat = rng.uniform(1.02, 1.10)
        con = rng.uniform(1.01, 1.06)
        hue = rng.uniform(-2, 2)
        fc.append(
            f"[{i}:v]scale={ow}:{oh},setsar=1,fps=30,"
            f"scale=iw*{z:.3f}:ih*{z:.3f},"
            f"crop={ow}:{oh}:(iw-{ow})/2+iw*{bx:.3f}:(ih-{oh})/2{flip},"
            f"eq=contrast={con:.3f}:saturation={sat:.3f},hue=h={hue:.1f},"
            f"setpts=(PTS-STARTPTS)/{sp:.3f},fps=30,settb=1/30,format=yuv420p[v{i}]"
        )
        if audio:
            seg_d = (en - st) / sp
            afade = (f",afade=t=in:st=0:d={fade:.2f},"
                     f"afade=t=out:st={max(0.05, seg_d - fade):.2f}:d={fade:.2f}")
            fc.append(
                f"[{i}:a]aresample=48000,aformat=sample_rates=48000:channel_layouts=stereo,"
                f"asetpts=PTS-STARTPTS,atempo={sp:.3f}{afade}[a{i}]"
            )
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
    from .perf import encode_args, run_ffmpeg
    cmd += [*encode_args(cfg), str(out)]
    pr = run_ffmpeg(cmd, label="montage", duration_s=sum(seg_out) if seg_out else None)
    if pr.returncode != 0 or not out.is_file() or out.stat().st_size < 5000:
        print(f"[montage] build failed: {(pr.stderr or '')[-400:]}")
        return None
    md, _, _ = _probe(out)
    print(f"[montage] {len(moments)} moments "
          + ", ".join(f"{a:.0f}-{b:.0f}s" for a, b in moments)
          + f" → {md:.1f}s ({'ranking part' if ranking_part else 'clip'}, xfade {td:.2f}s)")
    if md + 0.3 < min_total:
        print(f"[montage] REJECT under-length result {md:.1f}s < {min_total:.0f}s")
        try: out.unlink(missing_ok=True)
        except Exception: pass
        return None
    return out
