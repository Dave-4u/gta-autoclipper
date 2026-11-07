"""Bounded Cutroom plans. CLI expert counts remain independent."""
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class AutomaticPlan:
    mode: str
    niche: str
    source_limit: int
    output_target: int
    clips_per_video: int
    probe_limit: int = 0
    spare_items: int = 0

    @property
    def summary(self):
        if self.mode == 'ranking':
            return ('Automatic: target Top 5; probe up to 24 candidates, collect up to 12 '
                    'usable sources and replace rejected parts within a 90-minute hard budget. '
                    'A shorter ranking is reported if fewer survive.')
        if self.mode == 'autopilot':
            return (f'Automatic: target {self.output_target} clips total; try up to '
                    f'{self.source_limit} processed sources, up to 2 clips per source, within the time budget.')
        return 'Automatic: 1 source, 1 clip; scene selection stays automatic.'

    def as_dict(self):
        return dict(asdict(self), summary=self.summary)


def automatic_plan(mode: str, niche: str) -> AutomaticPlan:
    from .niches import niche_is_enabled, NICHES
    mode = (mode or 'single').lower()
    mode = 'autopilot' if mode == 'auto' else mode
    if mode not in {'single', 'ranking', 'autopilot'}:
        raise ValueError('Unknown mode')
    if niche not in NICHES or not niche_is_enabled(niche):
        raise ValueError('Unknown or disabled niche')
    if mode == 'ranking':
        return AutomaticPlan(mode, niche, 12, 5, 1, 24, 7)
    if mode == 'autopilot':
        return AutomaticPlan(mode, niche, 3 if niche in {'anime', 'animation'} else 2, 2, 2)
    return AutomaticPlan(mode, niche, 1, 1, 1)


# One finite wall for automatic Cutroom rankings. Legacy config timeout=1500 must
# not override this; CLI expert deadlines remain explicit, finite overrides.
AUTO_RANKING_HARD_SECONDS = 5400
AUTO_RANKING_GRACE_SECONDS = 120


def ranking_timing(cfg, *, automatic=False, deadline_s=None):
    import math
    if deadline_s is not None and not math.isfinite(float(deadline_s)):
        raise ValueError("Ranking deadline must be finite")
    if automatic:
        budget = AUTO_RANKING_HARD_SECONDS - AUTO_RANKING_GRACE_SECONDS
        if deadline_s is not None:
            budget = max(1.0, min(float(deadline_s), budget))
        return float(budget), float(AUTO_RANKING_HARD_SECONDS)
    ranking = cfg.get('ranking') or {}
    budget = float(deadline_s if deadline_s is not None else ranking.get('timeout_seconds', 1500) or 1500)
    if not math.isfinite(budget):
        raise ValueError("Ranking budget must be finite")
    return max(1.0, budget), max(1.0, budget) + 300


def remaining_render_budget(part_times, configured_reserve=150):
    # Actual per-part wall includes inference/Whisper/VO/encoding. Use observations
    # rather than assuming the box's speed; keep a finite conservative assembly reserve.
    estimate = max(part_times[-1], sum(part_times)/len(part_times)) if part_times else 180.0
    return estimate * 1.25, max(300.0, float(configured_reserve))
