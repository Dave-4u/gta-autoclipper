"""Free CPU voiceover: Kokoro (primary) → edge-tts → Piper fallback.

Per-niche voice profiles, rate/pitch variation, punctuation pauses, loudnorm polish.
"""

from __future__ import annotations

import json
import random
import re
import subprocess
import tempfile
import urllib.error
import urllib.request
import wave
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from .hooks import HookContext, _clean_title, _detect_scene_type, _sentiment, _truncate

SCRIPT_STYLES = ("hype", "commentary", "reaction", "news")

# Niche → preferred Kokoro voices (rotate among list). af_=female am_=male US; bf_/bm_=UK
NICHE_VOICE_PROFILES: dict[str, dict[str, Any]] = {
    "gta6": {
        "kokoro": ["am_fenrir", "am_michael", "am_onyx", "am_adam"],  # deep hype male
        "edge": ["en-US-AndrewMultilingualNeural", "en-US-BrianNeural", "en-US-GuyNeural"],
        "piper": ["en_US-ryan-high", "en_US-lessac-high"],
        "speed": (0.92, 1.02),
        "style": "hype",
    },
    "anime": {
        "kokoro": ["af_bella", "af_nicole", "af_sarah", "am_puck"],  # energetic
        "edge": ["en-US-AvaMultilingualNeural", "en-US-JennyNeural", "en-US-AnaNeural"],
        "piper": ["en_US-amy-medium", "en_US-lessac-medium"],
        "speed": (1.02, 1.12),
        "style": "hype",
    },
    "reactions": {
        "kokoro": ["am_echo", "am_liam", "af_sky", "am_puck"],  # casual/fun
        "edge": ["en-US-BrianNeural", "en-US-ChristopherNeural", "en-US-RogerNeural"],
        "piper": ["en_US-joe-medium", "en_US-ryan-medium"],
        "speed": (1.0, 1.08),
        "style": "reaction",
    },
    "ranking": {
        "kokoro": ["am_fenrir", "am_michael", "bm_george", "am_onyx"],  # countdown hype
        "edge": ["en-US-AndrewMultilingualNeural", "en-US-BrianNeural"],
        "piper": ["en_US-ryan-high"],
        "speed": (0.95, 1.05),
        "style": "hype",
    },
    "gaming": {
        "kokoro": ["am_adam", "am_echo", "am_liam"],
        "edge": ["en-US-BrianNeural", "en-US-GuyNeural"],
        "piper": ["en_US-ryan-high"],
        "speed": (1.0, 1.08),
        "style": "hype",
    },
    "funny": {
        "kokoro": ["am_puck", "af_sky", "am_echo"],
        "edge": ["en-US-ChristopherNeural", "en-US-AvaMultilingualNeural"],
        "piper": ["en_US-joe-medium"],
        "speed": (1.02, 1.12),
        "style": "reaction",
    },
    "movies": {
        "kokoro": ["bm_george", "am_michael", "af_bella"],
        "edge": ["en-US-AndrewMultilingualNeural", "en-GB-RyanNeural"],
        "piper": ["en_US-lessac-high"],
        "speed": (0.95, 1.02),
        "style": "commentary",
    },
    "animation": {
        "kokoro": ["af_nicole", "af_bella", "am_puck"],
        "edge": ["en-US-AvaMultilingualNeural", "en-US-JennyNeural"],
        "piper": ["en_US-lessac-high"],
        "speed": (1.0, 1.08),
        "style": "commentary",
    },
}

ALL_KOKORO_VOICES = sorted({
    v for p in NICHE_VOICE_PROFILES.values() for v in p["kokoro"]
})
ALL_EDGE_VOICES = sorted({
    v for p in NICHE_VOICE_PROFILES.values() for v in p["edge"]
})


@dataclass
class VoiceoverResult:
    text: str
    style: str
    via: str
    wav_path: Path | None
    words: list[dict[str, Any]]
    engine: str
    voice: str = ""


def _profile(niche_id: str) -> dict[str, Any]:
    return NICHE_VOICE_PROFILES.get((niche_id or "").lower()) or NICHE_VOICE_PROFILES["gta6"]


def _pick_script_style(ctx: HookContext, cfg_style: str, recent: list[str]) -> str:
    if cfg_style and cfg_style != "auto" and cfg_style in SCRIPT_STYLES:
        return cfg_style
    prof = _profile(ctx.niche_id)
    if prof.get("style") in SCRIPT_STYLES:
        preferred = [prof["style"], "hype", "commentary", "reaction", "news"]
    else:
        preferred = ["commentary", "hype", "reaction", "news"]
    scene = _detect_scene_type(ctx)
    if scene == "reaction":
        preferred = ["reaction", "hype"] + preferred
    banned = set(recent[-2:]) if recent else set()
    for s in preferred:
        if s not in banned:
            return s
    return preferred[0]


