"""Ranking mode: Top-N grounded niche moments ordered by view count (low → high = #N → #1)."""

from __future__ import annotations

import json
import random
import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .analyze import Segment, find_highlights
from .config import ensure_dirs, load_config
from .discover import Candidate, discover
from .download import download_video
from .edit import _escape_drawtext, _fit_header, render_clip
from .entities import extract_entities, event_summary, validate_copy
from .history import History
from .niches import apply_niche_to_config, niche_relevance
from .voiceover import make_intro_vo


TOPIC_TEMPLATES = {
    "gta6": [
        "Top {n} wildest GTA 6 trailer moments",
        "Top {n} funniest GTA 6 reactions",
        "Top {n} craziest GTA 6 gameplay clips",
        "Top {n} GTA 6 moments you missed",
    ],
    "anime": [
        "Top {n} hardest anime fight moments",
        "Top {n} anime scenes that hit different",
        "Top {n} anime fights going crazy",
        "Top {n} anime moments worth rewatching",
    ],
    "reactions": [
        "Top {n} funniest streamer reactions",
        "Top {n} trailer reactions that broke chat",
        "Top {n} streamer meltdown moments",
        "Top {n} reactions that went viral",
    ],
    "gaming": [
        "Top {n} cleanest gaming clutches",
        "Top {n} gaming fails that hurt",
    ],
    "funny": [
        "Top {n} funniest viral moments",
    ],
    "movies": [
        "Top {n} movie trailer moments",
    ],
    "animation": [
        "Top {n} animation scenes that slap",
    ],
}


@dataclass
class RankItem:
    rank: int  # display number (#5 .. #1)
    candidate: Candidate
    path: Path
    segment: Segment
    label: str
    views: int
    header: str = ""


def suggest_topic(niche_id: str, n: int = 5, cfg: dict[str, Any] | None = None) -> str:
    niche = (niche_id or "gta6").lower()
    templates = TOPIC_TEMPLATES.get(niche) or [f"Top {{n}} {niche} moments"]
    # Prefer templates matching recent search keywords if present
    topic = random.choice(templates).format(n=n)
    if cfg:
        kws = list(((cfg.get("search") or {}).get("keywords")) or [])
        if kws and random.random() < 0.5:
            seed = random.choice(kws[:4])
            # Keep niche-locked
            topic = f"Top {n} {seed} moments"
            # Sanitize leak-heavy seeds for gta
            if niche == "gta6" and "leak" in topic.lower():
                topic = f"Top {n} GTA 6 trailer reactions"
    return topic


