"""Smart header engine: scene-aware pools, reject title-echoes / generic niche lines."""

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

GENERIC_BANNED = {
    "pov: anime",
    "pov: gta",
    "pov: gta 6",
    "pov: gaming",
    "pov: funny",
    "pov: movies",
    "pov: reaction",
    "pov: reactions",
    "pov: animation",
    "quick rundown",
    "breaking clip",
    "new drop you missed",
    "what you need to know",
    "chat is going crazy",
    "peak fiction",
    "lock in",
    "this is insane",
    "is this the best reaction yet?",
    "pov: reaction energy",
    "the face when the cut",
}


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
    via: str
    score: float = 0.0
    pool_size: int = 0
    candidates: list[str] = field(default_factory=list)


def _clean_title(title: str) -> str:
    t = re.sub(r"\s+", " ", title or "").strip()
    t = re.sub(
        r"(?i)\b(gta\s*5|gta\s*v|gta\s*vi|gta\s*6|gta\s*online|"
        r"anime|manga|reaction|reacts|shorts?|tiktok|#\w+|official|hd|4k|"
        r"trailer|edit|compilation|movie|film|launch trailer|gameplay|"
        r"8k|hdr|visuals?|insane)\b",
        " ",
        t,
    )
    t = re.sub(r"[|–—].*$", "", t)
    t = re.sub(r"\s+", " ", t).strip(" -_|:.")
    return t


def _action_tokens(ctx: HookContext) -> list[str]:
    """Derive action/scene tokens from reason + transcript + boost hits."""
    toks: list[str] = []
    reason = (ctx.reason or "").lower()
    for key, label in (
        ("loud", "the volume spike"),
        ("hype_speech", "that line"),
        ("motion", "the movement"),
        ("cut", "the cut"),
        ("keyword", "that beat"),
        ("laugh", "the laugh"),
        ("scream", "the scream"),
        ("face", "face cam"),
        ("react", "the reaction"),
    ):
        if key in reason:
            toks.append(label)
    blob = f"{ctx.transcript_snippet} {' '.join(ctx.boost_hits)}".lower()
    for w in (
        "chase", "crash", "fight", "jump", "flip", "gun", "police", "wanted",
        "domain", "bankai", "power", "final", "twist", "boss", "clutch", "fail",
        "laugh", "scream", "wait", "bro", "omg", "crazy", "insane", "look",
    ):
        if w in blob:
            toks.append(w)
    # uniq preserve order
    out: list[str] = []
    for t in toks:
        if t not in out:
            out.append(t)
    return out[:6]



def _sentiment(ctx: HookContext) -> str:
    blob = f"{ctx.title} {ctx.transcript_snippet} {ctx.reason}".lower()
    if any(k in blob for k in ("funny", "laugh", "fail", "meme", "joke")):
        return "funny"
    if any(k in blob for k in ("sad", "cry", "emotional", "tear")):
        return "emotional"
    if any(k in blob for k in ("insane", "crazy", "hype", "scream", "fire", "goat")):
        return "hype"
    if any(k in blob for k in ("wait", "what", "shock", "twist")):
        return "shock"
    return "neutral"


def _detect_scene_type(ctx: HookContext) -> str:
    blob = f"{ctx.title} {ctx.transcript_snippet} {' '.join(ctx.boost_hits)} {ctx.niche_id}".lower()
    rules = [
        ("trailer", ("trailer", "teaser", "cinematic")),
        ("fight", ("fight", "battle", "vs", "boss", "bankai")),
        ("reaction", ("reaction", "reacts", "streamer", "watching")),
        ("anime", ("anime", "manga", "shonen")),
        ("funny", ("funny", "laugh", "fail", "meme")),
        ("chase", ("chase", "police", "cops", "wanted")),
        ("movie", ("movie", "film", "cinema")),
    ]
    for name, keys in rules:
        if any(k in blob for k in keys):
            return name
    if "loud" in (ctx.reason or "") or "laugh" in (ctx.reason or ""):
        return "hype_moment"
    if "motion" in (ctx.reason or "") or "face" in (ctx.reason or ""):
        return "action"
    return "general"


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
    if niche_styles:
        preferred = list(niche_styles)
    elif scene == "reaction":
        preferred = ["reaction", "pov", "question", "hype"]
    elif scene in ("fight", "action", "hype_moment", "chase"):
        preferred = ["hype", "pov", "when_you", "question"]
    elif scene in ("funny",):
        preferred = ["when_you", "pov", "reaction", "hot_take"]
    else:
        preferred = ["hype", "question", "pov", "when_you", "hot_take", "reaction"]
    banned = set(recent[-3:]) if recent else set()
    ordered = [s for s in preferred if s not in banned] + [s for s in preferred if s in banned]
    for s in STYLES:
        if s not in ordered:
            ordered.append(s)
    return ordered


