"""Local FastAPI control desk — replaces Gradio."""

from __future__ import annotations

import json
import mimetypes
import re
import subprocess
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .autopilot import get_state, request_stop, run_niche_pass, run_autopilot
from .config import ensure_dirs, load_config, project_root
from .niches import list_niche_ids, list_autopilot_niche_ids, niche_is_enabled, apply_niche_to_config
from .progress import JobProgress
from .ranking import build_ranking_video, suggest_topic

WEB_DIR = Path(__file__).resolve().parent / "web"
THUMB_DIR_NAME = "_thumbs"

_progress = JobProgress()
_job_thread: threading.Thread | None = None
_job_lock = threading.Lock()
_stop_flag = threading.Event()
_config_path: str | None = None


class StartRequest(BaseModel):
    niche: str = "anime"
    mode: str = "single"  # single | ranking | autopilot
    topic: str = ""
    rank_n: int = 5
    max_videos: int = 1
    max_clips: int = 2
    voiceover: bool = True
    captions: bool = True
    fast: bool = True


def _cfg(path: str | None = None) -> dict[str, Any]:
    return load_config(path or _config_path)


def _apply_fast_defaults(cfg: dict[str, Any], *, fast: bool = True) -> dict[str, Any]:
    cfg = dict(cfg)
    cfg["analysis"] = dict(cfg.get("analysis") or {})
    cfg["search"] = dict(cfg.get("search") or {})
    cfg["download"] = dict(cfg.get("download") or {})
    cfg["clips"] = dict(cfg.get("clips") or {})
    cfg["voiceover"] = dict(cfg.get("voiceover") or {})
    # CPU-speed defaults so a run finishes or fails fast
    cfg["search"]["max_source_duration"] = min(
        int(cfg["search"].get("max_source_duration") or 900),
        240 if fast else 360,
    )
    cfg["analysis"]["whisper_model"] = "tiny"
    cfg["analysis"]["whisper_for_highlights"] = False
    cfg["analysis"]["max_analyze_seconds"] = min(
        float(cfg["analysis"].get("max_analyze_seconds") or 90),
        90 if fast else 120,
    )
    cfg["download"]["timeout_seconds"] = min(
        int(cfg["download"].get("timeout_seconds") or 180),
        120 if fast else 180,
    )
    cfg["job"] = {
        "overall_timeout_seconds": 15 * 60 if fast else 25 * 60,
        "analyze_timeout_seconds": 180 if fast else 300,
        "render_timeout_seconds": 240,
        "ranking_timeout_seconds": int((cfg.get("ranking") or {}).get("timeout_seconds") or (25 * 60)),
    }
    cfg.setdefault("ranking", {})
    cfg["ranking"].setdefault("timeout_seconds", 25 * 60)
    cfg["ranking"].setdefault("min_items_on_timeout", 3)
    cfg["ranking"].setdefault("max_probe", 7)
    cfg["ranking"].setdefault("workers", 3)
    cfg["ranking"].setdefault("download_seconds", 90)
    cfg["ranking"].setdefault("analyze_seconds", 75)
    cfg["ranking"].setdefault("use_trending", False)
    cfg["ranking"].setdefault("keywords", 3)
    cfg["ranking"].setdefault("results_per_keyword", 5)
    return cfg


