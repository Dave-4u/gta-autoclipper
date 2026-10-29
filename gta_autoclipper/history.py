"""Persist downloaded / processed video IDs to avoid repeats."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class History:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data: dict[str, Any] = {"processed": {}}
        self._load()

    def _load(self) -> None:
        if self.path.is_file():
            try:
                with open(self.path, encoding="utf-8") as f:
                    self.data = json.load(f)
            except (json.JSONDecodeError, OSError):
                self.data = {"processed": {}}
        self.data.setdefault("processed", {})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2)

    def seen(self, video_id: str) -> bool:
        return video_id in self.data["processed"]

    def mark(self, video_id: str, meta: dict[str, Any] | None = None) -> None:
        entry = {
            "at": datetime.now(timezone.utc).isoformat(),
            **(meta or {}),
        }
        self.data["processed"][video_id] = entry
        self.save()
