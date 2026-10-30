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


def _fit_header_words(text: str, max_chars: int = 28) -> str:
    """Truncate on word boundaries only — never mid-name."""
    text = re.sub(r"\s+", " ", (text or "").strip())
    if len(text) <= max_chars:
        return text
    words = text.split()
    out: list[str] = []
    for w in words:
        trial = (" ".join(out + [w])).strip()
        if len(trial) > max_chars:
            break
        out.append(w)
    return " ".join(out) if out else text[:max_chars].rsplit(" ", 1)[0]



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



def detect_topic_type(topic: str) -> str:
    """Classify ranking topic into a strict filter type."""
    t = (topic or "").lower()
    if any(k in t for k in ("reaction", "reacts", "react ", "streamer meltdown", "broke chat")):
        return "reactions"
    if any(k in t for k in ("fight", "battle", "vs", "sakuga", "boss")):
        return "fights"
    if any(k in t for k in ("funny", "fail", "meme", "hilarious", "comedy")):
        return "funny"
    if any(k in t for k in ("gameplay", "clutch", "playthrough", "walkthrough")):
        return "gameplay"
    if any(k in t for k in ("trailer", "teaser", "cinematic")) and "reaction" not in t:
        return "trailers"
    if any(k in t for k in ("meltdown", "rage", "unhinged")):
        return "reactions"
    return "general"


def _has_face_cam(path: Path, sample_secs: float = 8.0) -> bool:
    """True if YuNet/upper-third face motion suggests a reaction face-cam."""
    try:
        from .analyze import extract_face_motion_scores
        import numpy as np
        scores = extract_face_motion_scores(path, sample_fps=2.0)
        if scores is None or len(scores) == 0:
            return False
        # Look at first ~sample_secs and overall — need sustained face presence
        head = scores[: max(1, int(sample_secs))]
        return float(np.mean(head)) > 0.35 or float(np.max(scores)) > 0.7
    except Exception as e:
        print(f"[ranking] face-cam check failed: {e}")
        return False


def topic_matches(
    topic_type: str,
    *,
    title: str,
    channel: str = "",
    description: str = "",
    tags: list[str] | None = None,
    has_face_cam: bool | None = None,
) -> tuple[bool, str]:
    """Strict topic relevance. Returns (ok, reason)."""
    blob = f"{title} {channel} {description} {' '.join(tags or [])}".lower()
    title_l = (title or "").lower()
    ch_l = (channel or "").lower()

    # Hard-reject official trailers/uploads for non-trailer topics
    official_signals = (
        "official trailer", "launch trailer", "rockstar games",
        "grand theft auto vi trailer", "gta vi trailer 1", "gta 6 trailer 1",
    )
    is_official = any(s in blob for s in official_signals) or (
        "official" in title_l and "trailer" in title_l and "react" not in title_l
    )
    # Channel is Rockstar / pure game publisher
    publisher_ch = any(p in ch_l for p in ("rockstar", "playstation", "xbox", "nintendo", "crunchyroll"))

    if topic_type == "reactions":
        if is_official and "react" not in title_l:
            return False, "official_trailer"
        if publisher_ch and "react" not in title_l:
            return False, "publisher_channel"
        react_sig = any(
            k in blob
            for k in ("react", "reaction", "reacts", "reacting", "watched", "watching")
        )
        # Known streamer aliases in entities
        from .entities import STREAMER_ALIASES
        streamer_sig = any(a in blob for a, _ in STREAMER_ALIASES) or any(
            k in ch_l for k in ("react", "live", "twitch", "yt ")
        )
        # Channel looks like a person (not a game brand)
        person_ch = bool(ch_l) and not publisher_ch and len(ch_l) > 2
        if not (react_sig or streamer_sig):
            return False, "no_reaction_signal"
        # Face-cam required when we have the file (post-download)
        if has_face_cam is False:
            return False, "no_face_cam"
        if has_face_cam is None and not react_sig:
            # Pre-download: require explicit react in title if no face yet
            if "react" not in title_l and "reaction" not in title_l:
                return False, "need_react_in_title"
        return True, "ok"

    if topic_type == "fights":
        if is_official and "fight" not in title_l:
            return False, "official_not_fight"
        if not any(k in blob for k in ("fight", "battle", "vs", "duel", "boss", "spar")):
            return False, "no_fight_signal"
        return True, "ok"

    if topic_type == "funny":
        if is_official:
            return False, "official_trailer"
        if not any(k in blob for k in ("funny", "fail", "meme", "hilarious", "comedy", "laugh", "prank")):
            return False, "no_funny_signal"
        return True, "ok"

    if topic_type == "gameplay":
        if not any(k in blob for k in ("gameplay", "playthrough", "walkthrough", "clutch", "ranked", "stream")):
            # allow if not clearly a trailer-only upload
            if "trailer" in title_l and "gameplay" not in title_l:
                return False, "trailer_not_gameplay"
        return True, "ok"

    if topic_type == "trailers":
        if "react" in title_l:
            return False, "reaction_not_trailer"
        if not any(k in blob for k in ("trailer", "teaser", "cinematic")):
            return False, "no_trailer_signal"
        return True, "ok"

    # general: reject pure official trailers unless topic asks for them
    if is_official and "trailer" not in (topic or "").lower():
        return False, "official_trailer"
    return True, "ok"


