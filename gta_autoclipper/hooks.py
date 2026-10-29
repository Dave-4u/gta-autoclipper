"""Smart header/hook text: style picker with optional Ollama + rule fallback."""

from __future__ import annotations

import json
import random
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

STYLES = (
    "pov",
    "when_you",
    "hot_take",
    "question",
    "hype",
    "reaction",
    "info_news",
)


@dataclass
class HookContext:
    title: str = ""
    reason: str = ""  # loud/cut/motion/keyword…
    transcript_snippet: str = ""
    boost_hits: list[str] = field(default_factory=list)
    duration: float = 0.0
    view_count: int = 0
    source_url: str = ""


@dataclass
class HookResult:
    text: str
    style: str
    via: str  # "ollama" | "rules"


def _clean_title(title: str) -> str:
    t = re.sub(r"\s+", " ", title or "").strip()
    t = re.sub(
        r"(?i)\b(gta\s*5|gta\s*v|gta\s*vi|gta\s*6|gta\s*online|"
        r"shorts?|tiktok|#\w+|official|hd|4k|reaction)\b",
        " ",
        t,
    )
    t = re.sub(r"[|–—].*$", "", t)
    t = re.sub(r"\s+", " ", t).strip(" -_|:.")
    return t


def _detect_scene_type(ctx: HookContext) -> str:
    blob = f"{ctx.title} {ctx.transcript_snippet} {' '.join(ctx.boost_hits)}".lower()
    rules = [
        ("trailer", ("trailer", "teaser", "cinematic", "official")),
        ("leak", ("leak", "leaked", "datamine", "rumor", "rumour")),
        ("news", ("news", "update", "announcement", "delayed", "release date", "confirmed")),
        ("reaction", ("reaction", "reacts", "reacting", "watching")),
        ("rp", ("rp ", " roleplay", "gta rp", "nopixel", "streamer")),
        ("chase", ("chase", "police", "cops", "wanted")),
        ("crash", ("crash", "fail", "wipeout", "explosion")),
        ("stunt", ("stunt", "jump", "flip", "ramp")),
        ("funny", ("funny", "laugh", "hilarious", "comedy", "meme")),
    ]
    for name, keys in rules:
        if any(k in blob for k in keys):
            return name
    if "loud" in (ctx.reason or ""):
        return "hype_moment"
    if "motion" in (ctx.reason or ""):
        return "action"
    return "general"


def _sentiment(ctx: HookContext) -> str:
    blob = f"{ctx.title} {ctx.transcript_snippet}".lower()
    neg = ("fail", "crash", "death", "killed", "angry", "mad", "rage", "worst", "hate")
    pos = ("insane", "crazy", "best", "love", "fire", "beautiful", "hype", "amazing", "god")
    shock = ("wait", "what", "omg", "oh my", "no way", "bro", "look")
    if any(w in blob for w in shock):
        return "shock"
    if any(w in blob for w in neg):
        return "negative"
    if any(w in blob for w in pos):
        return "positive"
    if "leak" in blob or "news" in blob:
        return "neutral_info"
    return "neutral"


def _style_history_path(root: Path) -> Path:
    return root / "hook_style_history.json"


def load_recent_styles(root: Path, limit: int = 12) -> list[str]:
    path = _style_history_path(root)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        styles = data.get("recent") or []
        return [str(s) for s in styles][-limit:]
    except (json.JSONDecodeError, OSError):
        return []


def push_style(root: Path, style: str, limit: int = 24) -> None:
    path = _style_history_path(root)
    recent = load_recent_styles(root, limit=limit)
    recent.append(style)
    path.write_text(
        json.dumps({"recent": recent[-limit:]}, indent=2), encoding="utf-8"
    )


def pick_style(ctx: HookContext, recent: list[str]) -> str:
    scene = _detect_scene_type(ctx)
    sent = _sentiment(ctx)

    preferred: list[str]
    if scene in ("trailer", "news", "leak"):
        preferred = ["info_news", "question", "hype", "hot_take"]
    elif scene == "reaction":
        preferred = ["reaction", "pov", "question", "hype"]
    elif scene in ("crash", "fail", "funny"):
        preferred = ["when_you", "pov", "reaction", "hot_take"]
    elif scene in ("chase", "stunt", "action", "hype_moment"):
        preferred = ["hype", "pov", "question", "when_you"]
    elif scene == "rp":
        preferred = ["when_you", "pov", "hot_take", "reaction"]
    else:
        preferred = ["hype", "question", "pov", "when_you", "hot_take", "reaction"]

    if sent == "shock":
        preferred = ["reaction", "question"] + preferred
    elif sent == "neutral_info":
        preferred = ["info_news", "question", "hot_take"] + preferred

    # Avoid repeating the last 3 styles when possible
    banned = set(recent[-3:]) if recent else set()
    for s in preferred:
        if s not in banned:
            return s
    # Fall through: least-used among STYLES
    counts = {s: recent.count(s) for s in STYLES}
    return min(STYLES, key=lambda s: counts.get(s, 0))


