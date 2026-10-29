"""Render fully edited 9:16 clips with blur fill, smart hook, captions, loudnorm."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .analyze import Segment, get_word_timestamps
from .hooks import HookContext, generate_hook

# ASS colors are &HAABBGGRR
COLOR_ACTIVE = "H0000FFFF"   # yellow highlight (active word)
COLOR_DIM = "H00FFFFFF"      # white (upcoming / past in line)
COLOR_OUTLINE = "H00000000"  # black outline


def _find_font(cfg: dict[str, Any]) -> str:
    custom = (cfg.get("clips") or {}).get("font") or ""
    if custom and Path(custom).is_file():
        return custom
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    ]
    for c in candidates:
        if Path(c).is_file():
            return c
    return ""


def _escape_drawtext(text: str) -> str:
    text = text.replace("\\", "\\\\")
    text = text.replace(":", "\\:")
    text = text.replace("'", "\\'")
    text = text.replace("%", "%%")
    return text


def _ass_escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace("{", "(")
        .replace("}", ")")
        .replace("\n", " ")
    )


def _ts(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = int(t % 60)
    cs = int(round((t - int(t)) * 100))
    if cs >= 100:
        s += 1
        cs = 0
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _write_ass_captions(
    words: list[dict[str, Any]],
    ass_path: Path,
    width: int,
    height: int,
    font_name: str = "DejaVu Sans",
) -> int:
    """
    Burn colored word-by-word captions.
    For each word in a chunk, emit a dialogue line spanning that word's time
    where the active word is yellow and the rest of the chunk is white.
    Returns number of dialogue events written.
    """
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font_name},70,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,5,2,2,50,50,260,1
Style: Word,{font_name},78,&H0000FFFF,&H000000FF,&H00000000,&H90000000,-1,0,0,0,100,100,0,0,1,6,2,2,50,50,260,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    cleaned = []
    for w in words:
        token = (w.get("word") or "").strip()
        if not token:
            continue
        start = float(w["start"])
        end = float(w["end"])
        if end <= start:
            end = start + 0.18
        cleaned.append({"word": token, "start": start, "end": end})

    if not cleaned:
        ass_path.write_text(header, encoding="utf-8")
        return 0

    # Chunk into groups of ~5 words for readable karaoke lines
    chunks: list[list[dict[str, Any]]] = []
    cur: list[dict[str, Any]] = []
    for w in cleaned:
        cur.append(w)
        if len(cur) >= 5 or w["word"].endswith((".", "!", "?", ",")):
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)

    events: list[str] = []
    for chunk in chunks:
        tokens = [_ass_escape(c["word"]) for c in chunk]
        for i, w in enumerate(chunk):
            # Build colored line: past white, active yellow+bold/scale, future white
            parts = []
            for j, tok in enumerate(tokens):
                if j == i:
                    parts.append(
                        r"{\c&" + COLOR_ACTIVE + r"&\b1\fscx110\fscy110}"
                        + tok
                        + r"{\r}"
                    )
                else:
                    parts.append(r"{\c&" + COLOR_DIM + r"&}" + tok + r"{\r}")
            line = " ".join(parts)
            start = w["start"]
            end = w["end"]
            # Hold until next word if gap is small
            if i + 1 < len(chunk):
                end = max(end, chunk[i + 1]["start"])
            else:
                end = max(end, start + 0.35)
            if end <= start:
                end = start + 0.2
            events.append(
                f"Dialogue: 0,{_ts(start)},{_ts(end)},Word,,0,0,0,,{line}"
            )

    ass_path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    return len(events)


def _drawtext_caption_fallback(
    words: list[dict[str, Any]], font_opt: str, clip_dur: float
) -> str:
    """If ASS fails, stack timed drawtext filters for each word (colored)."""
    filters = []
    # Show ~1 word at a time in yellow
    for w in words[:80]:
        token = _escape_drawtext((w.get("word") or "").strip())
        if not token:
            continue
        s = float(w["start"])
        e = float(w["end"])
        if e <= s:
            e = s + 0.25
        filters.append(
            f"drawtext=text='{token}'{font_opt}:fontsize=64:fontcolor=yellow:"
            f"borderw=4:bordercolor=black:x=(w-text_w)/2:y=h*0.78:"
            f"enable='between(t\\,{s:.3f}\\,{e:.3f})'"
        )
    return ",".join(filters)


def render_clip(
    source: Path,
    segment: Segment,
    cfg: dict[str, Any],
    *,
    title: str = "",
    index: int = 0,
    video_id: str = "",
) -> Path | None:
    clips = cfg["clips"]
    width = int(clips.get("width", 1080))
    height = int(clips.get("height", 1920))
    lufs = float(clips.get("loudness_lufs", -14))
    hook_dur = float(clips.get("hook_duration", 2.0))
    out_dir = Path(clips["output_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)

    safe_id = re.sub(r"[^\w\-]+", "_", video_id or source.stem)[:40]
    out_path = out_dir / f"{safe_id}_clip{index+1:02d}.mp4"

    font = _find_font(cfg)
    font_name = "DejaVu Sans"
    if font:
        stem = Path(font).stem
        font_name = stem.replace("Bold", "").replace("-", " ").replace("_", " ").strip() or font_name

    ana = cfg.get("analysis") or {}
    model_name = str(ana.get("whisper_model", "base"))
    use_whisper = ana.get("use_whisper", True)

    words: list[dict[str, Any]] = []
    if use_whisper:
        words = get_word_timestamps(
            source,
            segment.start,
            segment.end,
            model_name=model_name,
        )
        if not words:
            print("[edit] ERROR: transcription returned no words — captions will use placeholders")
            words = get_word_timestamps(
                source, segment.start, segment.end, model_name="tiny"
            )
    else:
        print("[edit] WARNING: whisper disabled (--no-whisper); captions will be placeholders")
        dur = segment.end - segment.start
        words = [
            {"word": "[no captions — whisper off]", "start": 0.3, "end": min(3.0, dur)}
        ]

    real_words = [w for w in words if not str(w.get("word", "")).startswith("[")]
    print(f"[edit] caption words: {len(words)} ({len(real_words)} real dialogue tokens)")

    snippet = " ".join(w.get("word", "") for w in real_words[:40] if w.get("word"))
    boost = []
    for w in ana.get("transcript_boost_words") or []:
        if w.lower() in (snippet + " " + title).lower():
            boost.append(w)
    hook_res = generate_hook(
        HookContext(
            title=title,
            reason=segment.reason,
            transcript_snippet=snippet,
            boost_hits=boost,
            duration=segment.end - segment.start,
        ),
        cfg,
    )
    print(f"[hooks] style={hook_res.style} via={hook_res.via}: {hook_res.text}")
    hook = _escape_drawtext(hook_res.text)

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        ass_path = td_path / "captions.ass"
        n_events = _write_ass_captions(words, ass_path, width, height, font_name=font_name)
        print(f"[edit] ASS events: {n_events}")

        # Copy ASS next to output for debugging
        debug_ass = out_path.with_suffix(".ass")
        debug_ass.write_text(ass_path.read_text(encoding="utf-8"), encoding="utf-8")

        ass_escaped = str(ass_path).replace("\\", "/").replace(":", "\\:")
        fontsdir = ""
        if font:
            fontsdir = str(Path(font).parent).replace("\\", "/").replace(":", "\\:")
        ass_filter = f"ass='{ass_escaped}'"
        if fontsdir:
            ass_filter = f"ass='{ass_escaped}':fontsdir='{fontsdir}'"

        font_opt = f":fontfile={font.replace(':', '\\:')}" if font else ""

        vf = (
            f"split[bg][fg];"
            f"[bg]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},gblur=sigma=20[bg];"
            f"[fg]scale={width}:{height}:force_original_aspect_ratio=decrease[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2[base];"
            f"[base]{ass_filter}[cap];"
            f"[cap]drawtext=text='{hook}'{font_opt}:fontsize=72:fontcolor=yellow:"
            f"borderw=5:bordercolor=black:x=(w-text_w)/2:y=h*0.12:"
            f"enable='lte(t\\,{hook_dur})'"
        )

        cmd = [
            "ffmpeg", "-y",
            "-ss", str(segment.start),
            "-to", str(segment.end),
            "-i", str(source),
            "-vf", vf,
            "-af", f"loudnorm=I={lufs}:TP=-1.5:LRA=11",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            "-pix_fmt", "yuv420p",
            "-r", "30",
            str(out_path),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0 or not out_path.is_file():
            print("[edit] ASS burn failed, falling back to timed drawtext captions…")
            print((proc.stderr or "")[-1000:])
            dt = _drawtext_caption_fallback(words, font_opt, segment.end - segment.start)
            vf2 = (
                f"split[bg][fg];"
                f"[bg]scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},gblur=sigma=20[bg];"
                f"[fg]scale={width}:{height}:force_original_aspect_ratio=decrease[fg];"
                f"[bg][fg]overlay=(W-w)/2:(H-h)/2,"
                f"drawtext=text='{hook}'{font_opt}:fontsize=72:fontcolor=yellow:"
                f"borderw=5:bordercolor=black:x=(w-text_w)/2:y=h*0.12:"
                f"enable='lte(t\\,{hook_dur})'"
            )
            if dt:
                vf2 += "," + dt
            cmd2 = [
                "ffmpeg", "-y",
                "-ss", str(segment.start),
                "-to", str(segment.end),
                "-i", str(source),
                "-vf", vf2,
                "-af", f"loudnorm=I={lufs}:TP=-1.5:LRA=11",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart",
                "-pix_fmt", "yuv420p",
                "-r", "30",
                str(out_path),
            ]
            proc2 = subprocess.run(cmd2, capture_output=True, text=True, check=False)
            if proc2.returncode != 0 or not out_path.is_file():
                print(f"[edit] failed: {(proc2.stderr or '')[-600:]}")
                return None

    meta_side = out_path.with_suffix(".hook.json")
    meta_side.write_text(
        json.dumps(
            {
                "hook": hook_res.text,
                "style": hook_res.style,
                "via": hook_res.via,
                "caption_words": len(words),
                "real_dialogue_words": len(real_words),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return out_path


def probe_clip(path: Path) -> dict[str, Any]:
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,duration,codec_name",
        "-show_entries", "format=duration,size",
        "-of", "json", str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    try:
        return json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return {}
