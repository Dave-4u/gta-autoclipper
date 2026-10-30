"""Smart header engine: generate a pool, score, pick best (Ollama or rich rules)."""

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
    reason: str = ""
    transcript_snippet: str = ""
    boost_hits: list[str] = field(default_factory=list)
    duration: float = 0.0
    view_count: int = 0
    source_url: str = ""
    niche_id: str = "gta6"


@dataclass
class HookResult:
    text: str
    style: str
    via: str  # "ollama" | "rules"
    score: float = 0.0
    pool_size: int = 0
    candidates: list[str] = field(default_factory=list)


def _clean_title(title: str) -> str:
    t = re.sub(r"\s+", " ", title or "").strip()
    t = re.sub(
        r"(?i)\b(gta\s*5|gta\s*v|gta\s*vi|gta\s*6|gta\s*online|"
        r"anime|manga|reaction|shorts?|tiktok|#\w+|official|hd|4k|launch trailer|gameplay|"
        r"trailer|edit|compilation|movie|film)\b",
        " ",
        t,
    )
    t = re.sub(r"[|–—].*$", "", t)
    t = re.sub(r"\s+", " ", t).strip(" -_|:.")
    return t


def _detect_scene_type(ctx: HookContext) -> str:
    blob = f"{ctx.title} {ctx.transcript_snippet} {' '.join(ctx.boost_hits)} {ctx.niche_id}".lower()
    rules = [
        ("trailer", ("trailer", "teaser", "cinematic", "official")),
        ("leak", ("leak", "leaked", "datamine", "rumor", "rumour")),
        ("news", ("news", "update", "announcement", "release date", "confirmed")),
        ("reaction", ("reaction", "reacts", "reacting", "watching", "streamer")),
        ("fight", ("fight", "battle", "vs", "versus", "boss", "bankai", "power")),
        ("anime", ("anime", "manga", "shonen", "waifu", "sensei")),
        ("funny", ("funny", "laugh", "hilarious", "comedy", "meme", "fail")),
        ("chase", ("chase", "police", "cops", "wanted")),
        ("crash", ("crash", "wipeout", "explosion")),
        ("stunt", ("stunt", "jump", "flip", "ramp", "clutch")),
        ("movie", ("movie", "film", "cinema", "scene", "director")),
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
    pos = ("insane", "crazy", "best", "love", "fire", "beautiful", "hype", "amazing", "goat", "peak")
    shock = ("wait", "what", "omg", "oh my", "no way", "bro", "look")
    if any(w in blob for w in shock):
        return "shock"
    if any(w in blob for w in neg):
        return "negative"
    if any(w in blob for w in pos):
        return "positive"
    return "neutral"


def _style_history_path(root: Path) -> Path:
    return root / "hook_style_history.json"


def load_recent_styles(root: Path, limit: int = 12) -> list[str]:
    path = _style_history_path(root)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [str(s) for s in (data.get("recent") or [])][-limit:]
    except (json.JSONDecodeError, OSError):
        return []


def push_style(root: Path, style: str, limit: int = 24) -> None:
    path = _style_history_path(root)
    recent = load_recent_styles(root, limit=limit)
    recent.append(style)
    path.write_text(json.dumps({"recent": recent[-limit:]}, indent=2), encoding="utf-8")


def _truncate(text: str, max_len: int = 42) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_len:
        return text
    cut = text[: max_len - 1].rsplit(" ", 1)[0]
    return (cut or text[: max_len - 1]) + "…"


def _preferred_styles(ctx: HookContext, cfg: dict[str, Any], recent: list[str]) -> list[str]:
    hooks_cfg = cfg.get("hooks") or {}
    niche_styles = list(hooks_cfg.get("preferred_styles") or [])
    scene = _detect_scene_type(ctx)
    sent = _sentiment(ctx)
    if niche_styles:
        preferred = list(niche_styles)
    elif scene in ("trailer", "news", "leak", "movie"):
        preferred = ["info_news", "question", "hype", "hot_take"]
    elif scene in ("reaction",):
        preferred = ["reaction", "pov", "question", "hype"]
    elif scene in ("crash", "fail", "funny"):
        preferred = ["when_you", "pov", "reaction", "hot_take"]
    elif scene in ("chase", "stunt", "action", "hype_moment", "fight"):
        preferred = ["hype", "pov", "question", "when_you"]
    elif scene == "anime":
        preferred = ["hype", "pov", "reaction", "hot_take", "question"]
    else:
        preferred = ["hype", "question", "pov", "when_you", "hot_take", "reaction"]
    if sent == "shock":
        preferred = ["reaction", "question"] + preferred
    # shuffle lightly for variety but keep preferred first
    head = preferred[:3]
    tail = preferred[3:]
    random.shuffle(tail)
    preferred = head + tail
    banned = set(recent[-3:]) if recent else set()
    ordered = [s for s in preferred if s not in banned] + [s for s in preferred if s in banned]
    # ensure all STYLES appear somehow
    for s in STYLES:
        if s not in ordered:
            ordered.append(s)
    return ordered


def _rule_pool(style: str, ctx: HookContext, scene: str, vocab: list[str]) -> list[str]:
    clean = _clean_title(ctx.title)
    short = _truncate(clean, 28) if clean else ""
    hits = [h for h in ctx.boost_hits if h][:3]
    v = (random.choice(vocab) if vocab else "")
    snip = _truncate(ctx.transcript_snippet, 24) if ctx.transcript_snippet else ""

    templates = {
        "pov": [
            f"POV: {short}" if short else "POV: You weren't ready",
            f"POV: {v}" if v else "POV: Main character energy",
            "POV: The algorithm cooked",
            f"POV: {hits[0]}" if hits else "POV: Peak moment unlocked",
            "POV: Chat said say less",
        ],
        "when_you": [
            f"When you {short.lower()}" if short else "When the clip hits different",
            "When the plot twist hits",
            f"When {v} activates" if v else "When the aura starts speaking",
            "When you blink and miss it",
            "When the soundtrack goes crazy",
        ],
        "hot_take": [
            "Hot take: this is peak",
            f"Hot take: {short}" if short else "Hot take: underrated moment",
            f"Unpopular opinion: {v} clears" if v else "Unpopular opinion: this clears",
            "Hot take: cinema is back",
            "Hot take: the internet isn't ready",
        ],
        "question": [
            "Did you catch that detail?",
            f"Wait… {short}?" if short else "Wait… what just happened?",
            "Would you survive this?",
            "Is this even real?",
            f"Who else felt that {v}?" if v else "Who else felt that?",
        ],
        "hype": [
            "THIS IS INSANE",
            "LOCK IN",
            f"{(short or v or 'PEAK FICTION').upper()}",
            "ABSOLUTE CINEMA",
            "NO WAY THIS IS REAL",
            "AURA OVERLOAD",
        ],
        "reaction": [
            "My honest reaction:",
            "Bro wasn't ready",
            "I wasn't ready for this",
            "The face when it hits",
            f"Reaction to {short}" if short else "Chat is going crazy",
        ],
        "info_news": [
            f"Update: {short}" if short else "New drop you missed",
            "Quick rundown",
            f"Trailer detail: {v}" if v else "Hidden detail dropped",
            "What you need to know",
            f"Leak check: {short}" if scene == "leak" and short else "Breaking clip",
        ],
    }
    options = list(templates.get(style) or templates["hype"])
    if snip and style in ("question", "reaction", "pov"):
        options.append(_truncate(f'When they said "{snip}"', 42))
    return [_truncate(o, 42) for o in options if o]


def _score_candidate(text: str, style: str, ctx: HookContext, scene: str) -> float:
    score = 0.0
    words = text.split()
    n = len(words)
    if 3 <= n <= 8:
        score += 2.0
    elif n <= 10:
        score += 1.0
    else:
        score -= 0.5
    blob = text.lower()
    title_bits = [w for w in _clean_title(ctx.title).lower().split() if len(w) > 3][:4]
    for w in title_bits:
        if w in blob:
            score += 0.6
    for h in ctx.boost_hits[:5]:
        if h.lower() in blob:
            score += 0.8
    for w in (ctx.transcript_snippet or "").lower().split()[:12]:
        if len(w) > 4 and w in blob:
            score += 0.25
    # Style fit bonuses
    if style == "pov" and blob.startswith("pov"):
        score += 0.8
    if style == "hype" and text.isupper():
        score += 0.5
    if style == "question" and ("?" in text or blob.startswith("wait") or blob.startswith("did") or blob.startswith("would") or blob.startswith("is ") or blob.startswith("who")):
        score += 0.8
    if scene in ("funny", "fail") and style in ("when_you", "pov", "reaction"):
        score += 0.4
    if scene in ("fight", "anime", "hype_moment") and style in ("hype", "pov"):
        score += 0.4
    # Prefer punchy power words
    for pw in ("insane", "peak", "cinema", "aura", "goat", "crazy", "wait", "no way", "lock in"):
        if pw in blob:
            score += 0.35
    score += random.uniform(0, 0.6)  # jitter so runs vary
    return score


def try_ollama_pool(
    styles: list[str],
    ctx: HookContext,
    scene: str,
    *,
    model: str,
    host: str,
    timeout: float,
    n: int = 6,
) -> list[tuple[str, str]]:
    prompt = (
        "You write viral Shorts/TikTok HEADER lines.\n"
        f"Niche: {ctx.niche_id}\nScene: {scene}\nTitle: {ctx.title}\n"
        f"Transcript: {ctx.transcript_snippet[:280]}\n"
        f"Styles to cover: {', '.join(styles[:5])}\n"
        f"Write {n} different ONE-line headers (max 8 words each). "
        "No hashtags. No quotes. Number them 1..N. Match the styles."
    )
    body = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.95, "num_predict": 160},
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
        raw = (data.get("response") or "").strip()
        out: list[tuple[str, str]] = []
        for line in raw.splitlines():
            line = re.sub(r"^\s*\d+[\).:\-]\s*", "", line).strip().strip("\"'")
            if 3 <= len(line.split()) <= 12:
                style = styles[len(out) % len(styles)]
                out.append((_truncate(line, 42), style))
            if len(out) >= n:
                break
        return out
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return []


