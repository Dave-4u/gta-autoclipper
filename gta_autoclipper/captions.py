"""Generate post captions + hashtags for each clip."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .analyze import Segment


def make_post_caption(
    title: str,
    segment: Segment,
    cfg: dict[str, Any],
    clip_path: Path,
) -> dict[str, str]:
    tags = cfg.get("hashtags") or [
        "#GTA", "#GTA5", "#Gaming", "#Shorts", "#FYP",
    ]
    clean = re.sub(r"\s+", " ", title or clip_path.stem).strip()
    hook_lines = [
        f"🔥 {clean}" if clean else "🔥 GTA chaos you need to see",
        "This GTA moment hits different 💀",
        "POV: Los Santos goes crazy again",
    ]
    # Pick by reason
    if "loud" in segment.reason:
        lead = "🔊 The sound alone is crazy…"
    elif "keyword" in segment.reason:
        lead = "Wait for the line 😂"
    elif "motion" in segment.reason:
        lead = "The physics engine said nope"
    else:
        lead = hook_lines[hash(clip_path.name) % len(hook_lines)]

    body = f"{lead}\n\n{clean}\n\n{' '.join(tags)}"
    srt_note = (
        f"Clip: {clip_path.name}\n"
        f"Source window: {segment.start:.1f}s – {segment.end:.1f}s\n"
        f"Score reason: {segment.reason}\n"
    )
    return {
        "caption": body,
        "hashtags": " ".join(tags),
        "meta": srt_note,
        "title_suggestion": clean[:80] or "GTA Funny Moment",
    }


def write_sidecar(clip_path: Path, info: dict[str, str]) -> Path:
    side = clip_path.with_suffix(".txt")
    text = (
        f"TITLE:\n{info['title_suggestion']}\n\n"
        f"CAPTION:\n{info['caption']}\n\n"
        f"HASHTAGS:\n{info['hashtags']}\n\n"
        f"NOTES:\n{info['meta']}"
    )
    side.write_text(text, encoding="utf-8")
    # Also JSON for bots
    clip_path.with_suffix(".json").write_text(
        json.dumps(info, indent=2), encoding="utf-8"
    )
    return side
