"""Robust free transcription for dialogue-under-music (faster-whisper).

Drops hallucinated / music-only / off-niche whisper segments.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Any

# Soft music / filler loops whisper invents under score
_BAD_LOOP = {
    "music", "music.", "♪", "la", "na", "da", "ooh", "ooo", "ahh", "ah",
    "hmm", "mm", "uh", "um", "subtitles", "subscriber", "thanks for watching",
    "please subscribe", "like and subscribe",
}

# Absolute garbage phrases that signal hallucination
_HALLUCINATION_PHRASES = (
    "thanks for watching",
    "please subscribe",
    "like and subscribe",
    "字幕",
    "www.",
    "http",
    "amara.org",
    "translated by",
    "captioned by",
)


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
        cmd2 = ["ffmpeg", "-y"]
        if start is not None:
            cmd2 += ["-ss", f"{start:.3f}"]
        if end is not None and start is not None:
            cmd2 += ["-t", f"{max(0.1, end - start):.3f}"]
        cmd2 += ["-i", str(path), "-vn", "-ac", "1", "-ar", "16000", str(out)]
        subprocess.run(cmd2, capture_output=True, check=False)
    return out


def _niche_prompt(niche_id: str | None) -> str:
    niche = (niche_id or "gta6").lower()
    prompts = {
        "gta6": "Grand Theft Auto dialogue. Lucia and Jason talking. Clear spoken English under music.",
        "anime": "Anime English dub or commentary. Clear spoken English. Not music lyrics.",
        "reactions": "Streamer reaction commentary. Clear spoken English. Laughter and short reactions.",
        "gaming": "Gaming commentary and callouts. Clear spoken English.",
        "funny": "Comedy dialogue and reactions. Clear spoken English.",
        "movies": "Movie trailer dialogue or commentary. Clear spoken English.",
        "animation": "Animated show dialogue. Clear spoken English.",
    }
    return prompts.get(niche, "Clear spoken English dialogue. Not music lyrics.")


def _is_repeated_text(text: str) -> bool:
    toks = re.findall(r"[A-Za-z']+", text.lower())
    if len(toks) < 4:
        return False
    # same token dominates
    from collections import Counter
    c = Counter(toks)
    top, n = c.most_common(1)[0]
    if n >= max(4, int(0.55 * len(toks))) and len(top) <= 6:
        return True
    # exact phrase loop: "la la la la"
    if len(set(toks)) <= 2 and len(toks) >= 5:
        return True
    return False


def _segment_ok(
    seg: Any,
    *,
    niche_id: str | None = None,
    min_avg_logprob: float = -0.65,
    max_no_speech: float = 0.55,
    max_compression: float = 2.4,
) -> bool:
    """Quality gate for a faster-whisper segment."""
    text = (getattr(seg, "text", None) or "").strip()
    if not text:
        return False
    low = text.lower()
    if any(p in low for p in _HALLUCINATION_PHRASES):
        return False
    if _is_repeated_text(text):
        return False
    # Music-only: almost all tokens are filler
    toks = re.findall(r"[A-Za-z♪]+", low)
    if toks:
        bad = sum(1 for t in toks if t in _BAD_LOOP or t.startswith("♪"))
        if bad / len(toks) >= 0.6:
            return False
    avg_lp = float(getattr(seg, "avg_logprob", 0.0) or 0.0)
    no_speech = float(getattr(seg, "no_speech_prob", 0.0) or 0.0)
    comp = float(getattr(seg, "compression_ratio", 1.0) or 1.0)
    if avg_lp < min_avg_logprob:
        return False
    if no_speech > max_no_speech:
        return False
    if comp > max_compression:
        return False
    # Soft off-niche: if niche is anime/gta and text is pure other-niche spam, drop
    niche = (niche_id or "").lower()
    if niche == "anime" and re.search(r"\b(gta\s*6|grand theft auto|rockstar)\b", low):
        return False
    if niche == "gta6" and re.search(r"\b(naruto|one piece|jujutsu|demon slayer)\b", low):
        return False
    # Trailer on-screen text / announcer hallucinations (not streamer speech)
    if re.search(
        r"(grand theft auto|gta\s*6).{0,40}(coming|november|pre-?order|february|launch)",
        low,
    ) and not re.search(r"\b(i|i'm|im|bro|chat|dude|oh my|fuck|wait|look)\b", low):
        return False
    return True


def _clean_words(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not words:
        return words
    out = []
    streak = 0
    for w in words:
        tok = (w.get("word") or "").strip()
        low = tok.lower().strip(".,!?")
        if low in _BAD_LOOP or low.startswith("♪"):
            streak += 1
            if streak >= 2:
                continue
        else:
            streak = 0
        out.append(w)
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
    niche_id: str | None = None,
) -> list[dict[str, Any]]:
    model = _cached_model(model_name)
    prompt = initial_prompt or _niche_prompt(niche_id)
    # Stricter thresholds than before; we also filter per-segment below
    segments, info = model.transcribe(
        str(wav),
        language=language,
        word_timestamps=True,
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=400),
        no_speech_threshold=0.6,
        compression_ratio_threshold=2.4,
        log_prob_threshold=-1.0,
        condition_on_previous_text=False,
        beam_size=5,
        best_of=5,
        temperature=[0.0, 0.2],
        initial_prompt=prompt,
    )
    words: list[dict[str, Any]] = []
    kept = 0
    dropped = 0
    for seg in segments:
        if not _segment_ok(seg, niche_id=niche_id):
            dropped += 1
            continue
        kept += 1
        if seg.words:
            for w in seg.words:
                token = (w.word or "").strip()
                if not token:
                    continue
                low = token.lower().strip(".,!?")
                if low in _BAD_LOOP or low.startswith("♪"):
                    continue
                words.append({
                    "word": token,
                    "start": float(w.start),
                    "end": max(float(w.end), float(w.start) + 0.05),
                })
        else:
            text = (seg.text or "").strip()
            if text:
                parts = text.split()
                dur = max(0.2, float(seg.end) - float(seg.start))
                step = dur / max(1, len(parts))
                for i, p in enumerate(parts):
                    words.append({
                        "word": p,
                        "start": float(seg.start) + i * step,
                        "end": float(seg.start) + (i + 1) * step,
                    })
    if dropped:
        print(f"[transcribe] dropped {dropped} low-quality/music segments (kept {kept})")
    return _clean_words(words)


def get_word_timestamps(
    path: Path,
    start: float,
    end: float,
    *,
    model_name: str = "base",
    language: str | None = "en",
    niche_id: str | None = None,
) -> list[dict[str, Any]]:
    """
    Word-level timestamps relative to clip start.
    Returns [] (no captions) for music-only / no-speech — never fake placeholders.
    """
    duration = max(0.5, end - start)
    attempts: list[tuple[str, str, bool]] = [
        ("vocals+model", model_name, True),
        ("plain+model", model_name, False),
    ]
    alt = "tiny" if model_name != "tiny" else "base"
    attempts.append((f"vocals+{alt}", alt, True))

    last_err = None
    for label, mid, use_vocals in attempts:
        wav: Path | None = None
        try:
            print(f"[transcribe] attempt {label} window={start:.1f}-{end:.1f}s niche={niche_id or '-'}")
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
            words = _transcribe_wav(wav, mid, language=language, niche_id=niche_id)
            if words:
                sample = " ".join(w["word"] for w in words[:12])
                print(f"[transcribe] {label}: got {len(words)} words — {sample}…")
                return words
            print(f"[transcribe] {label}: 0 words (music-only or filtered)")
        except Exception as e:
            last_err = e
            print(f"[transcribe] {label} error: {e}")
        finally:
            if wav is not None:
                try:
                    wav.unlink(missing_ok=True)
                except OSError:
                    pass

    print(
        "[transcribe] no reliable dialogue after quality filters"
        + (f" (last error: {last_err})" if last_err else "")
        + " — skipping captions for this clip (no music placeholders)"
    )
    return []


def transcribe_full_for_keywords(
    path: Path,
    boost_words: list[str],
    *,
    model_name: str = "base",
    niche_id: str | None = None,
) -> list[tuple[float, float, str]]:
    wav = None
    try:
        wav = extract_vocals_wav(path)
        words = _transcribe_wav(wav, model_name, niche_id=niche_id)
        boost_lower = [w.lower() for w in boost_words]
        hits: list[tuple[float, float, str]] = []
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


# Back-compat alias used by analyze.py
def transcribe_keywords(
    path: Path,
    boost_words: list[str],
    *,
    model_name: str = "base",
    niche_id: str | None = None,
) -> list[tuple[float, float, str]]:
    return transcribe_full_for_keywords(
        path, boost_words, model_name=model_name, niche_id=niche_id
    )
