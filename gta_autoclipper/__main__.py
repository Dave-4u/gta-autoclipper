"""CLI entry: python -m gta_autoclipper [--once|--every N] [--max-videos N] ..."""

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
        description="GTA AutoClipper — free GTA 6 Shorts/TikTok clipper"
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to config.yaml (default: next to package)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single pass and exit (default if --every not set)",
    )
    parser.add_argument(
        "--every",
        type=float,
        default=None,
        metavar="HOURS",
        help="Repeat every N hours (e.g. 6)",
    )
    parser.add_argument(
        "--max-videos",
        type=int,
        default=2,
        help="Max source videos to process per pass (default 2)",
    )
    parser.add_argument(
        "--no-whisper",
        action="store_true",
        help="Skip faster-whisper (faster; placeholders only — not recommended)",
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help="Use whisper 'tiny' instead of 'base' (faster, less accurate under music)",
    )
    parser.add_argument(
        "--links-only",
        action="store_true",
        help="Only process URLs from links.txt (no YouTube search)",
    )
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    if args.links_only:
        cfg.setdefault("search", {})["keywords"] = []

    interval = args.every
    if interval is None and not args.once:
        # default: single pass
        interval = None

    if getattr(args, "fast", False):
        cfg.setdefault("analysis", {})["whisper_model"] = "tiny"

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
            sleep_s = interval * 3600
            print(f"[scheduler] sleeping {interval}h…")
            try:
                time.sleep(sleep_s)
            except KeyboardInterrupt:
                print("\n[scheduler] stopped")
                return 0
    else:
        pass_once()
    return 0


if __name__ == "__main__":
    sys.exit(main())
