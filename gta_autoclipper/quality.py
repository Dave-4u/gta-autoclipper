"""Versioned runtime defaults shared by CLI, UI and old config files (never writes config)."""
from copy import deepcopy

VERSION = 2


def resolve_quality(data):
    cfg = deepcopy(data)
    old = int(cfg.get('quality_policy_version') or 0) < VERSION
    cfg['quality_policy_version'] = VERSION
    # Safety/structure invariants apply even to older configs. Preserve credentials/preferences.
    cfg.setdefault('source_policy', {})['mode'] = 'episode_or_local'
    cfg.setdefault('framing', {})['mode'] = 'adaptive'
    cfg['framing'].setdefault('blur_width', 180)
    cfg['framing'].setdefault('blur_sigma', 5)
    cfg.setdefault('selection', {})['mode'] = 'coherent_scene'
    cfg['selection'].setdefault('max_analyze_seconds', 90)
    cfg.setdefault('montage', {})['enabled'] = False
    cfg.setdefault('clips', {}).setdefault('montage', {})['enabled'] = False
    perf = cfg.setdefault('performance', {})
    if old:
        # Migrate known historical low-quality encoder settings, not user's unrelated prefs.
        if perf.get('preset') == 'ultrafast':
            perf['preset'] = 'veryfast'
        if float(perf.get('crf') or 20) > 20:
            perf['crf'] = 20
    perf.setdefault('preset', 'veryfast')
    perf.setdefault('crf', 20)
    perf.setdefault('maxrate', '3500k')
    perf.setdefault('bufsize', '7000k')
    return cfg


def log_quality(cfg):
    p = cfg.get('performance') or {}
    print(f"[quality] policy=v{VERSION} migrated={cfg.get('_quality_migrated', False)} "
          f"low_cpu={p.get('low_cpu')} encode={p.get('preset')}/crf{p.get('crf')} "
          f"size={p.get('width')}x{p.get('height')} source=episode_or_local "
          "selection=coherent_scene montage=off framing=adaptive(uncertain=full-fit)", flush=True)
