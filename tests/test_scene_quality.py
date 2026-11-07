import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from gta_autoclipper.source_policy import source_decision, enforce_source
from gta_autoclipper.quality import resolve_quality
from gta_autoclipper.adaptive_frame import choose_framing, adaptive_filter
from gta_autoclipper.coherent import validate_window, align_to_pauses


def candidate(title, handle='@Crunchyroll', **extra):
    return SimpleNamespace(title=title, source='search', url='https://www.youtube.com/watch?v=test',
                           extra=dict(uploader_id=handle, **extra))


class SceneQualityTests(unittest.TestCase):
    def test_edits_rejected_even_from_publisher(self):
        for title in ('Naruto AMV clip', 'Naruto fight edit', 'Official episode compilation',
                      'Disney reaction clip', 'Adventure Time montage', 'Anime Fancam', 'Top 10 fights',
                      'Naruto reupload clip', 'JJK #shorts clip'):
            self.assertFalse(source_decision(candidate(title), {})[0], title)

    def test_provenance_not_official_title_or_display_name(self):
        self.assertFalse(source_decision(candidate('Official Naruto full fight scene', '@unverified'), {})[0])
        self.assertTrue(source_decision(candidate('Naruto full fight scene'), {})[0])
        self.assertFalse(source_decision(candidate('Disney official trailer', '@disney'), {})[0])
        result=source_decision(candidate('Original fan animation scene', '@creator'),
                               {'source_policy': {'authorized_channels': ['@creator']}})
        self.assertEqual(result[:3], (True, 'original_fan_animation', 'user_authorized'))
        self.assertFalse(source_decision(candidate('Original fan animation scene', '@creator'), {})[0])

    def test_local_explicit_only(self):
        with tempfile.NamedTemporaryFile(suffix='.mp4') as f:
            c=candidate('my scene', '@unknown'); c.url=f.name
            self.assertFalse(source_decision(c,{})[0])
            c.source='local'
            self.assertEqual(source_decision(c,{})[1], 'local_supplied')

    def test_framing_wide_unknown_vs_proven_safe(self):
        self.assertEqual(choose_framing(1280,720)[0],'fit')
        self.assertEqual(choose_framing(1280,720,[[.05,.2,.2,.5],[.75,.2,.2,.5]], True)[0],'fit')
        self.assertEqual(choose_framing(1280,720,[[.45,.2,.1,.5]]*3, True)[0],'crop')
        self.assertEqual(choose_framing(1280,720,[[-.1,.2,.1,.5]], True)[0],'fit')
        self.assertEqual(choose_framing(720,1280)[0],'crop')
        f=adaptive_filter(width=720,height=1280)
        self.assertIn('scale=180:320',f)
        self.assertIn('force_original_aspect_ratio=decrease',f)

    def test_scene_requires_coherence_range_and_pause_edges(self):
        data=dict(original_scene=True,coherent=True,start=10,end=30)
        self.assertEqual(validate_window(data,60,18,28),(10,30))
        self.assertIsNone(validate_window(dict(data,coherent=False),60,18,28))
        self.assertIsNone(validate_window(dict(data,end=200),60,18,28))
        self.assertIsNone(align_to_pauses((10,30),[0,60],60,18,28))
        self.assertEqual(align_to_pauses((10,30),[9.8,30.2],60,18,28),(9.8,30.2))

    def test_no_key_no_loud_fallback(self):
        from gta_autoclipper.coherent import select_scene
        with patch('gta_autoclipper.gemini_client.has_gemini',return_value=False):
            self.assertEqual(select_scene(Path('not-read.mp4'),{}),[])

    def test_selector_success_and_edited_rejection(self):
        from gta_autoclipper.coherent import select_scene
        with patch('gta_autoclipper.gemini_client.has_gemini',return_value=True), \
             patch('gta_autoclipper.analyze._video_duration',return_value=60), \
             patch('gta_autoclipper.scene_vlm.grab_frame_files',return_value=[Path('mock.jpg')]*8), \
             patch('gta_autoclipper.scene_vlm._cleanup'), \
             patch('gta_autoclipper.coherent.pause_boundaries',return_value=[5,35]), \
             patch('gta_autoclipper.gemini_client.generate',return_value=json.dumps(
                 dict(original_scene=True,coherent=True,start=5,end=35))) as gen:
            result=select_scene(Path('mock.mp4'),{})
            self.assertEqual(len(result),1)
            self.assertIn('coherent_scene_pause_aligned',result[0].reason)
            self.assertIn('SAME scene/characters/action thread',gen.call_args.kwargs['prompt'])
            gen.return_value=json.dumps(dict(original_scene=False,coherent=True,start=5,end=35))
            self.assertEqual(select_scene(Path('mock.mp4'),{}),[])

    def test_discovery_enforces_source_gate(self):
        from gta_autoclipper.discover import Candidate, discover
        def c(title,handle):
            return Candidate(id=title,url='https://www.youtube.com/watch?v=mock',title=title,
                             duration=60,source='search',extra={'uploader_id':handle})
        cfg={'niche_id':'anime','download':{'links_file':'not-a-file'},
             'search':{'keywords':['anime edit'],'use_trending':False,'max_age_days':0}}
        with patch('gta_autoclipper.discover.search_youtube',return_value=[
                 c('Naruto fight scene','@crunchyroll'),c('Naruto AMV clip','@crunchyroll'),
                 c('Naruto fight scene repost','@someone')]), \
             patch('gta_autoclipper.discover.smart_rank_score',return_value=1):
            result=discover(cfg,SimpleNamespace(seen=lambda x:False))
            self.assertEqual(len(result),1)
            self.assertEqual(result[0].extra['source_kind'],'episode_scene')

    def test_download_gate_before_cache_or_network(self):
        from gta_autoclipper.download import download_video
        c=candidate('Naruto AMV clip')
        with patch('gta_autoclipper.procutil.cancelled',return_value=False), \
             patch('gta_autoclipper.procutil.run_tracked') as net:
            self.assertIsNone(download_video(c,{'niche_id':'anime'}))
            net.assert_not_called()

    def test_runtime_migration_preserves_preferences_and_credentials(self):
        old={'performance':{'preset':'ultrafast','crf':25},'download':{'cookies':'private-path'},
             'gemini':{'api_key':'test-placeholder'},'voiceover':{'voice':'custom'},
             'montage':{'enabled':True},'clips':{'montage':{'enabled':True}},
             'watermark':{'text':'custom'},'source_policy':{'authorized_channels':['@mine']}}
        original=copy.deepcopy(old)
        resolved=resolve_quality(old)
        self.assertEqual(old,original)
        self.assertEqual(resolved['performance']['preset'],'veryfast')
        self.assertEqual(resolved['performance']['crf'],20)
        self.assertFalse(resolved['clips']['montage']['enabled'])
        for key in ('download','gemini','voiceover','watermark'):
            self.assertEqual(resolved[key],old[key])
        self.assertEqual(resolved['source_policy']['authorized_channels'],['@mine'])

    def test_ui_cli_shared_effective_quality(self):
        from gta_autoclipper.config import load_config
        from gta_autoclipper.webapp import _apply_fast_defaults, _cfg
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ,{'GTA_LOW_CPU':'1'}):
            p=Path(d)/'old.yaml'
            p.write_text('performance:\n  preset: ultrafast\n  crf: 25\n')
            cli=load_config(p)
            ui=_apply_fast_defaults(_cfg(str(p)))
            for key in ('source_policy','selection','framing','performance'):
                self.assertEqual(cli[key],ui[key])
            self.assertIn('ultrafast',p.read_text())  # runtime migration, not rewrite

    def test_render_cannot_bypass_provenance_gate(self):
        from gta_autoclipper.edit import render_clip
        from gta_autoclipper.analyze import Segment
        with patch('gta_autoclipper.edit._render_clip_impl') as render:
            self.assertIsNone(render_clip(Path('cached_edit.mp4'),Segment(0,30,1,'loud'),{'niche_id':'anime'}))
            render.assert_not_called()

if __name__=='__main__':
    unittest.main()
