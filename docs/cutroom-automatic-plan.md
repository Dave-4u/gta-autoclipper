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
