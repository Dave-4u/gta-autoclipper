"""Robust free transcription for dialogue-under-music (faster-whisper)."""

from __future__ import annotations

import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Any


def _whisper_model(name: str):
    from faster_whisper import WhisperModel

    return WhisperModel(name, device="cpu", compute_type="int8")


@lru_cache(maxsize=2)
def _cached_model(name: str):
    print(f"[transcribe] loading whisper model '{name}'…")
    return _whisper_model(name)


def extract_vocals_wav(
    path: Path,
    *,
    start: float | None = None,
    end: float | None = None,
    out: Path | None = None,
) -> Path:
    """
    Cheap vocal-emphasis pre-filter (no paid Demucs):
    highpass + lowpass speech band, light denoise, dynaudnorm, boost.
    """
    if out is None:
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        out = Path(tmp.name)
        tmp.close()

    af = (
        "highpass=f=120,"
        "lowpass=f=3800,"
        "afftdn=nr=10:nf=-25,"
        "dynaudnorm=f=75:g=15,"
        "volume=2.5"
    )
    cmd = ["ffmpeg", "-y"]
    if start is not None:
        cmd += ["-ss", f"{start:.3f}"]
    if end is not None and start is not None:
        cmd += ["-t", f"{max(0.1, end - start):.3f}"]
    elif end is not None:
        cmd += ["-to", f"{end:.3f}"]
    cmd += [
        "-i", str(path),
        "-vn",
        "-af", af,
        "-ac", "1",
        "-ar", "16000",
        "-c:a", "pcm_s16le",
        str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0 or not out.is_file() or out.stat().st_size < 1000:
        # Fallback: plain mono extract
        cmd2 = ["ffmpeg", "-y"]
        if start is not None:
            cmd2 += ["-ss", f"{start:.3f}"]
        if end is not None and start is not None:
            cmd2 += ["-t", f"{max(0.1, end - start):.3f}"]
        cmd2 += ["-i", str(path), "-vn", "-ac", "1", "-ar", "16000", str(out)]
        subprocess.run(cmd2, capture_output=True, check=False)
    return out


def _clean_words(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop obvious music-hallucination loops whisper invents under score."""
    if not words:
        return words
    bad_loop = {"music", "music.", "♪", "la", "na", "da", "ooh", "ooo", "ahh"}
    out = []
    streak = 0
    for w in words:
        tok = (w.get("word") or "").strip()
        low = tok.lower().strip(".,!?")
        if low in bad_loop or low.startswith("♪"):
            streak += 1
            if streak >= 3:
                continue
        else:
            streak = 0
        out.append(w)
    # Collapse exact duplicate tokens at identical timestamps
    dedup = []
    seen = set()
    for w in out:
        key = (round(float(w["start"]), 2), (w["word"] or "").lower())
        if key in seen:
            continue
        seen.add(key)
        dedup.append(w)
    return dedup


def _transcribe_wav(
    wav: Path,
    model_name: str,
    *,
    language: str | None = "en",
    initial_prompt: str | None = None,
) -> list[dict[str, Any]]:
    """Run faster-whisper with relaxed silence / VAD settings. Returns word dicts."""
    model = _cached_model(model_name)
    prompt = initial_prompt or (
        "Grand Theft Auto dialogue. Lucia and Jason talking. "
        "Clear spoken English under music."
    )
    # Raised no_speech_threshold => harder to discard as silence (keep dialogue under music)
    # vad_filter False => don't drop quiet speech under score
    segments, info = model.transcribe(
        str(wav),
        language=language,
        word_timestamps=True,
        vad_filter=False,
        no_speech_threshold=0.9,
        compression_ratio_threshold=2.8,
        log_prob_threshold=-1.2,
        condition_on_previous_text=False,
        beam_size=5,
        best_of=5,
        temperature=[0.0, 0.2, 0.4],
        initial_prompt=prompt,
    )
    words: list[dict[str, Any]] = []
    for seg in segments:
        if seg.words:
            for w in seg.words:
                token = (w.word or "").strip()
                if not token:
                    continue
                words.append({
                    "word": token,
                    "start": float(w.start),
                    "end": max(float(w.end), float(w.start) + 0.05),
                })
        else:
            text = (seg.text or "").strip()
            if text:
                # Split roughly so we still burn something word-like
                parts = text.split()
                dur = max(0.2, float(seg.end) - float(seg.start))
                step = dur / max(1, len(parts))
                for i, p in enumerate(parts):
                    words.append({
                        "word": p,
                        "start": float(seg.start) + i * step,
                        "end": float(seg.start) + (i + 1) * step,
                    })
    return _clean_words(words)


def get_word_timestamps(
    path: Path,
    start: float,
    end: float,
    *,
    model_name: str = "base",
    language: str | None = "en",
) -> list[dict[str, Any]]:
    """
    Word-level timestamps relative to clip start (0 = segment.start).
    Always tries hard: vocals WAV → plain WAV → alternate model.
    Never silently returns empty without logging.
    """
    duration = max(0.5, end - start)
    attempts: list[tuple[str, str, bool]] = [
        # (label, model, use_vocals_filter)
        ("vocals+model", model_name, True),
        ("plain+model", model_name, False),
    ]
    # If base failed, try tiny as last resort (and vice versa)
    alt = "tiny" if model_name != "tiny" else "base"
    attempts.append((f"vocals+{alt}", alt, True))
    attempts.append((f"plain+{alt}", alt, False))

    last_err = None
    for label, mid, use_vocals in attempts:
        wav: Path | None = None
        try:
            print(f"[transcribe] attempt {label} window={start:.1f}-{end:.1f}s")
            if use_vocals:
                wav = extract_vocals_wav(path, start=start, end=end)
            else:
                wav = Path(tempfile.mktemp(suffix=".wav"))
                cmd = [
                    "ffmpeg", "-y",
                    "-ss", f"{start:.3f}", "-t", f"{duration:.3f}",
                    "-i", str(path),
                    "-vn", "-ac", "1", "-ar", "16000", str(wav),
                ]
                subprocess.run(cmd, capture_output=True, check=False)
            if not wav.is_file() or wav.stat().st_size < 1000:
                print(f"[transcribe] {label}: wav extract failed")
                continue
            words = _transcribe_wav(wav, mid, language=language)
            # Timestamps already relative to window (we cut the wav)
            if words:
                sample = " ".join(w["word"] for w in words[:12])
                print(f"[transcribe] {label}: got {len(words)} words — {sample}…")
                return words
            print(f"[transcribe] {label}: 0 words")
        except Exception as e:
            last_err = e
            print(f"[transcribe] {label} error: {e}")
        finally:
            if wav is not None:
                try:
                    wav.unlink(missing_ok=True)
                except OSError:
                    pass

    # Absolute last resort: force a visible placeholder so we never burn blank
    print(
        "[transcribe] WARNING: no dialogue words detected after all attempts"
        + (f" (last error: {last_err})" if last_err else "")
        + " — burning [♪ music / unclear dialogue] placeholders"
    )
    # Spread a few markers so the caption track is visibly present
    markers = []
    t = 0.5
    while t < duration - 0.5:
        markers.append({
            "word": "[dialogue]",
            "start": t,
            "end": min(duration, t + 1.5),
        })
        t += 4.0
    if not markers:
        markers = [{"word": "[dialogue]", "start": 0.2, "end": min(2.0, duration)}]
    return markers


def transcribe_full_for_keywords(
    path: Path,
    boost_words: list[str],
    *,
    model_name: str = "base",
) -> list[tuple[float, float, str]]:
    """Full-file keyword hits for highlight scoring (absolute timestamps)."""
    wav = None
    try:
        wav = extract_vocals_wav(path)
        words = _transcribe_wav(wav, model_name)
        boost_lower = [w.lower() for w in boost_words]
        hits: list[tuple[float, float, str]] = []
        # Rebuild rough segment text in 3s buckets for keyword match
        if not words:
            print("[transcribe] keyword pass: no words")
            return []
        bucket: list[dict[str, Any]] = []
        for w in words:
            bucket.append(w)
            text = " ".join(x["word"] for x in bucket)
            if len(bucket) >= 8 or any(b in text.lower() for b in boost_lower):
                if any(b in text.lower() for b in boost_lower):
                    hits.append((float(bucket[0]["start"]), float(bucket[-1]["end"]), text))
                bucket = []
        return hits
    except Exception as e:
        print(f"[transcribe] keyword pass failed: {e}")
        return []
    finally:
        if wav is not None:
            try:
                wav.unlink(missing_ok=True)
            except OSError:
                pass
