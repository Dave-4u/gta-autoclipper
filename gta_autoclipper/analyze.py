"""Highlight detection using free signals: loudness, scenes, motion, transcript."""

from __future__ import annotations

import json
import random
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np


@dataclass
class Segment:
    start: float
    end: float
    score: float
    reason: str = ""


def _video_duration(path: Path) -> float:
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "json", str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    try:
        return float(json.loads(proc.stdout)["format"]["duration"])
    except Exception:
        return 0.0


def extract_loudness_peaks(path: Path, window: float = 1.0) -> np.ndarray:
    """Return per-second RMS-ish loudness array via ffmpeg astats / lavfi."""
    # Export mono 16-bit PCM at 16kHz for analysis
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        wav = Path(tmp.name)
    try:
        cmd = [
            "ffmpeg", "-y", "-i", str(path),
            "-ac", "1", "-ar", "16000", "-vn",
            str(wav),
        ]
        subprocess.run(cmd, capture_output=True, check=False)
        if not wav.is_file() or wav.stat().st_size < 100:
            dur = _video_duration(path)
            return np.zeros(max(1, int(dur)))
        import wave

        with wave.open(str(wav), "rb") as w:
            n = w.getnframes()
            rate = w.getframerate()
            raw = w.readframes(n)
        audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        samples_per_win = max(1, int(rate * window))
        n_wins = max(1, len(audio) // samples_per_win)
        rms = np.zeros(n_wins, dtype=np.float32)
        for i in range(n_wins):
            chunk = audio[i * samples_per_win : (i + 1) * samples_per_win]
            rms[i] = float(np.sqrt(np.mean(chunk ** 2) + 1e-12))
        return rms
    finally:
        try:
            wav.unlink(missing_ok=True)
        except Exception:
            pass


def extract_scene_changes(path: Path, threshold: float = 27.0) -> list[float]:
    """Return list of scene-change timestamps using PySceneDetect if available."""
    try:
        from scenedetect import open_video, SceneManager
        from scenedetect.detectors import ContentDetector

        video = open_video(str(path))
        sm = SceneManager()
        sm.add_detector(ContentDetector(threshold=threshold))
        sm.detect_scenes(video)
        scenes = sm.get_scene_list()
        # Return start times of each scene (skip 0)
        times = []
        for start, _end in scenes:
            t = start.get_seconds()
            if t > 0.5:
                times.append(t)
        return times
    except Exception as e:
        print(f"[analyze] scene detect fallback: {e}")
        return _scene_fallback_ffmpeg(path)


def _scene_fallback_ffmpeg(path: Path) -> list[float]:
    cmd = [
        "ffmpeg", "-i", str(path),
        "-filter:v", "select='gt(scene,0.3)',showinfo",
        "-f", "null", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    times = []
    for line in (proc.stderr or "").splitlines():
        if "pts_time:" in line:
            try:
                part = line.split("pts_time:")[1].split()[0]
                times.append(float(part))
            except (IndexError, ValueError):
                pass
    return times


def extract_motion_scores(path: Path, sample_fps: float = 2.0) -> np.ndarray:
    """Average frame-to-frame absolute difference per second (OpenCV)."""
    try:
        import cv2
    except ImportError:
        dur = _video_duration(path)
        return np.zeros(max(1, int(dur)))

    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        return np.zeros(1)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    duration = total / fps if fps else 0
    n_secs = max(1, int(duration) + 1)
    scores = np.zeros(n_secs, dtype=np.float32)
    counts = np.zeros(n_secs, dtype=np.float32)

    step = max(1, int(fps / sample_fps))
    prev = None
    idx = 0
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        if idx % step == 0:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            gray = cv2.resize(gray, (160, 90))
            t = idx / fps
            sec = min(n_secs - 1, int(t))
            if prev is not None:
                diff = float(np.mean(np.abs(gray.astype(np.float32) - prev.astype(np.float32))))
                scores[sec] += diff
                counts[sec] += 1
            prev = gray
        idx += 1
    cap.release()
    counts = np.maximum(counts, 1)
    return scores / counts


def _normalize(arr: np.ndarray) -> np.ndarray:
    if arr.size == 0:
        return arr
    mn, mx = float(arr.min()), float(arr.max())
    if mx - mn < 1e-9:
        return np.zeros_like(arr)
    return (arr - mn) / (mx - mn)



def extract_laughter_scream_scores(path: Path, window: float = 0.25) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Cheap CPU-only proxies for laughter / scream / loudness spikes from mono PCM.
    Returns (loud_spike_per_sec, laugh_score_per_sec, scream_score_per_sec).
    """
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        wav = Path(tmp.name)
    try:
        cmd = [
            "ffmpeg", "-y", "-i", str(path),
            "-ac", "1", "-ar", "16000", "-vn", str(wav),
        ]
        subprocess.run(cmd, capture_output=True, check=False)
        if not wav.is_file() or wav.stat().st_size < 100:
            dur = _video_duration(path)
            z = np.zeros(max(1, int(dur)), dtype=np.float32)
            return z, z.copy(), z.copy()
        import wave

        with wave.open(str(wav), "rb") as w:
            n = w.getnframes()
            rate = w.getframerate()
            raw = w.readframes(n)
        audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        # short windows for spike detection
        win = max(1, int(rate * window))
        n_wins = max(1, len(audio) // win)
        rms = np.zeros(n_wins, dtype=np.float32)
        zcr = np.zeros(n_wins, dtype=np.float32)
        for i in range(n_wins):
            chunk = audio[i * win : (i + 1) * win]
            rms[i] = float(np.sqrt(np.mean(chunk ** 2) + 1e-12))
            signs = np.sign(chunk)
            zcr[i] = float(np.mean(np.abs(np.diff(signs)) > 0))
        # Aggregate to per-second
        wins_per_sec = max(1, int(round(1.0 / window)))
        n_secs = max(1, int(np.ceil(n_wins / wins_per_sec)))
        loud = np.zeros(n_secs, dtype=np.float32)
        laugh = np.zeros(n_secs, dtype=np.float32)
        scream = np.zeros(n_secs, dtype=np.float32)
        for i in range(n_wins):
            sec = min(n_secs - 1, i // wins_per_sec)
            loud[sec] = max(loud[sec], rms[i])
            # laughter: mid RMS + high zero-crossing (bursty)
            if 0.08 < rms[i] < 0.45 and zcr[i] > 0.12:
                laugh[sec] += 1.0
            # scream: very loud + high zcr
            if rms[i] > 0.35 and zcr[i] > 0.15:
                scream[sec] += 1.2
            elif rms[i] > 0.5:
                scream[sec] += 0.6
        # Spike emphasis: delta from local median
        if loud.size >= 5:
            med = np.median(loud)
            spikes = np.clip(loud - med * 1.4, 0, None)
        else:
            spikes = loud.copy()
        return spikes.astype(np.float32), laugh.astype(np.float32), scream.astype(np.float32)
    finally:
        try:
            wav.unlink(missing_ok=True)
        except Exception:
            pass


def extract_face_motion_scores(path: Path, sample_fps: float = 3.0) -> np.ndarray:
    """
    Face presence + ROI motion for reaction cams (CPU-only).
    Tries FaceDetectorYN (YuNet) → Haar Cascade → upper-third motion fallback.
    """
    try:
        import cv2
    except ImportError:
        dur = _video_duration(path)
        return np.zeros(max(1, int(dur)), dtype=np.float32)

    face_yn = None
    face_cascade = None
    try:
        if hasattr(cv2, "FaceDetectorYN_create"):
            model_dir = Path(__file__).resolve().parent.parent / "models"
            model_dir.mkdir(parents=True, exist_ok=True)
            yunet = model_dir / "face_detection_yunet_2023mar.onnx"
            if not yunet.is_file():
                url = (
                    "https://github.com/opencv/opencv_zoo/raw/main/models/"
                    "face_detection_yunet/face_detection_yunet_2023mar.onnx"
                )
                try:
                    import urllib.request
                    print("[analyze] downloading YuNet face model…")
                    urllib.request.urlretrieve(url, yunet)
                except Exception as e:
                    print(f"[analyze] YuNet download failed: {e}")
            for cand in (yunet, Path("models") / "face_detection_yunet_2023mar.onnx"):
                if cand.is_file():
                    face_yn = cv2.FaceDetectorYN_create(str(cand), "", (320, 180), 0.7, 0.3, 5000)
                    print(f"[analyze] FaceDetectorYN loaded ({cand.name})")
                    break
    except Exception as e:
        print(f"[analyze] FaceDetectorYN skipped: {e}")
        face_yn = None
    if face_yn is None:
        try:
            if hasattr(cv2, "CascadeClassifier") and hasattr(cv2, "data"):
                cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
                face_cascade = cv2.CascadeClassifier(cascade_path)
                if face_cascade.empty():
                    face_cascade = None
            if face_cascade is None:
                print("[analyze] no face model — upper-third motion fallback")
        except Exception as e:
            print(f"[analyze] Haar unavailable ({e}) — upper-third fallback")
            face_cascade = None

    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        return np.zeros(1, dtype=np.float32)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    duration = total / fps if fps else 0
    n_secs = max(1, int(duration) + 1)
    scores = np.zeros(n_secs, dtype=np.float32)
    counts = np.zeros(n_secs, dtype=np.float32)
    step = max(1, int(fps / sample_fps))
    prev_roi = None
    idx = 0
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        if idx % step == 0:
            small = cv2.resize(frame, (320, 180))
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            t = idx / fps
            sec = min(n_secs - 1, int(t))
            face_present = 0.0
            motion = 0.0
            roi = None
            if face_yn is not None:
                try:
                    face_yn.setInputSize((small.shape[1], small.shape[0]))
                    _, faces = face_yn.detect(small)
                    if faces is not None and len(faces) > 0:
                        # x, y, w, h, ...
                        best = max(faces, key=lambda f: float(f[2]) * float(f[3]))
                        x, y, w, h = [int(v) for v in best[:4]]
                        x, y = max(0, x), max(0, y)
                        roi = gray[y:y + max(1, h), x:x + max(1, w)]
                        face_present = 1.0
                except Exception:
                    roi = None
            elif face_cascade is not None:
                faces = face_cascade.detectMultiScale(
                    gray, scaleFactor=1.2, minNeighbors=4, minSize=(28, 28)
                )
                if len(faces) > 0:
                    x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
                    roi = gray[y:y + h, x:x + w]
                    face_present = 1.0
            if roi is None or roi.size == 0:
                hh, ww = gray.shape[:2]
                roi = gray[0: max(1, hh // 3), :]
                face_present = 0.55
            roi = cv2.resize(roi, (48, 48))
            if prev_roi is not None and prev_roi.shape == roi.shape:
                motion = float(np.mean(np.abs(roi.astype(np.float32) - prev_roi.astype(np.float32))))
            prev_roi = roi
            scores[sec] += face_present * (0.4 + min(2.5, motion / 12.0))
            counts[sec] += 1
        idx += 1
    cap.release()
    counts = np.maximum(counts, 1)
    return scores / counts




def _snap_window(
    start: float,
    end: float,
    *,
    duration: float,
    scenes: list[float],
    min_d: float,
    max_d: float,
    peak_t: float | None = None,
) -> tuple[float, float]:
    """Snap start to nearest prior scene/sentence-ish boundary; end after peak at next cut."""
    sc = sorted(t for t in scenes if 0 < t < duration)
    # Start: nearest scene within 3s before start, else keep
    best_s = start
    for t in reversed(sc):
        if t <= start + 0.15 and start - t <= 3.5:
            best_s = t
            break
    # End: first scene after peak (or after 60% of window), within max_d
    peak = peak_t if peak_t is not None else (start + end) / 2
    best_e = end
    for t in sc:
        if t >= max(peak + 0.4, best_s + min_d * 0.7) and t - best_s <= max_d + 0.5:
            best_e = t
            break
    # Ensure duration bounds
    if best_e - best_s < min_d:
        best_e = min(duration, best_s + min_d)
    if best_e - best_s > max_d:
        best_e = best_s + max_d
    if best_e > duration:
        best_e = duration
        best_s = max(0.0, best_e - max_d)
    return round(best_s, 2), round(best_e, 2)


def find_highlights(
    path: Path, cfg: dict[str, Any]
) -> list[Segment]:
    """Score timeline; for reaction niches bias to laugh/scream/face peaks and center with lead-in."""
    clips_cfg = cfg.get("clips", {})
    ana = cfg.get("analysis", {})
    weights = ana.get("weights") or {}
    w_loud = float(weights.get("loudness", 1.0))
    w_scene = float(weights.get("scene_change", 1.2))
    w_motion = float(weights.get("motion", 0.8))
    w_kw = float(weights.get("keyword", 1.5))
    w_laugh = float(weights.get("laughter", 1.6))
    w_scream = float(weights.get("scream", 1.8))
    w_face = float(weights.get("face_motion", 1.7))

    min_d = float(clips_cfg.get("min_duration", 15))
    max_d = float(clips_cfg.get("max_duration", 60))
    target = float(clips_cfg.get("target_duration", 30))
    max_clips = int(clips_cfg.get("max_clips_per_video", 3))
    lead_in = float(clips_cfg.get("reaction_lead_in", 2.2))

    niche_id = str(cfg.get("niche_id") or (cfg.get("niche") or {}).get("id") or "").lower()
    reaction_mode = niche_id in {"reactions", "funny"} or "reaction" in niche_id

    duration = _video_duration(path)
    if duration < min_d:
        if duration >= 5:
            return [Segment(0, duration, 1.0, "full_short")]
        return []

    print("[analyze] loudness…")
    loud = extract_loudness_peaks(path, window=1.0)
    print("[analyze] scenes…")
    scenes = extract_scene_changes(path, threshold=float(ana.get("scene_threshold", 27)))
    print("[analyze] motion…")
    motion = extract_motion_scores(path)

    spikes = laugh = scream = face = None
    if reaction_mode or ana.get("always_reaction_signals", False):
        print("[analyze] laughter/scream spikes…")
        spikes, laugh, scream = extract_laughter_scream_scores(path)
        print("[analyze] face motion…")
        face = extract_face_motion_scores(path)

    n = max(len(loud), len(motion), int(duration) + 1)
    if spikes is not None:
        n = max(n, len(spikes), len(laugh), len(scream))
    if face is not None:
        n = max(n, len(face))
    score = np.zeros(n, dtype=np.float32)

    loud_n = _normalize(loud)
    for i, v in enumerate(loud_n):
        if i < n:
            score[i] += w_loud * v

    motion_n = _normalize(motion)
    for i, v in enumerate(motion_n):
        if i < n:
            score[i] += w_motion * v

    scene_arr = np.zeros(n, dtype=np.float32)
    for t in scenes:
        i = int(t)
        for j in range(max(0, i - 1), min(n, i + 2)):
            scene_arr[j] += 1.0
    score += w_scene * _normalize(scene_arr)

    laugh_n = scream_n = face_n = spike_n = np.zeros(n, dtype=np.float32)
    if spikes is not None:
        spike_n = _normalize(spikes)
        laugh_n = _normalize(laugh)
        scream_n = _normalize(scream)
        for i in range(min(n, len(spike_n))):
            score[i] += w_loud * 0.5 * float(spike_n[i])
            score[i] += w_laugh * float(laugh_n[i])
            score[i] += w_scream * float(scream_n[i])
    if face is not None:
        face_n = _normalize(face)
        for i in range(min(n, len(face_n))):
            score[i] += w_face * float(face_n[i])

    transcript_hits: list[tuple[float, float, str]] = []
    if ana.get("use_whisper", True):
        print("[analyze] transcript (faster-whisper)…")
        transcript_hits = transcribe_keywords(
            path,
            ana.get("transcript_boost_words") or [],
            model_name=str(ana.get("whisper_model", "base")),
            niche_id=niche_id or None,
        )
        kw_arr = np.zeros(n, dtype=np.float32)
        for s, e, _txt in transcript_hits:
            for j in range(max(0, int(s)), min(n, int(e) + 1)):
                kw_arr[j] += 1.0
        score += w_kw * _normalize(kw_arr)

    kernel = np.ones(5) / 5.0
    smooth = np.convolve(score, kernel, mode="same")

    half = target / 2.0
    pool: list[Segment] = []
    used = np.zeros(n, dtype=bool)
    order = np.argsort(smooth)[::-1]
    pool_target = max(max_clips * 4, 8)

    for idx in order:
        if len(pool) >= pool_target:
            break
        if used[idx]:
            continue
        if smooth[idx] < 0.05 and len(pool) > 0:
            break
        peak_t = float(idx)
        # Center on peak with short lead-in (especially for reactions)
        if reaction_mode:
            start = max(0.0, peak_t - lead_in)
            end = min(duration, start + target)
            start = max(0.0, end - target)
            # Prefer keeping the peak inside the window with lead-in
            if peak_t - start < lead_in * 0.5:
                start = max(0.0, peak_t - lead_in)
                end = min(duration, start + target)
        else:
            start = max(0.0, peak_t - half)
            end = min(duration, start + target)
            start = max(0.0, end - target)
        length = end - start
        if length < min_d:
            end = min(duration, start + min_d)
            start = max(0.0, end - min_d)
            length = end - start
        if length > max_d:
            end = start + max_d
            length = max_d
        if length < min_d * 0.8:
            continue
        pad = 3
        for j in range(max(0, int(start) - pad), min(n, int(end) + pad)):
            used[j] = True
        reasons = []
        ii = min(idx, n - 1)
        if loud_n[min(idx, len(loud_n) - 1)] > 0.6:
            reasons.append("loud")
        if scene_arr[ii] > 0:
            reasons.append("cut")
        if motion_n[min(idx, len(motion_n) - 1)] > 0.5:
            reasons.append("motion")
        if any(s <= idx <= e + 1 for s, e, _ in transcript_hits):
            reasons.append("keyword")
        if len(laugh_n) > ii and laugh_n[ii] > 0.35:
            reasons.append("laugh")
        if len(scream_n) > ii and scream_n[ii] > 0.35:
            reasons.append("scream")
        if len(face_n) > ii and face_n[ii] > 0.35:
            reasons.append("face")
        if len(spike_n) > ii and spike_n[ii] > 0.5:
            reasons.append("audio_spike")
        if "loud" in reasons and "keyword" in reasons:
            reasons.append("hype_speech")
        if reaction_mode and ("laugh" in reasons or "scream" in reasons or "face" in reasons):
            reasons.append("react_peak")
        start, end = _snap_window(
            start, end, duration=duration, scenes=scenes,
            min_d=min_d, max_d=max_d, peak_t=peak_t,
        )
        pool.append(
            Segment(start=round(start, 2), end=round(end, 2),
                    score=float(smooth[idx]), reason="+".join(reasons) or "peak")
        )

    pool.sort(key=lambda s: s.score, reverse=True)
    candidates: list[Segment] = []
    if pool:
        top = pool[: max(max_clips * 3, max_clips)]
        weights_l = [max(0.05, s.score) ** 1.5 for s in top]
        # Prefer reaction peaks when in reaction mode
        if reaction_mode:
            for j, seg in enumerate(top):
                if "react_peak" in seg.reason or "laugh" in seg.reason or "scream" in seg.reason:
                    weights_l[j] *= 2.2
        chosen_idx: set[int] = set()
        while len(candidates) < max_clips and len(chosen_idx) < len(top):
            i = random.choices(range(len(top)), weights=weights_l, k=1)[0]
            if i in chosen_idx:
                for j, _ in sorted(enumerate(top), key=lambda x: -x[1].score):
                    if j not in chosen_idx:
                        i = j
                        break
            chosen_idx.add(i)
            candidates.append(top[i])
            for j, seg in enumerate(top):
                if j in chosen_idx:
                    continue
                if abs(seg.start - top[i].start) < target * 0.6:
                    weights_l[j] *= 0.15
        candidates.sort(key=lambda s: s.start)
        print(f"[analyze] picked {len(candidates)}/{len(pool)} varied peak windows (reaction={reaction_mode})")

    if duration <= max_d and not candidates:
        candidates = [Segment(0, duration, 1.0, "full")]

    if not candidates and duration >= min_d:
        start = max(0, (duration - target) / 2)
        candidates = [Segment(start, min(duration, start + target), 0.5, "fallback_mid")]

    return candidates



def get_word_timestamps(
    path: Path,
    start: float,
    end: float,
    model_name: str = "base",
    **kwargs,
) -> list[dict[str, Any]]:
    """Delegate to robust transcribe module (vocals pre-filter, relaxed VAD)."""
    from .transcribe import get_word_timestamps as _gw
    return _gw(path, start, end, model_name=model_name, **kwargs)


def transcribe_keywords(
    path: Path,
    boost_words: list[str],
    model_name: str = "base",
    niche_id: str | None = None,
    **kwargs,
) -> list[tuple[float, float, str]]:
    from .transcribe import transcribe_full_for_keywords
    return transcribe_full_for_keywords(
        path, boost_words, model_name=model_name, niche_id=niche_id
    )

