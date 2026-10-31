"""Native Gemini VIDEO understanding: moment pick, layout bboxes, final gate."""
from __future__ import annotations

import json
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any


def _ffprobe_duration(path: Path) -> float:
    pr = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, check=False,
    )
    try:
        return float(json.loads(pr.stdout)["format"]["duration"])
    except Exception:
        return 0.0


def make_proxy(
    source: Path,
    out: Path,
    *,
    start: float = 0.0,
    duration: float | None = None,
    height: int = 360,
    fps: float = 1.0,
    audio: bool = True,
) -> Path | None:
    """Low-res sparse-fps proxy for free-tier video Gemini calls."""
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y"]
    if start > 0:
        cmd += ["-ss", f"{start:.2f}"]
    cmd += ["-i", str(source)]
    if duration is not None:
        cmd += ["-t", f"{duration:.2f}"]
    vf = f"scale=-2:{height},fps={fps}"
    cmd += ["-vf", vf, "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30"]
    if audio:
        cmd += ["-c:a", "aac", "-b:a", "48k", "-ac", "1", "-ar", "16000"]
    else:
        cmd += ["-an"]
    cmd += ["-movflags", "+faststart", str(out)]
    pr = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if pr.returncode != 0 or not out.is_file() or out.stat().st_size < 1000:
        print(f"[proxy] fail: {(pr.stderr or '')[-200:]}")
        return None
    return out


