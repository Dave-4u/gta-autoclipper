"""Google Gemini free API client for scene understanding + copy writing.

Key resolution: GEMINI_API_KEY env, then config gemini.api_key /
voiceover.gemini_api_key / hooks.gemini_api_key. Never log the key.
"""
from __future__ import annotations

import os
import random
import time
from pathlib import Path
from typing import Any

DEFAULT_MODEL = "gemini-2.5-flash"


def resolve_api_key(cfg: dict[str, Any] | None = None) -> str:
    env = (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()
    if env:
        return env
    # Optional file paths (never commit these files)
    for fp in (
        Path(os.environ.get("GEMINI_API_KEY_FILE") or ""),
        Path("/home/box/agent-data/secrets/GEMINI_API_KEY"),
        Path("/home/box/agent-data/GEMINI_API_KEY"),
        Path(".gemini_api_key"),
    ):
        try:
            if fp.is_file():
                val = fp.read_text(encoding="utf-8").strip()
                if val:
                    return val
        except OSError:
            pass
    cfg = cfg or {}
    for path in (("gemini", "api_key"), ("voiceover", "gemini_api_key"), ("hooks", "gemini_api_key")):
        cur: Any = cfg
        for k in path:
            if not isinstance(cur, dict):
                cur = None
                break
            cur = cur.get(k)
        if isinstance(cur, str) and cur.strip():
            return cur.strip()
    # api_key_file in config
    for path in (("gemini", "api_key_file"),):
        cur = cfg
        for k in path:
            cur = (cur or {}).get(k) if isinstance(cur, dict) else None
        if isinstance(cur, str) and cur.strip():
            try:
                val = Path(cur).read_text(encoding="utf-8").strip()
                if val:
                    return val
            except OSError:
                pass
    return ""


def has_gemini(cfg: dict[str, Any] | None = None) -> bool:
    return bool(resolve_api_key(cfg))


def _client(api_key: str):
    from google import genai
    return genai.Client(api_key=api_key)


def generate(
    *,
    prompt: str,
    images: list[Path] | None = None,
    cfg: dict[str, Any] | None = None,
    model: str | None = None,
    max_retries: int = 5,
) -> str:
    api_key = resolve_api_key(cfg)
    if not api_key:
        raise RuntimeError("no_gemini_key")
    model = model or str(((cfg or {}).get("gemini") or {}).get("model") or DEFAULT_MODEL)
    from google.genai import types

    client = _client(api_key)
    parts: list[Any] = []
    for img in images or []:
        p = Path(img)
        if not p.is_file():
            continue
        mime = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
        parts.append(types.Part.from_bytes(data=p.read_bytes(), mime_type=mime))
    parts.append(types.Part.from_text(text=prompt))
    contents = [types.Content(role="user", parts=parts)]

    last_err = ""
    for attempt in range(max_retries):
        try:
            resp = client.models.generate_content(
                model=model,
                contents=contents,
                config=types.GenerateContentConfig(temperature=0.4, max_output_tokens=400),
            )
            text = (getattr(resp, "text", None) or "").strip()
            if not text and getattr(resp, "candidates", None):
                chunks = []
                for c in resp.candidates or []:
                    content = getattr(c, "content", None)
                    for part in getattr(content, "parts", None) or []:
                        t = getattr(part, "text", None)
                        if t:
                            chunks.append(t)
                text = "\n".join(chunks).strip()
            return text
        except Exception as e:
            last_err = str(e)
            msg = last_err.lower()
            retryable = any(x in msg for x in ("429", "rate", "quota", "resource exhausted", "503", "unavailable"))
            if not retryable or attempt >= max_retries - 1:
                raise
            sleep_s = min(60.0, (2 ** attempt) + random.uniform(0, 0.5))
            print(f"[gemini] backoff {sleep_s:.1f}s ({last_err[:100]})")
            time.sleep(sleep_s)
    raise RuntimeError(f"gemini_failed:{last_err[:200]}")