def _niche_bank(niche: str, actions: list[str], vocab: list[str], hits: list[str], snip: str) -> dict[str, list[str]]:
    """Big varied template bank per niche — never 'POV: <niche>' alone."""
    a = actions[0] if actions else "this moment"
    a2 = actions[1] if len(actions) > 1 else a
    v = random.choice(vocab) if vocab else ""
    h = hits[0] if hits else ""
    sn = _truncate(snip, 20) if snip else ""

    common_hype = [
        "NO WAY THIS IS REAL",
        "ABSOLUTE CINEMA",
        "I WASN'T READY",
        "SEND THIS TO YOUR GROUP CHAT",
        f"WAIT FOR {a.upper()}" if a else "WAIT FOR IT",
    ]
    banks: dict[str, dict[str, list[str]]] = {
        "gta6": {
            "pov": [
                "POV: the wanted level just cooked",
                f"POV: {a} in Vice City" if a else "POV: Los Santos goes feral",
                "POV: Lucia said say less",
                "POV: Rockstar framed this shot",
                f"POV: chat after {h}" if h else "POV: the heist goes sideways",
            ],
            "when_you": [
                "When the cops finally catch up",
                f"When {a} hits in GTA physics",
                "When the trailer drops a new detail",
                "When Vice City lighting goes crazy",
            ],
            "hot_take": [
                "Hot take: this frame alone sold GTA 6",
                "Hot take: the physics already won",
                f"Hot take: {v} clears the last trailer" if v else "Hot take: Rockstar cooked",
            ],
            "question": [
                "Did Rockstar just spoil the heist?",
                "Would you survive this chase?",
                f"Wait… did you catch {a}?",
                "Is this the most detailed GTA ever?",
            ],
            "hype": common_hype + ["GTA 6 IS ACTUALLY COMING", "VICE CITY LOOKS ILLEGAL"],
            "reaction": [
                "Bro the trailer face:",
                "My honest GTA reaction:",
                "I paused on this frame",
                "Bro the face cam:",
            ],
            "info_news": [
                "New GTA 6 detail you missed",
                f"Trailer check: {v}" if v else "Hidden trailer detail",
                "Quick GTA 6 rundown",
            ],
        },
        "anime": {
            "pov": [
                "POV: the aura started speaking",
                f"POV: {a} before the power-up" if a else "POV: plot armor activates",
                "POV: sakuga frame one",
                f"POV: when they said {sn}" if sn else "POV: the final form drops",
                "POV: shonen beat just slapped",
            ],
            "when_you": [
                "When the animation budget hits",
                f"When {a} starts the comeback",
                "When the OST goes demonic",
                f"When {v} activates" if v else "When the domain expands",
            ],
            "hot_take": [
                "Hot take: this fight clears the season",
                f"Hot take: {v} is peak" if v else "Hot take: sakuga won",
                "Hot take: the still frames slap harder",
            ],
            "question": [
                "Did the animator just ascend?",
                "Would you tank this hit?",
                f"Wait… was that {h}?" if h else "Is this the best sakuga yet?",
                "Who else felt that aura?",
            ],
            "hype": common_hype + ["AURA OVERLOAD", "THE ANIMATION IS ILLEGAL", "BANKAI ENERGY"],
            "reaction": [
                "My honest anime reaction:",
                "Bro the frame rate:",
                "Bro the face cam:",
                "I rewound this three times",
            ],
            "info_news": [
                "New anime drop you missed",
                f"Fight detail: {v}" if v else "Hidden sakuga detail",
                "Quick anime rundown",
            ],
        },
        "reactions": {
            "pov": [
                "POV: chat typed W instantly",
                f"POV: {a} breaks the streamer" if a else "POV: the mic check fails",
                "POV: headphones can't handle this",
                "POV: first time watching hits",
            ],
            "when_you": [
                "When the streamer goes silent",
                f"When {a} hits the face cam",
                "When chat starts spamming",
                "When the laugh ruins the take",
            ],
            "hot_take": [
                "Hot take: the reaction > the trailer",
                "Hot take: face cam carried",
                f"Hot take: {v} reactions clear" if v else "Hot take: this scream is iconic",
            ],
            "question": [
                "Did chat survive this?",
                "Would you react the same?",
                f"Wait… did you catch {a}?" if a else "Wait… did chat survive?",
                "Did the face cam just break?",
            ],
            "hype": common_hype + ["CHAT IS GOING FERAL", "THE SCREAM THO"],
            "reaction": [
                "My honest reaction to his reaction:",
                "Bro the face cam:",
                f"The scream when {a}" if a else "The scream though",
                "I felt that through the mic",
            ],
            "info_news": [
                "Streamer meltdown you missed",
                "Clip check: face cam peak",
                "Quick reaction rundown",
            ],
        },
        "gaming": {
            "pov": [
                "POV: the clutch with 1 HP",
                f"POV: {a} wins the round",
                "POV: ranked trauma unlocked",
            ],
            "when_you": [
                "When the clutch actually hits",
                f"When {a} saves the team",
                "When the boss phase starts",
            ],
            "hot_take": [
                "Hot take: this play was illegal",
                f"Hot take: {v} is cracked" if v else "Hot take: mechanics won",
            ],
            "question": [
                "How is this even possible?",
                f"Wait… did they just {a}?",
                "Would you hit this shot?",
            ],
            "hype": common_hype + ["CLEANEST CLIP OF THE WEEK", "THAT DIFF WAS CRAZY"],
            "reaction": ["The scream after the clutch:", "Bro the 1v3:", f"Face when {a}"],
            "info_news": ["New gaming clip rundown", f"Play detail: {v}" if v else "Hidden tech"],
        },
        "funny": {
            "pov": ["POV: the fail was personal", f"POV: {a} ruins the take", "POV: main character syndrome"],
            "when_you": ["When the joke writes itself", f"When {a} goes too far", "When you blink and miss the bit"],
            "hot_take": ["Hot take: this is peak comedy", "Hot take: the fail cleared"],
            "question": ["Did anyone else rewind?", f"Wait… was that on purpose?", "Would you post this?"],
            "hype": common_hype + ["SEND IT", "I CAN'T BREATHE"],
            "reaction": ["My honest reaction:", "The laugh ruined me", f"Face when {a}"],
            "info_news": ["Viral bit you missed", "Quick funny rundown"],
        },
        "movies": {
            "pov": ["POV: the score just slapped", f"POV: {a} in IMAX", "POV: cinema lighting"],
            "when_you": ["When the trailer hides the twist", f"When {a} hits the theater", "When the director cooks"],
            "hot_take": ["Hot take: this trailer sold the movie", f"Hot take: {v} directed this vibe" if v else "Hot take: frame one cinema"],
            "question": ["Did you catch the Easter egg?", f"Wait… was that {h}?" if h else "Is this Oscar bait?", "Would you watch opening night?"],
            "hype": common_hype + ["CINEMA IS BACK", "THE TRAILER COOKED"],
            "reaction": ["My theater reaction:", "Bro the score:", f"Face when {a}"],
            "info_news": ["Trailer detail you missed", f"Film check: {v}" if v else "Hidden frame"],
        },
        "animation": {
            "pov": ["POV: the squash and stretch cooked", f"POV: {a} in frame one", "POV: toon force activates"],
            "when_you": ["When the animation budget prints", f"When {a} goes full cartoon", "When the still frame slaps"],
            "hot_take": ["Hot take: this short clears features", f"Hot take: {v} animation" if v else "Hot take: indie animators ate"],
            "question": ["Did the animator just peak?", f"Wait… how many frames was {a}?"],
            "hype": common_hype + ["FRAME ONE CINEMA", "THE INBETWEENING THO"],
            "reaction": ["My honest toon reaction:", f"Face when {a}"],
            "info_news": ["Animation detail you missed", "Quick cartoon rundown"],
        },
    }
    # default/custom falls back to funny+hype mix
    base = banks.get(niche) or banks["funny"]
    # Transcript-driven lines only when we have a short quoted phrase
    # Reject truncated mid-sentence fragments (ellipses / dangling connectors)
    bad_starts = ("when ", "the ", "look ", "done ", "not ", "because ", "also ", "gives ", "us ")
    sn_ok = (
        bool(sn)
        and len(sn.split()) >= 2
        and "…" not in sn
        and not sn.lower().startswith(bad_starts)
        and not sn.lower().endswith((" the", " a", " to", " of", " and"))
    )
    quote = sn if sn_ok else ""
    extra = {
        "pov": ([f'POV: they whispered "{quote}"'] if quote else []) + ([f"POV: {h} energy"] if h and h.lower() not in {"anime","gta","gta 6","gta6","gaming","funny","movies","reaction","reactions","animation","trailer"} else []),
        "question": ([f'Wait… did they say "{quote}"?'] if quote else []),
        "when_you": ([f'When they said "{quote}"'] if quote else []),
        "hype": [],
        "hot_take": [],
        "reaction": [],
        "info_news": [],
    }
    out: dict[str, list[str]] = {}
    for style, lines in base.items():
        out[style] = list(lines) + list(extra.get(style) or [])
    return out