def _rule_script(style: str, ctx: HookContext, scene: str, vocab: list[str] | None = None) -> str:
    """One short grounded line — never title echo or analyzer jargon."""
    from .entities import NEUTRAL_VO, event_summary, validate_copy, extract_entities

    es = getattr(ctx, "entities", None)
    if es is None:
        es = extract_entities(
            title=ctx.title,
            channel=getattr(ctx, "channel", "") or "",
            description=getattr(ctx, "description", "") or "",
            tags=list(getattr(ctx, "tags", None) or []),
            transcript=ctx.transcript_snippet or "",
            niche_id=ctx.niche_id,
        )
        ctx.entities = es
    event = getattr(ctx, "event", "") or event_summary(es, ctx.title, ctx.transcript_snippet, ctx.reason)
    conf = float(getattr(ctx, "confidence", None) or getattr(es, "confidence", 0) or 0)

    detail = ""
    evl = (event or "").lower()
    if "reaction" in evl and es.streamers:
        detail = es.streamers[0]
    elif es.streamers and (ctx.niche_id or "") == "reactions":
        detail = es.streamers[0]
    elif es.characters and "reaction" not in evl:
        # Only name a character if they appear in title/transcript (not just franchise pack)
        blob = f"{ctx.title} {ctx.transcript_snippet}".lower()
        named = [c for c in es.characters if c.lower() in blob]
        if named:
            detail = named[0].title()
    elif es.streamers:
        detail = es.streamers[0]
    else:
        stop = {"this", "that", "with", "from", "have", "been", "were", "they", "just", "like"}
        title_bits = set(re.findall(r"[a-z0-9]+", (ctx.title or "").lower()))
        toks = [
            w for w in re.findall(r"[A-Za-z']+", ctx.transcript_snippet or "")
            if len(w) > 3 and w.lower() not in stop and w.lower() not in title_bits
        ]
        if toks:
            detail = " ".join(toks[:3])

    if conf < 0.35:
        line = random.choice(NEUTRAL_VO)
    else:
        options = [
            f"Watch {event}." if event else "Watch what happens next.",
            f"Here — {detail}." if detail else (f"Stay for {event}." if event else "Stay for this part."),
            f"That {event} is the one." if event else "This is the beat that matters.",
            f"{detail} cooked here." if detail else "Don't scroll past this.",
        ]
        if style == "reaction":
            options = [
                f"Wait — did you catch {event}?" if event else "Wait — did you catch that?",
                f"{detail} wasn't ready." if detail else "I wasn't ready for this.",
                "Look at their face here." if (ctx.niche_id or "") == "reactions" else (
                    f"Look at {event}." if event else "Look at that."
                ),
            ]
        line = random.choice(options)

    clean = _clean_title(ctx.title).lower()
    if clean:
        bits = [w for w in clean.split() if len(w) > 3]
        if bits and sum(1 for b in bits if b in line.lower()) >= max(2, int(0.5 * len(bits))):
            line = random.choice(NEUTRAL_VO)

    ok, _why = validate_copy(line, es)
    if not ok:
        line = random.choice(NEUTRAL_VO)
    words = line.split()
    if len(words) > 18:
        line = " ".join(words[:18])
    return line.strip()