def _format_views(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M views".replace(".0M", "M")
    if n >= 1_000:
        return f"{n/1_000:.1f}K views".replace(".0K", "K")
    return f"{n} views"


def _topic_search_queries(niche_id: str, topic: str) -> list[str]:
    # Derive searchable phrases from topic + niche defaults
    base = re.sub(r"(?i)^top\s*\d+\s*", "", topic).strip()
    base = re.sub(r"\s+", " ", base)
    qs = [base, topic]
    niche = (niche_id or "").lower()
    if niche == "gta6":
        qs += ["GTA 6 trailer reaction", "GTA 6 gameplay", "GTA VI official trailer"]
    elif niche == "anime":
        qs += ["anime fight scene 4k", "best anime fight moments", "anime edit fight"]
    elif niche == "reactions":
        qs += ["streamer reaction funny", "trailer reaction", "twitch funny reaction"]
    # uniq
    out, seen = [], set()
    for q in qs:
        k = q.lower()
        if k and k not in seen:
            seen.add(k)
            out.append(q)
    return out[:6]


def collect_rank_items(
    niche_id: str,
    topic: str,
    *,
    n: int = 5,
    cfg: dict[str, Any] | None = None,
    log=None,
) -> list[RankItem]:
    def _log(msg: str) -> None:
        print(msg)
        if log:
            log(msg)

    cfg = apply_niche_to_config(cfg or load_config(), niche_id)
    root = Path(cfg.get("_root") or ".")
    cfg.setdefault("search", {})
    cfg["search"]["keywords"] = _topic_search_queries(niche_id, topic)
    cfg["search"]["results_per_keyword"] = max(6, int(cfg["search"].get("results_per_keyword") or 6))
    # Don't reuse history for ranking variety — use a side history file
    root = Path(cfg.get("_root") or ".")
    hist = History(root / "history_ranking.json")
    cfg_rank = dict(cfg)
    # discover uses history from cfg
    # Point history file at ranking-specific path via config
    cfg_rank.setdefault("history", {})
    cfg_rank["history"]["file"] = str((root / "history_ranking.json").resolve())
    cands = discover(cfg_rank, hist)
    _log(f"[ranking] {len(cands)} candidates for topic={topic!r}")

    picked: list[tuple[Candidate, Path, Segment, str]] = []
    seen_ids: set[str] = set()
    for cand in cands:
        if len(picked) >= n:
            break
        if cand.id in seen_ids:
            continue
        ok, rel, reason = niche_relevance(
            niche_id,
            title=cand.title,
            description=str((cand.extra or {}).get("description") or ""),
            channel=str((cand.extra or {}).get("channel") or ""),
            tags=list((cand.extra or {}).get("tags") or []),
        )
        if not ok:
            _log(f"[ranking] skip off-niche ({reason}): {cand.title[:70]}")
            continue
        # Topic soft relevance
        topic_bits = [w for w in re.findall(r"[a-z0-9]+", topic.lower()) if len(w) > 3 and w not in {"top", "best", "most", "moments", "moment"}]
        title_l = cand.title.lower()
        if topic_bits and sum(1 for b in topic_bits if b in title_l) < 1:
            # allow if niche relevance strong
            if rel < 0.5:
                continue
        try:
            path = download_video(cand, cfg_rank)
            if not path:
                continue
            segs = find_highlights(path, cfg_rank)
            if not segs:
                continue
            seg = segs[0]
            es = extract_entities(
                title=cand.title,
                channel=str((cand.extra or {}).get("channel") or ""),
                description=str((cand.extra or {}).get("description") or ""),
                tags=list((cand.extra or {}).get("tags") or []),
                niche_id=niche_id,
            )
            label = event_summary(es, cand.title, "", seg.reason)
            # Short grounded label
            label = re.sub(r"\s+", " ", label).strip()[:36] or "viral moment"
            ok_l, _ = validate_copy(label, es)
            if not ok_l:
                label = "viral moment"
            picked.append((cand, path, seg, label))
            seen_ids.add(cand.id)
            hist.mark(cand.id, {"status": "ranking", "niche": niche_id, "topic": topic})
            _log(f"[ranking] keep {cand.title[:60]} views={cand.view_count} label={label}")
        except Exception as e:
            _log(f"[ranking] error {cand.id}: {e}")
            continue

    if not picked:
        return []

    # Order LOWEST → HIGHEST views so last segment is #1 (most views)
    picked.sort(key=lambda x: x[0].view_count)
    items: list[RankItem] = []
    total = len(picked)
    for i, (cand, path, seg, label) in enumerate(picked):
        rank_num = total - i  # first (lowest views) = #N
        items.append(
            RankItem(
                rank=rank_num,
                candidate=cand,
                path=path,
                segment=seg,
                label=label,
                views=int(cand.view_count or 0),
            )
        )
    return items


def _render_title_card(
    topic: str,
    out_wav_dir: Path,
    cfg: dict[str, Any],
    *,
    seconds: float = 3.0,
) -> tuple[Path, Path | None]:
    """Generate a solid-color title card mp4 + optional intro VO wav."""
    width = int((cfg.get("clips") or {}).get("width", 1080))
    height = int((cfg.get("clips") or {}).get("height", 1920))
    card = out_wav_dir / "title_card.mp4"
    topic_fit = _fit_header(topic, 40)
    topic_esc = _escape_drawtext(topic_fit)
    # VO
    vo_wav = out_wav_dir / "topic_intro.wav"
    intro = make_intro_vo(topic_fit, cfg, vo_wav)
    vo_path = intro.wav_path if intro and intro.wav_path and intro.wav_path.is_file() else None
    dur = seconds
    if vo_path:
        try:
            import wave
            with wave.open(str(vo_path), "rb") as w:
                dur = max(2.5, min(4.5, w.getnframes() / float(w.getframerate() or 1) + 0.4))
        except Exception:
            pass
    font = ""
    # Simple black card with yellow topic
    vf = (
        f"color=c=black:s={width}x{height}:d={dur:.2f},"
        f"drawtext=text='{topic_esc}':fontsize=52:fontcolor=yellow:"
        f"borderw=4:bordercolor=black:x=(w-text_w)/2:y=(h*0.42)"
    )
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"color=c=black:s={width}x{height}:d={dur:.2f}",
        "-vf", f"drawtext=text='{topic_esc}':fontsize=52:fontcolor=yellow:borderw=4:bordercolor=black:x=(w-text_w)/2:y=(h*0.42)",
        "-t", f"{dur:.2f}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30",
        str(card),
    ]
    if vo_path:
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"color=c=black:s={width}x{height}:d={dur:.2f}",
            "-i", str(vo_path),
            "-vf", f"drawtext=text='{topic_esc}':fontsize=48:fontcolor=yellow:borderw=4:bordercolor=black:x=(w-text_w)/2:y=(h*0.40)",
            "-t", f"{dur:.2f}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30",
            "-c:a", "aac", "-shortest",
            str(card),
        ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0 or not card.is_file():
        # Fallback without drawtext quirks
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c=black:s={width}x{height}:d=3",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", str(card)],
            capture_output=True, check=False,
        )
    return card, vo_path


