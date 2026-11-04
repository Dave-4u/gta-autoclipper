"""Named watermark presets persisted in watermarks.json (project root, gitignored)."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

DEFAULT_TEXT = "@officialdevclips"
_lock = threading.Lock()


def _path(root: Path | None = None) -> Path:
    from .config import project_root
    return Path(root or project_root()) / "watermarks.json"


def load(root: Path | None = None) -> dict[str, Any]:
    p = _path(root)
    data: dict[str, Any] = {}
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8")) or {}
        except Exception:
            data = {}
    presets = data.get("presets") if isinstance(data.get("presets"), dict) else {}
    presets = {str(k)[:40]: str(v)[:40] for k, v in presets.items() if str(k).strip() and str(v).strip()}
    if not presets:
        presets = {"Default": DEFAULT_TEXT}
    active = str(data.get("active") or "")
    if active not in presets:
        active = next(iter(presets))
    return {"enabled": bool(data.get("enabled", True)), "active": active, "presets": presets}


def save(data: dict[str, Any], root: Path | None = None) -> dict[str, Any]:
    with _lock:
        p = _path(root)
        p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return load(root)


def apply_to_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    """Overlay the user's active preset/toggle onto cfg['watermark'] (every mode)."""
    st = load()
    wm = dict(cfg.get("watermark") or {})
    p = _path()
    if p.is_file():
        wm["enabled"] = st["enabled"]
        wm["text"] = st["presets"][st["active"]]
    cfg["watermark"] = wm
    return cfg