def _try_ollama_script(style, ctx, scene, *, model, host, timeout):
    prompt = (
        "You write short TikTok/Shorts voiceover scripts.\n"
        f"Niche: {ctx.niche_id}\n"
        f"Transcript keywords: {(ctx.transcript_snippet or '')[:220]}\n"
        f"Scene reason: {ctx.reason}\n"
        "Rules: 1-3 short spoken sentences, max 36 words, energetic, no hashtags, "
        "NEVER quote or paraphrase the video title, write original lines from the scene. "
        "No stage directions. Output ONLY the spoken script."
    )
    body = json.dumps({
        "model": model, "prompt": prompt, "stream": False,
        "options": {"temperature": 0.85, "num_predict": 80},
    }).encode()
    req = urllib.request.Request(
        f"{host.rstrip('/')}/api/generate", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
        text = (data.get("response") or "").strip()
        text = re.sub(r"^script:\s*", "", text, flags=re.I)
        text = " ".join(text.split())
        if 8 <= len(text.split()) <= 40:
            return text
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None
    return None


def generate_script(ctx: HookContext, cfg: dict[str, Any]) -> tuple[str, str, str]:
    vo = cfg.get("voiceover") or {}
    hooks_cfg = cfg.get("hooks") or {}
    root = Path(cfg.get("_root") or ".")
    hist_path = root / "voiceover_style_history.json"
    recent: list[str] = []
    if hist_path.is_file():
        try:
            recent = list((json.loads(hist_path.read_text(encoding="utf-8")).get("recent") or []))[-12:]
        except (json.JSONDecodeError, OSError):
            recent = []
    style = _pick_script_style(ctx, str(vo.get("script_style", "auto")), recent)
    scene = _detect_scene_type(ctx)
    text = None
    via = "rules"
    if hooks_cfg.get("use_ollama", True):
        text = _try_ollama_script(
            style, ctx, scene,
            model=str(hooks_cfg.get("ollama_model", "llama3.2")),
            host=str(hooks_cfg.get("ollama_host", "http://127.0.0.1:11434")),
            timeout=float(hooks_cfg.get("ollama_timeout", 8)),
        )
        if text:
            via = "ollama"
    if not text:
        vocab = list((cfg.get("hooks") or {}).get("vocab") or [])
        text = _rule_script(style, ctx, scene, vocab=vocab)
        via = "rules"
    from .entities import validate_copy, extract_entities, NEUTRAL_VO
    es = getattr(ctx, "entities", None) or extract_entities(
        title=ctx.title, transcript=ctx.transcript_snippet or "", niche_id=ctx.niche_id
    )
    ok, _ = validate_copy(text or "", es)
    if not ok:
        text = _rule_script(style, ctx, scene, vocab=None)
        ok2, _ = validate_copy(text or "", es)
        if not ok2:
            text = random.choice(NEUTRAL_VO)
        via = "rules_regen"
    # Natural pauses via punctuation polish
    text = _punctuate_for_speech(text or "")
    recent.append(style)
    hist_path.write_text(json.dumps({"recent": recent[-24:]}, indent=2), encoding="utf-8")
    return text, style, via


def _punctuate_for_speech(text: str) -> str:
    """Ensure commas/dashes for natural pauses; emphasize short punchy clauses."""
    t = re.sub(r"\s+", " ", (text or "").strip())
    if not t:
        return t
    if not t.endswith((".", "!", "?")):
        t += "."
    # Light emphasis: wrap key words with commas for pause (Kokoro/edge respect punctuation)
    for kw in ("Wait", "No way", "Look", "Here"):
        if "—" in t or " - " in t:
            break
        t = re.sub(rf"\b({kw})\b(?!\s*[—-])", r"\1 —", t, count=1)
    t = t.replace("— —", "—").replace("--", "—")
    return t


def _estimate_words(text: str, duration: float) -> list[dict[str, Any]]:
    tokens = [t for t in re.findall(r"\S+", text) if t]
    if not tokens:
        return []
    weights = [max(1, len(re.sub(r"[^A-Za-z0-9]", "", t))) for t in tokens]
    total_w = float(sum(weights)) or 1.0
    start = 0.15
    usable = max(0.5, duration - start - 0.1)
    t = start
    out: list[dict[str, Any]] = []
    for tok, w in zip(tokens, weights):
        dur = max(0.12, usable * (w / total_w))
        out.append({"word": tok, "start": round(t, 3), "end": round(t + dur, 3)})
        t += dur
    return out


def _wav_duration(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / float(w.getframerate() or 1)
    except Exception:
        return 0.0


def _kokoro_paths(root: Path) -> tuple[Path, Path]:
    d = root / "models" / "kokoro"
    return d / "kokoro-v1.0.int8.onnx", d / "voices-v1.0.bin"


@lru_cache(maxsize=1)
def _kokoro_engine(model_s: str, voices_s: str):
    from kokoro_onnx import Kokoro
    return Kokoro(model_s, voices_s)


def synthesize_kokoro(
    text: str,
    out_wav: Path,
    voice: str,
    *,
    speed: float = 1.0,
    root: Path | None = None,
) -> Path:
    import numpy as np
    import soundfile as sf

    root = root or Path(".")
    model, voices = _kokoro_paths(root)
    if not model.is_file() or not voices.is_file():
        raise FileNotFoundError(f"Kokoro models missing under {model.parent}")
    kk = _kokoro_engine(str(model), str(voices))
    # Slight random speed jitter for variety
    spd = float(speed) * random.uniform(0.98, 1.03)
    samples, sr = kk.create(text=text, voice=voice, speed=spd, lang="en-us", sentence_pause=0.28, clause_pause=0.12)
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(out_wav), samples, sr)
    return out_wav


def _ensure_piper_voice(voice: str, models_dir: Path) -> Path:
    models_dir.mkdir(parents=True, exist_ok=True)
    onnx = models_dir / f"{voice}.onnx"
    if onnx.is_file():
        return onnx
    from piper.download_voices import download_voice
    print(f"[voiceover] downloading Piper voice {voice}…")
    download_voice(voice, models_dir)
    if not onnx.is_file():
        matches = list(models_dir.glob(f"{voice}*.onnx"))
        if matches:
            return matches[0]
        raise FileNotFoundError(f"Piper voice not found after download: {voice}")
    return onnx


def synthesize_piper(text: str, out_wav: Path, voice: str, models_dir: Path) -> Path:
    from piper import PiperVoice
    model = _ensure_piper_voice(voice, models_dir)
    pv = PiperVoice.load(model, download_dir=models_dir)
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out_wav), "wb") as wf:
        pv.synthesize_wav(text, wf)
    return out_wav


