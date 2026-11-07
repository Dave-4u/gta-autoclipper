# Cutroom automatic plans

Sources and Clips are no longer user controls. `/api/plan` and the start response
expose a read-only plan; legacy `max_videos`, `max_clips`, and `rank_n` request
fields are ignored, including malformed values. Unsupported modes/niches return 422.

- Single: one source, one clip (not two clips per niche).
- Autopilot: two output clips total for the selected niche, allowing up to two
  clips from one source. Anime/animation permit three processed sources;
  other enabled niches permit two. This uses existing autopilot accounting:
  failed downloads/no-highlight skips don't consume the processed-source counter.
- Ranking: five final parts, up to 24 candidate probes and 12 collected usable
  sources (five plus seven replacement spares). Gates/render failures consume
  spares; discovery results are not treated as the number of final parts.
  Existing discovery passes, worker limits and deadline/assembly reserve remain.
  Five is a target, not a guarantee: existing partial-result behavior can return
  Top 3/4 and honestly retitles it, or fail when too few survive.

Existing CPU profiles, individual operation timeouts and job watchdogs still
apply. No new render/discovery algorithms, episode-footage guarantees or
copyright-safety guarantees are introduced. Direct CLI expert count flags
continue to work; only Cutroom uses the hidden `--automatic-plan` ranking flag.

Offline verification:

```
.venv/bin/python -m unittest discover -s tests -p test_automatic_plan.py -v
.venv/bin/python tests/test_gemini_pipeline.py
node --check gta_autoclipper/web/static/cutroom.js
```

## Episode quality policy v2

Runtime-only migration is shared by config.load_config (UI and CLI); it never
rewrites keys, cookies, voice or watermark preferences. Legacy ultrafast and
CRF>20 performance settings migrate to veryfast/CRF20. Low-CPU remains 720x1280,
tiny Whisper, single-pass low-resolution background blur; selected CPU/profile,
source policy and framing are logged without credentials. All modes retain the
automatic count rules above. No extra user count knobs were introduced.

For anime/animation:
- Exact publisher handles (see source_policy.py), or user-authorized channel
  IDs/handles in source_policy.authorized_channels, plus episode/scene/clip
  metadata are required. Display names and 'official' in a title aren't proof.
- AMV/edit/compilation/fancam/montage/reaction/shorts/repost signals are rejected,
  even from a publisher. Trailers/highlights are not treated as episodes.
- Explicit local file paths in links.txt are supported (relative to that file).
  Local supply is recorded separately, not inferred from a cached download.
- source_kind, uploader_status and fan/official origin are distinct concepts.
  Explicitly authorized original fan animations remain separate from episodes.
  A fan request cannot silently switch to official, or vice versa.
- No eligible source means an actionable failure, never a fallback to an edit.
  Exact metadata gates are conservative; missing platform handles can reject
  legitimate sources. Uploader provenance is NOT a license or copyright clearance.

### Scene prompt and enforced behavior

The new selector replaces loudness-only anime/animation picking. It samples eight
chronological 384px frames in at most the first 90 seconds, requests ONE continuous
same-scene/characters/action thread with setup and payoff, explicitly rejects AMVs,
recaps, social overlays, speed ramps and unrelated cuts, and asks for complete beats with natural boundaries.
JSON must affirm original_scene=true and coherent=true and contain valid bounds.
Ranking target is 18–28s; other clips 30–45s. Bounds must align within 2s to detected
-35dB / 0.18s audio pauses first. Continuous-audio action may instead use nearby
FFmpeg-detected shot cuts, only with >=0.85 continuity confidence, confirmed natural
boundaries and explicit no-dialogue evidence. Dialogue requires detected sentence/word
gaps, not arbitrary cuts. Proxy/file edges are not assumed safe. No key, malformed answer or unavailable validation fails
closed, without a loudness fallback. A validated scene is never expanded/capped
across new boundaries or stitched into distant moments. Render requires source
provenance AND the coherent pause/shot/word-gap-aligned selection marker.

Final Gemini QA additionally rejects edit overlays, compilation transitions,
unconnected characters/locations, missing setup/payoff and cropped main combatants.
For episode niches, ranking no longer soft-passes that final mismatch gate.
These are sampled-frame and audio/shot-boundary checks, NOT perfect temporal understanding
or linguistic sentence detection. False rejections and missed mid-word errors
are possible; review the output. They cannot establish licensing or fair use.

### Stable adaptive framing

Near-native portrait (<=2% aspect loss) can crop. Other footage only crops when
all three 384px samples provide confident action/character union boxes that fit
inside 9:16 with 5% horizontal margin. Wide/two-character/uncertain/failed evidence
retains the ENTIRE image centered above a blurred duplicate. Choice and crop
center stay fixed for the segment, not jittery per-frame tracking. Foreground
keeps aspect ratio; no stretching or extra zoom. Background blur is computed at
180px width (clamped 90–240), sigma 5 (clamped 2–6), then enlarged. Existing
caption/rank/watermark layers and preferences are retained. Sparse evidence can
miss motion between samples; uncertain inputs default to full fit.

Offline fixture: `PYTHONPATH=. .venv/bin/python scripts/render_framing_fixture.py`
produces clearly labeled synthetic tests, not an episode-source validation.

## Windows review and automatic ranking budget follow-up

