"""Free CPU voiceover: Piper TTS (local) with optional edge-tts fallback."""

from __future__ import annotations

import json
import random
import re
import subprocess
import urllib.error
import urllib.request
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .hooks import HookContext, _clean_title, _detect_scene_type, _sentiment, _truncate

SCRIPT_STYLES = ("hype", "commentary", "reaction", "news")


@dataclass
class VoiceoverResult:
    text: str
    style: str
    via: str
    wav_path: Path | None
    words: list[dict[str, Any]]
    engine: str


def _pick_script_style(ctx: HookContext, cfg_style: str, recent: list[str]) -> str:
    if cfg_style and cfg_style != "auto" and cfg_style in SCRIPT_STYLES:
        return cfg_style
    scene = _detect_scene_type(ctx)
    sent = _sentiment(ctx)
    if scene in ("news", "leak", "trailer"):
        preferred = ["news", "commentary", "hype", "reaction"]
    elif scene == "reaction":
        preferred = ["reaction", "hype", "commentary", "news"]
    elif scene in ("chase", "stunt", "crash", "action", "hype_moment"):
        preferred = ["hype", "commentary", "reaction", "news"]
    else:
        preferred = ["commentary", "hype", "reaction", "news"]
    if sent == "shock":
        preferred = ["reaction", "hype"] + preferred
    banned = set(recent[-2:]) if recent else set()
    for s in preferred:
        if s not in banned:
            return s
    return preferred[0]


def _rule_script(style: str, ctx: HookContext, scene: str, vocab: list[str] | None = None) -> str:
    """Original commentary from scene/transcript/actions — never echo the source title."""
    niche = (ctx.niche_id or "gta6").lower()
    v = random.choice(vocab) if vocab else ""
    # Scene / action cues (not title)
    reason = (ctx.reason or "").lower()
    actions = []
    for key, label in (
        ("laugh", "the laugh"),
        ("scream", "the scream"),
        ("face", "the face cam"),
        ("loud", "that volume spike"),
        ("motion", "the movement"),
        ("cut", "the cut"),
        ("react", "the reaction"),
        ("keyword", "that line"),
        ("hype_speech", "that line"),
        ("audio_spike", "the spike"),
    ):
        if key in reason:
            actions.append(label)
    # Transcript keywords (content words only)
    stop = {
        "this", "that", "with", "from", "have", "been", "were", "they", "them",
        "your", "what", "when", "where", "just", "like", "really", "about", "into",
        "because", "there", "their", "would", "could", "should", "gonna", "look",
    }
    toks = [
        w.lower() for w in re.findall(r"[A-Za-z']+", ctx.transcript_snippet or "")
        if len(w) > 3 and w.lower() not in stop
    ]
    # Drop tokens that look like title leftovers
    title_bits = set(re.findall(r"[a-z0-9]+", (ctx.title or "").lower()))
    toks = [t for t in toks if t not in title_bits or len(t) <= 4][:8]
    cue = actions[0] if actions else (toks[0] if toks else "")
    detail = " ".join(toks[:3]) if toks else ""

    niche_openers = {
        "gta6": ["Vice City energy", "Wanted-level chaos", "Rockstar cooked here", "Lucia energy"],
        "anime": ["Aura overload", "Sakuga frame one", "That power-up hit", "Shonen beat"],
        "reactions": ["Face cam carried", "Chat is melting", "The mic caught it", "Streamer broke"],
        "gaming": ["Cleanest clip", "Clutch timing", "Ranked trauma", "Boss phase"],
        "funny": ["The fail was personal", "Joke writes itself", "I can't breathe"],
        "movies": ["Cinema lighting", "Trailer cooked", "Theater reaction"],
        "animation": ["Frame one cinema", "Toon force", "Inbetweening cooked"],
    }
    flavor = random.choice(niche_openers.get(niche, ["This moment hits"]))
    if v and random.random() < 0.45:
        flavor = v[0].upper() + v[1:] if v else flavor

    snippets = {
        "hype": [
            f"Yo lock in. {flavor}. Watch {cue or 'this beat'}." if cue else f"Yo lock in. {flavor}. Do not scroll.",
            f"Insane. {flavor} right here. Feel {cue or 'that hit'}." if cue else f"Insane. {flavor}. Absolute cinema.",
            f"Bro. {flavor}. {('Catch ' + detail) if detail else 'This is the drop'}.",
            f"No way. {flavor}. {(cue[0].upper()+cue[1:] if cue else 'Peak moment')} goes crazy.",
        ],
        "commentary": [
            f"Okay watch the detail. {('See ' + detail) if detail else flavor}. That is the shot.",
            f"Quick take: {flavor}. {('Focus on ' + cue) if cue else 'Stay for the ending'}.",
            f"Here is why it hits. {flavor}. {('Listen for ' + cue) if cue else 'Do not blink'}.",
            f"See that? {flavor}. {('The ' + cue + ' sells it') if cue else 'Pure cinema'}." ,
        ],
        "reaction": [
            "Bro I was not ready for this. Look at that.",
            f"Wait. Did you catch {cue or 'that detail'}?",
            "My honest reaction: this looks too real.",
            f"No way. {flavor} just hit different.",
        ],
        "news": [
            f"Update: {flavor} and people are melting down.",
            f"Quick rundown: new {niche} drop, watch {cue or 'the peak'}.",
            f"Here is what matters: {flavor}. {('Hit is ' + cue) if cue else 'Stay locked in'}.",
            f"News flash: {flavor} is everywhere right now.",
        ],
    }
    options = snippets.get(style) or snippets["hype"]
    text_out = random.choice(options)
    # Hard strip any leftover title echo
    clean_title = _clean_title(ctx.title).lower()
    if clean_title:
        bits = [w for w in clean_title.split() if len(w) > 3]
        low = text_out.lower()
        if bits and sum(1 for b in bits if b in low) >= max(2, int(0.5 * len(bits))):
            text_out = f"Yo lock in. {flavor}. Do not scroll."
    words = text_out.split()
    if len(words) > 36:
        text_out = " ".join(words[:36])
    return text_out.strip()



