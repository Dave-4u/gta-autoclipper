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
    custom = (cfg.get("font") or cfg.get("font_path") or "").strip()
    if custom and Path(custom).is_file():
        return custom
    candidates = [
        r"C:\Windows\Fonts\arialbd.ttf",
        r"C:\Windows\Fonts\arial.ttf",
        r"C:\Windows\Fonts\segoeui.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/Library/Fonts/Arial Bold.ttf",
    ]
    for c in candidates:
        if Path(c).is_file():
            return c
    return ""


def _escape_drawtext(text: str) -> str:
    """Sanitize text for ffmpeg drawtext inside filter_complex single quotes."""
    text = (text or "").replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")
    text = re.sub(r"[\x00-\x1f]", " ", text)
    return text


def _fc_path(path: str | Path) -> str:
    """Path for ffmpeg filter options: forward slashes, escape drive colon."""
    s = str(path).replace(chr(92), "/")
    # Escape Windows drive colon so filter option parsing does not split at C:
    if len(s) >= 2 and s[1] == ":":
        s = s[0] + chr(92) + ":" + s[2:]
    return s


def _font_opt(font: str) -> str:
    if not font:
        return ""
    # Quote path — unquoted fontfile=C\:/Windows/... breaks FFmpeg option parsing on Windows
    return f":fontfile='{_fc_path(font)}'"


def _run_ffmpeg_with_script(
    cmd_prefix: list[str],
    filter_complex: str,
    cmd_suffix: list[str],
    script_path: Path,
) -> subprocess.CompletedProcess:
    """Write filtergraph to a file; use -filter_complex_script (Windows-safe)."""
    # Normalize newlines — CRLF inside the script confuses some ffmpeg builds
    body = filter_complex.replace("\r\n", "\n").replace("\r", "\n")
    script_path.write_text(body, encoding="utf-8")
    # Forward-slash absolute path (C:/...) — backslashes break -filter_complex_script on Windows
    script_arg = Path(script_path).resolve().as_posix()
    cmd = list(cmd_prefix) + ["-filter_complex_script", script_arg] + list(cmd_suffix)
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        err = (proc.stderr or "") + (proc.stdout or "")
        if "Invalid argument" in err or "No such file" in err or "filter_complex_script" in err:
            # Fallback 1: relative path with forward slashes
            try:
                rel = Path(script_path).resolve().relative_to(Path.cwd().resolve()).as_posix()
            except Exception:
                rel = Path(script_path).name
            cmd2 = list(cmd_prefix) + ["-filter_complex_script", rel] + list(cmd_suffix)
            proc2 = subprocess.run(cmd2, capture_output=True, text=True, check=False)
            if proc2.returncode == 0:
                return proc2
            # Fallback 2: inline -filter_complex (ok for our graph sizes)
            cmd3 = list(cmd_prefix) + ["-filter_complex", body] + list(cmd_suffix)
            proc3 = subprocess.run(cmd3, capture_output=True, text=True, check=False)
            return proc3
    return proc
