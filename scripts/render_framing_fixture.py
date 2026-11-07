"""Tiny offline synthetic framing regression; NOT episode footage or tracking evidence."""
import argparse
import json
import subprocess
import time
from pathlib import Path
from gta_autoclipper.adaptive_frame import adaptive_filter, choose_framing
from gta_autoclipper.perf import encode_args


def run(out):
    out.mkdir(parents=True,exist_ok=True)
    records=[]
    cfg={'performance':{'preset':'veryfast','crf':20,'width':720,'height':1280,'threads':'2','maxrate':'3500k','bufsize':'7000k'}}
    # Known synthetic object bounds, supplied explicitly; no detector/AI is being claimed.
    cases=[('wide_fit', [[.05,.2,.18,.55],[.77,.2,.18,.55]],
            'drawbox=x=64:y=144:w=230:h=396:color=red:t=fill,drawbox=x=986:y=144:w=230:h=396:color=cyan:t=fill'),
           ('safe_crop', [[.45,.2,.10,.55]]*3,
            'drawbox=x=576:y=144:w=128:h=396:color=yellow:t=fill')]
    for name,boxes,draw in cases:
        start=time.monotonic()
        src=out/f'{name}_source.mp4'
        subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','color=c=0x203040:s=1280x720:r=30:d=4',
                        '-f','lavfi','-i','sine=frequency=330:sample_rate=48000:duration=4',
                        '-vf',draw,'-c:v','libx264','-preset','veryfast','-crf','18','-threads','2',
                        '-c:a','aac','-shortest',str(src)],check=True)
        mode,cx,reason=choose_framing(1280,720,boxes,True)
        fc=adaptive_filter(width=720,height=1280,mode=mode,cx=cx)
        # Fixed top/bottom safe-zone labels; foreground is not stretched.
        fc += (f';[base]drawtext=text=SYNTHETIC_{name}:x=24:y=80:fontsize=28:fontcolor=white,'
               'drawtext=text=FRAMING_TEST_ONLY:x=24:y=h-140:fontsize=26:fontcolor=white[out]')
        dst=out/f'{name}_review.mp4'
        subprocess.run(['ffmpeg','-v','error','-y','-i',str(src),'-filter_complex_threads','1','-filter_complex',fc,
                        '-map','[out]','-map','0:a',*encode_args(cfg),'-r','30',str(dst)],check=True)
        probe=subprocess.run(['ffprobe','-v','error','-show_entries',
                              'stream=codec_name,width,height,r_frame_rate,sample_rate:format=duration',
                              '-of','json',str(dst)],check=True,capture_output=True,text=True)
        records.append(dict(case=name,mode=mode,reason=reason,seconds=round(time.monotonic()-start,3),
                            output=str(dst),probe=json.loads(probe.stdout)))
        subprocess.run(['ffmpeg','-v','error','-y','-ss','2','-i',str(dst),'-frames:v','1',str(out/f'{name}.jpg')],check=True)
    (out/'verification.json').write_text(json.dumps(records,indent=2))
    print(json.dumps(records,indent=2))

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,default=Path('/workspace/outputs/framing_review'))
    run(parser.parse_args().output)
