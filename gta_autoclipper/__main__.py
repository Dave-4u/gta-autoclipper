"""CLI: python -m gta_autoclipper [--ui|--auto|--once] ..."""

from __future__ import annotations

import argparse
import sys
import time
import traceback
from pathlib import Path

from .analyze import find_highlights
from .captions import make_post_caption, write_sidecar
from .config import ensure_dirs, load_config
from .discover import discover
from .download import download_video, probe_duration
from .edit import probe_clip, render_clip
from .history import History
from .niches import apply_niche_to_config, list_niche_ids


def run_once(cfg: dict, *, max_videos: int = 2, skip_whisper: bool | None = None) -> list[Path]:
    ensure_dirs(cfg)
    if skip_whisper is True:
        cfg.setdefault("analysis", {})["use_whisper"] = False
    elif skip_whisper is False:
        cfg.setdefault("analysis", {})["use_whisper"] = True

    history = History(cfg["history"]["file"])
    print("[run] discovering…")
    candidates = discover(cfg, history)
    if not candidates:
        print("[run] no new candidates (history may be full — delete history.json to reset)")
        return []

    print(f"[run] {len(candidates)} candidates; processing up to {max_videos}")
    produced: list[Path] = []

    for cand in candidates[:max_videos]:
        print(f"\n[run] === {cand.title} ({cand.id}) views={cand.view_count} score={cand.rank_score:.2f}")
        try:
            path = download_video(cand, cfg)
            if not path:
                print("[run] download failed, skipping")
                history.mark(cand.id, {"status": "download_failed", "title": cand.title})
                continue
            dur = probe_duration(path)
            print(f"[run] downloaded {path.name} ({dur:.1f}s)")

            segments = find_highlights(path, cfg)
            if not segments:
                print("[run] no highlights found")
                history.mark(cand.id, {"status": "no_highlights", "title": cand.title})
                continue

            for i, seg in enumerate(segments):
                print(f"[run] rendering clip {i+1}/{len(segments)} "
                      f"{seg.start:.1f}-{seg.end:.1f}s ({seg.reason})")
                out = render_clip(
                    path, seg, cfg, title=cand.title, index=i, video_id=cand.id
                )
                if not out:
                    continue
                info = make_post_caption(cand.title, seg, cfg, out)
                write_sidecar(out, info)
                meta = probe_clip(out)
                print(f"[run] wrote {out}")
                streams = meta.get("streams") or [{}]
                fmt = meta.get("format") or {}
                w = streams[0].get("width")
                h = streams[0].get("height")
                print(f"[run]   {w}x{h} duration≈{fmt.get('duration')} size={fmt.get('size')}")
                produced.append(out)

            history.mark(
                cand.id,
                {
                    "status": "ok",
                    "title": cand.title,
                    "clips": len(produced),
                    "url": cand.url,
                },
            )
        except Exception as e:
            print(f"[run] error on {cand.id}: {e}")
            traceback.print_exc()
            history.mark(cand.id, {"status": "error", "error": str(e), "title": cand.title})

    print(f"\n[run] done — {len(produced)} clip(s) in {cfg['clips']['output_dir']}")
    return produced


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="AutoClipper — free multi-niche Shorts/TikTok clipper"
    )
    parser.add_argument("--config", default=None, help="Path to config.yaml")
    parser.add_argument("--ui", action="store_true", help="Launch Gradio web UI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument(
        "--auto",
        action="store_true",
        help="Autopilot: discover + clip niches with no babysitting",
    )
    parser.add_argument(
        "--niche",
        action="append",
        default=None,
        help="Niche id (repeatable): " + ", ".join(list_niche_ids()),
    )
    parser.add_argument(
        "--custom-keywords",
        default="",
        help="Comma-separated keywords when using --niche custom",
    )
    parser.add_argument("--ranking", action="store_true", help="Build a Top-N ranking video for one niche")
    parser.add_argument("--topic", default=None, help="Ranking topic (auto-suggest if omitted)")
    parser.add_argument("--rank-n", type=int, default=5, help="Number of ranking items")
    parser.add_argument("--clips-per-niche", type=int, default=2)
    parser.add_argument("--interval", type=float, default=0.0, help="Autopilot hours between loops (0=once)")
    parser.add_argument("--once", action="store_true", help="Single pass (CLI default)")
    parser.add_argument("--every", type=float, default=None, metavar="HOURS")
    parser.add_argument("--max-videos", type=int, default=2)
    parser.add_argument("--no-whisper", action="store_true")
    parser.add_argument("--fast", action="store_true")
    parser.add_argument("--links-only", action="store_true")
    vo_group = parser.add_mutually_exclusive_group()
    vo_group.add_argument("--voiceover", action="store_true")
    vo_group.add_argument("--no-voiceover", action="store_true")
    parser.add_argument("--no-copyright-safe", action="store_true")
    parser.add_argument("--no-intro-vo", action="store_true", help="Disable header intro TTS")
    args = parser.parse_args(argv)

    if args.ui:
        from .ui import launch_ui

        launch_ui(
            config_path=args.config,
            host=args.host,
            port=args.port,
            open_browser=not args.no_browser,
        )
        return 0

    if args.ranking:
        from .ranking import build_ranking_video, suggest_topic
        niches = args.niche or ["gta6"]
        niche = niches[0]
        out = build_ranking_video(
            niche,
            topic=args.topic,
            n=max(3, min(7, args.rank_n)),
            config_path=args.config,
        )
        print(out or "[ranking] failed")
        return
    if args.auto:
        from .autopilot import run_autopilot

        niches = args.niche or ["gta6", "anime"]
        custom = [k.strip() for k in args.custom_keywords.split(",") if k.strip()]
        run_autopilot(
            niches=niches,
            clips_per_niche=args.clips_per_niche,
            max_videos=args.max_videos,
            interval_hours=float(args.interval or 0),
            custom_keywords=custom or None,
            config_path=args.config,
            once=True if not args.interval else False,
        )
        return 0

    cfg = load_config(args.config)
    if args.niche and len(args.niche) == 1:
        custom = [k.strip() for k in args.custom_keywords.split(",") if k.strip()]
        cfg = apply_niche_to_config(cfg, args.niche[0], custom_keywords=custom or None)
        # force outputs/<niche>
        from pathlib import Path as P
        root = P(cfg.get("_root") or ".")
        cfg["clips"]["output_dir"] = str((root / "outputs" / cfg["niche_id"]).resolve())

    if args.links_only:
        cfg.setdefault("search", {})["keywords"] = []
    if args.voiceover:
        cfg.setdefault("voiceover", {})["enabled"] = True
    if args.no_voiceover:
        cfg.setdefault("voiceover", {})["enabled"] = False
    if args.no_intro_vo:
        cfg.setdefault("voiceover", {})["intro_header"] = False
    if args.no_copyright_safe:
        cfg.setdefault("clips", {}).setdefault("copyright_safe", {})["enabled"] = False
    if args.fast:
        cfg.setdefault("analysis", {})["whisper_model"] = "tiny"

    interval = args.every
    if interval is None and not args.once:
        interval = None

    def pass_once() -> None:
        run_once(
            cfg,
            max_videos=args.max_videos,
            skip_whisper=True if args.no_whisper else None,
        )

    if interval and interval > 0:
        print(f"[scheduler] running every {interval} hours (Ctrl+C to stop)")
        while True:
            pass_once()
            print(f"[scheduler] sleeping {interval}h…")
            try:
                time.sleep(interval * 3600)
            except KeyboardInterrupt:
                print("\n[scheduler] stopped")
                return 0
    else:
        pass_once()
    return 0


if __name__ == "__main__":
    sys.exit(main())
