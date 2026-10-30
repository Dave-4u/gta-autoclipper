"""Free local Gradio web UI for GTA AutoClipper (CPU, opens in browser)."""

from __future__ import annotations

import io
import os
import re
import subprocess
import sys
import threading
import time
import traceback
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Generator

from .analyze import Segment, find_highlights
from .captions import make_post_caption, write_sidecar
from .config import ensure_dirs, load_config, project_root
from .discover import Candidate, discover, _url_to_id
from .download import download_video, probe_duration
from .edit import probe_clip, render_clip
from .history import History
from .niches import list_niche_ids
from .autopilot import get_state, request_stop, start_autopilot_background

try:
    from .voiceover import list_available_voices
    _av = list_available_voices()
    VOICE_CHOICES = (
        ["auto (kokoro→edge→piper)"]
        + [f"kokoro:{v}" for v in _av.get("kokoro", [])]
        + [f"edge:{v}" for v in _av.get("edge", [])]
        + [f"piper:{v}" for v in _av.get("piper", [])]
    )
except Exception:
    VOICE_CHOICES = [
        "auto (kokoro→edge→piper)",
        "kokoro:am_fenrir",
        "kokoro:af_bella",
        "edge:en-US-AndrewMultilingualNeural",
        "piper:en_US-ryan-high",
    ]
PIPER_VOICES = VOICE_CHOICES  # back-compat alias


class LogCapture:
    """Thread-safe log buffer that also prints to stdout."""

    def __init__(self) -> None:
        self._lines: list[str] = []
        self._lock = threading.Lock()

    def write(self, msg: str) -> None:
        if not msg:
            return
        # print() often ends with \n; keep readable lines
        text = msg if isinstance(msg, str) else str(msg)
        sys.__stdout__.write(text)
        sys.__stdout__.flush()
        with self._lock:
            for part in text.splitlines():
                if part.strip():
                    self._lines.append(part)

    def flush(self) -> None:
        sys.__stdout__.flush()

    def text(self) -> str:
        with self._lock:
            return "\n".join(self._lines[-400:])

    def clear(self) -> None:
        with self._lock:
            self._lines.clear()


def _parse_urls(text: str) -> list[str]:
    if not text:
        return []
    urls: list[str] = []
    for line in text.replace(",", "\n").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Accept bare YouTube IDs
        if re.fullmatch(r"[A-Za-z0-9_-]{11}", line):
            urls.append(f"https://www.youtube.com/watch?v={line}")
            continue
        m = re.search(r"https?://\S+", line)
        if m:
            urls.append(m.group(0).rstrip(").,]\"'"))
        elif "youtube.com" in line or "youtu.be" in line or "tiktok.com" in line:
            urls.append(line)
    # de-dupe preserve order
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _candidates_from_urls(urls: list[str]) -> list[Candidate]:
    out: list[Candidate] = []
    for i, url in enumerate(urls):
        vid = _url_to_id(url)
        out.append(
            Candidate(
                id=vid,
                title=f"url_{i+1}",
                url=url,
                duration=None,
                source="links",
                view_count=0,
                upload_date=None,
                rank_score=9999.0 - i,
            )
        )
    return out


