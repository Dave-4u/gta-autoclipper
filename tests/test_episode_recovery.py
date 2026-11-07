import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from gta_autoclipper.download_watch import ProgressClock,SourceHealth,run_download,diagnostic_reason
from gta_autoclipper.episode_windows import episode_windows,prepare_episode
from gta_autoclipper.coherent import aligned_scene,scene_reply


class EpisodeRecoveryTests(unittest.TestCase):
    def setUp(self):
        from gta_autoclipper import procutil
        procutil.clear_cancel()

    def test_active_progress_not_idle_but_finite_total(self):
        clock=ProgressClock(0,0)
        for second in range(1,200):
            clock.observe(second*100,second)
            self.assertIsNone(clock.expired(second,45,240))
        self.assertEqual(clock.expired(240,45,240),'source_budget')
        self.assertEqual(clock.expired(245,45,500),'inactivity_stall')

    def test_real_active_process_and_stall(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            active="import time; from pathlib import Path; p=Path(%r);\nfor i in range(6):\n p.write_bytes(b'x'*(i+1)*100);print('gta_progress:'+str((i+1)*100),flush=True);time.sleep(.08)" % str(root/'active.part')
            ok,reason,_=run_download([sys.executable,'-u','-c',active],out_dir=root,stem='active',total_seconds=2,idle_seconds=.25,tick=.02)
            self.assertTrue(ok,reason)
            stalled="import time; print('warning',flush=True); time.sleep(5)"
            start=time.monotonic()
            ok,reason,_=run_download([sys.executable,'-u','-c',stalled],out_dir=root,stem='stall',total_seconds=2,idle_seconds=.2,tick=.02)
            self.assertFalse(ok)
            self.assertEqual(reason,'inactivity_stall')
            self.assertLess(time.monotonic()-start,1.5)

    def test_warning_chatter_does_not_count_as_progress(self):
        with tempfile.TemporaryDirectory() as d:
            code="import time;\nfor i in range(100):\n print('warning',flush=True);time.sleep(.03)"
            ok,reason,_=run_download([sys.executable,'-u','-c',code],out_dir=Path(d),stem='none',total_seconds=2,idle_seconds=.2,tick=.02)
            self.assertEqual((ok,reason),(False,'inactivity_stall'))

    def test_source_failure_cache_and_circuit(self):
        health=SourceHealth()
        health.record('a',False)
        self.assertFalse(health.allowed('a'))
        health.record('b',False);health.record('c',False)
        self.assertFalse(health.allowed('new'))

    def test_download_retries_at_most_two_and_keeps_720(self):
        from gta_autoclipper.download import download_video
        with tempfile.TemporaryDirectory() as d:
            candidate=SimpleNamespace(id='mock',source='search',title='Naruto fight scene',
                url='https://www.youtube.com/watch?v=mock',duration=1431,extra={'uploader_id':'@crunchyroll'})
            cfg={'niche_id':'anime','download':{'output_dir':d,'max_download_seconds':60,'section_start_seconds':250}}
            with patch('gta_autoclipper.download_watch.run_download',return_value=(False,'transient_network','SECRET_URL_NOT_LOGGED')) as run:
                self.assertIsNone(download_video(candidate,cfg))
                self.assertEqual(run.call_count,2)
                cmd=run.call_args.args[0]
                self.assertNotIn('--force-keyframes-at-cuts',cmd)
                self.assertIn('height>=720',cmd[cmd.index('-f')+1])
                self.assertEqual(cmd[cmd.index('--retries')+1],'0')
                self.assertIsNone(download_video(candidate,cfg))
                self.assertEqual(run.call_count,2)  # failed source not retried elsewhere in run

    def test_no_retry_after_total_budget(self):
        from gta_autoclipper.download import download_video
        with tempfile.TemporaryDirectory() as d:
            c=SimpleNamespace(id='mock',source='search',title='Naruto scene',url='https://www.youtube.com/watch?v=mock',duration=1000,extra={'uploader_id':'@crunchyroll'})
            with patch('gta_autoclipper.download_watch.run_download',return_value=(False,'source_budget','')) as run:
                download_video(c,{'niche_id':'anime','download':{'output_dir':d,'max_download_seconds':60}})
                self.assertEqual(run.call_count,1)

    def test_distinct_windows_not_fixed_35_percent(self):
        windows=episode_windows(1431,60)
        self.assertEqual(len(set(windows)),3)
        self.assertNotIn(round(1431*.35,2),windows)
        self.assertTrue(all(x+60<=1431 for x in windows))
        self.assertEqual(episode_windows(60),[0])

    def test_next_window_only_on_content_rejection(self):
        c=SimpleNamespace(id='x',duration=1431,source='search',extra={})
        starts=[]
        def download(c,cfg):
            starts.append(cfg['download']['section_start_seconds'])
            return Path('mock.mp4')
        def analyze(p,cfg):
            cfg['_scene_status']='content_rejected'
            return [] if len(starts)==1 else ['coherent-scene']
        with patch('gta_autoclipper.download.download_video',side_effect=download):
            path,segs=prepare_episode(c,{'download':{}},analyze)
        self.assertEqual(segs,['coherent-scene'])
        self.assertEqual(len(starts),2)
        self.assertNotEqual(starts[0],starts[1])

    def test_provider_failure_does_not_spend_another_window(self):
        c=SimpleNamespace(id='x',duration=1431,source='search',extra={})
        def analyze(p,cfg):cfg['_scene_status']='provider_transient';return []
        with patch('gta_autoclipper.download.download_video',return_value=Path('mock.mp4')) as download:
            self.assertEqual(prepare_episode(c,{'download':{}},analyze),(None,[]))
            self.assertEqual(download.call_count,1)

    def test_continuous_audio_action_can_use_coherent_shot_boundaries(self):
        data={'continuity_score':.95,'natural_boundaries':True,'dialogue':False}
        with patch('gta_autoclipper.coherent.pause_boundaries',return_value=[]), \
             patch('gta_autoclipper.coherent.shot_boundaries',return_value=[5,35]):
            self.assertEqual(aligned_scene((5,35),data,Path('mock.mp4'),60,30,45,{}),((5,35),'shot'))
            self.assertEqual(aligned_scene((5,35),dict(data,continuity_score=.3),Path('mock.mp4'),60,30,45,{})[0],None)

    def test_dialogue_needs_word_gap_not_arbitrary_shot(self):
        data={'continuity_score':.95,'natural_boundaries':True,'dialogue':True}
        with patch('gta_autoclipper.coherent.pause_boundaries',return_value=[]), \
             patch('gta_autoclipper.coherent.shot_boundaries') as shots, \
             patch('gta_autoclipper.coherent.word_gap_boundaries',return_value=[5,35]):
            self.assertEqual(aligned_scene((5,35),data,Path('mock.mp4'),60,30,45,{}),((5,35),'word_gap'))
            shots.assert_not_called()

    def test_provider_transient_retry_malformed_not_content(self):
        good=json.dumps({'original_scene':True,'coherent':True,'start':5,'end':35})
        with patch('gta_autoclipper.gemini_client.generate',side_effect=[RuntimeError('503 unavailable'),good]) as call:
            data,status=scene_reply('prompt',[],{})
            self.assertEqual(status,'validated_response')
            self.assertEqual(call.call_count,2)
        with patch('gta_autoclipper.gemini_client.generate',return_value='bad') as call:
            self.assertEqual(scene_reply('prompt',[],{}),(None,'provider_malformed'))
            self.assertEqual(call.call_count,2)
        with patch('gta_autoclipper.gemini_client.generate',return_value=json.dumps({'original_scene':False,'coherent':False})) as call:
            self.assertEqual(scene_reply('prompt',[],{})[1],'validated_response')
            self.assertEqual(call.call_count,1)  # content rejection is not provider failure

    def test_out_of_bounds_provider_reply_corrected_once_not_clamped(self):
        bad=json.dumps({'original_scene':True,'coherent':True,'start':0,'end':45})
        good=json.dumps({'original_scene':True,'coherent':True,'start':5,'end':28})
        with patch('gta_autoclipper.gemini_client.generate',side_effect=[bad,good]) as call:
            data,status=scene_reply('prompt',[],{},bounds=(45,18,28))
            self.assertEqual(data['start'],5)
            self.assertEqual(status,'validated_response')
            self.assertIn('CORRECTION',call.call_args.kwargs['prompt'])
        with patch('gta_autoclipper.gemini_client.generate',return_value=bad) as call:
            self.assertEqual(scene_reply('prompt',[],{},bounds=(45,18,28)),(None,'provider_invalid_bounds'))
            self.assertEqual(call.call_count,2)

    def test_no_new_windows_inside_assembly_reserve(self):
        cfg={'_run_deadline_monotonic':time.monotonic()+290}
        cand=SimpleNamespace(id='example',url='https://example.invalid',title='Official episode',duration=1400,source='youtube')
        with patch('gta_autoclipper.download.download_video') as download:
            self.assertEqual(prepare_episode(cand,cfg,lambda *args: []),(None,[]))
            download.assert_not_called()

    def test_disposable_word_worker_and_remaining_budget(self):
        from gta_autoclipper.coherent import word_gap_boundaries
        def worker(cmd,**kwargs):
            Path(cmd[6]).write_text(json.dumps([{'word':'x','end':4},{'word':'y','start':4.5}]))
            self.assertLessEqual(kwargs['total_seconds'],40)
            return True,'complete',''
        with patch('gta_autoclipper.download_watch.run_download',side_effect=worker):
            self.assertEqual(word_gap_boundaries(Path('source.mp4'),(5,35),60,{}),[7.25])
        with self.assertRaises(TimeoutError):
            word_gap_boundaries(Path('source.mp4'),(5,35),60,{'_source_deadline':time.monotonic()-1})

    def test_deleted_outputs_cache_temp_recreated(self):
        from gta_autoclipper.config import ensure_dirs
        from gta_autoclipper.tempfiles import closed_temp_path
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'deleted';root.mkdir();shutil.rmtree(root)
            cfg={'download':{'output_dir':str(root/'cache')},'clips':{'output_dir':str(root/'outputs')}}
            ensure_dirs(cfg)
            self.assertTrue((root/'cache').is_dir());self.assertTrue((root/'outputs').is_dir())
            with patch('gta_autoclipper.tempfiles.tempfile.gettempdir',return_value=str(root/'temp')):
                p=closed_temp_path(suffix='.jpg')
                self.assertTrue(p.exists());p.unlink()

    def test_diagnostics_never_return_remote_secret_text(self):
        self.assertEqual(diagnostic_reason('503 https://secret?token=PRIVATE'),'transient_network')
        self.assertNotIn('PRIVATE',diagnostic_reason('ffmpeg token=PRIVATE'))

    def test_accidental_fullfile_and_low_quality_rejected(self):
        from gta_autoclipper.download import validate_download
        def probe(duration,height):
            return SimpleNamespace(returncode=0,stdout=json.dumps({'streams':[{'width':1280,'height':height}],
                                                                  'format':{'duration':duration}}))
        with patch('gta_autoclipper.download.subprocess.run',return_value=probe(1431,720)):
            self.assertIsNone(validate_download(Path('mock.mp4'),section_seconds=60))
        with patch('gta_autoclipper.download.subprocess.run',return_value=probe(60,480)):
            self.assertIsNone(validate_download(Path('mock.mp4'),section_seconds=60))

if __name__=='__main__':unittest.main()
