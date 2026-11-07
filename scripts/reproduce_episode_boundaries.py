"""Offline continuous-audio boundary reproduction. Synthetic, not episode/AI verification."""
import argparse
import json
import subprocess
import time
from pathlib import Path
from gta_autoclipper.coherent import pause_boundaries,shot_boundaries,aligned_scene


def run(root):
    root.mkdir(parents=True,exist_ok=True)
    source=root/'continuous_audio_boundary_fixture.mp4'
    start=time.monotonic()
    cmd=['ffmpeg','-v','error','-y']
    for color,duration in [('0x103050',5),('0xe0e0e0',30),('0x103050',10)]:
        cmd += ['-f','lavfi','-i',f'color=c={color}:s=1280x720:r=30:d={duration}']
    cmd += ['-f','lavfi','-i','sine=frequency=330:sample_rate=48000:duration=45',
        '-filter_complex_threads','1','-filter_complex',
        '[0:v][1:v][2:v]concat=n=3:v=1:a=0,drawbox=x=540:y=200:w=200:h=350:color=yellow:t=fill,drawtext=text=SYNTHETIC_BOUNDARY_TEST_NOT_EPISODE:x=30:y=40:fontsize=30:fontcolor=red[v]',
        '-map','[v]','-map','3:a','-c:v','libx264','-preset','veryfast','-crf','20','-threads','2',
        '-c:a','aac','-shortest',str(source)]
    subprocess.run(cmd,check=True,timeout=30)
    pauses=pause_boundaries(source,45);shots=shot_boundaries(source,45)
    # Explicit supplied confidence tests boundary policy ONLY, not Gemini's semantic judgment.
    window,kind=aligned_scene((5,35),{'continuity_score':.95,'natural_boundaries':True,'dialogue':False},source,45,30,45,{})
    result={'source':str(source),'synthetic':True,'silence_boundaries':pauses,'shot_boundaries':shots,
        'aligned_window':window,'boundary_kind':kind,'elapsed_seconds':round(time.monotonic()-start,2),
        'gemini_semantic_validation_tested':False,'episode_e2e_verified':False}
    (root/'boundary_verification.json').write_text(json.dumps(result,indent=2))
    print(json.dumps(result,indent=2))
    assert not pauses and kind=='shot' and window,result

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,default=Path('/workspace/outputs/episode_recovery'))
    run(parser.parse_args().output)