def _truncate(text: str, max_len: int = 42) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_len:
        return text
    cut = text[: max_len - 1].rsplit(" ", 1)[0]
    return (cut or text[: max_len - 1]) + "…"


def _rule_hook(style: str, ctx: HookContext, scene: str) -> str:
    clean = _clean_title(ctx.title)
    short = _truncate(clean, 28) if clean else ""
    hits = [h for h in ctx.boost_hits if h][:3]

    templates = {
        "pov": [
            f"POV: {short}" if short else "POV: Los Santos goes crazy",
            "POV: You just opened GTA 6 news",
            "POV: Rockstar drops something again",
            f"POV: {hits[0]}" if hits else "POV: The clip everyone is sharing",
        ],
        "when_you": [
            f"When you {short.lower()}" if short else "When the GTA physics kick in",
            "When GTA RP goes too far",
            "When the cops finally catch up",
            "When the trailer hits different",
        ],
        "hot_take": [
            "Hot take: this is peak GTA",
            "Hot take: GTA 6 already won",
            "Unpopular opinion: this clip is better than the trailer",
            f"Hot take: {short}" if short else "Hot take: Rockstar cooked",
        ],
        "question": [
            "Did Rockstar just spoil GTA 6?",
            "Would you play this?",
            "Is this real or a leak?",
            f"Wait… {short}?" if short else "Did you catch this detail?",
        ],
        "hype": [
            "GTA 6 IS COMING",
            "THIS IS INSANE",
            "LOCK IN FOR GTA 6",
            f"{short.upper()}" if short else "ABSOLUTE CINEMA",
        ],
        "reaction": [
            "My honest reaction:",
            "Bro wasn't ready for this",
            "The face when the trailer drops",
            "I wasn't ready 💀",
        ],
        "info_news": [
            f"GTA 6 update: {short}" if short else "GTA 6 news you missed",
            "New GTA 6 detail dropped",
            "Quick GTA 6 rundown",
            f"Leak check: {short}" if scene == "leak" and short else "What's new in GTA 6",
        ],
    }
    options = templates.get(style) or templates["hype"]
    # Prefer options that include title substance when available
    if short:
        with_title = [o for o in options if short.lower() in o.lower() or short in o]
        if with_title:
            return _truncate(random.choice(with_title), 42)
    return _truncate(random.choice(options), 42)


def try_ollama_hook(
    style: str,
    ctx: HookContext,
    scene: str,
    *,
    model: str = "llama3.2",
    host: str = "http://127.0.0.1:11434",
    timeout: float = 8.0,
) -> str | None:
    """Ask local Ollama for a short hook; return None if unavailable."""
    prompt = (
        "You write viral YouTube Shorts / TikTok header text for GTA 6 clipping.\n"
        f"Style required: {style}\n"
        f"Scene type: {scene}\n"
        f"Source title: {ctx.title}\n"
        f"Transcript snippet: {ctx.transcript_snippet[:300]}\n"
        f"Keywords: {', '.join(ctx.boost_hits[:8])}\n"
        "Rules: ONE line only, max 8 words, no hashtags, no emojis overload "
        "(0-1 ok), no quotes around the line, match the style exactly "
        "(POV / When you / Hot take / Question / Hype / Reaction / News).\n"
        "Output only the header line."
    )
    body = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.9, "num_predict": 40},
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
        text = (data.get("response") or "").strip().splitlines()[0].strip().strip('"\'')
        text = re.sub(r"^header:\s*", "", text, flags=re.I)
        if 3 <= len(text) <= 60:
            return _truncate(text, 42)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError, IndexError):
        return None
    return None


def generate_hook(
    ctx: HookContext,
    cfg: dict[str, Any],
) -> HookResult:
    root = Path(cfg.get("_root") or ".")
    hooks_cfg = cfg.get("hooks") or {}
    recent = load_recent_styles(root)
    style = pick_style(ctx, recent)
    scene = _detect_scene_type(ctx)

    use_ollama = hooks_cfg.get("use_ollama", True)
    text = None
    via = "rules"
    if use_ollama:
        text = try_ollama_hook(
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
        text = _rule_hook(style, ctx, scene)
        via = "rules"

    push_style(root, style)
    return HookResult(text=text, style=style, via=via)
