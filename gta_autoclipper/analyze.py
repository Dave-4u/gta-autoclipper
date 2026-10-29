"""Highlight detection using free signals: loudness, scenes, motion, transcript."""

from __future__ import annotations

import json
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


def find_highlights(
    path: Path, cfg: dict[str, Any]
) -> list[Segment]:
    """Score the timeline and return best clip windows."""
    clips_cfg = cfg.get("clips", {})
    ana = cfg.get("analysis", {})
    weights = ana.get("weights") or {}
    w_loud = float(weights.get("loudness", 1.0))
    w_scene = float(weights.get("scene_change", 1.2))
    w_motion = float(weights.get("motion", 0.8))
    w_kw = float(weights.get("keyword", 1.5))

    min_d = float(clips_cfg.get("min_duration", 15))
    max_d = float(clips_cfg.get("max_duration", 60))
    target = float(clips_cfg.get("target_duration", 30))
    max_clips = int(clips_cfg.get("max_clips_per_video", 3))

    duration = _video_duration(path)
    if duration < min_d:
        # Whole video as one clip if short enough
        if duration >= 5:
            return [Segment(0, duration, 1.0, "full_short")]
        return []

    print("[analyze] loudness…")
    loud = extract_loudness_peaks(path, window=1.0)
    print("[analyze] scenes…")
    scenes = extract_scene_changes(path, threshold=float(ana.get("scene_threshold", 27)))
    print("[analyze] motion…")
    motion = extract_motion_scores(path)

    n = max(len(loud), len(motion), int(duration) + 1)
    score = np.zeros(n, dtype=np.float32)

    loud_n = _normalize(loud)
    for i, v in enumerate(loud_n):
        if i < n:
            score[i] += w_loud * v

    motion_n = _normalize(motion)
    for i, v in enumerate(motion_n):
        if i < n:
            score[i] += w_motion * v

    # Scene density: +1 in a 2s window around each cut
    scene_arr = np.zeros(n, dtype=np.float32)
    for t in scenes:
        i = int(t)
        for j in range(max(0, i - 1), min(n, i + 2)):
            scene_arr[j] += 1.0
    score += w_scene * _normalize(scene_arr)

    transcript_hits: list[tuple[float, float, str]] = []
    if ana.get("use_whisper", True):
        print("[analyze] transcript (faster-whisper)…")
        transcript_hits = transcribe_keywords(
            path,
            ana.get("transcript_boost_words") or [],
            model_name=str(ana.get("whisper_model", "base")),
        )
        kw_arr = np.zeros(n, dtype=np.float32)
        for s, e, _txt in transcript_hits:
            for j in range(max(0, int(s)), min(n, int(e) + 1)):
                kw_arr[j] += 1.0
        score += w_kw * _normalize(kw_arr)

    # Smooth with moving average
    kernel = np.ones(5) / 5.0
    smooth = np.convolve(score, kernel, mode="same")

    # Pick peaks: greedy non-overlapping windows of ~target length
    half = target / 2.0
    candidates: list[Segment] = []
    used = np.zeros(n, dtype=bool)
    order = np.argsort(smooth)[::-1]

    for idx in order:
        if len(candidates) >= max_clips:
            break
        if used[idx]:
            continue
        if smooth[idx] < 0.05 and len(candidates) > 0:
            break
        start = max(0.0, idx - half)
        end = min(duration, start + target)
        start = max(0.0, end - target)
        # Clamp to [min_d, max_d]
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
        # Mark used region with padding
        pad = 3
        for j in range(max(0, int(start) - pad), min(n, int(end) + pad)):
            used[j] = True
        reasons = []
        if loud_n[min(idx, len(loud_n) - 1)] > 0.6:
            reasons.append("loud")
        if scene_arr[min(idx, n - 1)] > 0:
            reasons.append("cut")
        if motion_n[min(idx, len(motion_n) - 1)] > 0.5:
            reasons.append("motion")
        if any(s <= idx <= e + 1 for s, e, _ in transcript_hits):
            reasons.append("keyword")
        candidates.append(
            Segment(start=round(start, 2), end=round(end, 2),
                    score=float(smooth[idx]), reason="+".join(reasons) or "peak")
        )

    candidates.sort(key=lambda s: s.start)

    # If source is already short vertical-friendly, also offer full (capped)
    if duration <= max_d and not candidates:
        candidates = [Segment(0, duration, 1.0, "full")]

    if not candidates and duration >= min_d:
        # Fallback: middle chunk
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
    path: Path, boost_words: list[str], model_name: str = "base"
) -> list[tuple[float, float, str]]:
    from .transcribe import transcribe_full_for_keywords
    return transcribe_full_for_keywords(path, boost_words, model_name=model_name)