The reported PC was already veryfast/CRF20/720p; that evidence does NOT support
blaming ultrafast for its failed output. Code audit found `mkstemp(...)[1]` in both
scene-frame extraction and face-layout extraction: the returned file descriptor
was discarded without closing it. Windows can then refuse FFmpeg/read/delete
access. Both paths now use a shared close-before-use helper and finally cleanup;
partial extraction failure cleans previously created frames. Frame extraction has
30s per-frame and review probing 15s timeouts.

Required clip/header/style review now rejects exceptions, missing frames/key,
skipped QA and malformed/nonaffirmative answers. A review failure is not a
successful ranking part even when the separate expensive video-upload gate is
disabled. CLI ranking failure now exits 1, not 0 with zero files.

The prior automatic path preferred `ranking.timeout_seconds` (often 1500) over
performance/job 5400 values. Automatic UI/CLI now share 5280s internal work budget
plus 120s outer grace under the existing 5400s process wall. `--fast` cannot shorten
this automatic budget. Expert direct CLI `--deadline` remains an explicit finite
override. Abort/watchdog controls are unchanged. After each part, remaining-time
planning uses max(last part time, mean part time) * 1.25 and an assembly reserve
of at least 300s, logs remaining parts and measured estimates, and stops before
exhaustion. It targets five but cannot guarantee five if sources/QA/time fail.

The initial flat-color framing fixture concealed the blur/crop; no actual layout
bug was reproduced. The fixture now uses textured video. Numerical checks compare
full fitted foreground and edge-to-edge crop against independently scaled/cropped
references, require texture on both crop edges (no pillarboxing), and compare
blurred versus sharp background edge gradients. Review artifacts remain synthetic.


## Episode recovery / bounded downloads

Anime/animation lazily try at most three **separate** sections at 18%, 50%, 78%
of an episode (short/unknown duration starts at zero). Default 60s, configurable
45–90s; never stitch these windows. A valid scene ends the search immediately.
Each source has a shared 240s download/selection deadline, further limited by the
run budget minus a 300s finishing reserve. Automatic ranking still targets five
with its existing 5280s internal / 5400s outer limit; no count controls return.
Scheduled autopilot cycles get fresh run/circuit state.

The download supervisor observes increased file bytes or FFmpeg media position:
45s without real progress is a stall, but an active slow download continues until
the finite source deadline. Warning chatter is not progress. Socket timeout15s;
yt-dlp's nested retries are disabled; at most two outer attempts, only for transient
network/stall errors and only if >=30s remains. No second lighter-quality attempt:
section formats remain >=720p, <=1080p, non-AV1. Forced-keyframe reencoding on section
downloads was removed. Exact cache entries must probe as usable >=720p media;
accidental full-file results are rejected. Never accept a different cached window.
Failed IDs are skipped for the run; three consecutive download failures open a
circuit. Two consecutive provider failures open a separate provider circuit.

Scene replies have at most two calls within a shared 40s wall / remaining source
time (20s request cap). Invalid JSON/empty/provider errors and invalid timestamps
are distinct from a real content rejection. A valid coherent verdict with invalid
timestamps gets one constrained correction, not silent truncation or arbitrary
clamping. Content rejection is not retried on the same window. Provider failure
stops new-window attempts; genuine content/boundary rejection may try the next.
Boundary FFmpeg scans cap45s and remaining source time. Tiny Whisper word-gap
analysis runs in a disposable subprocess capped40s/remaining time and is cancelled
with its process tree, rather than leaving unbounded native inference running.
Logs expose reason enums, bytes, media position and remaining time, not raw
provider/yt-dlp stderr, cookie paths or signed URLs. Missing output/cache/temp
parents are recreated. Original footage and niche/required QA gates stay strict.

### Reproduction evidence (Linux box, not Windows verification)

Artifacts: `/workspace/outputs/episode_recovery/` (not committed).
A public source that passed the publisher-handle/original-scene metadata gate
(`RzClCJFpSoM`) downloaded a real45s 1280x720 section in31.92s, including increasing
bytes/media-position logs. The initial selection was rejected, then the revised
prompt revealed a coherent/original verdict with invalid duration. With the bounded
timestamp correction, it selected4.60–28.44s at natural shot boundaries in6.00s.
A production-adaptive full-fit render and required header/style QA passed in10.99s:
`live_scene_review_sample.mp4`, 23.840s, H264/AAC,720x1280. Style QA identified
**Boruto: Naruto Next Generations**. See `live_render_review.json`. This verifies
one download/selection/framing/QA path, NOT a full UI/Top5 run, captions, VO or Windows.
No footage is copyright-cleared by these checks; regional/network access can differ.

`PYTHONPATH=. .venv/bin/python scripts/reproduce_episode_boundaries.py` produces a
clearly labelled synthetic continuous-sine fixture. Actual FFmpeg finds no silence
but shot cuts at5s/35s, and accepts the30s coherent action window (3.98s original
measurement). This is boundary mechanics, not Gemini semantic verification.
`scripts/reproduce_episode_sample.py` reproduces the render/QA from the existing
live section and accepted selection JSON; it removes the output if required QA fails.

PC log timeouts and the restrictive pause-only code path are confirmed evidence;
the precise Windows/network cause behind FFmpeg3436169992 is still unknown.
Removing forced reencoding is a plausible performance improvement, not proof of
that specific exit code's cause. No PC changes or Windows runtime tests were made.
