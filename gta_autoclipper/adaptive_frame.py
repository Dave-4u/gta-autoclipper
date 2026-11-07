"""Conservative stable segment framing, not a subject tracker."""
import json
import re
import subprocess


def choose_framing(width, height, boxes=None, certain=False):
    aspect = width / height
    target = 9 / 16
    if abs(aspect / target - 1) <= .02:
        return 'crop', .5, 'near-native portrait (<=2% loss)'
    if not certain or not boxes or aspect <= target:
        return 'fit', .5, 'uncertain or wide action; retain entire source'
    visible = target / aspect
    try:
        left = min(float(b[0]) for b in boxes) - .05
        right = max(float(b[0]) + float(b[2]) for b in boxes) + .05
        if any(len(b) != 4 or any(not 0 <= float(v) <= 1 for v in b) or
               float(b[0])+float(b[2]) > 1 or float(b[1])+float(b[3]) > 1 for b in boxes):
            return 'fit', .5, 'invalid evidence'
        center = max(visible/2, min(1-visible/2, (left+right)/2))
        if left < center-visible/2 or right > center+visible/2:
            return 'fit', .5, 'subjects/action union exceeds portrait crop'
        # fill_crop_filter cx denotes fraction of excess pixels, not a source-coordinate center.
        offset = (center-visible/2) / (1-visible)
        return 'crop', offset, 'all sampled action bounds fit with 5% horizontal margin'
    except (ValueError, TypeError, IndexError):
        return 'fit', .5, 'invalid evidence'


def adaptive_filter(*, width, height, speed=1, mode='fit', cx=.5, blur_width=180, blur_sigma=5):
    if mode == 'crop':
        from .face_layout import fill_crop_filter
        return fill_crop_filter(width=width, height=height, speed=speed, zoom=1, cx=cx)
    # Blur only a tiny duplicate; keep foreground full resolution and full image, no stretching.
    blur_sigma = max(2.0, min(6.0, float(blur_sigma)))
    bw = max(90, min(240, int(blur_width)))
    bh = int(round(bw*height/width/2)*2)
    return (f'[0:v]setpts=PTS/{speed},split=2[afg][abg];'
            f'[abg]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},'
            f'gblur=sigma={float(blur_sigma)},scale={width}:{height},setsar=1[back];'
            f'[afg]scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1[front];'
            '[back][front]overlay=(W-w)/2:(H-h)/2,format=yuv420p[base]')


def plan_segment(path, start, end, cfg):
    from .gemini_client import has_gemini, generate
    from .scene_vlm import grab_frame_files, _cleanup
    pr = subprocess.run(['ffprobe','-v','error','-select_streams','v:0','-show_entries',
                         'stream=width,height','-of','json',str(path)], capture_output=True, text=True, timeout=15, check=True)
    stream = json.loads(pr.stdout)['streams'][0]
    w,h = int(stream['width']), int(stream['height'])
    if abs((w/h)/(9/16)-1) <= .02 or not has_gemini(cfg):
        return choose_framing(w,h)
    times = [start+(end-start)*x for x in (.15,.5,.85)]
    frames = grab_frame_files(path,times,width=384)
    try:
        if len(frames) != 3:
            return choose_framing(w,h)
        raw = generate(prompt=('For EACH chronological frame give the normalized [x,y,w,h] bounding '
            'box enclosing ALL main characters AND relevant action/weapons/effects, not just faces. '
            'Only mark certain=true if the entire action remains contained between samples. '
            'Two characters far apart, off-screen motion, changing shots, uncertainty => certain=false. '
            'Return JSON {"certain":false,"boxes":[[0,0,1,1],[0,0,1,1],[0,0,1,1]]}.'),
            images=frames,cfg=cfg,max_retries=1)
        m=re.search(r'\{.*\}',raw or '',re.S)
        d=json.loads(m.group()) if m else {}
        boxes=d.get('boxes')
        return choose_framing(w,h,boxes,certain=d.get('certain') is True and isinstance(boxes,list) and len(boxes)==3)
    except Exception:
        return choose_framing(w,h)
    finally:
        _cleanup(frames)
