import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from gta_autoclipper.planning import ranking_timing, remaining_render_budget
from gta_autoclipper.edit import required_clip_review
from gta_autoclipper.scene_vlm import grab_frame_files, self_review, _cleanup


class WindowsReviewBudgetTests(unittest.TestCase):
    def test_temp_descriptor_closed_before_ffmpeg_and_gemini(self):
        real_mkstemp=tempfile.mkstemp
        descriptors=[]
        def create(**kwargs):
            fd,name=real_mkstemp(**kwargs)
            descriptors.append(fd)
            return fd,name
        def run(cmd,**kwargs):
            for fd in descriptors:
                with self.assertRaises(OSError):
                    os.fstat(fd)
            self.assertEqual(kwargs['timeout'],30)
            Path(cmd[-1]).write_bytes(b'x'*600)
            return SimpleNamespace(returncode=0)
        def generate(**kwargs):
            for fd in descriptors:
                with self.assertRaises(OSError):
                    os.fstat(fd)
            self.assertEqual(len(kwargs['images']),3)
            return 'DESC: original scene\nMATCH: yes\nEMOTION: n/a\nREASON: ok'
        with patch('gta_autoclipper.tempfiles.tempfile.mkstemp',side_effect=create), \
             patch('gta_autoclipper.scene_vlm.subprocess.run',side_effect=run), \
             patch('gta_autoclipper.gemini_client.has_gemini',return_value=True), \
             patch('gta_autoclipper.gemini_client.generate',side_effect=generate):
            self.assertTrue(self_review(Path('mock.mp4'),'scene',start=0,end=5)['ok'])

    def test_partial_extraction_exception_cleans_all_frames(self):
        files=[]
        def run(cmd,**kwargs):
            files.append(Path(cmd[-1]))
            if len(files)==2:
                raise PermissionError(32,'Windows-style file lock')
            files[-1].write_bytes(b'x'*600)
            return SimpleNamespace(returncode=0)
        with patch('gta_autoclipper.scene_vlm.subprocess.run',side_effect=run):
            with self.assertRaises(PermissionError):
                grab_frame_files(Path('mock.mp4'),[1,2])
        self.assertEqual(len(files),2)
        self.assertTrue(all(not p.exists() for p in files))

    def test_face_grab_close_before_use_cleanup_after_reader_failure(self):
        import sys
        from gta_autoclipper.face_layout import _grab
        files=[]
        def run(cmd,**kwargs):
            files.append(Path(cmd[-1]))
            # A closed path can be renamed/reopened by ffmpeg on Windows.
            files[-1].write_bytes(b'x'*600)
            return SimpleNamespace(returncode=0)
        with patch.dict(sys.modules,{'cv2':SimpleNamespace(imread=lambda _: (_ for _ in ()).throw(RuntimeError('read failed')))}), \
             patch('gta_autoclipper.face_layout.subprocess.run',side_effect=run):
            with self.assertRaises(RuntimeError):
                _grab(Path('mock.mp4'),1)
        self.assertTrue(all(not p.exists() for p in files))

    def test_self_review_errors_malformed_and_missing_frames_fail(self):
        with patch('gta_autoclipper.gemini_client.has_gemini',return_value=True), \
             patch('gta_autoclipper.scene_vlm.grab_frame_files',return_value=[]) as frames, \
             patch('gta_autoclipper.scene_vlm._cleanup') as cleanup, \
             patch('gta_autoclipper.gemini_client.generate',return_value='unexpected reply') as gen:
            self.assertFalse(self_review(Path('mock.mp4'),'scene',start=0,end=5)['ok'])
            gen.assert_not_called()
            frames.return_value=[Path('a'),Path('b'),Path('c')]
            self.assertFalse(self_review(Path('mock.mp4'),'scene',start=0,end=5)['ok'])
            gen.side_effect=PermissionError(32,'Windows lock')
            self.assertFalse(self_review(Path('mock.mp4'),'scene',start=0,end=5)['ok'])
            cleanup.assert_called_with(frames.return_value)

    def test_required_qa_cannot_skip_on_error_or_missing_key(self):
        cfg={'niche_id':'anime','_ranking_soft_header':True,'_skip_header_match':True,
             'clips':{'skip_final_review':True}}
        with patch('gta_autoclipper.scene_vlm.self_review',side_effect=PermissionError(32,'Windows lock')):
            self.assertFalse(required_clip_review(Path('mock.mp4'),'scene',cfg))
        with patch('gta_autoclipper.scene_vlm.self_review',return_value={'ok':True,'skipped':'error'}):
            self.assertFalse(required_clip_review(Path('mock.mp4'),'scene',cfg))
        with patch('gta_autoclipper.scene_vlm.self_review',return_value={'ok':True}), \
             patch('gta_autoclipper.style_kind.vision_style_check',return_value={'ok':True,'skipped':'no_key'}):
            self.assertFalse(required_clip_review(Path('mock.mp4'),'scene',cfg))

    def test_automatic_budget_ignores_old_short_config_not_expert_deadline(self):
        cfg={'ranking':{'timeout_seconds':1500},'job':{'ranking_timeout_seconds':900}}
        self.assertEqual(ranking_timing(cfg,automatic=True),(5280,5400))
        self.assertEqual(ranking_timing(cfg,automatic=True,deadline_s=99999),(5280,5400))
        self.assertEqual(ranking_timing(cfg,automatic=True,deadline_s=900),(900,5400))
        self.assertEqual(ranking_timing(cfg,deadline_s=600),(600,900))
        for invalid in (float('inf'),float('nan')):
            with self.assertRaises(ValueError):
                ranking_timing(cfg,automatic=True,deadline_s=invalid)
        estimate,reserve=remaining_render_budget([130],150)
        self.assertEqual((estimate,reserve),(162.5,300))
        # Observed old run had 193s left after 1307s of a 1500s budget.
        self.assertGreater(5280-1307,4*estimate+reserve)
        self.assertLess(193,estimate+reserve)

    def test_api_outer_wall_matches_automatic_cli_budget(self):
        from fastapi.testclient import TestClient
        from gta_autoclipper.webapp import create_app
        from gta_autoclipper.jobrunner import JOB
        with patch.object(JOB,'running',return_value=False), patch.object(JOB,'start') as start, \
             patch('gta_autoclipper.webapp._cfg',return_value={'ranking':{'timeout_seconds':1500}}):
            response=TestClient(create_app()).post('/api/start',json={'mode':'ranking','niche':'anime','fast':True})
            self.assertEqual(response.status_code,200)
            self.assertEqual(start.call_args.kwargs['hard_timeout_s'],5400)
            self.assertIn('--automatic-plan',start.call_args.args[0])
            self.assertNotIn('--deadline',start.call_args.args[0])

    def test_ranking_cli_failure_nonzero(self):
        from gta_autoclipper.__main__ import main
        with patch('gta_autoclipper.ranking.build_ranking_video',return_value=None):
            self.assertEqual(main(['--ranking']),1)

if __name__=='__main__':
    unittest.main()