def generate_hook_pool(ctx: HookContext, cfg: dict[str, Any], pool_size: int = 16) -> list[tuple[str, str, str]]:
    """Return list of (text, style, via)."""
    root = Path(cfg.get("_root") or ".")
    hooks_cfg = cfg.get("hooks") or {}
    recent = load_recent_styles(root)
    styles = _preferred_styles(ctx, cfg, recent)
    scene = _detect_scene_type(ctx)
    vocab = list(hooks_cfg.get("vocab") or [])
    pool: list[tuple[str, str, str]] = []

    if hooks_cfg.get("use_ollama", True):
        ollama = try_ollama_pool(
            styles,
            ctx,
            scene,
            model=str(hooks_cfg.get("ollama_model", "llama3.2")),
            host=str(hooks_cfg.get("ollama_host", "http://127.0.0.1:11434")),
            timeout=float(hooks_cfg.get("ollama_timeout", 8)),
            n=min(8, pool_size),
        )
        for text, style in ollama:
            pool.append((text, style, "ollama"))

    # Rich rule templates across shuffled styles
    style_cycle = styles[:]
    random.shuffle(style_cycle)
    for style in style_cycle:
        for text in _rule_pool(style, ctx, scene, vocab):
            pool.append((text, style, "rules"))
            if len(pool) >= pool_size * 2:
                break
        if len(pool) >= pool_size * 2:
            break

    # Dedup by lowercase text
    seen: set[str] = set()
    uniq: list[tuple[str, str, str]] = []
    for text, style, via in pool:
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        uniq.append((text, style, via))
    random.shuffle(uniq)
    return uniq[: max(pool_size, 8)]


