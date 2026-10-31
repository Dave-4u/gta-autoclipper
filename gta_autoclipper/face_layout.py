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
            pad_x, pad_y = fw * 0.55, fh * 0.65
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



def cropdetect_box(
    path: Path,
    t: float,
    bbox: tuple[float, float, float, float],
    *,
    frames: int = 48,
) -> tuple[float, float, float, float]:
    """Run ffmpeg cropdetect inside a normalized bbox; return tightened bbox fractions."""
    import re
    import subprocess
    gx, gy, gw, gh = bbox
    # Sample a mid frame region via crop then cropdetect
    # Use absolute pixel crop from a scaled probe
    probe_w = 720
    cmd = [
        "ffmpeg", "-hide_banner", "-ss", f"{max(0.0, t):.2f}", "-i", str(path),
        "-frames:v", str(frames),
        "-vf", (
            f"scale={probe_w}:-2,"
            f"crop=iw*{gw:.4f}:ih*{gh:.4f}:iw*{gx:.4f}:ih*{gy:.4f},"
            "cropdetect=16:2:0"
        ),
        "-f", "null", "-",
    ]
    pr = subprocess.run(cmd, capture_output=True, text=True, check=False)
    crops = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", pr.stderr or "")
    if not crops:
        return bbox
    # Last cropdetect is usually the stable one
    cw, ch, cx, cy = [int(x) for x in crops[-1]]
    # Region size in probe pixels after first crop
    # We need fractions relative to ORIGINAL frame, so map back through the region
    # Approximated: region was gw*W x gh*H; cropdetect offsets are within region
    # Get probe height from a quick probe
    try:
        # Infer region pixel size from scale width
        # After scale=720:-2, width=720; region width = 720*gw
        rw = max(1.0, probe_w * gw)
        # height unknown precisely — use cropdetect's parent: if crop is almost full, skip
        if cw < 8 or ch < 8:
            return bbox
        # Fractions within the game bbox
        fx = cx / rw
        # For height, approximate rh from crop+cy: if cy+ch suggests rh
        # Use last cropdetect line's context — better: assume rh ~= (cy+ch) if near edge else ch/(1-margin)
        rh = max(float(cy + ch), float(ch) / 0.9)
        fy = cy / rh
        fw = cw / rw
        fh = ch / rh
        # Clamp and map back to full-frame fractions
        fx = max(0.0, min(0.95, gx + gw * fx))
        fy = max(0.0, min(0.95, gy + gh * fy))
        fw = max(0.15, min(1.0 - fx, gw * fw))
        fh = max(0.15, min(1.0 - fy, gh * fh))
        # Only accept if we actually removed meaningful bars (>3% shrink)
        if fw * fh > gw * gh * 0.995:
            return bbox
        return (fx, fy, fw, fh)
    except Exception:
        return bbox


