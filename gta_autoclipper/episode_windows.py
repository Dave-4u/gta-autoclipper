"""Try distinct sections lazily within one source/run budget, never stitch them."""
import time
from pathlib import Path
from .download_watch import SourceHealth


def episode_windows(duration, seconds=60, limit=3):
    duration=float(duration or 0)
    if duration <= seconds*1.5:
        return [0.0]
    latest=max(0,duration-seconds-2)
    starts=[min(latest,max(0,duration*f)) for f in (.18,.50,.78)]
    return list(dict.fromkeys(round(x,2) for x in starts))[:max(1,min(3,int(limit)))]


def prepare_episode(candidate,cfg,analyzer):
    from .download import download_video,probe_duration
    health=cfg.setdefault('_source_health',SourceHealth())
    if not health.allowed(candidate.id):
        print('[episode-window] status=skipped reason=failed_source_or_network_circuit_open')
        return None,[]
    provider_health=cfg.setdefault('_provider_health',SourceHealth(limit=2))
    if not provider_health.allowed(candidate.id):
        print('[episode-window] status=skipped reason=provider_circuit_open')
        return None,[]
    source_cfg=dict(cfg)
    source_cfg['download']=dict(cfg.get('download') or {})
    deadline=min(time.monotonic()+240,float(cfg.get('_run_deadline_monotonic') or time.monotonic()+5400)-300)
    source_cfg['_source_deadline']=deadline
    seconds=min(90,max(45,float(source_cfg['download'].get('max_download_seconds') or 60)))
    source_cfg['download']['max_download_seconds']=seconds
    duration=candidate.duration
    if candidate.source=='local' and duration is None:
        duration=probe_duration(Path(candidate.url))
    for index,start in enumerate(episode_windows(duration,seconds)):
        if deadline-time.monotonic()<15:
            print('[episode-window] status=stopped reason=source_budget')
            break
        source_cfg['download']['section_start_seconds']=start
        print(f'[episode-window] attempt={index+1} window={start:.1f}-{start+seconds:.1f}s remaining={deadline-time.monotonic():.0f}s',flush=True)
        path=download_video(candidate,source_cfg)
        if not path:
            break  # do not repeat network failures at different timestamps
        segments=analyzer(path,source_cfg)
        if segments:
            provider_health.record(candidate.id,True)
            candidate.extra=dict(candidate.extra or {},episode_window_start=start,episode_window_seconds=seconds)
            return path,segments
        status=source_cfg.get('_scene_status','unknown')
        print(f'[episode-window] status=no_selection reason={status}',flush=True)
        if status.startswith('provider_'):
            provider_health.record(candidate.id,False)
        if status.startswith('provider_') or status in {'source_budget','frame_extraction_failed','boundary_validation_unavailable'}:
            break  # provider availability is NOT evidence another section is bad
    return None,[]