def _list_outputs(limit: int = 40) -> list[dict[str, Any]]:
    root = project_root() / "outputs"
    if not root.is_dir():
        return []
    files = sorted(
        [p for p in root.rglob("*.mp4") if p.is_file() and p.stat().st_size > 1000 and "_thumbs" not in p.parts and "/_work_" not in str(p).replace("\\", "/")],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    out = []
    for p in files[:limit]:
        rel = p.relative_to(project_root()).as_posix()
        out.append(
            {
                "id": rel,
                "name": p.name,
                "niche": p.parent.name if p.parent.name != "outputs" else "root",
                "size": p.stat().st_size,
                "mtime": p.stat().st_mtime,
                "url": f"/media/{rel}",
                "thumb": f"/api/thumb?path={rel}",
            }
        )
    return out


def _ensure_thumb(rel: str) -> Path | None:
    root = project_root()
    src = (root / rel).resolve()
    if not str(src).startswith(str(root.resolve())) or not src.is_file():
        return None
    thumb_dir = root / "outputs" / THUMB_DIR_NAME
    thumb_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", rel)
    dest = thumb_dir / f"{safe}.jpg"
    if dest.is_file() and dest.stat().st_mtime >= src.stat().st_mtime:
        return dest
    cmd = [
        "ffmpeg", "-y", "-ss", "0.8", "-i", str(src),
        "-frames:v", "1", "-vf", "scale=360:-2",
        "-q:v", "4", str(dest),
    ]
    proc = subprocess.run(cmd, capture_output=True, check=False)
    if proc.returncode != 0 or not dest.is_file():
        return None
    return dest


def _run_single(req: StartRequest) -> None:
    _progress.start_job()
    _stop_flag.clear()
    try:
        cfg = _apply_fast_defaults(_cfg(), fast=req.fast)
        niche = req.niche if niche_is_enabled(req.niche) else (list_autopilot_niche_ids() or ["anime"])[0]
        cfg = apply_niche_to_config(cfg, niche)
        ensure_dirs(cfg)
        cfg["voiceover"]["enabled"] = bool(req.voiceover)
        cfg["clips"]["burn_captions"] = bool(req.captions)
        cfg["clips"]["max_clips_per_video"] = max(1, int(req.max_clips))

        overall = float((cfg.get("job") or {}).get("overall_timeout_seconds") or 900)
        deadline = time.time() + overall
        _progress.begin("discover", f"niche={niche}")

        def _discover():
            from .history import History
            from .discover import discover
            hist = History(cfg["history"]["file"])
            return discover(cfg, hist)

        ok, cands, err = True, None, ""
        # inline discover with soft awareness of deadline
        from .history import History
        from .discover import discover
        from .download import download_video, probe_duration
        from .analyze import find_highlights
        from .edit import render_clip, probe_clip
        from .captions import make_post_caption, write_sidecar
        from .progress import run_with_timeout

        hist = History(cfg["history"]["file"])
        try:
            cands = discover(cfg, hist)
        except Exception as e:
            _progress.end("discover", status="fail", detail=str(e))
            _progress.finish_job(ok=False, error=str(e))
            return
        if _stop_flag.is_set():
            _progress.end("discover", status="skipped", detail="stopped")
            _progress.finish_job(ok=False, error="stopped")
            return
        cands = (cands or [])[: max(1, int(req.max_videos))]
        _progress.end("discover", status="ok", detail=f"{len(cands)} candidate(s)")
        if not cands:
            _progress.finish_job(ok=False, error="No candidates found (cookies / niche / history?)")
            return

        produced: list[str] = []
        for ci, cand in enumerate(cands):
            if _stop_flag.is_set() or time.time() > deadline:
                _progress.log("[job] stopped or overall timeout")
                break
            _progress.begin("download", f"{cand.id} — {cand.title[:60]}")
            path = download_video(cand, cfg)
            if not path:
                _progress.end("download", status="fail", detail="download failed")
                hist.mark(cand.id, {"status": "download_failed", "niche": niche})
                continue
            dur = probe_duration(path)
            _progress.end("download", status="ok", detail=f"{path.name} ({dur:.0f}s)")

            if time.time() > deadline:
                break
            analyze_timeout = float((cfg.get("job") or {}).get("analyze_timeout_seconds") or 180)
            _progress.begin("analyze", f"highlights ≤{analyze_timeout:.0f}s")

            def _an():
                return find_highlights(path, cfg)

            ok_a, segs, err_a = run_with_timeout(_an, seconds=analyze_timeout, label="analyze", progress=_progress)
            if not ok_a:
                _progress.end("analyze", status="timeout" if "timed out" in err_a else "fail", detail=err_a)
                hist.mark(cand.id, {"status": "analyze_timeout" if "timed out" in err_a else "error", "niche": niche})
                continue
            if not segs:
                _progress.end("analyze", status="fail", detail="no highlights")
                hist.mark(cand.id, {"status": "no_highlights", "niche": niche})
                continue
            segs = segs[: max(1, int(req.max_clips))]
            _progress.end("analyze", status="ok", detail=f"{len(segs)} window(s)")

            for i, seg in enumerate(segs):
                if _stop_flag.is_set() or time.time() > deadline:
                    break
                _progress.begin("render", f"clip {i+1}/{len(segs)} {seg.start:.1f}-{seg.end:.1f}s")
                render_timeout = float((cfg.get("job") or {}).get("render_timeout_seconds") or 240)

                def _ren(seg=seg, i=i):
                    return render_clip(
                        path, seg, cfg, title=cand.title, index=i, video_id=cand.id,
                        source_meta=dict(cand.extra or {}),
                    )

                ok_r, out, err_r = run_with_timeout(_ren, seconds=render_timeout, label="render", progress=_progress)
                if not ok_r or not out:
                    _progress.end("render", status="fail", detail=err_r or "render failed")
                    continue
                info = make_post_caption(cand.title, seg, cfg, out)
                write_sidecar(out, info)
                meta = probe_clip(out)
                size = (meta.get("format") or {}).get("size")
                _progress.end("render", status="ok", detail=f"{out.name} size={size}")
                produced.append(str(out))
                with _progress._lock:
                    _progress.produced.append(str(out))

            hist.mark(
                cand.id,
                {"status": "ok", "niche": niche, "title": cand.title, "clips": len(produced), "url": cand.url},
            )

        if produced:
            _progress.finish_job(ok=True)
            _progress.log(f"[job] done — {len(produced)} file(s)")
        else:
            _progress.finish_job(ok=False, error="No clips produced")
    except Exception as e:
        _progress.log(traceback.format_exc())
        _progress.finish_job(ok=False, error=str(e))


def _run_ranking(req: StartRequest) -> None:
    _progress.start_job()
    _stop_flag.clear()
    try:
        from . import procutil
        from .procutil import run_with_timeout_cancelable

        cfg = _apply_fast_defaults(_cfg(), fast=req.fast)
        niche = req.niche if niche_is_enabled(req.niche) else "anime"
        topic = (req.topic or "").strip() or suggest_topic(niche, int(req.rank_n or 5), cfg)
        _progress.begin("ranking", f"{niche} — {topic}")
        budget = float(
            (cfg.get("job") or {}).get("ranking_timeout_seconds")
            or (cfg.get("ranking") or {}).get("timeout_seconds")
            or (25 * 60)
        )
        cancel = threading.Event()
        procutil.clear_cancel()

        def _build():
            return build_ranking_video(
                niche,
                topic=topic,
                n=max(3, min(7, int(req.rank_n or 5))),
                config_path=_config_path,
                log=lambda m: _progress.log(m),
                deadline_s=budget,
                cancel_event=cancel,
            )

        def _on_timeout():
            cancel.set()
            _stop_flag.set()
            procutil.request_cancel()

        # Hard wall slightly above budget so internal partial-render can finish
        ok, out, err = run_with_timeout_cancelable(
            _build,
            seconds=budget + 90,
            label="ranking",
            progress=_progress,
            on_timeout=_on_timeout,
        )
        if out:
            _progress.end("ranking", status="ok", detail=str(out) + (" (partial/timeout)" if not ok else ""))
            with _progress._lock:
                _progress.produced.append(str(out))
            _progress.finish_job(ok=True)
            return
        _progress.end("ranking", status="fail", detail=err or "failed")
        _progress.finish_job(ok=False, error=err or "Ranking failed")
    except Exception as e:
        _progress.log(traceback.format_exc())
        _progress.finish_job(ok=False, error=str(e))


def _run_autopilot(req: StartRequest) -> None:
    _progress.start_job()
    _stop_flag.clear()
    try:
        cfg = _apply_fast_defaults(_cfg(), fast=req.fast)
        niche = req.niche if niche_is_enabled(req.niche) else (list_autopilot_niche_ids() or ["anime"])[0]
        _progress.begin("autopilot", f"niche={niche}")
        paths = run_niche_pass(
            niche,
            cfg,
            clips_per_niche=max(1, int(req.max_clips)),
            max_videos=max(1, int(req.max_videos)),
            log=lambda m: _progress.log(m),
        )
        for p in paths:
            with _progress._lock:
                _progress.produced.append(str(p))
        _progress.end("autopilot", status="ok" if paths else "fail", detail=f"{len(paths)} clip(s)")
        _progress.finish_job(ok=bool(paths), error="" if paths else "No clips produced")
    except Exception as e:
        _progress.log(traceback.format_exc())
        _progress.finish_job(ok=False, error=str(e))


def create_app(config_path: str | None = None) -> FastAPI:
    global _config_path
    _config_path = config_path
    app = FastAPI(title="Cutroom", docs_url=None, redoc_url=None)

    @app.get("/", response_class=HTMLResponse)
    def index():
        html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html)

    @app.get("/api/meta")
    def meta():
        niches = [
            {"id": n, "label": n}
            for n in list_niche_ids()
        ]
        # Prefer friendly labels
        from .niches import NICHES
        for item in niches:
            item["label"] = (NICHES.get(item["id"]) or {}).get("label") or item["id"]
        return {
            "niches": niches,
            "defaults": {"niche": (list_autopilot_niche_ids() or ["anime"])[0], "mode": "single"},
            "outputs_count": len(_list_outputs(200)),
        }

    @app.get("/api/status")
    def status():
        snap = _progress.snapshot()
        ap = get_state()
        snap["autopilot_running"] = ap.running
        return snap

    @app.post("/api/start")
    def start(req: StartRequest):
        global _job_thread
        with _job_lock:
            if _job_thread and _job_thread.is_alive():
                raise HTTPException(409, "A job is already running")
            _stop_flag.clear()
            mode = (req.mode or "single").lower()
            target = _run_single
            if mode == "ranking":
                target = _run_ranking
            elif mode in {"autopilot", "auto"}:
                target = _run_autopilot
            _job_thread = threading.Thread(target=target, args=(req,), daemon=True, name="cutroom-job")
            _job_thread.start()
        return {"ok": True, "mode": req.mode, "niche": req.niche}

    @app.post("/api/stop")
    def stop():
        _stop_flag.set()
        request_stop()
        try:
            from . import procutil
            procutil.request_cancel()
        except Exception:
            pass
        _progress.log("[job] stop requested")
        return {"ok": True}

    @app.get("/api/outputs")
    def outputs():
        return {"items": _list_outputs()}

    @app.get("/api/thumb")
    def thumb(path: str):
        dest = _ensure_thumb(path)
        if not dest:
            raise HTTPException(404, "thumb unavailable")
        return FileResponse(dest, media_type="image/jpeg")

    @app.get("/media/{file_path:path}")
    def media(file_path: str):
        root = project_root().resolve()
        src = (root / file_path).resolve()
        if not str(src).startswith(str(root)) or not src.is_file():
            raise HTTPException(404)
        mime, _ = mimetypes.guess_type(str(src))
        return FileResponse(src, media_type=mime or "application/octet-stream")

    # static assets
    static = WEB_DIR / "static"
    static.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static)), name="static")
    return app