def list_output_clips(cfg: dict[str, Any] | None = None) -> list[str]:
    root = project_root()
    if cfg:
        out_dir = Path(cfg["clips"]["output_dir"])
    else:
        out_dir = root / "outputs"
    if not out_dir.is_dir():
        return []
    clips = sorted(
        [p for p in out_dir.glob("*.mp4") if p.is_file() and p.stat().st_size > 1000],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return [str(p) for p in clips[:40]]


def open_outputs_folder(cfg: dict[str, Any] | None = None) -> str:
    root = project_root()
    out_dir = Path(cfg["clips"]["output_dir"]) if cfg else root / "outputs"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = str(out_dir.resolve())
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
        return f"Opened: {path}"
    except Exception as e:
        return f"Could not open folder ({e}). Path: {path}"


def join_clips(paths: list[Path], out_path: Path) -> Path | None:
    """Concat clips with ffmpeg concat demuxer (stream copy when possible)."""
    if not paths:
        return None
    if len(paths) == 1:
        return paths[0]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    list_file = out_path.with_suffix(".txt")
    # Re-encode for consistent params (vertical clips should already match)
    # Use concat filter via demuxer after normalizing
    lines = []
    for p in paths:
        # ffmpeg concat demuxer needs escaped paths
        esc = str(p.resolve()).replace("'", "'\\''")
        lines.append(f"file '{esc}'")
    list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(list_file),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        "-pix_fmt", "yuv420p",
        str(out_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    list_file.unlink(missing_ok=True)
    if proc.returncode != 0 or not out_path.is_file():
        print(f"[join] failed: {(proc.stderr or '')[-600:]}")
        return None
    return out_path


def apply_ui_options(
    cfg: dict[str, Any],
    *,
    voiceover: bool,
    voice: str,
    captions: bool,
    copyright_safe: bool,
    fast: bool,
    max_clips: int,
) -> dict[str, Any]:
    cfg = dict(cfg)
    # deep-ish copies of nested dicts we mutate
    cfg["voiceover"] = dict(cfg.get("voiceover") or {})
    cfg["analysis"] = dict(cfg.get("analysis") or {})
    cfg["clips"] = dict(cfg.get("clips") or {})
    cfg["clips"]["copyright_safe"] = dict(cfg["clips"].get("copyright_safe") or {})
    cfg["search"] = dict(cfg.get("search") or {})

    cfg["voiceover"]["enabled"] = bool(voiceover)
    if voice:
        v = str(voice).strip()
        if v.startswith("auto"):
            cfg["voiceover"]["engine"] = "auto"
            cfg["voiceover"]["voice"] = ""
        elif ":" in v:
            eng, name = v.split(":", 1)
            cfg["voiceover"]["engine"] = "edge" if eng.startswith("edge") else eng
            cfg["voiceover"]["voice"] = name
        else:
            cfg["voiceover"]["voice"] = v
    cfg["clips"]["burn_captions"] = bool(captions)
    cfg["clips"]["copyright_safe"]["enabled"] = bool(copyright_safe)
    cfg["clips"]["max_clips_per_video"] = max(1, int(max_clips))
    if fast:
        cfg["analysis"]["whisper_model"] = "tiny"
    if not captions and not voiceover:
        # no need for whisper if neither captions nor VO context needed much;
        # keep whisper for hooks when VO off? still useful for hooks — leave on unless fast+no captions
        pass
    return cfg


def run_ui_job(
    *,
    urls_text: str,
    auto_discover: bool,
    voiceover: bool,
    voice: str,
    captions: bool,
    copyright_safe: bool,
    fast: bool,
    join_all: bool,
    max_clips: int,
    max_videos: int = 1,
    config_path: str | None = None,
    log: LogCapture | None = None,
    skip_history: bool = True,
) -> tuple[list[str], str]:
    """
    Core job used by the Gradio UI.
    Returns (list of output mp4 paths, log text).
    """
    log = log or LogCapture()
    log.clear()
    produced: list[Path] = []

    def progress(msg: str) -> None:
        log.write(msg + "\n")

    try:
        cfg = apply_ui_options(
            load_config(config_path),
            voiceover=voiceover,
            voice=voice,
            captions=captions,
            copyright_safe=copyright_safe,
            fast=fast,
            max_clips=max_clips,
        )
        ensure_dirs(cfg)

        # Captions off → tell edit to skip ASS (also skip whisper if VO off)
        if not captions:
            # still allow VO word captions when voiceover on
            if not voiceover:
                cfg["analysis"]["use_whisper"] = False

        urls = _parse_urls(urls_text)
        history = History(cfg["history"]["file"])

        candidates: list[Candidate] = []
        if urls:
            progress(f"[ui] {len(urls)} URL(s) pasted")
            candidates.extend(_candidates_from_urls(urls))
        if auto_discover or not candidates:
            if not candidates:
                progress("[ui] no URLs — auto-discovering GTA 6 content…")
            else:
                progress("[ui] also running auto-discover…") if auto_discover else None
            if auto_discover or not urls:
                found = discover(cfg, history)
                # Prefer unseen; if skip_history and empty, still allow pasted URLs
                for c in found:
                    if skip_history or not history.seen(c.id):
                        if c.id not in {x.id for x in candidates}:
                            candidates.append(c)

        if not candidates:
            progress("[ui] no candidates found")
            return [], log.text()

        # Cap videos processed
        max_videos = max(1, int(max_videos))
        batch = candidates[:max_videos]
        progress(f"[ui] processing {len(batch)} source video(s), up to {max_clips} clip(s) each")

        for cand in batch:
            progress(f"\n[ui] === {cand.title} ({cand.id})")
            try:
                path = download_video(cand, cfg)
                if not path:
                    progress("[ui] download failed")
                    continue
                # Refresh title from file if still placeholder
                dur = probe_duration(path)
                progress(f"[ui] source ready: {path.name} ({dur:.1f}s)")

                segments = find_highlights(path, cfg)
                if not segments:
                    # Fallback short window for UI testing / short sources
                    if dur >= 5:
                        end = min(dur, 18.0 if copyright_safe else min(dur, 30.0))
                        segments = [Segment(0, end, 1.0, "ui_fallback")]
                    else:
                        progress("[ui] no highlights")
                        continue
                segments = segments[: max(1, int(max_clips))]

                clip_paths: list[Path] = []
                for i, seg in enumerate(segments):
                    progress(
                        f"[ui] rendering clip {i+1}/{len(segments)} "
                        f"{seg.start:.1f}-{seg.end:.1f}s ({seg.reason})"
                    )
                    # Pass burn_captions into cfg for edit
                    out = render_clip(
                        path, seg, cfg, title=cand.title, index=i, video_id=cand.id
                    )
                    if not out:
                        progress("[ui] render failed")
                        continue
                    info = make_post_caption(cand.title, seg, cfg, out)
                    write_sidecar(out, info)
                    meta = probe_clip(out)
                    fmt = meta.get("format") or {}
                    progress(f"[ui] wrote {out.name} size={fmt.get('size')}")
                    clip_paths.append(out)
                    produced.append(out)

                if join_all and len(clip_paths) >= 1:
                    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                    joined = Path(cfg["clips"]["output_dir"]) / f"{cand.id}_joined_{ts}.mp4"
                    progress(f"[ui] joining {len(clip_paths)} clip(s)…")
                    j = join_clips(clip_paths, joined)
                    if j:
                        progress(f"[ui] joined → {j.name}")
                        produced.append(j)

                if not skip_history:
                    history.mark(
                        cand.id,
                        {"status": "ok", "title": cand.title, "clips": len(clip_paths), "url": cand.url},
                    )
            except Exception as e:
                progress(f"[ui] error: {e}")
                traceback.print_exc()

        progress(f"\n[ui] done — {len(produced)} file(s)")
    except Exception as e:
        progress(f"[ui] fatal: {e}")
        traceback.print_exc()

    return [str(p) for p in produced], log.text()


def on_suggest_topic(niche: str, n: float):
    from .ranking import suggest_topic
    from .config import load_config
    cfg = load_config()
    return suggest_topic(str(niche or "gta6"), int(n or 5), cfg)


def on_ranking_make(niche: str, topic: str, n: float, progress=None):
    from .ranking import build_ranking_video
    logs = []
    def log(m):
        logs.append(m)
        print(m)
    try:
        if progress is not None:
            progress(0.1, desc="Ranking search…")
        out = build_ranking_video(
            str(niche or "gta6"),
            topic=topic or None,
            n=max(3, min(7, int(n or 5))),
            log=log,
        )
        if progress is not None:
            progress(1.0, desc="Done")
        if not out:
            return "Ranking failed — see log", "\n".join(logs[-40:]), None, []
        return f"Wrote {out}", "\n".join(logs[-60:]), str(out), [str(out)]
    except Exception as e:
        return f"Error: {e}", "\n".join(logs[-40:]) + f"\n{e}", None, []


def build_app(config_path: str | None = None):
    import gradio as gr

    cfg0 = load_config(config_path)
    ensure_dirs(cfg0)
    default_voice = str((cfg0.get("voiceover") or {}).get("voice") or PIPER_VOICES[0])
    voices = list(dict.fromkeys([default_voice, *PIPER_VOICES]))

    def refresh_gallery():
        return list_output_clips(cfg0)

    def on_open_folder():
        return open_outputs_folder(cfg0)

    def on_make(
        urls_text,
        auto_discover,
        voiceover,
        voice,
        captions,
        copyright_safe,
        fast,
        join_all,
        max_clips,
        max_videos,
    ):
        log = LogCapture()
        # Yield initial state
        yield [], "Starting…\n", refresh_gallery()
        buf = io.StringIO()

        def run():
            with redirect_stdout(log), redirect_stderr(log):
                return run_ui_job(
                    urls_text=urls_text or "",
                    auto_discover=bool(auto_discover),
                    voiceover=bool(voiceover),
                    voice=str(voice or default_voice),
                    captions=bool(captions),
                    copyright_safe=bool(copyright_safe),
                    fast=bool(fast),
                    join_all=bool(join_all),
                    max_clips=int(max_clips or 1),
                    max_videos=int(max_videos or 1),
                    config_path=config_path,
                    log=log,
                    skip_history=True,
                )

        result_holder: dict[str, Any] = {}

        def worker():
            try:
                result_holder["out"] = run()
            except Exception as e:
                result_holder["err"] = e
                log.write(f"fatal: {e}\n")

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        while t.is_alive():
            yield [], log.text() or "Working…", refresh_gallery()
            time.sleep(0.8)
        t.join()
        if "err" in result_holder:
            yield [], log.text() + f"\nERROR: {result_holder['err']}", refresh_gallery()
            return
        paths, _ = result_holder.get("out", ([], log.text()))
        yield paths, log.text(), refresh_gallery()

    niche_ids = list_niche_ids()

    def refresh_gallery_all():
        root = project_root() / "outputs"
        paths = []
        if root.is_dir():
            for pth in sorted(root.rglob("*.mp4"), key=lambda x: x.stat().st_mtime, reverse=True):
                if pth.stat().st_size > 1000:
                    paths.append(str(pth))
                if len(paths) >= 40:
                    break
        return paths

    def on_auto_start(niches, clips_n, max_vids, interval, custom_kw):
        niches = niches or ["gta6"]
        custom = [k.strip() for k in (custom_kw or "").replace("\n", ",").split(",") if k.strip()]
        start_autopilot_background(
            niches=list(niches),
            clips_per_niche=int(clips_n or 2),
            max_videos=int(max_vids or 1),
            interval_hours=float(interval or 0),
            custom_keywords=custom or None,
            config_path=config_path,
            once=float(interval or 0) <= 0,
        )
        return "Autopilot started…", "\n".join(get_state().log_lines[-50:]) or "Starting…"

    def on_auto_stop():
        request_stop()
        return "Stop requested…", "\n".join(get_state().log_lines[-80:])

    def on_auto_poll():
        st = get_state()
        status = (
            f"running={st.running} niche={st.current_niche or '-'} "
            f"produced={len(st.produced)} stop={st.stop_requested}"
        )
        return status, "\n".join(st.log_lines[-120:]), refresh_gallery_all(), st.produced[-20:]

    with gr.Blocks(title="AutoClipper") as demo:
        gr.Markdown(
            """
# AutoClipper
Free local multi-niche Shorts clipper — smart discovery, catchy headers, intro VO, captions, copyright-safer cuts. Fully automated autopilot available.
"""
        )
        with gr.Tabs():
            with gr.Tab("Manual"):
                with gr.Row():
                    with gr.Column(scale=2):
                        urls = gr.Textbox(
                            label="YouTube / TikTok / Facebook URLs (one per line)",
                            placeholder="https://www.youtube.com/watch?v=…",
                            lines=4,
                        )
                        auto = gr.Checkbox(
                            label="Auto-discover (trending + search keywords)",
                            value=False,
                        )
                        with gr.Row():
                            voiceover = gr.Checkbox(label="Commentary AI voiceover", value=True)
                            voice = gr.Dropdown(voices, value=default_voice, label="Voice (Kokoro / edge-tts / Piper)")
                        with gr.Row():
                            captions = gr.Checkbox(label="Burn captions", value=True)
                            copyright_safe = gr.Checkbox(
                                label="Copyright-safe mode (~18s / zoom / 1.05x)", value=True
                            )
                        with gr.Row():
                            fast = gr.Checkbox(label="Fast mode (whisper tiny)", value=True)
                            join_all = gr.Checkbox(label="Join all clips into one video", value=False)
                        with gr.Row():
                            max_clips = gr.Slider(1, 5, value=2, step=1, label="Clips per video")
                            max_videos = gr.Slider(1, 3, value=1, step=1, label="Source videos")
                        make_btn = gr.Button("Make Clips", variant="primary")
                        open_btn = gr.Button("Open outputs folder")
                        open_status = gr.Textbox(label="Folder", interactive=False)
                    with gr.Column(scale=3):
                        log_box = gr.Textbox(label="Live progress / log", lines=18, interactive=False)
                        gallery = gr.Gallery(
                            label="Finished clips",
                            columns=3,
                            height=360,
                            object_fit="contain",
                            value=refresh_gallery_all(),
                        )
                        refresh_btn = gr.Button("Refresh gallery")
                        download_files = gr.Files(label="Download produced clips")

                make_btn.click(
                    on_make,
                    inputs=[urls, auto, voiceover, voice, captions, copyright_safe, fast, join_all, max_clips, max_videos],
                    outputs=[download_files, log_box, gallery],
                )
                refresh_btn.click(lambda: refresh_gallery_all(), outputs=[gallery])
                open_btn.click(on_open_folder, outputs=[open_status])

            with gr.Tab("Autopilot"):
                gr.Markdown(
                    "Hands-free loop: discover best videos per niche → pick peak moments → "
                    "catchy header + intro VO → render into `outputs/<niche>/`."
                )
                niche_select = gr.CheckboxGroup(
                    choices=niche_ids,
                    value=["gta6", "anime"],
                    label="Niches",
                )
                custom_kw = gr.Textbox(
                    label="Custom keywords (for 'custom' niche, comma-separated)",
                    placeholder="one piece edit, jjk fight, ...",
                )
                with gr.Row():
                    ap_clips = gr.Slider(1, 5, value=2, step=1, label="Clips per niche")
                    ap_videos = gr.Slider(1, 3, value=1, step=1, label="Source videos per niche")
                    ap_interval = gr.Number(value=0, label="Repeat every N hours (0 = once)")
                with gr.Row():
                    start_btn = gr.Button("Start Autopilot", variant="primary")
                    stop_btn = gr.Button("Stop")
                    poll_btn = gr.Button("Refresh status")
                ap_status = gr.Textbox(label="Status", interactive=False)
                ap_log = gr.Textbox(label="Autopilot log", lines=16, interactive=False)
                ap_gallery = gr.Gallery(label="Autopilot outputs", columns=3, height=320)
                ap_files = gr.Files(label="Latest produced files")

                start_btn.click(
                    on_auto_start,
                    inputs=[niche_select, ap_clips, ap_videos, ap_interval, custom_kw],
                    outputs=[ap_status, ap_log],
                )
                stop_btn.click(on_auto_stop, outputs=[ap_status, ap_log])
                poll_btn.click(on_auto_poll, outputs=[ap_status, ap_log, ap_gallery, ap_files])

            with gr.Tab("Ranking"):
                gr.Markdown(
                    "Top-N ranking video: auto topic → trending moments in one niche → "
                    "ordered by views (#N lowest → #1 highest) with rank overlays."
                )
                rk_niche = gr.Dropdown(choices=niche_ids, value="gta6", label="Niche")
                with gr.Row():
                    rk_n = gr.Slider(3, 7, value=5, step=1, label="Items (N)")
                    suggest_btn = gr.Button("Suggest topic")
                rk_topic = gr.Textbox(label="Topic", placeholder="Top 5 funniest GTA 6 reactions")
                rk_make = gr.Button("Make Ranking Video", variant="primary")
                rk_status = gr.Textbox(label="Status", interactive=False)
                rk_log = gr.Textbox(label="Log", lines=12, interactive=False)
                rk_video = gr.Video(label="Ranking output")
                rk_files = gr.Files(label="Output file")
                suggest_btn.click(on_suggest_topic, inputs=[rk_niche, rk_n], outputs=[rk_topic])
                rk_make.click(
                    on_ranking_make,
                    inputs=[rk_niche, rk_topic, rk_n],
                    outputs=[rk_status, rk_log, rk_video, rk_files],
                )


    return demo


def launch_ui(
    *,
    config_path: str | None = None,
    host: str = "127.0.0.1",
    port: int = 7860,
    open_browser: bool = True,
    share: bool = False,
    prevent_thread_lock: bool = False,
) -> None:
    import gradio as gr

    demo = build_app(config_path)
    print(f"[ui] launching http://{host}:{port}", flush=True)
    # Gradio 6: theme belongs on launch(); queue() optional
    demo.queue()
    demo.launch(
        server_name=host,
        server_port=port,
        inbrowser=open_browser,
        share=share,
        show_error=True,
        theme=gr.themes.Soft(),
        prevent_thread_lock=prevent_thread_lock,
    )


if __name__ == "__main__":
    launch_ui()
