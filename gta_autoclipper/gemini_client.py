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

DEFAULT_MODEL = "gemini-flash-lite-latest"
FALLBACK_MODELS = [
    "gemini-flash-lite-latest",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-flash-latest",
    "gemini-2.5-flash",
]


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


def _error_label(message):
    low=str(message).lower()
    if any(x in low for x in ("429","quota","resource_exhausted","rate limit")):
        return "provider_rate_limit"
    if any(x in low for x in ("503","502","unavailable")):
        return "provider_unavailable"
    if "timeout" in low or "timed out" in low:
        return "provider_timeout"
    if any(x in low for x in ("401","403","api key","permission")):
        return "provider_authentication"
    return "provider_error"


def _client(api_key: str, timeout_seconds: float | None = None):
    from google import genai
    import os
    # Without a timeout a stalled free-tier request hangs the whole job forever (seen in ranking).
    t_ms = int(float(timeout_seconds if timeout_seconds is not None else os.environ.get("GEMINI_TIMEOUT_SECONDS", "90") or 90) * 1000)
    try:
        from google.genai import types
        return genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=t_ms))
    except Exception:
        return genai.Client(api_key=api_key)


def generate(
    *,
    prompt: str,
    images: list[Path] | None = None,
    cfg: dict[str, Any] | None = None,
    model: str | None = None,
    max_retries: int = 5,
    timeout_seconds: float | None = None,
    total_seconds: float | None = None,
) -> str:
    api_key = resolve_api_key(cfg)
    if not api_key:
        raise RuntimeError("no_gemini_key")
    preferred = model or str(((cfg or {}).get("gemini") or {}).get("model") or DEFAULT_MODEL)
    models = [preferred] + [m for m in FALLBACK_MODELS if m != preferred]
    from google.genai import types

    client = _client(api_key, timeout_seconds)
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
    import os as _os, time as _time
    _t0 = _time.time()
    _wall = float(total_seconds if total_seconds is not None else _os.environ.get("GEMINI_TOTAL_SECONDS", "150") or 150)
    for model_name in models:
        for attempt in range(max_retries):
            remaining_wall = _wall - (_time.time() - _t0)
            if remaining_wall <= 0:
                print(f"[gemini] gave up after {_wall:.0f}s total (models/retries) — using fallback")
                return ""
            try:
                if total_seconds is not None:
                    client = _client(api_key, min(float(timeout_seconds or 20), remaining_wall))
                resp = client.models.generate_content(
                    model=model_name,
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
                if model_name != preferred:
                    print(f"[gemini] used fallback model {model_name}")
                return text
            except Exception as e:
                last_err = str(e)
                msg = last_err.lower()
                # Move to next model on daily quota / not found
                if any(x in msg for x in ("per day", "not found", "no longer available")):
                    print(f"[gemini] switch model reason={_error_label(last_err)}")
                    break
                retryable = any(x in msg for x in ("429", "rate", "quota", "resource exhausted", "503", "unavailable"))
                if not retryable or attempt >= max_retries - 1:
                    break
                sleep_s = min(60.0, (2 ** attempt) + random.uniform(0, 0.5))
                print(f"[gemini] backoff {sleep_s:.1f}s reason={_error_label(last_err)}")
                time.sleep(sleep_s)
    raise RuntimeError(f"gemini_failed:{_error_label(last_err)}")


def generate_video(
    *,
    prompt: str,
    video_path: Path,
    cfg: dict[str, Any] | None = None,
    model: str | None = None,
    max_retries: int = 4,
    max_output_tokens: int = 500,
) -> str:
    """Upload a video via Files API and generate content (free-tier friendly)."""
    api_key = resolve_api_key(cfg)
    if not api_key:
        raise RuntimeError("no_gemini_key")
    preferred = model or str(((cfg or {}).get("gemini") or {}).get("model") or DEFAULT_MODEL)
    models = [preferred] + [m for m in FALLBACK_MODELS if m != preferred]
    from google.genai import types

    client = _client(api_key)
    video_path = Path(video_path)
    if not video_path.is_file():
        raise FileNotFoundError(str(video_path))

    uploaded = None
    last_err = ""
    try:
        uploaded = client.files.upload(file=video_path)
        # Wait until ACTIVE
        for _ in range(40):
            meta = client.files.get(name=uploaded.name)
            state = str(getattr(meta, "state", "") or "")
            if "ACTIVE" in state:
                uploaded = meta
                break
            if "FAILED" in state:
                raise RuntimeError(f"file_upload_failed:{state}")
            time.sleep(1.5)
        else:
            raise RuntimeError("file_upload_timeout")

        mime = getattr(uploaded, "mime_type", None) or "video/mp4"
        uri = getattr(uploaded, "uri", None)
        if not uri:
            raise RuntimeError("no_file_uri")

        contents = [types.Content(role="user", parts=[
            types.Part.from_uri(file_uri=uri, mime_type=mime),
            types.Part.from_text(text=prompt),
        ])]

        for model_name in models:
            for attempt in range(max_retries):
                try:
                    resp = client.models.generate_content(
                        model=model_name,
                        contents=contents,
                        config=types.GenerateContentConfig(
                            temperature=0.2, max_output_tokens=max_output_tokens,
                        ),
                    )
                    text = (getattr(resp, "text", None) or "").strip()
                    if not text and getattr(resp, "candidates", None):
                        chunks = []
                        for c in resp.candidates or []:
                            content = getattr(c, "content", None)
                            for part in getattr(content, "parts", None) or []:
                                tt = getattr(part, "text", None)
                                if tt:
                                    chunks.append(tt)
                        text = "\n".join(chunks).strip()
                    if model_name != preferred:
                        print(f"[gemini] video used fallback {model_name}")
                    return text
                except Exception as e:
                    last_err = str(e)
                    msg = last_err.lower()
                    if any(x in msg for x in ("per day", "not found", "no longer available")):
                        print(f"[gemini] video switch model: {last_err[:120]}")
                        break
                    retryable = any(x in msg for x in ("429", "rate", "quota", "resource exhausted", "503", "unavailable"))
                    if not retryable or attempt >= max_retries - 1:
                        break
                    sleep_s = min(45.0, (2 ** attempt) + random.uniform(0, 0.4))
                    print(f"[gemini] video backoff {sleep_s:.1f}s")
                    time.sleep(sleep_s)
        raise RuntimeError(f"gemini_video_failed:{last_err[:200]}")
    finally:
        if uploaded is not None:
            try:
                client.files.delete(name=uploaded.name)
            except Exception:
                pass
