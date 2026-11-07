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
        subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','testsrc2=size=1280x720:rate=30:duration=4',
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
        from PIL import Image
        import numpy as np
        image = out/f'{name}.png'
        subprocess.run(['ffmpeg','-v','error','-y','-ss','2','-i',str(dst),'-frames:v','1',str(image)],check=True)
        actual=np.asarray(Image.open(image).convert('RGB')).astype(float)
        expected=out/f'{name}_expected.png'
        # Independent standard fit/crop references, not solid-color heuristics.
        vf=('format=rgb24,scale=-2:1280,crop=720:1280,format=yuv420p' if mode=='crop'
            else 'scale=720:1280:force_original_aspect_ratio=decrease:force_divisible_by=2,format=yuv420p')
        subprocess.run(['ffmpeg','-v','error','-y','-ss','2','-i',str(src),'-frames:v','1','-vf',vf,str(expected)],check=True)
        reference=np.asarray(Image.open(expected).convert('RGB')).astype(float)
        if mode=='crop':
            roi=actual[300:1000]
            expected_roi=reference[300:1000]
            metrics={'crop_reference_mae':float(np.abs(roi-expected_roi).mean()),
                     'left_edge_std':float(roi[:,:24].std()),'right_edge_std':float(roi[:,-24:].std())}
            assert metrics['crop_reference_mae'] < 12, metrics
            assert metrics['left_edge_std'] > 15 and metrics['right_edge_std'] > 15, metrics
        else:
            y=(1280-reference.shape[0])//2
            metrics={'full_foreground_reference_mae':float(np.abs(actual[y:y+reference.shape[0]]-reference).mean())}
            # Texture must exist outside the fitted foreground; blurred background is observable.
            background=actual[180:400]
            metrics['background_std']=float(background.std())
            metrics['background_gradient']=float(np.abs(np.diff(background,axis=1)).mean())
            sharp=out/'wide_sharp_background.png'
            subprocess.run(['ffmpeg','-v','error','-y','-ss','2','-i',str(src),'-frames:v','1',
                            '-vf','scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280',str(sharp)],check=True)
            unblurred=np.asarray(Image.open(sharp).convert('RGB')).astype(float)[180:400]
            metrics['unblurred_background_gradient']=float(np.abs(np.diff(unblurred,axis=1)).mean())
            metrics['blurred_max_gradient']=float(np.abs(np.diff(background,axis=1)).max())
            metrics['sharp_max_gradient']=float(np.abs(np.diff(unblurred,axis=1)).max())
            assert metrics['full_foreground_reference_mae'] < 12 and metrics['background_std'] > 15,metrics
            assert metrics['background_gradient'] < metrics['unblurred_background_gradient'],metrics
            assert metrics['blurred_max_gradient'] < metrics['sharp_max_gradient'] / 2,metrics
        records[-1]['pixel_verification']=metrics

    (out/'verification.json').write_text(json.dumps(records,indent=2))
    print(json.dumps(records,indent=2))

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,default=Path('/workspace/outputs/framing_review'))
    run(parser.parse_args().output)