def _rule_pool(style: str, ctx: HookContext, scene: str, vocab: list[str]) -> list[str]:
    actions = _action_tokens(ctx)
    hits = [h for h in ctx.boost_hits if h and len(h) > 2][:4]
    snip = (ctx.transcript_snippet or "").strip()
    # Prefer energetic words from transcript
    snip_words = [w for w in re.findall(r"[A-Za-z']+", snip) if len(w) > 3][:8]
    snip_short = " ".join(snip_words[:5])
    niche = (ctx.niche_id or "gta6").lower()
    bank = _niche_bank(niche, actions, vocab, hits, snip_short)
    options = list(bank.get(style) or bank.get("hype") or ["ABSOLUTE CINEMA"])
    # Never include raw cleaned title shout
    return [_truncate(o, 42) for o in options if o]


def _is_title_echo(text: str, ctx: HookContext) -> bool:
    clean = _clean_title(ctx.title).lower()
    blob = text.lower()
    if not clean:
        # still catch niche-only generics
        return False
    # If most title tokens appear in the header, it's an echo
    bits = [w for w in re.findall(r"[a-z0-9]+", clean) if len(w) > 3]
    if not bits:
        return False
    hits = sum(1 for w in bits if w in blob)
    if hits >= max(2, int(0.6 * len(bits))):
        return True
    # Header is basically the title
    if clean and clean[:18] in blob:
        return True
    return False