def smart_rank_label(
    *,
    title: str,
    channel: str = "",
    topic_type: str = "general",
    niche_id: str = "",
) -> str:
    """Short label that never truncates mid-name."""
    from .entities import extract_entities, STREAMER_ALIASES, detect_reactor

    es = extract_entities(title=title, channel=channel, niche_id=niche_id)
    title_l = (title or "").lower()

    # Prefer the actual reactor (title says X reacts) — never a random mentioned name
    if topic_type == "reactions" or "react" in title_l:
        # Mashups / multi-reactor comps first — never a single wrong name
        if any(k in title_l for k in (
            "mashup", "compilation", "streamers react", "youtubers react",
            "mega reaction", "reactors react", "everyone react",
        )):
            if es.franchises and any(f.startswith("gta") for f in es.franchises):
                return "GTA 6 reactions"
            return "Reaction mashup"
        reactor = detect_reactor(title, channel)
        if reactor and reactor.lower() not in ("youtubers", "streamers", "everyone"):
            return reactor[:28]
        m = re.search(r"^(.{3,28}?)\s+react", title or "", flags=re.I)
        if m:
            cand = m.group(1).strip(" -_|:")
            if cand.lower() not in ("youtubers", "streamers", "everyone", "big streamers"):
                return cand[:28]
        # Event-focused label if no clear reactor
        for kind in ("trailer", "gameplay", "fight", "ending"):
            if kind in title_l:
                if es.franchises:
                    show = es.franchises[0].replace("_", " ")
                    if show.startswith("gta"):
                        show = "GTA 6"
                    return f"{show} {kind}"[:28]
                return f"{kind} reaction"
        if es.franchises and any(f.startswith("gta") for f in es.franchises):
            return "GTA 6 reaction"
        return "Streamer reaction"

    # Franchise + content type
    if es.franchises:
        show = es.franchises[0].replace("_", " ")
        if show.startswith("gta"):
            show = "GTA 6" if "gta6" in es.franchises or "gta 6" in title_l or "gta vi" in title_l else "GTA"
        else:
            show = show.title()
        for kind in ("trailer", "gameplay", "fight", "teaser", "clutch", "fail"):
            if kind in title_l:
                return f"{show} {kind}"
        if es.characters:
            return f"{es.characters[0].title()}"
        return show

    if es.characters:
        return es.characters[0].title()

    # Clean title: drop noise words, keep whole words only
    clean = re.sub(
        r"(?i)\b(official|hd|4k|shorts?|tiktok|part\s*\d+|finally+|crazy|insane)\b",
        " ",
        title or "",
    )
    clean = re.sub(r"[|\[\]\(\)#_]+", " ", clean)
    clean = re.sub(r"\s+", " ", clean).strip(" -")
    words = clean.split()
    # Take whole words up to ~28 chars — never cut a word
    out: list[str] = []
    for w in words[:6]:
        trial = (" ".join(out + [w])).strip()
        if len(trial) > 28:
            break
        out.append(w)
    label = " ".join(out).strip() or "viral moment"
    return label


