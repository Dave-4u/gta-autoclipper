"""Render fully edited 9:16 clips with blur fill, smart hook, captions, VO, loudnorm."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .analyze import Segment, get_word_timestamps
from .hooks import HookContext, generate_hook
from .voiceover import make_intro_vo, make_voiceover

COLOR_ACTIVE = "H0000FFFF"
COLOR_DIM = "H00FFFFFF"


def _find_font(cfg: dict[str, Any]) -> str:
    custom = (cfg.get("clips") or {}).get("font") or ""
    if custom and Path(custom).is_file():
        return custom
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/segoeuib.ttf",
        "C:/Windows/Fonts/segoeui.ttf",
        "C:/Windows/Fonts/tahomabd.ttf",
        "C:/Windows/Fonts/Tahoma.ttf",
        "C:/Windows/Fonts/impact.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ]
    # Also scan Windows Fonts dir for any bold-ish TTF if explicit paths missed
    try:
        import os
        windir = os.environ.get("WINDIR") or os.environ.get("SystemRoot")
        if windir:
            fontdir = Path(windir) / "Fonts"
            for name in ("arialbd.ttf", "segoeuib.ttf", "calibrib.ttf", "verdanab.ttf", "arial.ttf"):
                candidates.append(str(fontdir / name))
    except Exception:
        pass
    for c in candidates:
        if Path(c).is_file():
            return c
    return ""


def _escape_drawtext(text: str) -> str:
    """Sanitize text for ffmpeg drawtext inside filter_complex single quotes."""
    text = text.replace(chr(92), " ")
    text = text.replace("'", "")
    text = text.replace(chr(34), "")
    # Replace ":" with " -" so ffmpeg drawtext option parsing stays valid
    text = text.replace(":", " -")
    text = text.replace("%", "%%")
    text = text.replace(",", " ")
    text = text.replace("[", "(").replace("]", ")")
    text = text.replace(";", ",")
    text = text.replace(chr(8230), "...")
    return text



def _fit_header(text: str, max_chars: int = 30) -> str:
    """Keep headers short so they do not clip off the 9:16 frame."""
    text = re.sub(r"\s+", " ", (text or "").strip())
    if len(text) <= max_chars:
        return text
    # Prefer cutting at word boundary
    cut = text[: max_chars - 1].rsplit(" ", 1)[0]
    return (cut or text[: max_chars - 1]).rstrip(" .,:;-") + "…"


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
    words: list[dict[str, Any]], font_opt: str
) -> str:
    filters = []
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


def _copyright_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    clips = cfg.get("clips") or {}
    cs = clips.get("copyright_safe")
    if isinstance(cs, dict):
        return cs
    top = cfg.get("copyright_safe")
    if isinstance(top, dict):
        return top
    return {"enabled": True}


def _cap_segment(segment: Segment, cfg: dict[str, Any]) -> Segment:
    cs = _copyright_cfg(cfg)
    if not cs.get("enabled", True):
        return segment
    max_src = float(cs.get("max_source_seconds", 18))
    length = segment.end - segment.start
    if length <= max_src:
        return segment
    mid = (segment.start + segment.end) / 2.0
    start = max(segment.start, mid - max_src / 2.0)
    end = start + max_src
    if end > segment.end:
        end = segment.end
        start = max(segment.start, end - max_src)
    return Segment(
        start=round(start, 2),
        end=round(end, 2),
        score=segment.score,
        reason=segment.reason + "+safe",
    )


def _video_chain(
    *,
    width: int,
    height: int,
    ass_filter: str | None,
    hook: str,
    font_opt: str,
    hook_dur: float,
    speed: float,
    zoom: float,
    commentary: str,
    commentary_on: bool,
    drawtext_extra: str = "",
    in_label: str = "0:v",
    out_label: str = "vout",
) -> str:
    z = max(1.0, float(zoom))
    speed = max(0.5, min(2.0, float(speed)))
    parts = [
        f"[{in_label}]split[bg][fg]",
        f"[bg]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},gblur=sigma=20[bg]",
        f"[fg]scale=iw*{z}:ih*{z},crop=iw/{z}:ih/{z},"
        f"scale={width}:{height}:force_original_aspect_ratio=decrease[fg]",
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setpts=PTS/{speed}[base]",
    ]
    cur = "base"
    if ass_filter:
        parts.append(f"[{cur}]{ass_filter}[cap]")
        cur = "cap"
    parts.append(
        f"[{cur}]drawtext=text='{hook}'{font_opt}:fontsize=72:fontcolor=yellow:"
        f"borderw=5:bordercolor=black:x=(w-text_w)/2:y=h*0.15:"
        f"enable='lte(t\\,{hook_dur})'[hooked]"
    )
    cur = "hooked"
    if commentary_on and commentary:
        parts.append(
            f"[{cur}]drawtext=text='{commentary}'{font_opt}:fontsize=40:"
            f"fontcolor=white:borderw=3:bordercolor=black:"
            f"x=(w-text_w)/2:y=h*0.22:"
            f"enable='lte(t\\,{max(hook_dur + 2.5, 4.0)})'[com]"
        )
        cur = "com"
    if drawtext_extra:
        parts.append(f"[{cur}]{drawtext_extra}[{out_label}]")
    else:
        # Relabel final pad
        if cur != out_label:
            parts.append(f"[{cur}]null[{out_label}]")
    return ";".join(parts)


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

    segment = _cap_segment(segment, cfg)
    cs = _copyright_cfg(cfg)
    safe_on = bool(cs.get("enabled", True))
    speed = float(cs.get("speed", 1.05)) if safe_on else 1.0
    zoom = float(cs.get("zoom", 1.08)) if safe_on else 1.0
    commentary_on = bool(cs.get("commentary_layer", True)) if safe_on else False

    safe_id = re.sub(r"[^\w\-]+", "_", video_id or source.stem)[:40]
    out_path = out_dir / f"{safe_id}_clip{index+1:02d}.mp4"

    font = _find_font(cfg)
    font_name = "DejaVu Sans"
    if font:
        stem = Path(font).stem
        font_name = (
            stem.replace("Bold", "").replace("-", " ").replace("_", " ").strip()
            or font_name
        )

    ana = cfg.get("analysis") or {}
    model_name = str(ana.get("whisper_model", "base"))
    use_whisper = ana.get("use_whisper", True)
    vo_cfg = cfg.get("voiceover") or {}
    vo_enabled = bool(vo_cfg.get("enabled", True))
    burn_captions = bool(clips.get("burn_captions", True))

    words: list[dict[str, Any]] = []
    if use_whisper:
        words = get_word_timestamps(
            source, segment.start, segment.end, model_name=model_name
        )
        if not words and not vo_enabled:
            print("[edit] ERROR: transcription returned no words — retrying tiny")
            words = get_word_timestamps(
                source, segment.start, segment.end, model_name="tiny"
            )
    else:
        print("[edit] WARNING: whisper disabled (--no-whisper)")
        dur = segment.end - segment.start
        words = [
            {"word": "[no captions — whisper off]", "start": 0.3, "end": min(3.0, dur)}
        ]

    real_words = [w for w in words if not str(w.get("word", "")).startswith("[")]
    print(f"[edit] dialogue tokens: {len(real_words)}")

    snippet = " ".join(w.get("word", "") for w in real_words[:40] if w.get("word"))
    boost = []
    for w in ana.get("transcript_boost_words") or []:
        if w.lower() in (snippet + " " + title).lower():
            boost.append(w)
    ctx = HookContext(
        title=title,
        reason=segment.reason,
        transcript_snippet=snippet,
        boost_hits=boost,
        duration=segment.end - segment.start,
        niche_id=str(cfg.get("niche_id") or (cfg.get("niche") or {}).get("id") or "gta6"),
    )
    hook_res = generate_hook(ctx, cfg)
    print(
        f"[hooks] style={hook_res.style} via={hook_res.via} "
        f"score={hook_res.score:.2f} pool={hook_res.pool_size}: {hook_res.text}"
    )
    if hook_res.candidates:
        print("[hooks] pool sample: " + " | ".join(hook_res.candidates[:5]))
    hook_display = _fit_header(hook_res.text, 30)
    hook = _escape_drawtext(hook_display)
    intro_result = None
    vo_result = None
    caption_words: list[dict[str, Any]] = []
    intro_dur = float((cfg.get("voiceover") or {}).get("intro_seconds", 1.8))

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        vo_result = None
        intro_result = None
        caption_words = list(words)

        intro_cfg_on = bool(vo_cfg.get("intro_header", True))
        intro_dur = float(vo_cfg.get("intro_seconds", 1.8))
        if intro_cfg_on and hook_res.text:
            intro_wav = td_path / "intro.wav"
            intro_result = make_intro_vo(hook_res.text, cfg, intro_wav)
            if intro_result.wav_path and intro_result.wav_path.is_file():
                # Cap spoken intro conceptually; actual wav may be shorter/longer
                try:
                    import wave
                    with wave.open(str(intro_result.wav_path), "rb") as w:
                        intro_dur = min(2.2, max(0.9, w.getnframes() / float(w.getframerate() or 1)))
                except Exception:
                    intro_dur = min(2.0, max(1.0, intro_dur))

        if vo_enabled:
            vo_wav = td_path / "voiceover.wav"
            vo_result = make_voiceover(ctx, cfg, vo_wav)
            if vo_result.words:
                # Shift commentary captions after intro
                shift = intro_dur if (intro_result and intro_result.wav_path) else 0.0
                caption_words = [
                    {
                        "word": w["word"],
                        "start": float(w["start"]) + shift,
                        "end": float(w["end"]) + shift,
                    }
                    for w in vo_result.words
                ]
            elif vo_result.text:
                shift = intro_dur if (intro_result and intro_result.wav_path) else 0.0
                caption_words = [
                    {
                        "word": tok,
                        "start": shift + 0.2 + i * 0.35,
                        "end": shift + 0.55 + i * 0.35,
                    }
                    for i, tok in enumerate(vo_result.text.split()[:24])
                ]

        # Prefer intro words as early captions when present
        if intro_result and intro_result.words and burn_captions:
            caption_words = list(intro_result.words) + [
                w for w in caption_words if float(w.get("start", 0)) >= intro_dur - 0.05
            ]

        ass_path = td_path / "captions.ass"
        words_for_burn = caption_words if burn_captions else []
        n_events = _write_ass_captions(
            words_for_burn, ass_path, width, height, font_name=font_name
        )
        print(f"[edit] ASS events: {n_events}")
        out_path.with_suffix(".ass").write_text(
            ass_path.read_text(encoding="utf-8"), encoding="utf-8"
        )

        ass_escaped = str(ass_path).replace("\\", "/").replace(":", chr(92) + ":")
        fontsdir = ""
        if font:
            fontsdir = str(Path(font).parent).replace("\\", "/").replace(":", chr(92) + ":")
        ass_filter = f"ass='{ass_escaped}'"
        if fontsdir:
            ass_filter = f"ass='{ass_escaped}':fontsdir='{fontsdir}'"

        if font:
            _font_esc = font.replace(":", chr(92) + ":")
            font_opt = f":fontfile={_font_esc}"
        else:
            font_opt = ""

        commentary_text = ""
        # Avoid stacking commentary banner on top of karaoke captions/VO
        if commentary_on and not (burn_captions and (vo_enabled or intro_cfg_on)):
            raw = vo_result.text if vo_result else hook_res.text
            line = raw.split(".")[0].strip() or raw
            if len(line.split()) > 10:
                line = " ".join(line.split()[:10]) + "…"
            commentary_text = _escape_drawtext(line)

        # Animated header: bigger for intro_dur then settle
        hook_dur = max(hook_dur, intro_dur if intro_result and intro_result.wav_path else hook_dur)

        atempo = speed if abs(speed - 1.0) > 0.001 else 1.0
        src_af = f"atempo={atempo}" if abs(atempo - 1.0) > 0.001 else "anull"
        vo_vol = float(vo_cfg.get("volume", 1.0))
        duck = float(vo_cfg.get("duck_level", 0.22))
        use_sidechain = bool(vo_cfg.get("sidechain", True))

        # Build optional combined voice track: intro + delayed commentary
        voice_inputs: list[Path] = []
        if intro_result and intro_result.wav_path and intro_result.wav_path.is_file():
            voice_inputs.append(intro_result.wav_path)
        if vo_enabled and vo_result and vo_result.wav_path and vo_result.wav_path.is_file():
            voice_inputs.append(vo_result.wav_path)

        combined_vo = None
        if voice_inputs:
            combined_vo = td_path / "voice_mix.wav"
            if len(voice_inputs) == 1:
                combined_vo.write_bytes(voice_inputs[0].read_bytes())
            else:
                # intro then commentary with tiny gap; pad intro to intro_dur
                intro_p = voice_inputs[0]
                com_p = voice_inputs[1]
                # adelay commentary by intro_dur ms
                delay_ms = int(intro_dur * 1000)
                cmd_mix = [
                    "ffmpeg", "-y",
                    "-i", str(intro_p),
                    "-i", str(com_p),
                    "-filter_complex",
                    f"[0:a]apad=pad_dur={intro_dur:.3f},atrim=0:{intro_dur:.3f},volume={vo_vol}[intro];"
                    f"[1:a]adelay={delay_ms}|{delay_ms},volume={vo_vol}[com];"
                    f"[intro][com]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0[aout]",
                    "-map", "[aout]", str(combined_vo),
                ]
                proc_m = subprocess.run(cmd_mix, capture_output=True, text=True, check=False)
                if proc_m.returncode != 0 or not combined_vo.is_file():
                    print("[edit] voice mix failed, using intro only")
                    combined_vo.write_bytes(intro_p.read_bytes())

        def run_ffmpeg(use_ass: bool, use_dt_fallback: bool = False) -> bool:
            dt = _drawtext_caption_fallback(words_for_burn if burn_captions else [], font_opt) if use_dt_fallback else ""
            # Extra animated header layer: large then normal via two drawtexts
            header_fs_big = 54 if len(hook_display) > 20 else 68
            header_fs = 46 if len(hook_display) > 20 else 56
            anim = (
                f"drawtext=text='{hook}'{font_opt}:fontsize={header_fs_big}:fontcolor=yellow:"
                f"borderw=6:bordercolor=black:x=(w-text_w)/2:y=h*0.14:"
                f"enable='lte(t\\,{intro_dur:.3f})',"
                f"drawtext=text='{hook}'{font_opt}:fontsize={header_fs}:fontcolor=yellow:"
                f"borderw=5:bordercolor=black:x=(w-text_w)/2:y=h*0.15:"
                f"enable='gte(t\\,{intro_dur:.3f})*lte(t\\,{hook_dur:.3f})'"
            )
            # Use animated hook instead of single hook inside _video_chain — pass empty hook and append anim
            vchain = _video_chain(
                width=width,
                height=height,
                ass_filter=ass_filter if use_ass else None,
                hook=" ",  # placeholder tiny; replaced below
                font_opt=font_opt,
                hook_dur=0.01,
                speed=speed,
                zoom=zoom,
                commentary=commentary_text,
                commentary_on=commentary_on and bool(commentary_text),
                drawtext_extra=(anim + (("," + dt) if dt else "")),
                out_label="vout",
            )
            # Remove tiny placeholder drawtext if present — keep chain working
            has_vo = combined_vo is not None and combined_vo.is_file()
            if has_vo:
                if use_sidechain:
                    achain = (
                        f"[0:a]{src_af}[a0];"
                        f"[1:a]volume=1.0,apad=whole_dur=3600,asplit=2[sc][voice];"
                        f"[a0][sc]sidechaincompress=threshold=0.012:ratio=8:"
                        f"attack=40:release=280:makeup=1[ducked];"
                        f"[ducked][voice]amix=inputs=2:duration=first:"
                        f"dropout_transition=0:normalize=0,"
                        f"loudnorm=I={lufs}:TP=-1.5:LRA=11[aout]"
                    )
                else:
                    achain = (
                        f"[0:a]{src_af},volume={duck}[a0];"
                        f"[1:a]volume=1.0,apad=whole_dur=3600[voice];"
                        f"[a0][voice]amix=inputs=2:duration=first:"
                        f"dropout_transition=0:normalize=0,"
                        f"loudnorm=I={lufs}:TP=-1.5:LRA=11[aout]"
                    )
                fc = f"{vchain};{achain}"
                cmd = [
                    "ffmpeg", "-y",
                    "-ss", str(segment.start),
                    "-to", str(segment.end),
                    "-i", str(source),
                    "-i", str(combined_vo),
                    "-filter_complex", fc,
                    "-map", "[vout]",
                    "-map", "[aout]",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                    "-c:a", "aac", "-b:a", "192k",
                    "-movflags", "+faststart",
                    "-pix_fmt", "yuv420p",
                    "-r", "30",
                    str(out_path),
                ]
            else:
                achain = (
                    f"[0:a]{src_af},loudnorm=I={lufs}:TP=-1.5:LRA=11[aout]"
                    if src_af != "anull"
                    else f"[0:a]loudnorm=I={lufs}:TP=-1.5:LRA=11[aout]"
                )
                fc = f"{vchain};{achain}"
                cmd = [
                    "ffmpeg", "-y",
                    "-ss", str(segment.start),
                    "-to", str(segment.end),
                    "-i", str(source),
                    "-filter_complex", fc,
                    "-map", "[vout]",
                    "-map", "[aout]",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                    "-c:a", "aac", "-b:a", "192k",
                    "-movflags", "+faststart",
                    "-pix_fmt", "yuv420p",
                    "-r", "30",
                    str(out_path),
                ]
            proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if proc.returncode != 0 or not out_path.is_file():
                print((proc.stderr or "")[-1200:])
                return False
            return True

        ok = run_ffmpeg(use_ass=True)
        if not ok:
            print("[edit] ASS burn failed, falling back to timed drawtext…")
            if out_path.is_file():
                out_path.unlink(missing_ok=True)
            ok = run_ffmpeg(use_ass=False, use_dt_fallback=True)
            if not ok:
                print("[edit] render failed")
                return None

    meta_side = out_path.with_suffix(".hook.json")
    meta_side.write_text(
        json.dumps(
            {
                "hook": hook_res.text,
                "style": hook_res.style,
                "via": hook_res.via,
                "score": hook_res.score,
                "pool": hook_res.candidates[:8],
                "niche": cfg.get("niche_id"),
                "caption_words": len(caption_words) if "caption_words" in dir() else 0,
                "real_dialogue_words": len(real_words),
                "intro_vo": None
                if not intro_result
                else {
                    "text": intro_result.text,
                    "engine": intro_result.engine,
                    "seconds": intro_dur if "intro_dur" in dir() else None,
                },
                "voiceover": None
                if not vo_result
                else {
                    "text": vo_result.text,
                    "style": vo_result.style,
                    "via": vo_result.via,
                    "engine": vo_result.engine,
                },
                "copyright_safe": {
                    "enabled": safe_on,
                    "source_seconds": round(segment.end - segment.start, 2),
                    "speed": speed,
                    "zoom": zoom,
                },
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