def _is_generic(text: str, ctx: HookContext) -> bool:
    blob = re.sub(r"\s+", " ", text.lower().strip())
    if blob in GENERIC_BANNED:
        return True
    # POV: <single niche word>
    m = re.match(r"^pov:\s*([a-z0-9 ]{1,16})$", blob)
    if m:
        token = m.group(1).strip()
        if token in {
            "anime", "gta", "gta 6", "gaming", "funny", "movies", "reaction",
            "reactions", "animation", "trailer", "clip", "video",
        }:
            return True
        if token == (ctx.niche_id or "").lower():
            return True
    # Too short / hollow
    words = text.split()
    if len(words) < 3:
        return True
    if blob in {"absolute cinema", "no way this is real", "this is insane", "lock in"}:
        return False
    # POV: <niche-ish> energy
    m2 = re.match(r"^pov:\s*(.+?)\s+energy$", blob)
    if m2:
        token = m2.group(1).strip()
        if token in {
            "anime", "gta", "gta 6", "gta6", "gaming", "funny", "movies",
            "reaction", "reactions", "animation", "trailer", "clip",
        } or token == (ctx.niche_id or "").lower():
            return True
    # Hollow "best X yet" without specifics — soft allow via score, not hard ban
    return False


def _score_candidate(text: str, style: str, ctx: HookContext, scene: str) -> float:
    if _is_title_echo(text, ctx) or _is_generic(text, ctx):
        return -100.0
    score = 0.0
    words = text.split()
    n = len(words)
    if 3 <= n <= 7:
        score += 2.4
    elif n <= 9:
        score += 1.2
    else:
        score -= 1.0
    # Prefer headers that fit on screen without ellipsis
    if len(text) <= 36:
        score += 0.8
    elif len(text) > 42:
        score -= 1.2
    blob = text.lower()
    # Penalize hollow "best X yet"
    if "best" in blob and "yet" in blob:
        score -= 1.8
    actions = _action_tokens(ctx)
    for a in actions:
        if a.lower() in blob:
            score += 1.1
    for h in ctx.boost_hits[:5]:
        if h.lower() in blob:
            score += 0.9
    for w in (ctx.transcript_snippet or "").lower().split()[:12]:
        if len(w) > 4 and w in blob:
            score += 0.35
    # Curiosity / specificity
    if "?" in text:
        score += 0.9
    if any(x in blob for x in ("wait", "did you", "would you", "who else", "no way")):
        score += 0.6
    if style == "pov" and blob.startswith("pov"):
        score += 0.5
    if style == "hype" and text.isupper():
        score += 0.35
    # Penalize near-empty hype spam
    if blob in {"absolute cinema", "no way this is real", "this is insane", "lock in"}:
        score -= 1.5
    # Soft title-token overlap (one word ok)
    clean_bits = [w for w in _clean_title(ctx.title).lower().split() if len(w) > 3][:5]
    overlap = sum(1 for w in clean_bits if w in blob)
    if overlap >= 2:
        score -= 1.5
    elif overlap == 1:
        score -= 0.2
    score += random.uniform(0, 0.5)
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
        f"Niche: {ctx.niche_id}\nScene: {scene}\nActions: {', '.join(_action_tokens(ctx))}\n"
        f"Transcript keywords: {ctx.transcript_snippet[:220]}\n"
        f"Styles: {', '.join(styles[:5])}\n"
        "Rules: ONE line, 3-8 words, NO quoting the video title, NO 'POV: anime' or niche-only lines, "
        "be specific and curious. Number 1..N."
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
            if 3 <= len(line.split()) <= 10:
                style = styles[len(out) % len(styles)]
                out.append((_truncate(line, 42), style))
            if len(out) >= n:
                break
        return out
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return []


