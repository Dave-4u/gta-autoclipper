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
recaps, social overlays, speed ramps and unrelated cuts, and asks for natural pauses.
JSON must affirm original_scene=true and coherent=true and contain valid bounds.
Ranking target is 18–28s; other clips 30–45s. Bounds must align within 2s to detected
-35dB / 0.18s audio pauses; otherwise the candidate is rejected. Proxy/file edges
are not assumed safe. No key, malformed answer or unavailable validation fails
closed, without a loudness fallback. A validated scene is never expanded/capped
across new boundaries or stitched into distant moments. Render requires source
provenance AND the coherent pause-aligned selection marker.

Final Gemini QA additionally rejects edit overlays, compilation transitions,
unconnected characters/locations, missing setup/payoff and cropped main combatants.
For episode niches, ranking no longer soft-passes that final mismatch gate.
These are sampled-frame and audio-pause checks, NOT perfect temporal understanding
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
