"""Review a previously downloaded/validated section; not a full Cutroom ranking."""
import json
import os
import subprocess
import time
from pathlib import Path
os.environ.setdefault('GTA_LOW_CPU','1')
from gta_autoclipper.adaptive_frame import plan_segment,adaptive_filter
from gta_autoclipper.config import load_config
from gta_autoclipper.edit import required_clip_review
from gta_autoclipper.perf import encode_args

root=Path('/workspace/outputs/episode_recovery')
source=root/'live_section/RzClCJFpSoM_r45s30.mp4'
selection=json.loads((root/'live_selection_updated.json').read_text())
assert selection['status']=='accepted' and len(selection['segments'])==1
seg=selection['segments'][0];a,b=seg['start'],seg['end']
cfg=load_config();cfg['niche_id']='anime'
t=time.monotonic();mode,cx,reason=plan_segment(source,a,b,cfg)
out=root/'live_scene_review_sample.mp4'
subprocess.run(['ffmpeg','-v','error','-y','-ss',str(a),'-i',str(source),'-t',str(b-a),
    '-filter_complex_threads','1','-filter_complex',adaptive_filter(width=720,height=1280,mode=mode,cx=cx),
    '-map','[base]','-map','0:a?',*encode_args(cfg),str(out)],check=True,timeout=120)
ok=required_clip_review(out,'Anime episode action scene',cfg,title='Anime episode action scene')
probe=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_entries','format=duration:stream=codec_name,width,height','-of','json',str(out)]))
r={'output':str(out),'start':a,'end':b,'framing':mode,'framing_reason':reason,'required_qa_passed':ok,
   'probe':probe,'elapsed_seconds':round(time.monotonic()-t,2),'full_cutroom_ranking_e2e_verified':False,
   'scope':'Original section -> accepted coherent scene -> adaptive render -> required header/style QA. No ranking, VO, captions or full UI run.'}
(root/'live_render_review.json').write_text(json.dumps(r,indent=2));print(json.dumps(r,indent=2))
if not ok:
    out.unlink(missing_ok=True)
    raise SystemExit('Required QA rejected sample; output removed')
