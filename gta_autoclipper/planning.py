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
                    'usable sources and replace rejected parts within the time budget. '
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
