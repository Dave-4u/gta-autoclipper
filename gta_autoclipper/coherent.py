"""Bounded, continuous scene selection. No distant loudness montage fallback."""
import json
import re
import subprocess
from pathlib import Path

SCENE_PROMPT = '''Select ONE coherent continuous episode scene from these chronological samples.
Times in seconds: {times}. Analyzed duration: {duration}.
Require the SAME scene/characters/action thread, a readable setup and a resolved beat/payoff.
Do not choose unrelated fights, music videos, recaps, reaction overlays, edited shorts,
social captions, speed ramps, or compilation transitions. Ordinary original shot/reverse-shot is OK.
Choose natural entry/exit pauses; never begin/end mid-dialogue or mid-action payoff.
Return ONLY JSON: {{"original_scene":true,"coherent":true,"start":0,"end":30,"reason":"setup -> payoff"}}.
Set original_scene=false or coherent=false if uncertain. Aim for {minimum}-{maximum} seconds.
These sparse samples cannot establish continuity by themselves: if a connecting event is not evident, reject.
'''


def validate_window(data, duration, minimum, maximum):
    if data.get('original_scene') is not True or data.get('coherent') is not True:
        return None
    try:
        start, end = float(data['start']), float(data['end'])
    except (KeyError, ValueError, TypeError):
        return None
    if not (0 <= start < end <= duration and minimum <= end - start <= maximum):
        return None
    return start, end


def pause_boundaries(path, duration):
    pr = subprocess.run(['ffmpeg', '-v', 'info', '-i', str(path), '-t', str(duration),
                         '-vn', '-af', 'silencedetect=noise=-35dB:d=0.18', '-f', 'null', '-'],
                        capture_output=True, text=True, timeout=45, check=False)
    if pr.returncode:
        return []
    starts = [float(x) for x in re.findall(r'silence_start: ([\d.]+)', pr.stderr)]
    ends = [float(x) for x in re.findall(r'silence_end: ([\d.]+)', pr.stderr)]
    # A proxy edge is NOT evidence of a pause (the section may start mid-sentence).
    return [(a + b) / 2 for a, b in zip(starts, ends) if b >= a]


def align_to_pauses(window, pauses, duration, minimum, maximum):
    if not pauses:
        return None
    start, end = window
    a = min(pauses, key=lambda t: abs(t-start))
    b = min(pauses, key=lambda t: abs(t-end))
    if abs(a-start) > 2 or abs(b-end) > 2:
        return None
    return validate_window(dict(original_scene=True, coherent=True, start=a, end=b), duration, minimum, maximum)


def select_scene(path: Path, cfg):
    from .analyze import Segment, _video_duration
    from .gemini_client import generate, has_gemini
    from .scene_vlm import grab_frame_files, _cleanup
    if not has_gemini(cfg):
        print('[scene] No Gemini key: cannot validate coherent scene. Configure Gemini and supply a clean continuous scene; no loud/edit fallback.')
        return []
    dur = min(_video_duration(path), float((cfg.get('selection') or {}).get('max_analyze_seconds', 90)))
    ranking = bool((cfg.get('clips') or {}).get('ranking_part') or cfg.get('_ranking_selection'))
    minimum, maximum = (18, 28) if ranking else (30, 45)
    if dur < minimum:
        print('[scene] Source too short for a coherent window')
        return []
    times = [round(dur * (i + .5) / 8, 2) for i in range(8)]
    frames = grab_frame_files(path, times, width=384)
    try:
        if len(frames) != len(times):
            return []
        raw = generate(prompt=SCENE_PROMPT.format(times=times, duration=dur, minimum=minimum, maximum=maximum),
                       images=frames, cfg=cfg, max_retries=1)
        match = re.search(r'\{.*\}', raw or '', re.S)
        data = json.loads(match.group()) if match else {}
        window = validate_window(data, dur, minimum, maximum)
        if window:
            window = align_to_pauses(window, pause_boundaries(path, dur), dur, minimum, maximum)
        if not window:
            print('[scene] Rejected incoherent/edited/uncertain or no safe pause boundaries. Supply a longer clean scene.')
            return []
        print(f'[scene] continuous={window[0]:.2f}-{window[1]:.2f}s pause-aligned; no montage')
        return [Segment(window[0], window[1], 1.0, 'coherent_scene_pause_aligned')]
    except Exception as e:
        print(f'[scene] Validation unavailable ({type(e).__name__}); no loudness fallback')
        return []
    finally:
        _cleanup(frames)
