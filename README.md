# AutoClipper (GTA + multi-niche)

**Free, self-hosted, CPU-only** Shorts/TikTok clipper. Discovers trending clips across niches (GTA 6, anime, animation, reactions, gaming, funny/viral, movies), picks peak moments, writes catchy headers with intro voiceover, burns captions, and can run fully hands-free.

No paid APIs. Optional local [Ollama](https://ollama.com) for smarter headers. Voiceover uses **Piper TTS** on CPU (auto-downloads voices) with optional **edge-tts** fallback.

---

## What’s new (smart autopilot)

1. **Multi-niche presets** — `gta6`, `anime`, `animation`, `reactions`, `gaming`, `funny`, `movies`, plus `custom` keywords. Each has search queries, header styles, Piper voice, hashtags, and vocab.
2. **Smart discovery** — yt-dlp search + date-sorted / “new / trending” queries. Scores by **view velocity**, **freshness**, **engagement**, and **title hook**, with jitter so runs vary. Dedupes via `history.json`.
3. **Smart moments** — audio energy, scene-change density, motion, transcript keywords / hype speech. Builds a pool of peaks and **randomly samples varied top moments**.
4. **Header pool** — big niche-specific template banks (POV / When you / hot take / question / hype / reaction / news). Scores for specificity + curiosity, **rejects title-echoes and hollow lines** like `POV: anime`. Prefers headers that fit on-screen. Ollama if present, else rules.
5. **Intro VO** — Piper **speaks the header** over the first ~1–2s with a larger→smaller header animation, then the clip continues. Optional commentary VO + ducking + captions still work.
6. **Whisper quality gates** — drops segments with low `avg_logprob`, high `no_speech_prob` / compression ratio, music-only loops, repeats, or off-niche text. Music-only clips get **no fake captions**.
7. **Reaction peaks** — laughter/scream audio spikes + YuNet/OpenCV face-cam motion; centers the window on the peak with a short lead-in.
6. **Autopilot** — CLI `--auto` and Gradio **Autopilot** tab: loop niches, render N clips each into `outputs/<niche>/` with metadata `.txt`. Optional schedule interval.
7. **Copyright-safer defaults** — ~18s source, slight zoom, 1.05× speed, commentary text layer.

---

## Quick start (Windows)

```bat
cd %USERPROFILE%\gta-autoclipper
git pull
ui.bat
```

Or CLI autopilot (no UI):

```bat
cd %USERPROFILE%\gta-autoclipper
git pull
run.bat --auto --niche gta6 --niche anime --clips-per-niche 2 --fast
```

Linux:

```bash
./ui.sh
# or
./run.sh --auto --niche gta6 --niche anime --clips-per-niche 2 --fast
```

---

## Web UI

`ui.bat` / `./ui.sh` / `run.bat --ui` opens **http://127.0.0.1:7860**.

- **Manual** tab — paste URLs or auto-discover, toggles for commentary VO / captions / copyright-safe / fast / join-all.
- **Autopilot** tab — multi-select niches, clips count, optional repeat interval, **Start / Stop**, live log + gallery.

Keep the terminal open while the UI runs.

---

## CLI cheatsheet

```bash
# Autopilot once across niches
python -m gta_autoclipper --auto --niche gta6 --niche anime --clips-per-niche 2 --fast

# Autopilot every 6 hours
python -m gta_autoclipper --auto --niche gaming --niche funny --interval 6

# Single niche pass
python -m gta_autoclipper --once --niche anime --max-videos 1 --fast

# Custom keywords
python -m gta_autoclipper --auto --niche custom --custom-keywords "one piece edit, jjk fight"

# Disable intro header VO
python -m gta_autoclipper --once --no-intro-vo --max-videos 1
```

Outputs land in **`outputs/<niche>/`** as:

- `VIDEOID_clip01.mp4`
- `.txt` / `.json` caption sidecars
- `.hook.json` — picked header, score, pool samples, intro VO meta
- `.meta.txt` — niche, title, header, caption, hashtags, source

---

## Niches

| Id | Focus |
|----|--------|
| `gta6` | GTA 6 / RP / Rockstar |
| `anime` | Anime fights, trailers, edits |
| `animation` | Cartoons / Pixar / animated |
| `reactions` | Streamer / reaction meltdowns |
| `gaming` | Clutch / fails / trailers |
| `funny` | Viral / comedy / fails |
| `movies` | Trailers / cinema moments |
| `custom` | Your keywords |

---

## Config

See `config.yaml`:

- `search.*` — age window, trending, keywords-per-run  
- `clips.copyright_safe` — short / zoom / speed  
- `hooks.pool_size` / Ollama  
- `voiceover.intro_header` / `intro_seconds` / Piper voice  
- `autopilot.*` — default niches for unattended runs  

---

## Requirements

- Python 3.10+
- ffmpeg + ffprobe on PATH
- ~2 GB disk for whisper + Piper voices on first runs
- (Optional) Ollama + small model

`run.bat` / `ui.bat` create `.venv` and `pip install -r requirements.txt` automatically.

---

## Legal

For personal automation and learning. Uploading others’ footage to monetized Shorts/TikTok can violate copyright and platform rules. Copyright-safer defaults and commentary **do not guarantee** fair use. **You** are responsible for what you post.

---

## Layout

```
gta-autoclipper/
  config.yaml
  run.bat / run.sh
  ui.bat / ui.sh
  gta_autoclipper/
    niches.py      # presets
    discover.py    # smart search + scoring
    analyze.py     # varied peak moments
    hooks.py       # header pool + scoring
    voiceover.py   # intro + commentary TTS
    autopilot.py   # hands-free loop
    ui.py          # Gradio Manual + Autopilot
  downloads/
  outputs/<niche>/
  models/piper/
```
