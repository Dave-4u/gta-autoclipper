"""Full autopilot: discover → clip → render N clips per niche on a loop."""

from __future__ import annotations

import json
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .analyze import find_highlights
from .captions import make_post_caption, write_sidecar
from .config import ensure_dirs, load_config, project_root
from .discover import discover
from .download import download_video, probe_duration
from .edit import probe_clip, render_clip
from .history import History
from .niches import apply_niche_to_config, list_niche_ids, niche_relevance


LogFn = Callable[[str], None]


@dataclass
class AutopilotState:
    running: bool = False
    stop_requested: bool = False
    current_niche: str = ""
    produced: list[str] = field(default_factory=list)
    log_lines: list[str] = field(default_factory=list)


_STATE = AutopilotState()
_LOCK = threading.Lock()


def get_state() -> AutopilotState:
    with _LOCK:
        return AutopilotState(
            running=_STATE.running,
            stop_requested=_STATE.stop_requested,
            current_niche=_STATE.current_niche,
            produced=list(_STATE.produced),
            log_lines=list(_STATE.log_lines[-300:]),
        )


def request_stop() -> None:
    with _LOCK:
        _STATE.stop_requested = True


def _log(msg: str, extra: LogFn | None = None) -> None:
    line = msg if isinstance(msg, str) else str(msg)
    print(line, flush=True)
    with _LOCK:
        _STATE.log_lines.append(line)
        if len(_STATE.log_lines) > 500:
            _STATE.log_lines = _STATE.log_lines[-400:]
    if extra:
        extra(line)


def run_niche_pass(
    niche_id: str,
    cfg: dict[str, Any],
    *,
    clips_per_niche: int = 2,
    max_videos: int = 2,
    custom_keywords: list[str] | None = None,
    log: LogFn | None = None,
) -> list[Path]:
    """One discover/render pass for a niche. Returns produced clip paths."""
    from .niches import apply_niche_to_config

    niche_cfg = apply_niche_to_config(cfg, niche_id, custom_keywords=custom_keywords)
    # Fix output dir under project outputs/<niche>
    root = Path(niche_cfg.get("_root") or project_root())
    out_dir = root / "outputs" / niche_id
    out_dir.mkdir(parents=True, exist_ok=True)
    niche_cfg["clips"] = dict(niche_cfg["clips"])
    niche_cfg["clips"]["output_dir"] = str(out_dir)
    # Downloads stay shared
    ensure_dirs(niche_cfg)

    history = History(niche_cfg["history"]["file"])
    _log(f"[auto] niche={niche_id} discovering…", log)
    candidates = discover(niche_cfg, history)
    if not candidates:
        _log(f"[auto] niche={niche_id} no candidates", log)
        return []

    produced: list[Path] = []
    videos_done = 0
    for cand in candidates:
        if get_state().stop_requested:
            break
        if videos_done >= max_videos or len(produced) >= clips_per_niche:
            break
        with _LOCK:
            _STATE.current_niche = niche_id
        _log(f"[auto] === [{niche_id}] {cand.title} ({cand.id}) score={cand.rank_score:.2f}", log)
        ok, rel, reason = niche_relevance(
            niche_id,
            title=cand.title,
            description=str((cand.extra or {}).get("description") or ""),
            channel=str((cand.extra or {}).get("channel") or ""),
            tags=list((cand.extra or {}).get("tags") or []),
            custom_keywords=custom_keywords,
        )
        if not ok:
            _log(f"[auto] skip off-niche ({reason}): {cand.title[:80]}", log)
            continue
        try:
            path = download_video(cand, niche_cfg)
            if not path:
                history.mark(cand.id, {"status": "download_failed", "niche": niche_id})
                continue
            dur = probe_duration(path)
            _log(f"[auto] downloaded {path.name} ({dur:.1f}s)", log)
            segments = find_highlights(path, niche_cfg)
            if not segments:
                history.mark(cand.id, {"status": "no_highlights", "niche": niche_id})
                continue
            need = clips_per_niche - len(produced)
            for i, seg in enumerate(segments[: max(1, need)]):
                if get_state().stop_requested:
                    break
                _log(
                    f"[auto] render {i+1} {seg.start:.1f}-{seg.end:.1f}s ({seg.reason})",
                    log,
                )
                out = render_clip(
                    path,
                    seg,
                    niche_cfg,
                    title=cand.title,
                    index=i,
                    video_id=cand.id,
                )
                if not out:
                    continue
                info = make_post_caption(cand.title, seg, niche_cfg, out)
                write_sidecar(out, info)
                # Extra metadata next to clip
                meta_path = out.with_suffix(".meta.txt")
                hook_json = out.with_suffix(".hook.json")
                hook_txt = ""
                if hook_json.is_file():
                    try:
                        hook_txt = json.loads(hook_json.read_text(encoding="utf-8")).get("hook", "")
                    except Exception:
                        pass
                meta_path.write_text(
                    (
                        f"NICHE: {niche_id}\n"
                        f"TITLE: {info.get('title_suggestion','')}\n"
                        f"HEADER: {hook_txt}\n"
                        f"CAPTION:\n{info.get('caption','')}\n\n"
                        f"HASHTAGS: {info.get('hashtags','')}\n"
                        f"SOURCE: {cand.url}\n"
                        f"CREATED: {datetime.now().isoformat()}\n"
                    ),
                    encoding="utf-8",
                )
                meta = probe_clip(out)
                _log(f"[auto] wrote {out} ({(meta.get('format') or {}).get('size')})", log)
                produced.append(out)
                with _LOCK:
                    _STATE.produced.append(str(out))
            history.mark(
                cand.id,
                {
                    "status": "ok",
                    "niche": niche_id,
                    "title": cand.title,
                    "clips": len(produced),
                    "url": cand.url,
                },
            )
            videos_done += 1
        except Exception as e:
            _log(f"[auto] error: {e}", log)
            traceback.print_exc()
            history.mark(cand.id, {"status": "error", "error": str(e), "niche": niche_id})
    return produced


