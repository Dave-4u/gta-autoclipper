"""Load and validate config.yaml."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_NAME = "config.yaml"


def project_root() -> Path:
    """Directory that contains config.yaml (package parent)."""
    return Path(__file__).resolve().parent.parent



def _load_dotenv(root: Path | None = None) -> None:
    """Load KEY=VALUE pairs from project .env into os.environ (no overwrite)."""
    root = root or project_root()
    env_path = root / ".env"
    if not env_path.is_file():
        return
    try:
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.lower().startswith("export "):
                line = line[7:].strip()
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val
    except OSError as e:
        print(f"[config] .env read failed: {e}")


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    cfg_path = Path(path) if path else project_root() / DEFAULT_CONFIG_NAME
    _load_dotenv(cfg_path.parent)
    if not cfg_path.is_file():
        raise FileNotFoundError(f"Config not found: {cfg_path}")
    with open(cfg_path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    # Resolve relative paths against project root
    root = cfg_path.parent
    data["_root"] = root
    hist = data.setdefault("history", {})
    hist["file"] = str((root / hist.get("file", "history.json")).resolve())
    dl = data.setdefault("download", {})
    dl["output_dir"] = str((root / dl.get("output_dir", "downloads")).resolve())
    dl["links_file"] = str((root / dl.get("links_file", "links.txt")).resolve())
    clips = data.setdefault("clips", {})
    clips["output_dir"] = str((root / clips.get("output_dir", "outputs")).resolve())
    return data


def ensure_dirs(cfg: dict[str, Any]) -> None:
    Path(cfg["download"]["output_dir"]).mkdir(parents=True, exist_ok=True)
    Path(cfg["clips"]["output_dir"]).mkdir(parents=True, exist_ok=True)