def _parse_json(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if not raw:
        return {}
    # strip fences
    m = re.search(r"\{[\s\S]*\}", raw)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception:
        # trailing commas / soft fix
        soft = re.sub(r",\s*}", "}", m.group(0))
        soft = re.sub(r",\s*]", "]", soft)
        try:
            return json.loads(soft)
        except Exception:
            return {}


def niche_moment_prompt(niche_id: str, title: str, channel: str, window_start: float) -> str:
    niche = (niche_id or "").lower()
    if niche in {"gta6", "reactions"}:
        req = (
            "Find the BEST continuous 10-20 second segment where BOTH are true at the same time: "
            "(1) GTA 6 / GTA VI trailer or clear GTA gameplay/cinematic is VISIBLY playing on screen "
            "(not a static menu, release-date card, logo, or talking-head only), AND "
            "(2) the primary reactor's face-cam shows a STRONG visible reaction "
            "(laughing hard, jaw-drop shock, yelling, hands-on-face) — NOT calm chatting."
        )
    elif niche == "anime":
        req = (
            "Find the BEST continuous 10-18 second segment of peak fight/action "
            "(clear character motion, impacts, clash) — avoid black frames, title cards, and stills."
        )
    else:
        req = "Find the BEST continuous 10-18 second highlight for a vertical Shorts clip."
    return (
        f"Title: {title[:140]}\nChannel: {channel[:80]}\n"
        f"This proxy starts at source time {window_start:.1f}s (t=0 in the file = {window_start:.1f}s absolute).\n"
        f"{req}\n"
        "Reply ONLY JSON with keys:\n"
        "{\n"
        '  "ok": true/false,\n'
        '  "start": <seconds relative to THIS proxy file>,\n'
        '  "end": <seconds relative to THIS proxy file>,\n'
        '  "abs_start": <absolute source seconds>,\n'
        '  "abs_end": <absolute source seconds>,\n'
        '  "who": "<primary person/character on screen>",\n'
        '  "layout": "single_facecam"|"multi_cam"|"gameplay"|"other",\n'
        '  "face_bbox": [x,y,w,h] fractions 0-1 or null,\n'
        '  "game_bbox": [x,y,w,h] fractions 0-1 or null,\n'
        '  "trailer_or_game_visible": true/false,\n'
        '  "reaction_strong": true/false,\n'
        '  "desc": "<one factual sentence>",\n'
        '  "confidence": 0.0-1.0\n'
        "}\n"
        "Set ok=false if nothing qualifies. Prefer single clean face-cam layouts over multi-cam mashups."
    )


def pick_moment_from_video(
    source: Path,
    *,
    niche_id: str,
    title: str = "",
    channel: str = "",
    window_start: float = 0.0,
    window_dur: float = 90.0,
    cfg: dict[str, Any] | None = None,
    min_confidence: float = 0.55,
) -> dict[str, Any]:
    """Upload a proxy window and ask Gemini for the best segment + layout."""
    from .gemini_client import generate_video, has_gemini
    if not has_gemini(cfg):
        return {"ok": False, "reason": "no_key"}
    dur = _ffprobe_duration(source)
    if dur <= 0:
        return {"ok": False, "reason": "bad_source"}
    ws = max(0.0, float(window_start))
    wd = min(float(window_dur), max(5.0, dur - ws))
    with tempfile.TemporaryDirectory() as td:
        proxy = Path(td) / "proxy.mp4"
        if not make_proxy(source, proxy, start=ws, duration=wd, height=360, fps=1.0, audio=True):
            return {"ok": False, "reason": "proxy_fail"}
        prompt = niche_moment_prompt(niche_id, title, channel, ws)
        try:
            raw = generate_video(prompt=prompt, video_path=proxy, cfg=cfg, max_output_tokens=500)
        except Exception as e:
            print(f"[video_vlm] pick failed: {e}")
            return {"ok": False, "reason": str(e)[:160]}
    data = _parse_json(raw)
    data["raw"] = (raw or "")[:600]
    if not data.get("ok"):
        data["ok"] = False
        return data
    # Normalize absolute times
    rel_s = float(data.get("start") or 0)
    rel_e = float(data.get("end") or (rel_s + 12))
    if rel_e <= rel_s:
        rel_e = rel_s + 12
    abs_s = float(data.get("abs_start") or (ws + rel_s))
    abs_e = float(data.get("abs_end") or (ws + rel_e))
    # Prefer relative+window when abs looks wrong
    if abs_s < ws - 1 or abs_s > ws + wd + 1:
        abs_s, abs_e = ws + rel_s, ws + rel_e
    abs_e = min(dur, max(abs_s + 8.0, abs_e))
    abs_s = max(0.0, min(abs_s, dur - 8.0))
    if abs_e - abs_s > 25:
        abs_e = abs_s + 18
    if abs_e - abs_s < 8:
        abs_e = min(dur, abs_s + 12)
    conf = float(data.get("confidence") or 0)
    layout = str(data.get("layout") or "other")
    niche = (niche_id or "").lower()
    ok = conf >= min_confidence and bool(data.get("ok"))
    if niche in {"gta6", "reactions"}:
        ok = ok and bool(data.get("trailer_or_game_visible")) and bool(data.get("reaction_strong"))
        # multi_cam is still a usable MOMENT — render path will fill-crop instead of split
        if layout == "multi_cam":
            data["reject"] = None
            data["use_fill_crop"] = True
    data.update({
        "ok": ok,
        "abs_start": round(abs_s, 2),
        "abs_end": round(abs_e, 2),
        "confidence": conf,
        "window_start": ws,
    })
    return data


def scan_best_moment(
    source: Path,
    *,
    niche_id: str,
    title: str = "",
    channel: str = "",
    heatmap: list | None = None,
    cfg: dict[str, Any] | None = None,
    max_windows: int = 3,
) -> dict[str, Any]:
    """Try a few windows (heatmap peaks / mid) and return the best ok moment."""
    dur = _ffprobe_duration(source)
    centers: list[float] = []
    for h in heatmap or []:
        try:
            if isinstance(h, dict):
                c = float(h.get("start_time") or h.get("start") or 0)
                inten = float(h.get("value") or h.get("intensity") or 0.5)
            else:
                c = float(h[0]); inten = float(h[1]) if len(h) > 1 else 0.5
            centers.append((inten, c))
        except Exception:
            pass
    centers_s = [c for _, c in sorted(centers, reverse=True)]
    # Uniform fallbacks
    if dur > 40:
        for frac in (0.25, 0.45, 0.65):
            centers_s.append(dur * frac)
    else:
        centers_s.append(max(0.0, dur * 0.2))
    picked: list[float] = []
    for c in centers_s:
        if any(abs(c - p) < 40 for p in picked):
            continue
        if c < 10 or c > dur - 15:
            continue
        picked.append(c)
        if len(picked) >= max_windows:
            break
    if not picked:
        picked = [max(0.0, min(dur * 0.3, max(0.0, dur - 60)))]

    best: dict[str, Any] = {"ok": False, "confidence": 0}
    for c in picked:
        ws = max(0.0, c - 45)
        wd = min(90.0, dur - ws)
        print(f"[video_vlm] scan window @{ws:.1f}+{wd:.1f}s")
        hit = pick_moment_from_video(
            source, niche_id=niche_id, title=title, channel=channel,
            window_start=ws, window_dur=wd, cfg=cfg,
        )
        print(f"[video_vlm] -> ok={hit.get('ok')} conf={hit.get('confidence')} "
              f"layout={hit.get('layout')} {hit.get('abs_start')}-{hit.get('abs_end')} "
              f"desc={str(hit.get('desc',''))[:100]}")
        if hit.get("ok") and float(hit.get("confidence") or 0) >= float(best.get("confidence") or 0):
            best = hit
            if float(hit.get("confidence") or 0) >= 0.8:
                break
    return best


FINAL_GATE_PROMPT = """You are a strict Shorts QA reviewer. Watch the FULL rendered vertical clip WITH AUDIO.
Score 0-10 and answer carefully. Reply ONLY JSON:
{
  "score": <0-10>,
  "header_matches": true/false,
  "vo_matches_visuals": true/false,
  "trailer_or_subject_visible": true/false,
  "reaction_real": true/false_or_null,
  "captions_synced": true/false,
  "layout_clean": true/false,
  "stray_wrong_audio": true/false,
  "wrong_footage": true/false,
  "who": "<who is actually on screen>",
  "reasons": ["short reason", ...],
  "desc": "<one sentence of what the clip shows>"
}
Rules for score:
- Deduct heavily if header names someone not on screen, VO invents actions, wrong game/show footage,
  multi-cam illegible layout, long silent gaps, jumbled captions, or static cards instead of real action.
- For GTA/reaction clips: require trailer/gameplay visible AND a real visible reaction.
- For anime: require clear fight/action filling the frame (not tiny letterboxed).
Pass threshold is score >= 7.
"""


def final_gate_video(
    rendered: Path,
    *,
    header: str = "",
    vo: str = "",
    niche_id: str = "",
    title: str = "",
    cfg: dict[str, Any] | None = None,
    min_score: float = 7.0,
) -> dict[str, Any]:
    """Upload rendered clip proxy to Gemini for end-to-end QA."""
    from .gemini_client import generate_video, has_gemini
    if not has_gemini(cfg):
        return {"ok": True, "score": 7, "skipped": "no_key", "reasons": []}
    if not rendered.is_file():
        return {"ok": False, "score": 0, "reasons": ["missing_file"]}
    dur = _ffprobe_duration(rendered)
    with tempfile.TemporaryDirectory() as td:
        proxy = Path(td) / "rendered_proxy.mp4"
        # Keep audio; 360p 2fps is enough for QA and cheaper
        if not make_proxy(rendered, proxy, start=0, duration=min(dur, 45), height=360, fps=2.0, audio=True):
            return {"ok": False, "score": 0, "reasons": ["proxy_fail"]}
        prompt = (
            FINAL_GATE_PROMPT
            + f"\nNiche: {niche_id}\nSource title: {title[:120]}\n"
            + f"On-screen header: {header!r}\nSpoken VO line: {vo!r}\n"
        )
        try:
            raw = generate_video(prompt=prompt, video_path=proxy, cfg=cfg, max_output_tokens=500)
        except Exception as e:
            print(f"[video_vlm] final gate failed: {e}")
            return {"ok": False, "score": 0, "reasons": [f"gate_error:{e}"], "raw": str(e)[:200]}
    data = _parse_json(raw)
    data["raw"] = (raw or "")[:700]
    score = float(data.get("score") or 0)
    # Hard fails
    reasons = list(data.get("reasons") or [])
    if data.get("wrong_footage"):
        reasons.append("wrong_footage")
    if data.get("stray_wrong_audio"):
        reasons.append("stray_audio")
    if niche_id in {"gta6", "reactions"}:
        if data.get("trailer_or_subject_visible") is False:
            reasons.append("no_trailer")
        if data.get("reaction_real") is False:
            reasons.append("no_reaction")
    if data.get("header_matches") is False:
        reasons.append("header_mismatch")
    if data.get("vo_matches_visuals") is False:
        reasons.append("vo_mismatch")
    if data.get("layout_clean") is False:
        reasons.append("messy_layout")
    ok = score >= min_score and "wrong_footage" not in reasons and "no_trailer" not in reasons
    if niche_id in {"gta6", "reactions"} and "no_reaction" in reasons:
        ok = False
    data["ok"] = ok
    data["score"] = score
    data["reasons"] = reasons
    print(f"[final-gate] score={score} ok={ok} reasons={reasons} desc={str(data.get('desc',''))[:120]}")
    return data