def _expand_face_pip(box: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    """Grow a face bbox into the PiP window — not into surrounding gameplay.

    Tiny Gemini face hits (area < ~6%) live *inside* an overlay PiP. Expanding
    past ~22% width pulls trailer pixels into the face panel (seen as
    blurry fingers beside Kai). Stop at the PiP chrome.
    """
    fx, fy, fw, fh = box
    cx, cy = fx + fw / 2.0, fy + fh / 2.0
    area = fw * fh
    if area < 0.06:
        # Face-in-PiP: pad ~1.5–1.6x, hard-cap to typical overlay size
        fw2 = min(0.22, max(fw * 1.55, 0.16))
        fh2 = min(0.32, max(fh * 1.45, 0.24))
    else:
        fw2 = min(0.40, max(fw * 1.15, 0.26))
        fh2 = min(0.48, max(fh * 1.15, 0.30))
    fx2 = max(0.0, min(1.0 - fw2, cx - fw2 / 2.0))
    fy2 = max(0.0, min(1.0 - fh2, cy - fh2 / 2.0))
    return (fx2, fy2, fw2, fh2)




def _face_panel_at(
    path: Path,
    t: float,
    hint: tuple[float, float, float, float] | None = None,
) -> tuple[float, float, float, float] | None:
    """Build a face-centered PiP panel from YuNet, optionally biased by a Gemini hint."""
    img = _grab(path, t, width=960)
    if img is None:
        return None
    faces = _detect_faces(img)
    if not faces:
        return None
    # Drop chat avatars (tiny) and full-frame trailer heads (huge)
    usable = [f for f in faces if 0.0003 <= f[2] * f[3] <= 0.08]
    if not usable:
        return None
    if hint:
        hx, hy, hw, hh = hint
        hcx, hcy = hx + hw / 2.0, hy + hh / 2.0
        usable = sorted(
            usable,
            key=lambda f: (f[0] + f[2] / 2 - hcx) ** 2 + (f[1] + f[3] / 2 - hcy) ** 2,
        )
        fx, fy, fw, fh = usable[0]
        dist = ((fx + fw / 2 - hcx) ** 2 + (fy + fh / 2 - hcy) ** 2) ** 0.5
        if dist > 0.35:
            return None
    else:
        usable = sorted(usable, key=lambda f: -(f[2] * f[3]))
        fx, fy, fw, fh = usable[0]
    cx, cy = fx + fw / 2.0, fy + fh / 2.0
    # Panel fills top half after scale-to-fill; bias face up to crop stream HUD
    pw, ph = 0.20, 0.26
    px = max(0.0, min(1.0 - pw, cx - pw / 2.0))
    # Bias face upward so Twitch/YT viewer HUD under the webcam is cropped out
    py = max(0.0, min(1.0 - ph, cy - ph * 0.55))
    if py + ph > 0.58:
        ph = max(0.20, 0.58 - py)
    return (px, py, pw, ph)


def _box_area(box: tuple[float, float, float, float]) -> float:
    return max(0.0, float(box[2]) * float(box[3]))


def _region_mean(path: Path, t: float, box: tuple[float, float, float, float]) -> float:
    """Mean luma of a normalized bbox at time t (0 if unreadable)."""
    try:
        import cv2
        import numpy as np
        img = _grab(path, t, width=720)
        if img is None:
            return 0.0
        h, w = img.shape[:2]
        x, y, bw, bh = box
        x0, y0 = int(max(0, x * w)), int(max(0, y * h))
        x1, y1 = int(min(w, (x + bw) * w)), int(min(h, (y + bh) * h))
        if x1 <= x0 + 2 or y1 <= y0 + 2:
            return 0.0
        gray = cv2.cvtColor(img[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
        return float(np.mean(gray))
    except Exception:
        return 0.0


def _accept_refined(
    original: tuple[float, float, float, float],
    refined: tuple[float, float, float, float],
    *,
    min_area: float,
    min_keep_frac: float = 0.50,
) -> bool:
    """Reject cropdetect results that collapse the region or stay tiny."""
    oa, ra = _box_area(original), _box_area(refined)
    if ra < min_area:
        return False
    if oa > 0 and ra < oa * min_keep_frac:
        return False
    # Reject needle-thin boxes
    if refined[2] < 0.18 or refined[3] < 0.18:
        return False
    return True


def split_filter_complex(
    layout: FaceCamLayout,
    *,
    width: int = 1080,
    height: int = 1920,
    speed: float = 1.0,
    in_label: str = "0:v",
    out_label: str = "vlaid",
    face_frac: float = 0.42,
    source: Path | None = None,
    refine_t: float | None = None,
) -> str | None:
    """Build ffmpeg filter that stacks face-cam (top) + game (bottom).

    Returns None if no usable face cam — caller should use default layout.
    """
    if not layout.face:
        return None
    # Prefer YuNet face-centered panel (centers the person; crops stream HUD).
    # Fall back to Gemini bbox expanded into PiP chrome only.
    face_orig = None
    if source is not None and refine_t is not None:
        try:
            face_orig = _face_panel_at(Path(source), float(refine_t), hint=layout.face)
            if face_orig:
                print(f"[layout] YuNet face panel -> {face_orig}")
        except Exception as e:
            print(f"[layout] YuNet face panel skip: {e}")
    if face_orig is None:
        face_orig = _expand_face_pip(layout.face)
        print(f"[layout] Gemini/expand face panel -> {face_orig}")
    fx, fy, fw, fh = face_orig
    fx, fy = max(0.0, min(0.75, fx)), max(0.0, min(0.75, fy))
    fw, fh = max(0.16, min(1.0 - fx, fw)), max(0.22, min(1.0 - fy, fh))
    face_orig = (fx, fy, fw, fh)

    if layout.game:
        gx, gy, gw, gh = layout.game
    else:
        gx, gy, gw, gh = 0.05, 0.05, 0.9, 0.9
    gx, gy = max(0.0, min(0.75, gx)), max(0.0, min(0.75, gy))
    gw, gh = max(0.35, min(1.0 - gx, gw)), max(0.35, min(1.0 - gy, gh))
    game_orig = (gx, gy, gw, gh)

    # Carve face out of near-full-frame game bbox only (never touch face box)
    if gw * gh > 0.85 and layout.face:
        face_cx = fx + fw / 2
        if face_cx > 0.65:
            gw = min(gw, max(0.40, fx - 0.02 - gx))
        elif face_cx < 0.40:
            new_x = max(gx, fx + fw + 0.01)
            gw = max(0.40, gx + gw - new_x)
            gx = new_x
        print(f"[layout] carved game around face -> {(gx, gy, gw, gh)}")
        game_orig = (gx, gy, gw, gh)

    if source is not None and refine_t is not None:
        src = Path(source)
        tref = float(refine_t)
        fx, fy, fw, fh = face_orig
        # Tighten face panel only (remove chat/game margins); never expand/shrink <70%
        try:
            refined_f = cropdetect_box(src, tref, face_orig)
            if (
                _accept_refined(face_orig, refined_f, min_area=0.03, min_keep_frac=0.70)
                and refined_f[2] <= face_orig[2] * 1.05
                and refined_f[3] <= face_orig[3] * 1.05
            ):
                fx, fy, fw, fh = refined_f
                print(f"[layout] face tighten -> {(fx, fy, fw, fh)}")
            else:
                print(f"[layout] face keep panel -> {face_orig}")
        except Exception as e:
            print(f"[layout] face tighten skip: {e}")
        try:
            refined_g = cropdetect_box(src, tref, game_orig)
            mean_g = _region_mean(src, tref, refined_g)
            mean_o = _region_mean(src, tref, game_orig)
            ok = _accept_refined(game_orig, refined_g, min_area=0.22, min_keep_frac=0.50)
            # Dark unreadable zoom (mean << original) → reject
            if ok and mean_g < 28 and mean_g < mean_o * 0.75:
                ok = False
                print(f"[layout] cropdetect game too dark mean={mean_g:.1f} vs orig={mean_o:.1f}")
            if ok:
                gx, gy, gw, gh = refined_g
                print(f"[layout] cropdetect game kept -> {(gx, gy, gw, gh)} mean={mean_g:.1f}")
            else:
                gx, gy, gw, gh = game_orig
                print(f"[layout] cropdetect game rejected; keep Gemini {game_orig}")
        except Exception as e:
            print(f"[layout] cropdetect game skip: {e}")
            gx, gy, gw, gh = game_orig

    # Trim Twitch chat / right-edge player UI and bottom sub bars
    chat = layout.chat_side
    if chat == "right" or (gx + gw) > 0.82:
        gw = min(gw, max(0.45, 0.80 - gx))
    if chat == "left" and gx < 0.16:
        trim = 0.16 - gx
        gx = 0.16
        gw = max(0.40, gw - trim)
    # Bottom stream HUD
    if gy + gh > 0.92:
        gh = max(0.40, 0.92 - gy)
    # Light top trim for overlays
    if gy < 0.02:
        gy = 0.02
        gh = min(gh, 1.0 - gy)
    gw = min(gw, 1.0 - gx)
    gh = min(gh, 1.0 - gy)

    # NEVER expand game crop into a "safe" band — that previously swallowed the face
    # cam / grey carpet and made the bottom panel look solid grey mid-clip.
    # Prefer Gemini/YuNet game box; only shrink (chat trim above), never enlarge.
    if source is not None and refine_t is not None:
        mean_final = _region_mean(Path(source), float(refine_t), (gx, gy, gw, gh))
        print(f"[layout] game box mean={mean_final:.1f} area={_box_area((gx, gy, gw, gh)):.3f} (no safe-expand)")

    # Exact even panel heights that sum to frame height (yuv420-safe)
    width = int(width) - (int(width) % 2)
    height = int(height) - (int(height) % 2)
    top_h = int(round(height * face_frac))
    top_h = top_h - (top_h % 2)
    bot_h = height - top_h
    if bot_h % 2:
        top_h -= 1
        bot_h = height - top_h
    speed = max(0.5, min(2.0, float(speed)))

    def even_crop(fx_, fy_, fw_, fh_):
        return (
            f"crop=floor(iw*{fw_:.6f}/2)*2:floor(ih*{fh_:.6f}/2)*2:"
            f"floor(iw*{fx_:.6f}/2)*2:floor(ih*{fy_:.6f}/2)*2"
        )

    # Mild 2% inward on game only (chat chrome) — NOT the old 12% that over-zoomed
    parts = [
        f"[{in_label}]split=2[fsrc][gsrc]",
        f"[fsrc]format=rgb24,{even_crop(fx, fy, fw, fh)},"
        f"scale={width}:{top_h}:force_original_aspect_ratio=increase,"
        f"crop={width}:{top_h},"
        f"scale=iw*1.32:ih*1.32,"
        f"crop={width}:{top_h}:(iw-{width})/2:(ih-{top_h})/2,"
        # Trim last 2px of face (webcam bottom edge) then scale back — kills grey seam
        f"crop={width}:{top_h}-2:0:0,scale={width}:{top_h}:flags=bilinear,"
        f"setsar=1,format=rgb24[face]",
        f"[gsrc]format=rgb24,{even_crop(gx, gy, gw, gh)},"
        f"crop=floor(iw*0.96/2)*2:floor(ih*0.94/2)*2:floor(iw*0.02/2)*2:floor(ih*0.03/2)*2,"
        f"scale={width}:{bot_h}:force_original_aspect_ratio=increase,"
        f"crop={width}:{bot_h},"
        # Trim first 2px of game then scale back
        f"crop={width}:{bot_h}-2:0:2,scale={width}:{bot_h}:flags=bilinear,"
        f"setsar=1,format=rgb24[game],"
        # vstack in rgb24 (no pad) then yuv420 — heights sum exactly to height
        f"[face][game]vstack=inputs=2,setsar=1,setpts=PTS/{speed},"
        f"crop={width}:{height}:0:0,"
        # Blend 3 rows at the panel join so no 1px grey seam line remains
        f"geq="
        f"r='if(between(Y,{top_h}-1,{top_h}+1),"
        f"(r(X,{top_h}-2)+r(X,{top_h}-1)+r(X,{top_h})+r(X,{top_h}+1)+r(X,{top_h}+2))/5,r(X,Y))':"
        f"g='if(between(Y,{top_h}-1,{top_h}+1),"
        f"(g(X,{top_h}-2)+g(X,{top_h}-1)+g(X,{top_h})+g(X,{top_h}+1)+g(X,{top_h}+2))/5,g(X,Y))':"
        f"b='if(between(Y,{top_h}-1,{top_h}+1),"
        f"(b(X,{top_h}-2)+b(X,{top_h}-1)+b(X,{top_h})+b(X,{top_h}+1)+b(X,{top_h}+2))/5,b(X,Y))',"
        f"format=yuv420p[{out_label}]",
    ]
    assert top_h + bot_h == height, (top_h, bot_h, height)
    print(f"[layout] panels top_h={top_h}+bot_h={bot_h}={height} face={(fx, fy, fw, fh)} game={(gx, gy, gw, gh)}")
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


def layout_from_gemini(data: dict) -> FaceCamLayout:
    """Build layout from Gemini JSON bboxes."""
    def box(key):
        b = (data or {}).get(key)
        if not (isinstance(b, (list, tuple)) and len(b) == 4):
            return None
        try:
            x, y, w, h = [float(v) for v in b]
        except Exception:
            return None
        # Gemini sometimes returns pixel coords — normalize assuming ~1280x720/1080p
        if max(x, y, w, h) > 1.5:
            # Guess canvas from magnitudes
            cw = 1280.0 if max(x + w, w) <= 1280 else 1920.0
            ch = 720.0 if max(y + h, h) <= 720 else (1080.0 if max(y + h, h) <= 1080 else 1920.0)
            x, y, w, h = x / cw, y / ch, w / cw, h / ch
        if w <= 0.05 or h <= 0.05:
            return None
        x, y = max(0.0, min(0.95, x)), max(0.0, min(0.95, y))
        w, h = max(0.08, min(1.0 - x, w)), max(0.08, min(1.0 - y, h))
        return (x, y, w, h)
    face, game = box("face_bbox"), box("game_bbox")
    # Most reaction streams put Twitch chat on the right
    chat = None
    if face:
        fcx = face[0] + face[2] / 2.0
        if fcx < 0.45:
            chat = "right"
        elif fcx > 0.55:
            chat = "left"
    return FaceCamLayout(face=face, game=game, chat_side=chat, faces_found=1 if face else 0)


def fill_crop_filter(
    *,
    width: int = 1080,
    height: int = 1920,
    speed: float = 1.0,
    zoom: float = 1.08,
    in_label: str = "0:v",
    out_label: str = "base",
    cx: float = 0.5,
    cy: float = 0.5,
) -> str:
    """9:16 crop-to-fill (no pillarbox/letterbox). Optional center bias cx/cy 0-1."""
    z = max(1.0, float(zoom))
    speed = max(0.5, min(2.0, float(speed)))
    cx = max(0.0, min(1.0, float(cx)))
    cy = max(0.0, min(1.0, float(cy)))
    # Cover 9:16, then optional mild zoom; always crop — never pad
    return (
        f"[{in_label}]format=rgb24,"
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height}:(iw-{width})*{cx}:(ih-{height})*{cy},"
        f"scale=iw*{z}:ih*{z},"
        f"crop={width}:{height}:(iw-{width})/2:(ih-{height})/2,"
        f"setsar=1,setpts=PTS/{speed},format=yuv420p[{out_label}]"
    )
