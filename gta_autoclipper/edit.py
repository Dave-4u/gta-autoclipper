"""Render fully edited 9:16 clips with blur fill, smart hook, captions, VO, loudnorm."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .analyze import Segment, get_word_timestamps
from .hooks import HookContext, HookResult, generate_hook
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



def _fit_header(text: str, max_chars: int = 36) -> str:
    """Keep headers readable on 9:16; never truncate mid-word / mid-name."""
    text = re.sub(r"\s+", " ", (text or "").strip())
    if len(text) <= max_chars:
        return text
    words = text.split()
    out: list[str] = []
    for w in words:
        trial = (" ".join(out + [w])).strip()
        if len(trial) > max_chars:
            break
        out.append(w)
    if out:
        return " ".join(out)
    return text[:max_chars].rsplit(" ", 1)[0] or text[:max_chars]



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

    # Chronological order; drop punctuation-only tokens (emdash etc.)
    cleaned.sort(key=lambda w: (float(w["start"]), float(w["end"])))
    cleaned = [w for w in cleaned if any(ch.isalnum() for ch in w["word"])]
    if not cleaned:
        ass_path.write_text(header, encoding="utf-8")
        return 0

    # Short VO (<=8 words): ONE chunk so the full line is always visible
    if len(cleaned) <= 8:
        chunks: list[list[dict[str, Any]]] = [cleaned]
    else:
        chunks = []
        cur: list[dict[str, Any]] = []
        for w in cleaned:
            cur.append(w)
            if len(cur) >= 5 or str(w["word"]).endswith((".", "!", "?")):
                chunks.append(cur)
                cur = []
        if cur:
            chunks.append(cur)

    # One Dialogue per chunk (full phrase visible). Non-overlapping windows.
    # Karaoke \k highlights words in order without hiding prior text.
    events: list[tuple[float, str]] = []
    for ci, chunk in enumerate(chunks):
        start_t = float(chunk[0]["start"])
        end_t = float(chunk[-1]["end"]) + 0.55
        if ci + 1 < len(chunks):
            end_t = min(end_t, float(chunks[ci + 1][0]["start"]) - 0.02)
        if end_t <= start_t:
            end_t = start_t + max(0.6, 0.22 * len(chunk))
        parts = []
        for i, w in enumerate(chunk):
            w_start = float(w["start"])
            w_end = float(w["end"])
            if i + 1 < len(chunk):
                w_end = max(w_end, float(chunk[i + 1]["start"]))
            dur_cs = max(12, int(round((w_end - w_start) * 100)))
            tok = _ass_escape(w["word"])
            parts.append(r"{\k" + str(dur_cs) + r"\c&" + COLOR_ACTIVE + r"&}" + tok)
        line = " ".join(parts)
        events.append(
            (start_t, f"Dialogue: 0,{_ts(start_t)},{_ts(end_t)},Word,,0,0,0,,{line}")
        )

    events.sort(key=lambda e: e[0])
    ass_path.write_text(header + "\n".join(e[1] for e in events) + "\n", encoding="utf-8")
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
    base_filter: str | None = None,
) -> str:
    z = max(1.0, float(zoom))
    speed = max(0.5, min(2.0, float(speed)))
    if base_filter:
        # Caller already produced [base] (e.g. face-cam split layout)
        parts = [base_filter]
    else:
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
    source_meta: dict[str, Any] | None = None,
) -> Path | None:
    clips = cfg["clips"]
    width = int(clips.get("width", 1080))
    height = int(clips.get("height", 1920))
    lufs = float(clips.get("loudness_lufs", -14))
    hook_dur = float(clips.get("hook_duration", 2.0))
    out_dir = Path(clips["output_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)

    segment = _cap_segment(segment, cfg)
    # Motion / silence gate on the source window (skip frozen trailer / dead air)
    try:
        from .window_gate import window_ok
        meta0 = source_meta or {}
        gbox = None
        gl = (meta0.get("gemini_layout") or {})
        if isinstance(gl.get("game_bbox"), (list, tuple)) and len(gl["game_bbox"]) == 4:
            gbox = tuple(float(x) for x in gl["game_bbox"])
        niche_for_gate = str(cfg.get("niche_id") or (cfg.get("niche") or {}).get("id") or "gta6")
        pre = bool((source_meta or {}).get("preconfirmed"))
        gate = window_ok(
            source, segment.start, segment.end, game_box=gbox,
            niche_id=niche_for_gate,
            min_speech_coverage=0.50, require_speech=not pre,
        )
        if not gate.get("ok"):
            # Soft: preconfirmed upstream pick, or speech+motion close enough
            if pre or (gate.get("speech_coverage", 0) >= 0.25 and gate.get("motion", 0) >= 6):
                print(f"[edit] window soft-pass: {gate.get('reasons')} pre={pre}")
            else:
                print(f"[edit] window rejected: {gate.get('reasons')}")
                return None
    except Exception as e:
        print(f"[edit] window gate skip: {e}")
    cs = _copyright_cfg(cfg)
    safe_on = bool(cs.get("enabled", True))
    # NEVER change playback tempo (setpts/atempo mismatch = chipmunk/jitter).
    # Copyright-safe zoom still OK; speed stays 1.0.
    speed = 1.0
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

    niche_id = str(cfg.get("niche_id") or (cfg.get("niche") or {}).get("id") or "gta6")
    words: list[dict[str, Any]] = []
    if use_whisper:
        words = get_word_timestamps(
            source, segment.start, segment.end, model_name=model_name, niche_id=niche_id
        )
        if not words and not vo_enabled:
            print("[edit] no dialogue words after filters — retrying tiny")
            words = get_word_timestamps(
                source, segment.start, segment.end, model_name="tiny", niche_id=niche_id
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
    meta = source_meta or {}
    from .entities import extract_entities, event_summary, grounded_vocab
    es = extract_entities(
        title=title,
        channel=str(meta.get("channel") or ""),
        description=str(meta.get("description") or ""),
        tags=list(meta.get("tags") or []),
        transcript=snippet,
        niche_id=niche_id,
    )
    # Restrict hook vocab to grounded terms only
    cfg = dict(cfg)
    hooks_cfg = dict(cfg.get("hooks") or {})
    hooks_cfg["vocab"] = grounded_vocab(es, niche_id)
    cfg["hooks"] = hooks_cfg
    ev = event_summary(es, title, snippet, segment.reason)
    print(f"[ground] franchises={es.franchises} streamers={es.streamers[:2]} "
          f"event={ev!r} conf={es.confidence:.2f}")
    ctx = HookContext(
        title=title,
        reason=segment.reason,
        transcript_snippet=snippet,
        boost_hits=boost,
        duration=segment.end - segment.start,
        niche_id=niche_id,
        channel=str(meta.get("channel") or ""),
        description=str(meta.get("description") or ""),
        tags=list(meta.get("tags") or []),
        entities=es,
        event=ev,
        confidence=es.confidence,
    )
    hook_res = generate_hook(ctx, cfg)
    print(
        f"[hooks] style={hook_res.style} via={hook_res.via} "
        f"score={hook_res.score:.2f} pool={hook_res.pool_size}: {hook_res.text}"
    )
    if hook_res.candidates:
        print("[hooks] pool sample: " + " | ".join(hook_res.candidates[:5]))

    # --- Gemini scene understanding + writing (primary); templates if no key ---
    from .scene_vlm import describe_moment, self_review, confirm_reaction_moment
    from .write_llm import write_header_vo
    from .hooks import HookResult
    
    # Layout: Gemini bboxes > detected face split > fill-crop (no letterbox)
    from .face_layout import (
        detect_at_segment, split_filter_complex, layout_from_gemini, fill_crop_filter,
    )
    face_layout = None
    split_base = None
    is_reactionish = niche_id in {"gta6", "reactions"} or "react" in (title or "").lower()
    gem_layout = meta.get("gemini_layout") or {}
    if gem_layout.get("face_bbox") and gem_layout.get("game_bbox"):
        face_layout = layout_from_gemini(gem_layout)
        if face_layout.face and face_layout.game:
            split_base = split_filter_complex(
                face_layout, width=width, height=height, speed=speed, out_label="base",
                source=source, refine_t=(segment.start + segment.end) / 2,
            )
            print(f"[layout] gemini split face={face_layout.face} game={face_layout.game}")
        else:
            split_base = fill_crop_filter(width=width, height=height, speed=speed, zoom=1.15, out_label="base")
            print("[layout] gemini bboxes invalid — fill crop")
    elif gem_layout.get("layout") == "multi_cam" or gem_layout.get("use_fill_crop"):
        # Zoom toward face center if we have it
        cx = cy = 0.5
        fb = gem_layout.get("face_bbox")
        if isinstance(fb, (list, tuple)) and len(fb) == 4:
            cx = float(fb[0]) + float(fb[2]) / 2
            cy = float(fb[1]) + float(fb[3]) / 2
        split_base = fill_crop_filter(width=width, height=height, speed=speed, zoom=1.2, out_label="base", cx=cx, cy=cy)
        print(f"[layout] multi_cam fill-crop center=({cx:.2f},{cy:.2f})")
    elif gem_layout.get("face_bbox") or gem_layout.get("game_bbox"):
        face_layout = layout_from_gemini(gem_layout)
        if face_layout.face and face_layout.game:
            split_base = split_filter_complex(
                face_layout, width=width, height=height, speed=speed, out_label="base",
                source=source, refine_t=(segment.start + segment.end) / 2,
            )
            print(f"[layout] gemini split face={face_layout.face} game={face_layout.game}")
        else:
            split_base = fill_crop_filter(width=width, height=height, speed=speed, zoom=1.12, out_label="base")
            print("[layout] gemini incomplete — fill crop")
    elif is_reactionish:
        try:
            face_layout = detect_at_segment(source, segment.start, segment.end)
            if face_layout and face_layout.face:
                split_base = split_filter_complex(
                    face_layout, width=width, height=height, speed=speed, out_label="base",
                    source=source, refine_t=(segment.start + segment.end) / 2,
                )
                print(f"[layout] face_cam={face_layout.face} chat={face_layout.chat_side} split={bool(split_base)}")
            else:
                split_base = fill_crop_filter(width=width, height=height, speed=speed, zoom=1.12, out_label="base")
                print("[layout] no face-cam — fill crop")
        except Exception as e:
            print(f"[layout] detect failed: {e}")
            split_base = fill_crop_filter(width=width, height=height, speed=speed, zoom=1.12, out_label="base")
    else:
        # anime / gameplay: full-height crop-to-fill, never letterbox band
        split_base = fill_crop_filter(width=width, height=height, speed=speed, zoom=1.14, out_label="base")
        print("[layout] fill-crop for non-reaction niche")

    # Gate: require trailer + strong reaction for reaction niches
    reaction_meta = {"ok": True, "emotion": str(meta.get("gate_emotion") or ""), "desc": str(meta.get("gate_desc") or "")}
    preconfirmed = bool(meta.get("preconfirmed") or "gemini" in (segment.reason or ""))
    if is_reactionish and not preconfirmed:
        try:
            reaction_meta = confirm_reaction_moment(
                source, segment.start, segment.end, cfg=cfg, title=title, require_trailer=True,
            )
            print(f"[gate] ok={reaction_meta.get('ok')} emo={reaction_meta.get('emotion')} "
                  f"trailer={reaction_meta.get('trailer_visible')} reasons={reaction_meta.get('reasons')}")
            if not reaction_meta.get("ok"):
                print("[gate] FAIL — discarding (no strong on-screen reaction / trailer)")
                return None
        except Exception as e:
            print(f"[gate] error: {e}")
    elif is_reactionish and preconfirmed:
        print(f"[gate] trust preconfirmed emo={reaction_meta.get('emotion')!r}")
        # Still refresh emotion if empty
        if not reaction_meta.get("emotion"):
            try:
                reaction_meta = confirm_reaction_moment(
                    source, segment.start, segment.end, cfg=cfg, title=title, require_trailer=True,
                )
            except Exception:
                reaction_meta = {"ok": True, "emotion": "shocked", "desc": ""}

    vlm_desc = ""
    llm_vo = ""
    llm_say = ""
    try:
        t0 = __import__("time").time()
        vlm_desc = describe_moment(
            source, segment.start, segment.end, n_frames=4,
            transcript=snippet, title=title,
            channel=str(meta.get("channel") or ""), cfg=cfg,
            extra_prompt=(
                "Focus on what is visibly happening IN THESE FRAMES only. "
                "Name the reactor emotion if a face-cam is visible. "
                "Mention whether trailer/game footage is on screen."
            ),
        )
        if reaction_meta.get("desc") and not vlm_desc:
            vlm_desc = reaction_meta["desc"]
        print(f"[vlm] {__import__('time').time()-t0:.1f}s: {vlm_desc[:220]}")
    except Exception as e:
        print(f"[vlm] describe failed: {e}")
    try:
        written = write_header_vo(
            niche_id=niche_id,
            title=title,
            channel=str(meta.get("channel") or ""),
            vlm_desc=vlm_desc or reaction_meta.get("desc") or "",
            transcript=snippet,
            event=ev,
            entities=es,
            cfg=cfg,
            emotion=str(reaction_meta.get("emotion") or ""),
        )
        if written.get("header"):
            hook_res = HookResult(
                text=written["header"],
                style="gemini",
                via=written.get("via") or "gemini",
                score=9.0,
                pool_size=1,
                candidates=[written["header"]],
            )
            llm_vo = written.get("vo") or ""
            llm_say = written.get("say") or ""
            print(f"[llm] header={hook_res.text!r} vo={llm_vo!r} via={written.get('via')} say={llm_say!r}")
    except Exception as e:
        print(f"[llm] write failed: {e}")
    ctx.header_text = hook_res.text
    ctx.vlm_desc = vlm_desc
    hook_display = _fit_header(hook_res.text, 36)
    hook = _escape_drawtext(hook_display)
    skip_header = bool((source_meta or {}).get("skip_header"))
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
        # VO is PREPEND-ONLY (1.5–2.5s over frozen first frame). Never over streamer speech.
        # Prefer LLM punchline as the intro line; else speak the hook header.
        if intro_cfg_on and (llm_vo or hook_res.text):
            intro_wav = td_path / "intro.wav"
            intro_line = (llm_vo or hook_res.text or "").strip()
            # Keep intro short
            words_l = intro_line.split()
            if len(words_l) > 12:
                intro_line = " ".join(words_l[:12])
            intro_result = make_intro_vo(intro_line, cfg, intro_wav)
            if intro_result and intro_result.wav_path and intro_result.wav_path.is_file():
                try:
                    import wave
                    with wave.open(str(intro_result.wav_path), "rb") as w:
                        raw_d = w.getnframes() / float(w.getframerate() or 1)
                    intro_dur = min(2.5, max(1.5, raw_d))
                except Exception:
                    intro_dur = min(2.5, max(1.5, intro_dur))
                print(f"[intro-vo] prepend {intro_dur:.2f}s: {intro_line!r}")
            else:
                intro_result = None
                intro_dur = 0.0
        else:
            intro_dur = 0.0

        # Streamer captions only (shifted by intro prepend). No VO karaoke over speech.
        shift = intro_dur if (intro_result and intro_result.wav_path) else 0.0
        vo_result = None  # body commentary VO disabled — intro handles the spoken hook
        caption_words = [
            {
                "word": w["word"],
                "start": float(w["start"]) + shift,
                "end": float(w["end"]) + shift,
            }
            for w in words
            if float(w.get("end", 0)) > 0.05 and not str(w.get("word", "")).startswith("[")
        ]
        print(f"[edit] streamer captions: {len(caption_words)} words (shift={shift:.2f}s)")

        ass_path = td_path / "captions.ass"
        # First encode without burned captions; final-audio whisper pass burns once
        # so we never double-layer VO karaoke over streamer speech.
        words_for_burn = []
        n_events = _write_ass_captions(
            words_for_burn, ass_path, width, height, font_name=font_name
        )
        print(f"[edit] ASS events (pre-final): {n_events}; will whisper final audio")
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

        # Animated header: bigger for intro_dur then settle — keep visible most of the clip
        clip_len = max(0.5, float(segment.end - segment.start) / max(speed, 0.5))
        hook_dur = max(hook_dur, intro_dur if intro_result and intro_result.wav_path else hook_dur)
        hook_dur = max(hook_dur, min(clip_len - 0.3, max(5.0, clip_len * 0.85)))

        atempo = speed if abs(speed - 1.0) > 0.001 else 1.0
        src_af = f"atempo={atempo}" if abs(atempo - 1.0) > 0.001 else "anull"
        vo_vol = float(vo_cfg.get("volume", 1.0))
        # Body VO / sidechain ducking REMOVED — streamer+trailer audio stays full level.
        # Intro VO (if any) is prepended after the body render (frozen first frame).
        prepend_vo = None
        if intro_result and intro_result.wav_path and intro_result.wav_path.is_file() and intro_dur >= 1.4:
            prepend_vo = intro_result.wav_path
            # Clamp intro_dur to wav length
            try:
                import wave as _wave
                with _wave.open(str(prepend_vo), "rb") as _w:
                    wav_d = _w.getnframes() / float(_w.getframerate() or 1)
                intro_dur = min(2.5, max(1.5, min(intro_dur, wav_d + 0.15)))
            except Exception:
                intro_dur = min(2.5, max(1.5, intro_dur))
        else:
            intro_dur = 0.0
            prepend_vo = None

        def run_ffmpeg(use_ass: bool, use_dt_fallback: bool = False) -> bool:
            dt = _drawtext_caption_fallback(words_for_burn if burn_captions else [], font_opt) if use_dt_fallback else ""
            nlen = len(hook_display)
            if nlen > 32:
                header_fs_big, header_fs = 44, 38
            elif nlen > 24:
                header_fs_big, header_fs = 50, 42
            elif nlen > 18:
                header_fs_big, header_fs = 56, 48
            else:
                header_fs_big, header_fs = 68, 56
            # Header visible during body (intro card carries its own title)
            anim = (
                f"drawtext=text='{hook}'{font_opt}:fontsize={header_fs}:fontcolor=yellow:"
                f"borderw=5:bordercolor=black:x=(w-text_w)/2:y=h*0.15:"
                f"enable='lte(t\\,{hook_dur:.3f})'"
            )
            if skip_header:
                anim = ""
            vchain = _video_chain(
                width=width,
                height=height,
                ass_filter=ass_filter if use_ass else None,
                hook=" ",
                font_opt=font_opt,
                hook_dur=0.01,
                speed=speed,
                zoom=1.0 if split_base else zoom,
                commentary=commentary_text,
                commentary_on=commentary_on and bool(commentary_text),
                drawtext_extra=(anim + (("," + dt) if dt else "")),
                out_label="vout",
                base_filter=split_base,
            )
            # FULL-LEVEL source audio (streamer + trailer). No ducking, no VO mix.
            achain = (
                f"[0:a]{src_af},aresample=48000,aformat=sample_rates=48000:channel_layouts=stereo,"
                f"loudnorm=I={lufs}:TP=-1.5:LRA=11[aout]"
                if src_af != "anull"
                else f"[0:a]aresample=48000,aformat=sample_rates=48000:channel_layouts=stereo,"
                     f"loudnorm=I={lufs}:TP=-1.5:LRA=11[aout]"
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
                "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "192k",
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

        # Assert body duration == window length ±0.1s
        try:
            import json as _j
            _pr = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "json", str(out_path)],
                capture_output=True, text=True, check=False,
            )
            body_dur = float((_j.loads(_pr.stdout).get("format") or {}).get("duration") or 0)
            win_len = float(segment.end - segment.start) / max(speed, 0.5)
            if abs(body_dur - win_len) > 0.1:
                print(f"[edit] WARN body dur {body_dur:.3f}s vs window {win_len:.3f}s")
            else:
                print(f"[edit] body dur OK {body_dur:.3f}s ~= window {win_len:.3f}s")
        except Exception as e:
            print(f"[edit] dur assert skip: {e}")

        # PREPEND short VO intro over frozen first frame (never over streamer speech).
        # Audio hardening: kokoro emits 24kHz mono — ALWAYS resample to 48kHz stereo WAV
        # first, then concat with aresample. Never rely on aformat alone / never asetrate.
        if prepend_vo is not None and intro_dur >= 1.4 and out_path.is_file():
            try:
                intro_mp4 = td_path / "vo_intro.mp4"
                body_norm = td_path / "body_norm.mp4"
                vo48 = td_path / "intro_48k.wav"
                # Explicit resample VO → 48k stereo (prevents 24k-as-48k chipmunk)
                pr_rs = subprocess.run(
                    [
                        "ffmpeg", "-y", "-i", str(prepend_vo),
                        "-af", "aresample=48000:async=1,aformat=sample_rates=48000:channel_layouts=stereo",
                        "-ar", "48000", "-ac", "2", str(vo48),
                    ],
                    capture_output=True, text=True, check=False,
                )
                if pr_rs.returncode != 0 or not vo48.is_file():
                    print("[edit] VO resample failed:", (pr_rs.stderr or "")[-300:])
                    vo48 = prepend_vo
                else:
                    # Assert resampled duration ≈ original (±5%)
                    import wave as _w2
                    with _w2.open(str(prepend_vo), "rb") as _a:
                        d0 = _a.getnframes() / float(_a.getframerate() or 1)
                    with _w2.open(str(vo48), "rb") as _b:
                        d1 = _b.getnframes() / float(_b.getframerate() or 1)
                        sr1 = _b.getframerate()
                    if sr1 != 48000 or abs(d1 - d0) > 0.05 * max(d0, 0.1):
                        print(f"[edit] WARN VO resample sr={sr1} dur {d0:.3f}->{d1:.3f}")
                    intro_dur = min(2.5, max(1.5, d1 + 0.05))
                    print(f"[edit] VO resampled 48k stereo dur={d1:.3f}s")

                frame0 = td_path / "frame0.png"
                subprocess.run(
                    ["ffmpeg", "-y", "-i", str(out_path), "-frames:v", "1", "-q:v", "2", str(frame0)],
                    capture_output=True, check=False,
                )
                hook_esc = _escape_drawtext(hook_display)
                intro_vf = (
                    f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                    f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
                    f"fps=30,format=yuv420p,setsar=1,"
                    f"drawtext=text='{hook_esc}'{font_opt}:fontsize=56:fontcolor=yellow:"
                    f"borderw=5:bordercolor=black:x=(w-text_w)/2:y=h*0.14"
                )
                pr_i = subprocess.run(
                    [
                        "ffmpeg", "-y",
                        "-loop", "1", "-framerate", "30", "-t", f"{intro_dur:.3f}", "-i", str(frame0),
                        "-i", str(vo48),
                        "-filter_complex",
                        f"[0:v]{intro_vf}[v];"
                        f"[1:a]aresample=48000,aformat=sample_rates=48000:channel_layouts=stereo,"
                        f"volume={vo_vol},apad=whole_dur={intro_dur:.3f},atrim=0:{intro_dur:.3f},"
                        f"asetpts=PTS-STARTPTS[a]",
                        "-map", "[v]", "-map", "[a]",
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                        "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "192k",
                        "-pix_fmt", "yuv420p", "-r", "30",
                        "-shortest", str(intro_mp4),
                    ],
                    capture_output=True, text=True, check=False,
                )
                # Body: force 30fps + 48kHz with aresample (never asetrate)
                pr_b = subprocess.run(
                    [
                        "ffmpeg", "-y", "-i", str(out_path),
                        "-filter_complex",
                        f"[0:v]fps=30,scale={width}:{height},setsar=1,format=yuv420p,setpts=PTS-STARTPTS[v];"
                        f"[0:a]aresample=48000,aformat=sample_rates=48000:channel_layouts=stereo,"
                        f"asetpts=PTS-STARTPTS[a]",
                        "-map", "[v]", "-map", "[a]",
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                        "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "192k",
                        "-r", "30", str(body_norm),
                    ],
                    capture_output=True, text=True, check=False,
                )
                final_tmp = td_path / "with_intro.mp4"
                if intro_mp4.is_file() and body_norm.is_file() and pr_i.returncode == 0 and pr_b.returncode == 0:
                    pr_c = subprocess.run(
                        [
                            "ffmpeg", "-y",
                            "-i", str(intro_mp4), "-i", str(body_norm),
                            "-filter_complex",
                            "[0:v][0:a][1:v][1:a]concat=n=2:v=1:a=1[v][a]",
                            "-map", "[v]", "-map", "[a]",
                            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                            "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "192k",
                            "-pix_fmt", "yuv420p", "-r", "30",
                            "-movflags", "+faststart",
                            str(final_tmp),
                        ],
                        capture_output=True, text=True, check=False,
                    )
                    if pr_c.returncode == 0 and final_tmp.is_file():
                        out_path.write_bytes(final_tmp.read_bytes())
                        print(f"[edit] prepended VO intro {intro_dur:.2f}s (48k resampled, frozen frame)")
                    else:
                        print("[edit] intro concat failed:", (pr_c.stderr or "")[-400:])
                else:
                    print("[edit] intro build failed:", (pr_i.stderr or "")[-300:], (pr_b.stderr or "")[-200:])
            except Exception as e:
                print(f"[edit] VO prepend skip: {e}")

        # Pass 2: burn streamer-word captions (prefer source whisper shifted by intro)
        if burn_captions and out_path.is_file():
            try:
                import subprocess as _sp
                _dur = 12.0
                try:
                    _pr = _sp.run(
                        ["ffprobe","-v","error","-show_entries","format=duration",
                         "-of","csv=p=0",str(out_path)],
                        capture_output=True, text=True, check=False,
                    )
                    _dur = max(1.0, float((_pr.stdout or "12").strip()))
                except Exception:
                    pass
                # Prefer source-window streamer words (shifted). Only replace with final
                # whisper if it yields MORE confident streamer-like tokens (not trailer OCR).
                final_words = list(caption_words) if caption_words else []
                whispered = get_word_timestamps(
                    out_path, float(intro_dur), _dur, model_name=model_name, niche_id=niche_id
                )
                if len(whispered) < 3 and model_name != "tiny":
                    whispered = get_word_timestamps(
                        out_path, float(intro_dur), _dur,
                        model_name="base" if model_name == "tiny" else "tiny",
                        niche_id=niche_id,
                    )
                def _streamer_like(ws):
                    text = " ".join(str(w.get("word","")) for w in ws).lower()
                    if re.search(r"(grand theft auto|gta\s*6).{0,40}(coming|november|pre-?order)", text):
                        if not re.search(r"\b(i|i'm|bro|chat|dude|oh my|fuck|wait)\b", text):
                            return False
                    return len(ws) >= 4
                if _streamer_like(whispered) and len(whispered) > len(final_words) + 2:
                    final_words = whispered
                elif not final_words and _streamer_like(whispered):
                    final_words = whispered
                # Drop captions entirely if nothing confident
                if not _streamer_like(final_words) and len(final_words) < 6:
                    print("[edit] dropping low-confidence captions")
                    final_words = []
                if final_words:
                    ass2 = td_path / "captions_final.ass"
                    n2 = _write_ass_captions(final_words, ass2, width, height, font_name=font_name)
                    print(f"[edit] final-audio whisper captions: {n2} events / {len(final_words)} words")
                    tmp_out = td_path / "reburn.mp4"
                    ass_esc = str(ass2).replace("\\", "/").replace(":", chr(92) + ":")
                    vf = f"ass='{ass_esc}'"
                    # Keep existing burned captions? Replace by stripping is hard —
                    # overlay fresh ASS (karaoke style) on top of current frame.
                    pr2 = subprocess.run(
                        [
                            "ffmpeg", "-y", "-i", str(out_path),
                            "-vf", vf,
                            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                            "-c:a", "copy", "-pix_fmt", "yuv420p",
                            "-movflags", "+faststart",
                            str(tmp_out),
                        ],
                        capture_output=True, text=True, check=False,
                    )
                    if pr2.returncode == 0 and tmp_out.is_file():
                        out_path.write_bytes(tmp_out.read_bytes())
                        caption_words = final_words
                        out_path.with_suffix(".ass").write_text(
                            ass2.read_text(encoding="utf-8"), encoding="utf-8"
                        )
                    else:
                        print("[edit] final caption reburn failed:", (pr2.stderr or "")[-400:])
                else:
                    print("[edit] final-audio whisper returned no words — keeping prior captions")
            except Exception as e:
                print(f"[edit] final-audio caption pass skip: {e}")

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
                "event": ev,
                "franchises": list(es.franchises),
                "confidence": float(es.confidence),
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
    # Also stash VLM description for reporting
    try:
        meta_p = out_path.with_suffix(".scene.json")
        meta_p.write_text(
            __import__("json").dumps({
                "vlm_desc": getattr(ctx, "vlm_desc", "") or vlm_desc,
                "header": hook_res.text,
                "vo": (vo_result.text if vo_result else ""),
                "segment": {"start": segment.start, "end": segment.end, "reason": segment.reason},
            }, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass

    # Final VIDEO gate — upload rendered clip; require score >= 7
    try:
        from .video_vlm import final_gate_video
        review = final_gate_video(
            out_path,
            header=hook_res.text,
            vo=(vo_result.text if vo_result else ""),
            niche_id=niche_id,
            title=title,
            cfg=cfg,
            min_score=8.0,
        )
        print(f"[review] score={review.get('score')} ok={review.get('ok')} "
              f"reasons={review.get('reasons')} desc={str(review.get('desc',''))[:160]}")
        rev_path = out_path.with_suffix(".review.json")
        rev_path.write_text(__import__("json").dumps(review, indent=2), encoding="utf-8")
        if not review.get("ok"):
            print("[review] FAIL — discarding clip")
            try:
                out_path.unlink(missing_ok=True)
            except Exception:
                pass
            return None
    except Exception as e:
        print(f"[review] error (discarding): {e}")
        try:
            out_path.unlink(missing_ok=True)
        except Exception:
            pass
        return None

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