def run_autopilot(
    *,
    niches: list[str],
    clips_per_niche: int = 2,
    max_videos: int = 2,
    interval_hours: float = 0.0,
    custom_keywords: list[str] | None = None,
    config_path: str | None = None,
    once: bool = False,
    log: LogFn | None = None,
) -> list[str]:
    """
    Loop niches. If interval_hours > 0 and not once, sleep and repeat until stop.
    """
    with _LOCK:
        if _STATE.running:
            _log("[auto] already running", log)
            return list(_STATE.produced)
        _STATE.running = True
        _STATE.stop_requested = False
        _STATE.produced = []
        _STATE.log_lines = []

    cfg = load_config(config_path)
    # Faster defaults for autopilot CPU boxes
    cfg.setdefault("analysis", {})
    if not cfg["analysis"].get("whisper_model"):
        cfg["analysis"]["whisper_model"] = "tiny"

    niche_list = [n for n in niches if n] or ["gta6"]
    all_out: list[str] = []
    try:
        while True:
            for niche_id in niche_list:
                if get_state().stop_requested:
                    break
                paths = run_niche_pass(
                    niche_id,
                    cfg,
                    clips_per_niche=clips_per_niche,
                    max_videos=max_videos,
                    custom_keywords=custom_keywords if niche_id == "custom" else None,
                    log=log,
                )
                all_out.extend(str(p) for p in paths)
            if once or interval_hours <= 0 or get_state().stop_requested:
                break
            sleep_s = interval_hours * 3600
            _log(f"[auto] sleeping {interval_hours}h before next loop…", log)
            # interruptible sleep
            end = time.time() + sleep_s
            while time.time() < end:
                if get_state().stop_requested:
                    break
                time.sleep(min(5.0, end - time.time()))
            if get_state().stop_requested:
                break
    finally:
        with _LOCK:
            _STATE.running = False
            _STATE.current_niche = ""
        _log(f"[auto] stopped — {len(all_out)} clip(s) total", log)
    return all_out


def start_autopilot_background(**kwargs) -> None:
    t = threading.Thread(target=run_autopilot, kwargs=kwargs, daemon=True)
    t.start()