def _try_ollama_script(
    style: str,
    ctx: HookContext,
    scene: str,
    *,
    model: str,
    host: str,
    timeout: float,
) -> str | None:
    prompt = (
        "You write short TikTok/Shorts voiceover scripts for GTA 6 clips.\n"
        f"Style: {style}\n"
        f"Scene: {scene}\n"
        f"Niche: {ctx.niche_id}\n"
        f"Transcript keywords: {ctx.transcript_snippet[:220]}\n"
        f"Scene reason: {ctx.reason}\n"
        "Rules: 1-3 short spoken sentences, max 36 words, energetic, no hashtags, "
        "NEVER quote or paraphrase the video title, write original lines from the scene. "
        "No stage directions. Output ONLY the spoken script."
    )
    body = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.85, "num_predict": 80},
    }).encode()
    req = urllib.request.Request(
        f"{host.rstrip('/')}/api/generate",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
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
            style,
            ctx,
            scene,
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
    recent.append(style)
    hist_path.write_text(json.dumps({"recent": recent[-24:]}, indent=2), encoding="utf-8")
    return text, style, via


def _estimate_words(text: str, duration: float) -> list[dict[str, Any]]:
    tokens = [t for t in re.findall(r"\S+", text) if t]
    if not tokens:
        return []
    weights = [max(1, len(re.sub(r"[^A-Za-z0-9]", "", t))) for t in tokens]
    total_w = float(sum(weights)) or 1.0
    # Lead-in silence ~0.15s, leave ~0.1s at end
    start = 0.15
    usable = max(0.5, duration - start - 0.1)
    t = start
    out: list[dict[str, Any]] = []
    for tok, w in zip(tokens, weights):
        dur = usable * (w / total_w)
        dur = max(0.12, dur)
        out.append({"word": tok, "start": round(t, 3), "end": round(t + dur, 3)})
        t += dur
    return out


def _wav_duration(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / float(w.getframerate() or 1)
    except Exception:
        return 0.0


def _ensure_piper_voice(voice: str, models_dir: Path) -> Path:
    models_dir.mkdir(parents=True, exist_ok=True)
    onnx = models_dir / f"{voice}.onnx"
    if onnx.is_file():
        return onnx
    from piper.download_voices import download_voice

    print(f"[voiceover] downloading Piper voice {voice}…")
    download_voice(voice, models_dir)
    if not onnx.is_file():
        # some packages name slightly differently
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


def synthesize_edge(text: str, out_wav: Path, voice: str = "en-US-GuyNeural") -> Path:
    """Optional online fallback via edge-tts (Microsoft neural voices, free)."""
    import asyncio

    try:
        import edge_tts
    except ImportError as e:
        raise RuntimeError("edge-tts not installed") from e

    out_mp3 = out_wav.with_suffix(".mp3")

    async def _run() -> None:
        communicate = edge_tts.Communicate(text, voice)
        await communicate.save(str(out_mp3))

    asyncio.run(_run())
    # Convert mp3 -> wav for mixing
    cmd = [
        "ffmpeg", "-y", "-i", str(out_mp3),
        "-ac", "1", "-ar", "22050", str(out_wav),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    out_mp3.unlink(missing_ok=True)
    if proc.returncode != 0 or not out_wav.is_file():
        raise RuntimeError(f"edge-tts convert failed: {(proc.stderr or '')[-400:]}")
    return out_wav


def make_voiceover(
    ctx: HookContext,
    cfg: dict[str, Any],
    out_wav: Path,
) -> VoiceoverResult:
    vo = cfg.get("voiceover") or {}
    text, style, via = generate_script(ctx, cfg)
    print(f"[voiceover] style={style} via={via}: {text}")

    engine = str(vo.get("engine", "piper")).lower()
    voice = str(vo.get("voice", "en_US-ryan-high"))
    fallback = str(vo.get("fallback_voice", "en_US-lessac-high"))
    edge_voice = str(vo.get("edge_voice", "en-US-GuyNeural"))
    root = Path(cfg.get("_root") or ".")
    models_dir = Path(vo.get("models_dir") or (root / "models" / "piper"))
    if not models_dir.is_absolute():
        models_dir = root / models_dir

    used_engine = "none"
    err: Exception | None = None
    if engine in ("piper", "auto"):
        for v in (voice, fallback):
            try:
                synthesize_piper(text, out_wav, v, models_dir)
                used_engine = f"piper:{v}"
                break
            except Exception as e:
                err = e
                print(f"[voiceover] piper {v} failed: {e}")
    if used_engine == "none" and engine in ("edge-tts", "edge", "auto", "piper"):
        # piper failed or edge requested — try edge as optional online fallback
        allow_edge = bool(vo.get("allow_edge_fallback", True)) or engine in ("edge-tts", "edge")
        if allow_edge:
            try:
                synthesize_edge(text, out_wav, edge_voice)
                used_engine = f"edge-tts:{edge_voice}"
            except Exception as e:
                err = e
                print(f"[voiceover] edge-tts failed: {e}")

    if used_engine == "none" or not out_wav.is_file():
        print(f"[voiceover] synthesis failed ({err}); continuing without VO audio")
        return VoiceoverResult(text=text, style=style, via=via, wav_path=None, words=[], engine="failed")

    dur = _wav_duration(out_wav)
    words = _estimate_words(text, dur)
    print(f"[voiceover] engine={used_engine} duration={dur:.2f}s words={len(words)}")
    return VoiceoverResult(
        text=text, style=style, via=via, wav_path=out_wav, words=words, engine=used_engine
    )


def make_intro_vo(text: str, cfg: dict[str, Any], out_wav: Path) -> VoiceoverResult:
    """Speak the header/hook for the clip intro (1–2s typical)."""
    vo = cfg.get("voiceover") or {}
    voice = str(vo.get("voice", "en_US-ryan-high"))
    fallback = str(vo.get("fallback_voice", "en_US-lessac-high"))
    root = Path(cfg.get("_root") or ".")
    models_dir = Path(vo.get("models_dir") or (root / "models" / "piper"))
    if not models_dir.is_absolute():
        models_dir = root / models_dir
    clean = re.sub(r"\s+", " ", (text or "").strip())
    # Keep intros snappy (~1–2s speech)
    words = clean.split()
    if len(words) > 8:
        clean = " ".join(words[:8])
        # intro max words
    # Strip style prefixes that sound awkward spoken? Keep POV etc — they slap
    used = "none"
    err = None
    for v in (voice, fallback):
        try:
            synthesize_piper(clean, out_wav, v, models_dir)
            used = f"piper:{v}"
            break
        except Exception as e:
            err = e
            print(f"[intro-vo] piper {v} failed: {e}")
    if used == "none" and vo.get("allow_edge_fallback", True):
        try:
            synthesize_edge(clean, out_wav, str(vo.get("edge_voice", "en-US-GuyNeural")))
            used = "edge-tts"
        except Exception as e:
            err = e
    if used == "none" or not out_wav.is_file():
        print(f"[intro-vo] failed ({err})")
        return VoiceoverResult(text=clean, style="intro", via="failed", wav_path=None, words=[], engine="failed")
    dur = _wav_duration(out_wav)
    words = _estimate_words(clean, dur)
    print(f"[intro-vo] engine={used} duration={dur:.2f}s: {clean}")
    return VoiceoverResult(text=clean, style="intro", via="intro", wav_path=out_wav, words=words, engine=used)