def launch_ui(
    *,
    config_path: str | None = None,
    host: str = "127.0.0.1",
    port: int = 7860,
    open_browser: bool = True,
) -> None:
    import asyncio
    import logging
    import sys
    import uvicorn
    import webbrowser

    # Windows Proactor floods logs with ConnectionResetError (WinError 10054)
    # when the browser closes SSE/long-poll sockets. Prefer Selector policy + filter.
    if sys.platform.startswith("win"):
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
            print("[ui] WindowsSelectorEventLoopPolicy enabled", flush=True)
        except Exception as e:
            print(f"[ui] could not set Selector policy: {e}", flush=True)

        def _silence_proactor_connreset(loop, context):
            exc = context.get("exception")
            if isinstance(exc, ConnectionResetError):
                return
            msg = str(context.get("message", ""))
            if "ConnectionResetError" in msg or "10054" in msg:
                return
            loop.default_exception_handler(context)

        try:
            loop = asyncio.new_event_loop()
            loop.set_exception_handler(_silence_proactor_connreset)
            asyncio.set_event_loop(loop)
        except Exception:
            pass

        class _Drop10054(logging.Filter):
            def filter(self, record: logging.LogRecord) -> bool:
                msg = record.getMessage()
                if "10054" in msg or "ConnectionResetError" in msg or "_call_connection_lost" in msg:
                    return False
                return True

        for name in ("asyncio", "uvicorn.error", "uvicorn.access"):
            logging.getLogger(name).addFilter(_Drop10054())

    app = create_app(config_path)
    url = f"http://{host}:{port}"
    print(f"[ui] Cutroom desk → {url}", flush=True)
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=host, port=port, log_level="info")
