# GTA AutoClipper

**Free, self-hosted** Python tool that finds GTA 6 / GTA RP content, picks highlights, and renders **ready-to-post 9:16 Shorts/TikTok clips** (blurred background, smart header text, burned-in captions, loudness normalize).

No paid APIs. Optional local [Ollama](https://ollama.com) for smarter headers; rule-based fallback if Ollama is offline.

> Built for clipping **GTA 6 trailers, news, leaks, streamer reactions, and GTA RP** for YouTube Shorts / TikTok-style posting.

---

## What it does

1. **Finds** videos via `yt-dlp` search (`ytsearch` + optional date-sorted results URL) using keywords in `config.yaml`, ranked by **recency + views**. Also reads URLs from `links.txt` (YouTube, TikTok, Facebook, etc.).
2. **Downloads** with `yt-dlp` (skips IDs already in `history.json`).
3. **Picks highlights** using free signals: audio loudness peaks, scene-change density (PySceneDetect), motion (OpenCV), and optional **faster-whisper** transcript keywords.
4. **Renders** 1080×1920 MP4s (15–60s): blurred fill, **smart hook** for the first 2s, **colored word-by-word captions** (vocal bandpass pre-filter + whisper `base` by default so dialogue under music is kept), loudnorm.
5. **Writes** a `.txt` / `.json` sidecar per clip with caption + hashtags.
6. **Optional loop**: `--every 6` to re-run every 6 hours.

### Smart header styles (not only POV)

Each clip picks a style from transcript + title + scene type:

| Style | Example |
|-------|---------|
| POV | `POV: Rockstar drops something again` |
| When you… | `When GTA RP goes too far` |
| Hot take | `Hot take: GTA 6 already won` |
| Question | `Did Rockstar just spoil GTA 6?` |
| Hype | `GTA 6 IS COMING` |
| Reaction | `Bro wasn't ready for this` |
| Info / news | `New GTA 6 detail dropped` |

Styles are varied (recent styles are avoided). If **Ollama** is running locally, it drafts the line; otherwise rules generate it.

---

## Requirements

- **Python 3.10+**
- **ffmpeg** + **ffprobe** on PATH
- ~2 GB disk for models/downloads on first whisper run
- (Optional) [Ollama](https://ollama.com) + a small model e.g. `ollama pull llama3.2`

---

## Install (Windows)

1. Install **Python** from https://www.python.org/downloads/  
   - Check **“Add python.exe to PATH”**.
2. Install **ffmpeg**:
   - Download a release from https://www.gyan.dev/ffmpeg/builds/ (essentials zip).
   - Unzip, add the `bin` folder to system PATH.
   - Open a **new** Command Prompt and run: `ffmpeg -version`
3. Unzip this project anywhere, e.g. `C:\gta-autoclipper`
4. Double-click **`run.bat`** once (creates `.venv` and installs deps), **or** in Command Prompt:

```bat
cd C:\gta-autoclipper
run.bat --once --max-videos 1
```

### Install (Linux)

```bash
sudo apt update && sudo apt install -y python3 python3-venv python3-pip ffmpeg
cd gta-autoclipper
chmod +x run.sh
./run.sh --once --max-videos 1
```

---

## Usage

```bash
# One pass, process up to 2 new videos
./run.sh --once --max-videos 2

# Faster whisper (tiny model; captions still burned)
./run.sh --once --fast --max-videos 1

# Disable whisper entirely (placeholders only — not recommended)
./run.sh --once --no-whisper --max-videos 1

# Only URLs you paste into links.txt
./run.sh --once --links-only

# Auto-repeat every 6 hours
./run.sh --every 6 --max-videos 2
```

Windows: same flags with `run.bat`.

Outputs land in **`outputs/`** as:

- `VIDEOID_clip01.mp4` — vertical edited clip  
- `VIDEOID_clip01.txt` — title / caption / hashtags to paste  
- `VIDEOID_clip01.json` — same as structured data  
- `VIDEOID_clip01.hook.json` — which header style was used  

Downloaded sources stay in **`downloads/`**. Processed IDs are stored in **`history.json`** so they are not repeated.

---

## Config

Edit **`config.yaml`**:

- `search.keywords` — GTA 6 trailer / news / leaks / reactions / RP queries  
- `search.sort_by` — `recent_views` (default), `views`, or `recent`  
- `clips.*` — duration, loudness, resolution  
- `hooks.use_ollama` — set `false` to force rule-based headers  
- `hashtags` — appended to each sidecar caption  

Paste source links (one per line) into **`links.txt`**.

---

## Reposting rights / legal (read this)

This tool is for **personal automation and learning**. Uploading clipped gameplay or someone else’s trailer/reaction to monetized Shorts/TikTok can violate:

- **Copyright** (Rockstar / Take-Two; the original uploader)
- Platform **reuse / fair use** rules (fair use is narrow and not automatic)
- Creator **rights** if you re-upload their reaction or RP stream without permission

**You** are responsible for what you post. Prefer:

- Content you own or have a license to use  
- Transformative commentary with clear added value (not a raw re-upload)  
- Checking each platform’s monetization and copyright policies  
- Giving credit / linking originals when appropriate  

The authors of this tool provide **no warranty** and accept **no liability** for strikes, takedowns, or demonetization.

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `yt-dlp` fails on Facebook/TikTok | Update: `pip install -U yt-dlp`. Some sites need cookies; prefer YouTube search or public links. |
| Whisper slow / missing | First run downloads the `tiny` model. Or use `--no-whisper`. |
| No ffmpeg | Install ffmpeg and reopen the terminal. |
| Empty results | Clear `history.json`, or add URLs to `links.txt`, or broaden keywords. |
| Ollama unused | Normal if Ollama isn’t running — rule-based hooks still work. |

---

## Project layout

```
gta-autoclipper/
  config.yaml          # keywords, clip settings, hooks
  links.txt            # optional source URLs
  requirements.txt
  run.bat / run.sh
  gta_autoclipper/     # Python package
  downloads/           # sources
  outputs/             # finished vertical clips
  history.json         # created on first run
```

No paid cloud APIs. No account required beyond whatever `yt-dlp` needs for a given site.