def _topic_search_queries(niche_id: str, topic: str) -> list[str]:
    base = re.sub(r"(?i)^top\s*\d+\s*", "", topic).strip()
    base = re.sub(r"\s+", " ", base)
    ttype = detect_topic_type(topic)
    qs = [base, topic]
    niche = (niche_id or "").lower()
    if ttype == "reactions":
        if niche == "gta6":
            qs += [
                "GTA 6 trailer reaction",
                "GTA VI trailer reaction",
                "streamer reacts GTA 6",
                "reacts to GTA 6 trailer",
            ]
        else:
            qs += ["streamer reaction funny", "trailer reaction", "twitch funny reaction"]
    elif ttype == "fights" or niche == "anime":
        qs += ["anime fight scene 4k", "best anime fight moments", "anime edit fight"]
    elif ttype == "gameplay":
        qs += ["GTA 6 gameplay", "gameplay clutch", "gameplay funny"]
    elif ttype == "trailers":
        qs += ["GTA 6 official trailer", "GTA VI trailer"]
    elif niche == "gta6":
        qs += ["GTA 6 trailer reaction", "GTA 6 gameplay"]
    # Never add bare "official trailer" when topic is reactions
    out, seen = [], set()
    for q in qs:
        k = q.lower()
        if ttype == "reactions" and "official trailer" in k and "react" not in k:
            continue
        if k and k not in seen:
            seen.add(k)
            out.append(q)
    return out[:8]



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
    cfg["search"]["results_per_keyword"] = max(8, int(cfg["search"].get("results_per_keyword") or 8))
    ttype = detect_topic_type(topic)
    _log(f"[ranking] topic_type={ttype}")

    hist = History(root / "history_ranking.json")
    cfg_rank = dict(cfg)
    cfg_rank.setdefault("history", {})
    cfg_rank["history"]["file"] = str((root / "history_ranking.json").resolve())
    cands = discover(cfg_rank, hist)
    _log(f"[ranking] {len(cands)} candidates for topic={topic!r}")

    picked: list[tuple[Candidate, Path, Segment, str]] = []
    seen_ids: set[str] = set()
    # Over-fetch then filter strictly
    for cand in cands:
        if len(picked) >= n:
            break
        if cand.id in seen_ids:
            continue
        channel = str((cand.extra or {}).get("channel") or "")
        desc = str((cand.extra or {}).get("description") or "")
        tags = list((cand.extra or {}).get("tags") or [])
        ok, rel, reason = niche_relevance(
            niche_id,
            title=cand.title,
            description=desc,
            channel=channel,
            tags=tags,
        )
        if not ok:
            _log(f"[ranking] skip off-niche ({reason}): {cand.title[:70]}")
            continue
        ok_t, why_t = topic_matches(
            ttype, title=cand.title, channel=channel, description=desc, tags=tags, has_face_cam=None
        )
        if not ok_t:
            _log(f"[ranking] skip topic ({why_t}): {cand.title[:70]}")
            continue
        try:
            path = download_video(cand, cfg_rank)
            if not path:
                continue
            # Post-download face-cam gate for reaction topics
            face = None
            if ttype == "reactions":
                face = _has_face_cam(path)
                ok_f, why_f = topic_matches(
                    ttype, title=cand.title, channel=channel, description=desc, tags=tags, has_face_cam=face
                )
                if not ok_f:
                    _log(f"[ranking] skip after face check ({why_f}): {cand.title[:70]}")
                    continue
            segs = find_highlights(path, cfg_rank)
            if not segs:
                continue
            seg = segs[0]
            label = smart_rank_label(
                title=cand.title, channel=channel, topic_type=ttype, niche_id=niche_id
            )
            picked.append((cand, path, seg, label))
            seen_ids.add(cand.id)
            hist.mark(cand.id, {"status": "ranking", "niche": niche_id, "topic": topic, "topic_type": ttype})
            _log(f"[ranking] keep {cand.title[:60]} views={cand.view_count} label={label!r} face={face}")
        except Exception as e:
            _log(f"[ranking] error {cand.id}: {e}")
            continue

    if not picked:
        return []

    picked.sort(key=lambda x: x[0].view_count)
    items: list[RankItem] = []
    total = len(picked)
    for i, (cand, path, seg, label) in enumerate(picked):
        rank_num = total - i
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
    label = _escape_drawtext(_fit_header_words(item.label, 28))
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
    if len(items) < 3:
        _log(f"[ranking] not enough items ({len(items)}); need >=3")
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
