"""Detect face-cam / game regions for reaction split layouts."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import subprocess
import tempfile


@dataclass
class FaceCamLayout:
    """Normalized boxes in source frame coords (x,y,w,h) 0..1."""
    face: tuple[float, float, float, float] | None  # webcam / reactor
    game: tuple[float, float, float, float] | None  # main gameplay/trailer
    chat_side: str | None  # "left" | "right" | None
    faces_found: int = 0


def _yunet_path() -> Path:
    return Path(__file__).resolve().parent.parent / "models" / "face_detection_yunet_2023mar.onnx"


def _grab(path: Path, t: float, width: int = 640):
    import cv2
    out = Path(tempfile.mkstemp(suffix=".jpg")[1])
    subprocess.run(
        ["ffmpeg", "-y", "-ss", f"{max(0.0, t):.2f}", "-i", str(path),
         "-frames:v", "1", "-vf", f"scale={width}:-1", "-q:v", "3", str(out)],
        capture_output=True, check=False,
    )
    img = cv2.imread(str(out)) if out.is_file() else None
    try:
        out.unlink(missing_ok=True)
    except Exception:
        pass
    return img


def _detect_faces(img) -> list[tuple[float, float, float, float]]:
    """Return face boxes as fractions of frame (x, y, w, h)."""
    import cv2
    h, w = img.shape[:2]
    model = _yunet_path()
    boxes: list[tuple[float, float, float, float]] = []
    try:
        if hasattr(cv2, "FaceDetectorYN_create") and model.is_file():
            det = cv2.FaceDetectorYN_create(str(model), "", (w, h), 0.55, 0.3, 5000)
            det.setInputSize((w, h))
            _, faces = det.detect(img)
            if faces is not None:
                for f in faces:
                    fx, fy, fw, fh = float(f[0]), float(f[1]), float(f[2]), float(f[3])
                    boxes.append((fx / w, fy / h, fw / w, fh / h))
                return boxes
    except Exception:
        pass
    try:
        cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        for (x, y, fw, fh) in cascade.detectMultiScale(gray, 1.1, 4, minSize=(24, 24)):
            boxes.append((x / w, y / h, fw / w, fh / h))
    except Exception:
        pass
    return boxes


def _guess_chat_side(img) -> str | None:
    """Heuristic: tall dark/text-heavy strip on left or right = chat."""
    import cv2
    import numpy as np
    h, w = img.shape[:2]
    strip = max(8, w // 6)
    left = img[:, :strip]
    right = img[:, w - strip:]
    # Chat is often darker + higher vertical edge density
    def score(region):
        gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
        mean = float(np.mean(gray))
        edges = cv2.Canny(gray, 60, 120)
        e = float(np.mean(edges))
        # Prefer darker + edged (text)
        return e * 0.7 + max(0.0, 90.0 - mean) * 0.3
    ls, rs = score(left), score(right)
    # Only flag if one side clearly chat-like vs center
    center = img[:, strip: w - strip]
    cs = score(center)
    if ls > rs * 1.25 and ls > cs * 0.9:
        return "left"
    if rs > ls * 1.25 and rs > cs * 0.9:
        return "right"
    return None


def detect_layout(path: Path, t: float) -> FaceCamLayout:
    img = _grab(path, t, width=720)
    if img is None:
        return FaceCamLayout(None, None, None, 0)
    h, w = img.shape[:2]
    faces = _detect_faces(img)
    chat = _guess_chat_side(img)

    face_box = None
    if faces:
        # Prefer true webcam PiPs (small, corner) over full-frame talking heads
        scored = []
        for (x, y, fw, fh) in faces:
            area = fw * fh
            corner = min(x, y, 1.0 - (x + fw), 1.0 - (y + fh))
            if 0.008 <= area <= 0.18:
                pip_score = 2.0 + (0.6 if corner < 0.1 else 0.0)
            elif area <= 0.28:
                pip_score = 1.0 + (0.3 if corner < 0.12 else 0.0)
            else:
                pip_score = 0.2  # full-frame face — weak PiP candidate
            scored.append((pip_score, area, (x, y, fw, fh)))
        # Reject faces that live inside the chat strip (tiny avatars / chat UI)
        filtered = []
        for pip_score, area, box in scored:
            x, y, fw, fh = box
            if chat == "right" and x >= 0.70:
                continue
            if chat == "left" and (x + fw) <= 0.30:
                continue
            # Tiny square in a corner of a text panel — skip
            if area < 0.012 and (x > 0.65 or x < 0.05):
                continue
            filtered.append((pip_score, area, box))
        scored = filtered or scored
        scored.sort(key=lambda z: (-z[0], z[1]))
        # Only accept if it looks like a PiP (not a full talking-head frame)
        if scored[0][0] >= 1.0:
            face_box = scored[0][2]
            x, y, fw, fh = face_box
            pad_x, pad_y = fw * 0.45, fh * 0.55
            x0 = max(0.0, x - pad_x)
            y0 = max(0.0, y - pad_y)
            x1 = min(1.0, x + fw + pad_x)
            y1 = min(1.0, y + fh + pad_y * 0.3)
            # Cap expanded crop so it stays a panel, not the whole frame
            bw, bh = max(0.12, x1 - x0), max(0.12, y1 - y0)
            if bw * bh > 0.35:
                # shrink toward face center
                cx, cy = x + fw / 2, y + fh / 2
                bw = min(bw, 0.42)
                bh = min(bh, 0.42)
                x0 = max(0.0, cx - bw / 2)
                y0 = max(0.0, cy - bh / 2)
            face_box = (x0, y0, bw, bh)

    # Game region: the large remainder, excluding chat strip and face cam
    gx0, gy0, gx1, gy1 = 0.0, 0.0, 1.0, 1.0
    if chat == "left":
        gx0 = 0.18
    elif chat == "right":
        gx1 = 0.82
    if face_box:
        fx, fy, fw, fh = face_box
        # If face is in upper portion, game is below/beside; if lower-left etc.
        # Default: use center 70% excluding face bbox
        # Prefer the largest rectangular band that isn't the face cam
        # Simple approach: full frame minus chat, slightly inset
        pass
    # Keep a generous game window (center-weighted)
    game = (gx0 + 0.02, 0.05, max(0.4, gx1 - gx0 - 0.04), 0.9)
    # If face overlaps top-left heavily, push game to the right/bottom of face
    if face_box:
        fx, fy, fw, fh = face_box
        # Expand game to avoid covering only the face cam area
        if fx < 0.35 and fy < 0.45 and fw < 0.45:
            # classic corner webcam — game is the rest
            game = (min(0.35, fx + fw + 0.02) if fx < 0.3 else gx0 + 0.02,
                    0.02, 0.65 if fx < 0.3 else max(0.5, gx1 - gx0 - 0.04), 0.96)
            # Actually for split layout we crop face and game separately; game = non-chat main
            game = (gx0 + 0.02, 0.02, max(0.5, gx1 - gx0 - 0.04), 0.96)

    return FaceCamLayout(face=face_box, game=game, chat_side=chat, faces_found=len(faces))


def split_filter_complex(
    layout: FaceCamLayout,
    *,
    width: int = 1080,
    height: int = 1920,
    speed: float = 1.0,
    in_label: str = "0:v",
    out_label: str = "vlaid",
    face_frac: float = 0.42,
) -> str | None:
    """Build ffmpeg filter that stacks face-cam (top) + game (bottom).

    Returns None if no usable face cam — caller should use default layout.
    """
    if not layout.face:
        return None
    fx, fy, fw, fh = layout.face
    # Source crop uses iw/ih expressions
    # Clamp
    fx, fy = max(0.0, min(0.85, fx)), max(0.0, min(0.85, fy))
    fw, fh = max(0.12, min(1.0 - fx, fw)), max(0.12, min(1.0 - fy, fh))

    if layout.game:
        gx, gy, gw, gh = layout.game
    else:
        gx, gy, gw, gh = 0.05, 0.05, 0.9, 0.9
    gx, gy = max(0.0, min(0.8, gx)), max(0.0, min(0.8, gy))
    gw, gh = max(0.3, min(1.0 - gx, gw)), max(0.3, min(1.0 - gy, gh))

    top_h = int(height * face_frac) // 2 * 2
    bot_h = height - top_h
    speed = max(0.5, min(2.0, float(speed)))

    # Crop face & game from source, scale to panels, vstack
    # Hide chat by cropping game away from chat_side (already in layout.game)
    parts = [
        f"[{in_label}]split=2[fsrc][gsrc]",
        # Face panel — large headshot
        f"[fsrc]crop=iw*{fw:.4f}:ih*{fh:.4f}:iw*{fx:.4f}:ih*{fy:.4f},"
        f"scale={width}:{top_h}:force_original_aspect_ratio=increase,"
        f"crop={width}:{top_h},setsar=1[face]",
        # Game panel
        f"[gsrc]crop=iw*{gw:.4f}:ih*{gh:.4f}:iw*{gx:.4f}:ih*{gy:.4f},"
        f"scale={width}:{bot_h}:force_original_aspect_ratio=increase,"
        f"crop={width}:{bot_h},setsar=1[game]",
        f"[face][game]vstack=inputs=2,setpts=PTS/{speed}[{out_label}]",
    ]
    return ";".join(parts)


def detect_at_segment(path: Path, start: float, end: float) -> FaceCamLayout:
    """Sample a few points in the segment; pick the layout with a clear face cam."""
    mid = (start + end) / 2
    candidates = [start + 1.0, mid, end - 1.0, start + (end - start) * 0.7]
    best: FaceCamLayout | None = None
    for t in candidates:
        if t < 0:
            continue
        lay = detect_layout(path, t)
        if lay.face and (best is None or (lay.face[2] * lay.face[3]) > (best.face[2] * best.face[3] if best.face else 0)):
            best = lay
    return best or FaceCamLayout(None, None, None, 0)