def _render_rank_segment(
    item: RankItem,
    cfg: dict[str, Any],
    out_dir: Path,
    *,
    with_vo: bool = True,
) -> Path | None:
    """Render one ranked clip with #N overlay + label + views badge."""
    # First render base clip via existing pipeline
    base = render_clip(
        item.path,
        item.segment,
        cfg,
        title=item.candidate.title,
        index=item.rank,
        video_id=f"{item.candidate.id}_r{item.rank}",
        source_meta=dict(item.candidate.extra or {}),
    )
    if not base or not base.is_file():
        return None

    # Burn rank overlays on top
    width = int((cfg.get("clips") or {}).get("width", 1080))
    height = int((cfg.get("clips") or {}).get("height", 1920))
    rank_txt = _escape_drawtext(f"#{item.rank}")
    label = _escape_drawtext(_fit_header(item.label, 28))
    views = _escape_drawtext(_format_views(item.views))
    out = out_dir / f"rank_{item.rank:02d}_{item.candidate.id}.mp4"

    vf = (
        f"drawtext=text='{rank_txt}':fontsize=96:fontcolor=yellow:"
        f"borderw=6:bordercolor=black:x=40:y=80,"
        f"drawtext=text='{label}':fontsize=40:fontcolor=white:"
        f"borderw=3:bordercolor=black:x=40:y=200,"
        f"drawtext=text='{views}':fontsize=36:fontcolor=yellow:"
        f"borderw=3:bordercolor=black:x=40:y=260"
    )
    cmd = [
        "ffmpeg", "-y", "-i", str(base),
        "-vf", vf,
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0 or not out.is_file():
        print(f"[ranking] overlay failed: {(proc.stderr or '')[-300:]}")
        return base
    return out


def build_ranking_video(
    niche_id: str,
    topic: str | None = None,
    *,
    n: int = 5,
    config_path: str | None = None,
    with_vo: bool = True,
    log=None,
) -> Path | None:
    def _log(msg: str) -> None:
        print(msg)
        if log:
            log(msg)

    cfg = load_config(config_path)
    ensure_dirs(cfg)
    cfg = apply_niche_to_config(cfg, niche_id)
    # Ranking clips should be short
    cfg.setdefault("clips", {})
    cfg["clips"]["max_clips_per_video"] = 1
    cfg["clips"]["target_duration"] = 12
    cfg["clips"]["min_duration"] = 8
    cfg["clips"]["max_duration"] = 14
    cs = cfg["clips"].setdefault("copyright_safe", {})
    cs["enabled"] = True

    topic = (topic or "").strip() or suggest_topic(niche_id, n, cfg)
    _log(f"[ranking] topic={topic!r} niche={niche_id} n={n}")

    items = collect_rank_items(niche_id, topic, n=n, cfg=cfg, log=log)
    if len(items) < 2:
        _log("[ranking] not enough items")
        return None

    out_root = Path(cfg["clips"]["output_dir"]) / "ranking"
    out_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    work = out_root / f"_work_{stamp}"
    work.mkdir(parents=True, exist_ok=True)

    title_card, _ = _render_title_card(topic, work, cfg)
    parts: list[Path] = [title_card]
    for item in items:
        _log(f"[ranking] render #{item.rank} {item.candidate.title[:50]} ({_format_views(item.views)})")
        seg = _render_rank_segment(item, cfg, work, with_vo=with_vo)
        if seg:
            parts.append(seg)

    if len(parts) < 3:
        _log("[ranking] too few rendered parts")
        return None

    # Normalize AV then concat (avoids sample-rate / stream mismatches)
    final = out_root / f"{niche_id}_top{len(items)}_{stamp}.mp4"
    width = int((cfg.get("clips") or {}).get("width", 1080))
    height = int((cfg.get("clips") or {}).get("height", 1920))
    norm_parts: list[Path] = []
    for i, part in enumerate(parts):
        npth = work / f"norm_{i:02d}.mp4"
        cmd_n = [
            "ffmpeg", "-y", "-i", str(part),
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
            "-filter_complex",
            f"[0:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,fps=30,format=yuv420p[v];"
            f"[0:a]aformat=sample_rates=44100:channel_layouts=stereo[a0];"
            f"[a0][1:a]amix=inputs=2:duration=first:dropout_transition=0[a]",
            "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ar", "44100", "-ac", "2",
            "-shortest", str(npth),
        ]
        pr = subprocess.run(cmd_n, capture_output=True, text=True, check=False)
        if pr.returncode != 0 or not npth.is_file():
            # Simpler path: video only + silent audio
            cmd_n2 = [
                "ffmpeg", "-y", "-i", str(part),
                "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                "-vf", f"scale={width}:{height},fps=30,format=yuv420p",
                "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "libx264", "-preset", "veryfast",
                "-c:a", "aac", "-ar", "44100", "-ac", "2", "-shortest",
                str(npth),
            ]
            subprocess.run(cmd_n2, capture_output=True, check=False)
        if npth.is_file():
            norm_parts.append(npth)
    lst = work / "concat.txt"
    lst.write_text("\n".join(f"file '{pp.resolve()}'" for pp in norm_parts) + "\n", encoding="utf-8")
    cmd = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
        "-c", "copy", "-movflags", "+faststart",
        str(final),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0 or not final.is_file():
        _log(f"[ranking] concat failed: {(proc.stderr or '')[-400:]}")
        return None

    # Cap ~90s
    try:
        import json as _json
        pr = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(final)],
            capture_output=True, text=True, check=False,
        )
        dur = float((_json.loads(pr.stdout).get("format") or {}).get("duration") or 0)
        if dur > 92:
            trimmed = final.with_name(final.stem + "_90s.mp4")
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(final), "-t", "90", "-c", "copy", str(trimmed)],
                capture_output=True, check=False,
            )
            if trimmed.is_file():
                final.unlink(missing_ok=True)
                trimmed.rename(final)
                dur = 90.0
        _log(f"[ranking] final {final} ({dur:.1f}s)")
    except Exception:
        pass

    # Metadata
    hashtags = " ".join((cfg.get("hashtags") or [])[:8])
    meta = {
        "mode": "ranking",
        "niche": niche_id,
        "topic": topic,
        "items": [
            {
                "rank": it.rank,
                "title": it.candidate.title,
                "views": it.views,
                "views_label": _format_views(it.views),
                "label": it.label,
                "url": it.candidate.url,
                "id": it.candidate.id,
            }
            for it in items
        ],
        "hashtags": hashtags,
        "created": datetime.now().isoformat(),
    }
    final.with_suffix(".ranking.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    final.with_suffix(".meta.txt").write_text(
        (
            f"MODE: ranking\nNICHE: {niche_id}\nTOPIC: {topic}\n"
            + "\n".join(
                f"#{it.rank} {it.candidate.title} — {_format_views(it.views)} — {it.label}"
                for it in items
            )
            + f"\nHASHTAGS: {hashtags}\n"
        ),
        encoding="utf-8",
    )
    return final