def synthesize_edge(
    text: str,
    out_wav: Path,
    voice: str = "en-US-AndrewMultilingualNeural",
    *,
    rate: str = "+0%",
    pitch: str = "+0Hz",
) -> Path:
    import asyncio
    try:
        import edge_tts
    except ImportError as e:
        raise RuntimeError("edge-tts not installed") from e

    out_mp3 = out_wav.with_suffix(".mp3")

    async def _run() -> None:
        communicate = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
        await communicate.save(str(out_mp3))

    asyncio.run(_run())
    cmd = ["ffmpeg", "-y", "-i", str(out_mp3), "-ac", "1", "-ar", "24000", str(out_wav)]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    out_mp3.unlink(missing_ok=True)
    if proc.returncode != 0 or not out_wav.is_file():
        raise RuntimeError(f"edge-tts convert failed: {(proc.stderr or '')[-400:]}")
    return out_wav


def polish_wav(path: Path) -> Path:
    """Light loudnorm + compression + de-ess for more natural VO."""
    if not path.is_file():
        return path
    tmp = path.with_suffix(".polished.wav")
    af = (
        "highpass=f=80,"
        "lowpass=f=10000,"
        "acompressor=threshold=-18dB:ratio=3:attack=10:release=80:makeup=2,"
        "treble=g=-2,"  # mild de-ess proxy
        "loudnorm=I=-16:TP=-1.5:LRA=11"
    )
    cmd = ["ffmpeg", "-y", "-i", str(path), "-af", af, "-ar", "24000", "-ac", "1", str(tmp)]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode == 0 and tmp.is_file() and tmp.stat().st_size > 500:
        path.unlink(missing_ok=True)
        tmp.rename(path)
    else:
        tmp.unlink(missing_ok=True)
    return path


def _resolve_voice_choice(cfg: dict[str, Any], niche_id: str) -> tuple[str, str, float, str, str]:
    """Return (engine, voice, speed, edge_rate, edge_pitch)."""
    vo = cfg.get("voiceover") or {}
    prof = _profile(niche_id)
    engine = str(vo.get("engine", "auto")).lower()
    # User explicit voice overrides rotation
    user_voice = str(vo.get("voice") or "").strip()
    speed_lo, speed_hi = prof.get("speed", (1.0, 1.05))
    speed = float(vo.get("speed") or random.uniform(speed_lo, speed_hi))
    # Niche presets often stash a Piper id in voiceover.voice — ignore that when engine=auto/kokoro/edge
    if user_voice and ("en_US-" in user_voice or "en_GB-" in user_voice) and engine in ("auto", "kokoro", "edge", "edge-tts"):
        user_voice = ""

    if user_voice:
        if user_voice.startswith("en-") or "Neural" in user_voice:
            return ("edge" if engine in ("auto", "edge", "edge-tts") else engine), user_voice, speed, "+0%", "+0Hz"
        if user_voice.startswith(("af_", "am_", "bf_", "bm_")):
            return ("kokoro" if engine in ("auto", "kokoro") else engine), user_voice, speed, "+0%", "+0Hz"
        if "en_US-" in user_voice or "en_GB-" in user_voice:
            return "piper", user_voice, speed, "+0%", "+0Hz"

    # Rotate among niche profile — auto prefers Kokoro
    if engine in ("auto", "kokoro"):
        voice = random.choice(list(prof["kokoro"]))
        return "kokoro", voice, speed, "+0%", "+0Hz"
    if engine in ("edge", "edge-tts"):
        voice = random.choice(list(prof["edge"]))
        # Slight rate/pitch variation
        rate = random.choice(["-5%", "+0%", "+5%", "+8%"])
        pitch = random.choice(["-2Hz", "+0Hz", "+2Hz", "+4Hz"])
        return "edge", voice, speed, rate, pitch
    voice = random.choice(list(prof["piper"]))
    return "piper", voice, speed, "+0%", "+0Hz"