def generate_hook_pool(ctx: HookContext, cfg: dict[str, Any], pool_size: int = 24) -> list[tuple[str, str, str]]:
    root = Path(cfg.get("_root") or ".")
    hooks_cfg = cfg.get("hooks") or {}
    recent = load_recent_styles(root)
    styles = _preferred_styles(ctx, cfg, recent)
    scene = _detect_scene_type(ctx)
    vocab = list(hooks_cfg.get("vocab") or [])
    pool: list[tuple[str, str, str]] = []

    if hooks_cfg.get("use_ollama", True):
        for text, style in try_ollama_pool(
            styles, ctx, scene,
            model=str(hooks_cfg.get("ollama_model", "llama3.2")),
            host=str(hooks_cfg.get("ollama_host", "http://127.0.0.1:11434")),
            timeout=float(hooks_cfg.get("ollama_timeout", 8)),
            n=min(8, pool_size),
        ):
            pool.append((text, style, "ollama"))

    style_cycle = styles[:]
    random.shuffle(style_cycle)
    for style in style_cycle:
        for text in _rule_pool(style, ctx, scene, vocab):
            pool.append((text, style, "rules"))
            if len(pool) >= pool_size * 3:
                break
        if len(pool) >= pool_size * 3:
            break

    seen: set[str] = set()
    uniq: list[tuple[str, str, str]] = []
    for text, style, via in pool:
        key = text.lower()
        if key in seen:
            continue
        if _is_title_echo(text, ctx) or _is_generic(text, ctx):
            continue
        seen.add(key)
        uniq.append((text, style, via))
    random.shuffle(uniq)
    return uniq[: max(pool_size, 12)]


def generate_hook(ctx: HookContext, cfg: dict[str, Any]) -> HookResult:
    hooks_cfg = cfg.get("hooks") or {}
    pool_n = int(hooks_cfg.get("pool_size", 24))
    pool = generate_hook_pool(ctx, cfg, pool_size=pool_n)
    scene = _detect_scene_type(ctx)
    scored: list[tuple[float, str, str, str]] = []
    for text, style, via in pool:
        sc = _score_candidate(text, style, ctx, scene)
        if sc <= -50:
            continue
        scored.append((sc, text, style, via))
    scored.sort(key=lambda x: x[0], reverse=True)
    if not scored:
        # Safe niche-specific fallbacks (never title echo / POV: niche)
        niche = (ctx.niche_id or "gta6").lower()
        fallbacks = {
            "gta6": ("NO WAY THIS IS REAL", "hype"),
            "anime": ("AURA OVERLOAD", "hype"),
            "reactions": ("THE SCREAM THO", "hype"),
            "gaming": ("CLEANEST CLIP OF THE WEEK", "hype"),
            "funny": ("I CAN'T BREATHE", "hype"),
            "movies": ("CINEMA IS BACK", "hype"),
            "animation": ("FRAME ONE CINEMA", "hype"),
        }
        text, style = fallbacks.get(niche, ("ABSOLUTE CINEMA", "hype"))
        scored = [(0.5, text, style, "fallback")]
    # Prefer intact on-screen length among top scores
    top = scored[:8]
    weights = []
    for sc, tx, _st, _via in top:
        w = max(0.15, sc) ** 2
        if len(tx) <= 34:
            w *= 1.6
        elif len(tx) > 38:
            w *= 0.35
        weights.append(w)
    top = top[:5]
    weights = weights[:5]
    pick = random.choices(top, weights=weights, k=1)[0]
    _, text, style, via = pick
    push_style(Path(cfg.get("_root") or "."), style)
    return HookResult(
        text=text,
        style=style,
        via=via,
        score=float(pick[0]),
        pool_size=len(pool),
        candidates=[t[1] for t in scored[:8]],
    )


def pick_style(ctx: HookContext, recent: list[str]) -> str:
    return _preferred_styles(ctx, {"hooks": {}}, recent)[0]
