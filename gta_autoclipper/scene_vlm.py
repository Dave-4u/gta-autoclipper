"""Scene understanding via Google Gemini (frames + transcript + metadata)."""
from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any


def grab_frame_files(path: Path, times: list[float], *, width: int = 512) -> list[Path]:
    frames: list[Path] = []
    for t in times:
        out = Path(tempfile.mkstemp(suffix=".jpg")[1])
        cmd = [
            "ffmpeg", "-y", "-ss", f"{max(0.0, t):.2f}", "-i", str(path),
            "-frames:v", "1", "-vf", f"scale={width}:-1", "-q:v", "3", str(out),
        ]
        subprocess.run(cmd, capture_output=True, check=False)
        if out.is_file() and out.stat().st_size > 500:
            frames.append(out)
        else:
            out.unlink(missing_ok=True)
    return frames


def _cleanup(files: list[Path]) -> None:
    for p in files:
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass


def describe_moment(
    path: Path,
    start: float,
    end: float,
    *,
    n_frames: int = 4,
    extra_prompt: str = "",
    transcript: str = "",
    title: str = "",
    channel: str = "",
    cfg: dict[str, Any] | None = None,
) -> str:
    from .gemini_client import generate, has_gemini
    if not has_gemini(cfg):
        return ""
    dur = max(0.5, end - start)
    times = [start + dur * x for x in (0.15, 0.4, 0.65, 0.85)[:n_frames]]
    frames = grab_frame_files(path, times)
    if not frames:
        return ""
    prompt = (
        "You are labeling a short vertical video clip for TikTok/Shorts.\n"
        "Describe what is ON SCREEN factually in 2 short sentences.\n"
        "Name characters/shows/streamers ONLY if clearly visible or confirmed by metadata.\n"
        "If there is a face-cam streamer, say whether they look shocked, laughing, excited, or blank.\n"
        "Do NOT invent names.\n"
        f"Title: {title[:140]}\nChannel: {channel[:80]}\nTranscript: {transcript[:240]}\n"
    )
    if extra_prompt:
        prompt += extra_prompt + "\n"
    try:
        text = generate(prompt=prompt, images=frames, cfg=cfg)
    except Exception as e:
        print(f"[vlm/gemini] describe failed: {e}")
        text = ""
    finally:
        _cleanup(frames)
    return (text or "").strip()[:700]


def reaction_emotion(path: Path, t: float, *, cfg: dict[str, Any] | None = None, title: str = "") -> dict[str, Any]:
    from .gemini_client import generate, has_gemini
    if not has_gemini(cfg):
        return {"ok": True, "label": "unknown", "raw": "no_key"}
    frames = grab_frame_files(path, [t, t + 0.7, t + 1.4], width=512)
    if not frames:
        return {"ok": False, "label": "unknown", "raw": ""}
    prompt = (
        "Look at these frames from a reaction video. Focus on the face-cam person "
        "(usually a small inset webcam). Reply with ONE word only: "
        "laughing, shocked, excited, blank, or none.\n"
        f"Title hint: {title[:100]}"
    )
    try:
        raw = generate(prompt=prompt, images=frames, cfg=cfg).strip().lower()
    except Exception as e:
        print(f"[vlm/gemini] emotion failed: {e}")
        raw = ""
    finally:
        _cleanup(frames)
    label = "unknown"
    for cand in ("laughing", "shocked", "excited", "blank", "none"):
        if cand in raw:
            label = cand
            break
    return {"ok": label in {"laughing", "shocked", "excited"}, "label": label, "raw": raw[:80]}


def count_faces(path: Path, t: float) -> tuple[int, int]:
    try:
        import cv2
    except ImportError:
        return 0, 0
    frames = grab_frame_files(path, [t], width=640)
    if not frames:
        return 0, 0
    img = cv2.imread(str(frames[0]))
    _cleanup(frames)
    if img is None:
        return 0, 0
    h, w = img.shape[:2]
    area = float(h * w) or 1.0
    model_path = Path(__file__).resolve().parent.parent / "models" / "face_detection_yunet_2023mar.onnx"
    faces_n = small_n = 0
    try:
        if hasattr(cv2, "FaceDetectorYN_create") and model_path.is_file():
            det = cv2.FaceDetectorYN_create(str(model_path), "", (w, h), 0.6, 0.3, 5000)
            det.setInputSize((w, h))
            _, faces = det.detect(img)
            if faces is not None:
                for f in faces:
                    faces_n += 1
                    fw, fh = float(f[2]), float(f[3])
                    if (fw * fh) / area < 0.08:
                        small_n += 1
    except Exception:
        pass
    return faces_n, small_n


def is_illegible_mashup_frame(path: Path, t: float) -> bool:
    total, small = count_faces(path, t)
    return total >= 3 and small >= 3


def self_review(
    path: Path,
    header: str,
    *,
    start: float = 0.0,
    end: float | None = None,
    cfg: dict[str, Any] | None = None,
    title: str = "",
    transcript: str = "",
) -> dict[str, Any]:
    from .gemini_client import generate, has_gemini
    if end is None:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
            capture_output=True, text=True, check=False,
        )
        try:
            end = float(json.loads(proc.stdout)["format"]["duration"])
        except Exception:
            end = start + 8.0
    if not has_gemini(cfg):
        return {"ok": True, "desc": "", "overlap": 1.0, "reasons": [], "skipped": "no_key"}
    dur = max(0.5, end - start)
    frames = grab_frame_files(path, [start + dur * x for x in (0.2, 0.5, 0.8)])
    prompt = (
        "You are QA for a Shorts clip.\n"
        f"On-screen header text: {header!r}\n"
        f"Source title: {title[:120]}\n"
        "From the frames, answer in this exact format:\n"
        "DESC: <one factual sentence of who/what is on screen>\n"
        "MATCH: yes OR no\n"
        "EMOTION: laughing|shocked|excited|blank|none|n/a\n"
        "REASON: <short if MATCH=no, else ok>\n"
        "Rules: MATCH=no if the header names a person/character who is clearly NOT on screen. "
        "MATCH=no if header claims a reaction but the face-cam is blank/staring. "
        "MATCH=yes if header is a fair short label of the scene."
    )
    raw = ""
    try:
        raw = generate(prompt=prompt, images=frames, cfg=cfg)
    except Exception as e:
        print(f"[review/gemini] failed: {e}")
        _cleanup(frames)
        return {"ok": True, "desc": "", "overlap": 0.0, "reasons": [f"review_error:{e}"], "skipped": "error"}
    _cleanup(frames)
    desc = match = emotion = reason = ""
    for ln in (raw or "").splitlines():
        s = ln.strip()
        low = s.lower()
        if low.startswith("desc:"):
            desc = s.split(":", 1)[1].strip()
        elif low.startswith("match:"):
            match = s.split(":", 1)[1].strip().lower()
        elif low.startswith("emotion:"):
            emotion = s.split(":", 1)[1].strip().lower()
        elif low.startswith("reason:"):
            reason = s.split(":", 1)[1].strip()
    fail: list[str] = []
    if match.startswith("no"):
        fail.append(reason or "header_mismatch")
    if emotion == "blank" and any(k in (header or "").lower() for k in ("react", "face", "cinna", "kai")):
        fail.append("blank_face")
    return {"ok": not fail, "desc": desc or raw[:300], "overlap": 1.0 if not fail else 0.0,
            "reasons": fail, "emotion": emotion, "raw": raw[:500]}