def _synthesize_with_fallback(
    text: str,
    out_wav: Path,
    cfg: dict[str, Any],
    niche_id: str,
) -> tuple[Path | None, str, str]:
    root = Path(cfg.get("_root") or ".")
    models_dir = root / "models" / "piper"
    preferred, voice, speed, rate, pitch = _resolve_voice_choice(cfg, niche_id)
    order = []
    if preferred == "kokoro":
        order = ["kokoro", "edge", "piper"]
    elif preferred == "edge":
        order = ["edge", "kokoro", "piper"]
    else:
        order = ["piper", "kokoro", "edge"]
    # Honor force engine
    eng_cfg = str((cfg.get("voiceover") or {}).get("engine", "auto")).lower()
    if eng_cfg in ("kokoro", "edge", "edge-tts", "piper"):
        e = "edge" if eng_cfg.startswith("edge") else eng_cfg
        order = [e] + [x for x in order if x != e]

    err = ""
    for eng in order:
        try:
            if eng == "kokoro":
                v = voice if voice.startswith(("af_", "am_", "bf_", "bm_")) else random.choice(_profile(niche_id)["kokoro"])
                synthesize_kokoro(text, out_wav, v, speed=speed, root=root)
                polish_wav(out_wav)
                return out_wav, f"kokoro:{v}", v
            if eng == "edge":
                v = voice if ("Neural" in voice or voice.startswith("en-")) else random.choice(_profile(niche_id)["edge"])
                synthesize_edge(text, out_wav, v, rate=rate, pitch=pitch)
                polish_wav(out_wav)
                return out_wav, f"edge-tts:{v}", v
            if eng == "piper":
                v = voice if "en_" in voice else random.choice(_profile(niche_id)["piper"])
                synthesize_piper(text, out_wav, v, models_dir)
                polish_wav(out_wav)
                return out_wav, f"piper:{v}", v
        except Exception as e:
            err = str(e)
            print(f"[voiceover] {eng} failed: {e}")
            continue
    print(f"[voiceover] all engines failed ({err})")
    return None, "failed", ""


def make_voiceover(ctx: HookContext, cfg: dict[str, Any], out_wav: Path) -> VoiceoverResult:
    text, style, via = generate_script(ctx, cfg)
    print(f"[voiceover] style={style} via={via}: {text}")
    path, engine, voice = _synthesize_with_fallback(text, out_wav, cfg, ctx.niche_id or "gta6")
    if not path:
        return VoiceoverResult(text=text, style=style, via=via, wav_path=None, words=[], engine="failed", voice=voice)
    dur = _wav_duration(path)
    words = _estimate_words(text, dur)
    print(f"[voiceover] engine={engine} duration={dur:.2f}s words={len(words)}")
    return VoiceoverResult(text=text, style=style, via=via, wav_path=path, words=words, engine=engine, voice=voice)


def make_intro_vo(text: str, cfg: dict[str, Any], out_wav: Path) -> VoiceoverResult:
    clean = re.sub(r"\s+", " ", (text or "").strip())
    words = clean.split()
    if len(words) > 8:
        clean = " ".join(words[:8])
    clean = _punctuate_for_speech(clean)
    niche = str(cfg.get("niche_id") or (cfg.get("niche") or {}).get("id") or "gta6")
    path, engine, voice = _synthesize_with_fallback(clean, out_wav, cfg, niche)
    if not path:
        return VoiceoverResult(text=clean, style="intro", via="failed", wav_path=None, words=[], engine="failed", voice=voice)
    dur = _wav_duration(path)
    w = _estimate_words(clean, dur)
    print(f"[intro-vo] engine={engine} duration={dur:.2f}s: {clean}")
    return VoiceoverResult(text=clean, style="intro", via="intro", wav_path=path, words=w, engine=engine, voice=voice)


def list_available_voices() -> dict[str, list[str]]:
    return {
        "kokoro": ALL_KOKORO_VOICES,
        "edge": ALL_EDGE_VOICES,
        "piper": sorted({v for p in NICHE_VOICE_PROFILES.values() for v in p["piper"]}),
    }
