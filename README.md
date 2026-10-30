# GTA AutoClipper

**Free, self-hosted** Python tool that finds GTA 6 / GTA RP content, picks highlights, and renders **ready-to-post 9:16 Shorts/TikTok clips** (blurred background, smart header text, burned-in captions, **AI voiceover**, loudness normalize).

No paid APIs. Optional local [Ollama](https://ollama.com) for smarter headers/scripts; rule-based fallback if Ollama is offline. Voiceover uses **Piper TTS** on CPU (auto-downloads a voice on first run) with optional **edge-tts** online fallback.

> Built for clipping **GTA 6 trailers, news, leaks, streamer reactions, and GTA RP** for YouTube Shorts / TikTok-style posting.

---

## What it does

1. **Finds** videos via `yt-dlp` search using keywords in `config.yaml`, ranked by **recency + views**. Also reads URLs from `links.txt`.
2. **Downloads** with `yt-dlp` (skips IDs already in `history.json`).
3. **Picks highlights** using free signals: loudness, scene changes, motion, optional **faster-whisper** keywords.
4. **Renders** 1080×1920 MP4s with **copyright-safer defaults** (~18s source, slight zoom/crop, 1.05× speed, commentary text layer), smart hook, **colored word-by-word captions**, optional **AI voiceover** (ducks original audio), loudnorm.
5. **Writes** a `.txt` / `.json` sidecar per clip with caption + hashtags.
6. **Optional loop**: `--every 6` to re-run every 6 hours.

### Smart header styles (not only POV)

Each clip picks a style from transcript + title + scene type (POV / When you / Hot take / Question / Hype / Reaction / Info). Styles are varied. If **Ollama** is running locally, it drafts the line; otherwise rules generate it.

### AI voiceover (free, CPU)

On by default. Generates a short hype/commentary script that matches the clip, speaks it with **Piper** (`en_US-ryan-high` by default), ducks the original track under the voice, and burns **karaoke captions for the voiceover lines**.

```bat
REM Voiceover on (default)
run.bat --once --max-videos 1

REM Turn voiceover off for one run
run.bat --once --no-voiceover --max-videos 1

REM Force voiceover on even if config disabled it
run.bat --once --voiceover --max-videos 1
```

Config knobs in `config.yaml` → `voiceover:` (`enabled`, `voice`, `volume`, `script_style`, `sidechain`, …).

First voiceover run downloads the Piper model into `models/piper/` (~60MB). Needs no GPU.

### Copyright-safer defaults

To reduce YouTube Content ID “exceeds length limits” blocks, each clip by default:

- Uses **at most ~18 seconds** of source footage
- Applies a **slight zoom/crop** and **1.05× speed**
- Adds a **commentary text layer** under the hook

Turn off with `--no-copyright-safe` or `clips.copyright_safe.enabled: false`. This does **not** guarantee fair use — see legal notes below.

---

## Requirements

- **Python 3.10+**
- **ffmpeg** + **ffprobe** on PATH
- ~2 GB disk for whisper + Piper models on first run
- (Optional) [Ollama](https://ollama.com) + a small model e.g. `ollama pull llama3.2`

---

## Install (Windows)

1. Install **Python** from https://www.python.org/downloads/  
   - Check **“Add python.exe to PATH”**.
2. Install **ffmpeg**:
   - Download a release from https://www.gyan.dev/ffmpeg/builds/ (essentials zip).
   - Unzip, add the `bin` folder to system PATH.
   - Open a **new** Command Prompt and run: `ffmpeg -version`
3. Clone or unzip this project, e.g. `%USERPROFILE%\gta-autoclipper`
4. Double-click **`run.bat`** once (creates `.venv` and installs deps), **or**:

```bat
cd %USERPROFILE%\gta-autoclipper
run.bat --once --max-videos 1
```

### Update an existing install

```bat
cd %USERPROFILE%\gta-autoclipper
git pull
run.bat
```

`run.bat` re-runs `pip install -r requirements.txt` so new packages (Piper / edge-tts) install automatically.

### Install (Linux)

```bash
sudo apt update && sudo apt install -y python3 python3-venv python3-pip ffmpeg
cd gta-autoclipper
chmod +x run.sh
./run.sh --once --max-videos 1
```


---

## Web UI (Gradio, free, local)

Double-click **`ui.bat`** (Windows) or run **`./ui.sh`** (Linux). That creates/uses `.venv`, installs deps, and opens **http://127.0.0.1:7860** in your browser.

```bat
cd %USERPROFILE%\gta-autoclipper
git pull
ui.bat
```

Or from the CLI:

```bat
run.bat --ui
```

In the UI you can:

1. Paste one or more YouTube (or TikTok/Facebook) URLs — or tick **Auto-discover** for GTA 6 search keywords from `config.yaml`
2. Toggle **AI voiceover** (pick a Piper voice), **captions**, **copyright-safe mode**, **fast mode**, **join all clips**
3. Set how many clips per video / source videos
4. Click **Make Clips** and watch the live log
5. Preview finished files in the gallery, download them, or click **Open outputs folder**

The UI never needs a GPU. Keep the terminal/cmd window open while you use it (Ctrl+C to stop).

---

## Usage

```bash
# One pass, process up to 2 new videos
./run.sh --once --max-videos 2

# Faster whisper (tiny model)
./run.sh --once --fast --max-videos 1

# Disable whisper (placeholders only — not recommended)
./run.sh --once --no-whisper --max-videos 1

# Voiceover off / on
./run.sh --once --no-voiceover --max-videos 1
./run.sh --once --voiceover --max-videos 1

# Only URLs in links.txt
./run.sh --once --links-only

# Auto-repeat every 6 hours
./run.sh --every 6 --max-videos 2
```

Windows: same flags with `run.bat`.

Outputs land in **`outputs/`** as:

- `VIDEOID_clip01.mp4` — vertical edited clip  
- `VIDEOID_clip01.txt` — title / caption / hashtags to paste  
- `VIDEOID_clip01.json` — same as structured data  
- `VIDEOID_clip01.hook.json` — header + voiceover metadata  

Downloaded sources stay in **`downloads/`**. Processed IDs are stored in **`history.json`**.

---

## Config

Edit **`config.yaml`**:

- `search.keywords` — GTA 6 trailer / news / leaks / reactions / RP queries  
- `clips.copyright_safe` — short / zoom / speed / commentary layer  
- `voiceover.*` — enable, Piper voice, volume, script style, ducking  
- `hooks.use_ollama` — set `false` to force rule-based headers/scripts  
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
- Transformative commentary with clear added value (voiceover + text help, but are not a guarantee)  
- Checking each platform’s monetization and copyright policies  
- Giving credit / linking originals when appropriate  

The authors of this tool provide **no warranty** and accept **no liability** for strikes, takedowns, or demonetization.

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `yt-dlp` fails on Facebook/TikTok | Update: `pip install -U yt-dlp`. Prefer YouTube search or public links. |
| Whisper slow | First run downloads the model. Or use `--fast` / `--no-whisper`. |
| Piper voice download slow | First voiceover run only. Voice is cached in `models/piper/`. |
| No ffmpeg | Install ffmpeg and reopen the terminal. |
| Empty results | Clear `history.json`, or add URLs to `links.txt`. |
| Ollama unused | Normal if Ollama isn’t running — rule-based hooks/scripts still work. |
| Content ID length block | Keep `copyright_safe` on; prefer reactions/news; add your own commentary. |

---

## Project layout

```
gta-autoclipper/
  config.yaml          # keywords, clip settings, voiceover, hooks
  links.txt            # optional source URLs
  requirements.txt
  run.bat / run.sh
  ui.bat / ui.sh        # Gradio web UI
  gta_autoclipper/     # Python package
  downloads/           # sources
  outputs/             # finished vertical clips
  models/piper/        # Piper voices (auto-downloaded)
  history.json         # created on first run
```

No paid cloud APIs. No account required beyond whatever `yt-dlp` needs for a given site.
