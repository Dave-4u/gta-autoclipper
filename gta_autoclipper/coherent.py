"""Bounded, continuous scene selection. No distant loudness montage fallback."""
import json
import re
import subprocess
import time
from pathlib import Path

SCENE_PROMPT = '''Select ONE coherent continuous episode scene from these chronological samples.
Times in seconds: {times}. Analyzed duration: {duration}.
Require the SAME scene/characters/action thread, a readable setup and a resolved beat/payoff.
Do not choose unrelated fights, music videos, recaps, reaction overlays, edited shorts,
social captions, speed ramps, or compilation transitions. Ordinary original shot/reverse-shot,
official episode subtitles, broadcaster logos, and normal camera cuts are NOT edits/AMVs.
Choose natural entry/exit points: audio pauses, sentence/word gaps, or coherent shot cuts
for action with continuous music/effects. Silence is NOT required for every action scene.
Affirm natural_boundaries only when a complete action beat fits; never cut dialogue or payoff.
Return ONLY JSON: {{"original_scene":true,"coherent":true,"start":0,"end":30,"continuity_score":0.95,"natural_boundaries":true,"dialogue":false,"reason":"setup -> payoff"}}.
Set original_scene=false or coherent=false if uncertain. Aim for {minimum}-{maximum} seconds.
Assess chronological action continuity; do not reject solely because samples are spaced apart or
camera angles change. Reject actual evidence of unrelated scenes or an incomplete setup/payoff.
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


def pause_boundaries(path, duration, timeout_seconds=45):
    pr = subprocess.run(['ffmpeg', '-v', 'info', '-i', str(path), '-t', str(duration),
                         '-vn', '-af', 'silencedetect=noise=-35dB:d=0.18', '-f', 'null', '-'],
                        capture_output=True, text=True, timeout=timeout_seconds, check=False)
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


def shot_boundaries(path, duration, timeout_seconds=45):
    pr = subprocess.run(['ffmpeg','-v','info','-i',str(path),'-t',str(duration),'-an',
                         '-vf',"scale=320:-2,select='gt(scene,0.30)',showinfo",'-f','null','-'],
                        capture_output=True,text=True,timeout=timeout_seconds,check=False)
    if pr.returncode:
        return []
    return [float(x) for x in re.findall(r'pts_time:([\d.]+)',pr.stderr)]


def word_gap_boundaries(path, window, duration, cfg):
    import sys
    from .download_watch import run_download
    from .tempfiles import closed_temp_path
    start=max(0,window[0]-2); end=min(duration,window[1]+2)
    result=closed_temp_path(suffix='.json')
    try:
        ok,reason,_=run_download([sys.executable,'-m','gta_autoclipper.boundary_worker',
            str(path),str(start),str(end),str(result),str(cfg.get('niche_id') or '')],
            out_dir=result.parent,stem=result.stem,total_seconds=_boundary_timeout(cfg,40),idle_seconds=40)
        if not ok:
            raise RuntimeError('word_boundary_'+reason)
        words=json.loads(result.read_text())
    finally:
        result.unlink(missing_ok=True)
        import shutil
        shutil.rmtree(result.with_suffix('.work'),ignore_errors=True)
    gaps=[]
    for left,right in zip(words,words[1:]):
        a=float(left.get('end',0)); b=float(right.get('start',0))
        if b-a >= .4 or (b-a >= .18 and str(left.get('word','')).rstrip().endswith(('.','!','?'))):
            gaps.append(start+(a+b)/2)
    return gaps


def _boundary_timeout(cfg, limit=45):
    remaining=float(cfg.get('_source_deadline',time.monotonic()+limit))-time.monotonic()
    if remaining <= 1:
        raise TimeoutError('source_budget')
    return min(limit,remaining)


def aligned_scene(window, data, path, duration, minimum, maximum, cfg):
    pauses=pause_boundaries(path,duration,timeout_seconds=_boundary_timeout(cfg))
    aligned=align_to_pauses(window,pauses,duration,minimum,maximum)
    if aligned:
        return aligned,'pause'
    if float(data.get('continuity_score') or 0) < .85 or data.get('natural_boundaries') is not True:
        return None,'low_boundary_confidence'
    # Continuous-audio action can end at a natural shot cut, NOT only at silence.
    # Never assume no dialogue merely because Whisper returned no words.
    if data.get('dialogue') is False:
        aligned=align_to_pauses(window,shot_boundaries(path,duration,timeout_seconds=_boundary_timeout(cfg)),duration,minimum,maximum)
        if aligned:
            return aligned,'shot'
    else:
        aligned=align_to_pauses(window,word_gap_boundaries(path,window,duration,cfg),duration,minimum,maximum)
        if aligned:
            return aligned,'word_gap'
    return None,'no_natural_boundary'


def scene_reply(prompt, frames, cfg, bounds=None):
    from .gemini_client import generate
    status='provider_unavailable'
    deadline=min(time.monotonic()+40,float(cfg.get('_source_deadline') or time.monotonic()+40))
    for attempt in range(2):
        remaining=deadline-time.monotonic()
        if remaining < 5:
            return None,'source_budget'
        try:
            raw=generate(prompt=prompt,images=frames,cfg=cfg,max_retries=1,
                         timeout_seconds=min(20,remaining),total_seconds=min(40,remaining))
            if not raw:
                status='provider_empty'
            else:
                match=re.search(r'\{.*\}',raw,re.S)
                try:
                    data=json.loads(match.group()) if match else None
                except (ValueError,TypeError):
                    data=None
                if isinstance(data,dict) and isinstance(data.get('original_scene'),bool) and isinstance(data.get('coherent'),bool):
                    if data['original_scene'] is False or data['coherent'] is False:
                        return data,'validated_response'  # actual content verdict, never retried
                    if bounds is None or validate_window(data,*bounds):
                        return data,'validated_response'
                    status='provider_invalid_bounds'
                    duration,minimum,maximum=bounds
                    prompt += (f'\nCORRECTION: prior bounds violated constraints. Use 0<=start<end<={duration}; '
                               f'duration MUST be {minimum}-{maximum}s. Select a complete beat, never arbitrary truncation. '
                               'If none fits, set coherent=false. Return corrected JSON only.')
                else:
                    status='provider_malformed'
        except Exception as e:
            low=str(e).lower()
            status='provider_transient' if any(x in low for x in ('429','503','502','timeout','timed out','unavailable','rate_limit','transient')) else 'provider_error'
            if status=='provider_error':
                print(f'[scene] status=unavailable reason={status} attempt={attempt+1}',flush=True)
                return None,status
        print(f'[scene] status=unavailable reason={status} attempt={attempt+1}',flush=True)
    return None,status


def select_scene(path: Path, cfg):
    from .analyze import Segment, _video_duration
    from .gemini_client import generate, has_gemini
    from .scene_vlm import grab_frame_files, _cleanup
    if not has_gemini(cfg):
        cfg['_scene_status']='provider_no_key'
        print('[scene] status=unavailable reason=provider_no_key; configure Gemini, no footage/QA fallback')
        return []
    dur = min(_video_duration(path), float((cfg.get('selection') or {}).get('max_analyze_seconds', 90)))
    ranking = bool((cfg.get('clips') or {}).get('ranking_part') or cfg.get('_ranking_selection'))
    minimum, maximum = (18, 28) if ranking else (30, 45)
    if dur < minimum:
        cfg["_scene_status"]="source_too_short"
        print('[scene] status=rejected reason=source_too_short')
        return []
    times = [round(dur * (i + .5) / 8, 2) for i in range(8)]
    frames=[]
    try:
        frames = grab_frame_files(path, times, width=384)
        if len(frames) != len(times):
            cfg['_scene_status']='frame_extraction_failed'
            print('[scene] status=unavailable reason=frame_extraction_failed')
            return []
        data,status=scene_reply(SCENE_PROMPT.format(times=times,duration=dur,minimum=minimum,maximum=maximum),frames,cfg,bounds=(dur,minimum,maximum))
        if data is None:
            cfg['_scene_status']=status
            print(f'[scene] status=unavailable reason={status}; not a content rejection')
            return []
        window=validate_window(data,dur,minimum,maximum)
        if not window:
            reason='content_rejected' if data.get('original_scene') is False or data.get('coherent') is False else 'invalid_window_bounds'
            cfg['_scene_status']=reason
            print(f"[scene] status=rejected reason={reason} original_scene={data.get('original_scene')} coherent={data.get('coherent')}")
            return []
        window,boundary=aligned_scene(window,data,path,dur,minimum,maximum,cfg)
        if not window:
            cfg['_scene_status']='no_natural_boundary'
            print(f'[scene] status=rejected reason={boundary}')
            return []
        cfg['_scene_status']='accepted'
        print(f'[scene] status=accepted window={window[0]:.2f}-{window[1]:.2f}s boundary={boundary} coherent=true montage=false')
        return [Segment(window[0],window[1],1.0,f'coherent_scene_{boundary}_aligned')]
    except Exception as e:
        cfg['_scene_status']='boundary_validation_unavailable'
        print(f'[scene] status=unavailable reason=boundary_validation_unavailable error_type={type(e).__name__}')
        return []
    finally:
        _cleanup(frames)