def generate_hook(ctx: HookContext, cfg: dict[str, Any]) -> HookResult:
    hooks_cfg = cfg.get("hooks") or {}
    pool_n = int(hooks_cfg.get("pool_size", 16))
    pool = generate_hook_pool(ctx, cfg, pool_size=pool_n)
    scene = _detect_scene_type(ctx)
    scored: list[tuple[float, str, str, str]] = []
    for text, style, via in pool:
        scored.append((_score_candidate(text, style, ctx, scene), text, style, via))
    scored.sort(key=lambda x: x[0], reverse=True)
    # Softmax-ish random among top 5 for variety
    top = scored[:5] or [(0.0, "LOCK IN", "hype", "rules")]
    weights = [max(0.1, s[0]) ** 2 for s in top]
    pick = random.choices(top, weights=weights, k=1)[0]
    _, text, style, via = pick
    root = Path(cfg.get("_root") or ".")
    push_style(root, style)
    return HookResult(
        text=text,
        style=style,
        via=via,
        score=float(pick[0]),
        pool_size=len(pool),
        candidates=[t[1] for t in scored[:8]],
    )


# Back-compat exports used elsewhere
def pick_style(ctx: HookContext, recent: list[str]) -> str:
    return _preferred_styles(ctx, {"hooks": {}}, recent)[0]
