"""No network, downloads, inference, or media renders."""
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from gta_autoclipper.planning import automatic_plan
from gta_autoclipper.niches import list_niche_ids
from gta_autoclipper.jobrunner import cli_args_for
from gta_autoclipper.webapp import StartRequest, create_app


class AutomaticPlanTests(unittest.TestCase):
    def test_every_mode_and_enabled_niche(self):
        for niche in list_niche_ids():
            for mode in ('single', 'autopilot', 'auto', 'ranking'):
                with self.subTest(niche=niche, mode=mode):
                    p = automatic_plan(mode, niche)
                    self.assertTrue(p.summary.startswith('Automatic:'))
                    if mode == 'single':
                        self.assertEqual((p.source_limit, p.output_target, p.clips_per_video), (1, 1, 1))
                    elif mode == 'ranking':
                        self.assertEqual((p.output_target, p.source_limit, p.probe_limit, p.spare_items), (5, 12, 24, 7))
                    else:
                        self.assertEqual(p.output_target, 2)
                        self.assertEqual(p.source_limit, 3 if niche in {'anime', 'animation'} else 2)

    def test_unknown_or_disabled(self):
        for mode, niche in [('invalid', 'anime'), ('single', 'absent'), ('single', 'reactions')]:
            with self.assertRaises(ValueError):
                automatic_plan(mode, niche)

    def test_stale_counts_ignored_even_malformed(self):
        for mode in ('single', 'autopilot', 'ranking'):
            normal = StartRequest(mode=mode, niche='anime')
            old = StartRequest(mode=mode, niche='anime', max_videos='bad', max_clips=99999, rank_n=-20)
            self.assertEqual(cli_args_for(normal, None), cli_args_for(old, None))
            args = cli_args_for(old, None)
            if mode == 'ranking':
                self.assertEqual(args[args.index('--rank-n') + 1], '5')
                self.assertIn('--automatic-plan', args)
            elif mode == 'single':
                self.assertEqual(args[args.index('--max-clips') + 1], '1')
            else:
                self.assertEqual(args[args.index('--clips-per-niche') + 1], '2')

    def test_adapter_does_not_trust_arbitrary_request_counts(self):
        req = SimpleNamespace(mode='single', niche='anime', max_videos=999, max_clips=999)
        args = cli_args_for(req, None)
        self.assertEqual(args[args.index('--max-videos') + 1], '1')
        self.assertEqual(args[args.index('--max-clips') + 1], '1')

    def test_ranking_collects_replacements_not_only_five(self):
        from gta_autoclipper.ranking import build_ranking_video
        with patch('gta_autoclipper.ranking.load_config', return_value={'clips': {}}), \
             patch('gta_autoclipper.ranking.ensure_dirs'), \
             patch('gta_autoclipper.ranking.apply_niche_to_config', side_effect=lambda c, n: c), \
             patch('gta_autoclipper.ranking.collect_rank_items', return_value=[]) as collect:
            self.assertIsNone(build_ranking_video('anime', topic='Top 5 anime moments', automatic=True))
            self.assertEqual(collect.call_args.kwargs['n'], 12)
            self.assertEqual(collect.call_args.kwargs['max_probe'], 24)

    def test_http_compatibility_and_plan(self):
        from fastapi.testclient import TestClient
        from gta_autoclipper.jobrunner import JOB
        client = TestClient(create_app())
        response = client.get('/api/plan', params={'mode': 'ranking', 'niche': 'anime'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['output_target'], 5)
        self.assertEqual(client.get('/api/plan?mode=bad&niche=anime').status_code, 422)
        with patch.object(JOB, 'running', return_value=False), \
             patch.object(JOB, 'start') as start, \
             patch('gta_autoclipper.webapp._cfg', return_value={}):
            response = client.post('/api/start', json={'mode': 'single', 'niche': 'anime',
                                                      'max_videos': 'bad', 'max_clips': 999})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['plan']['output_target'], 1)
            args = start.call_args.args[0]
            self.assertEqual(args[args.index('--max-clips') + 1], '1')
            self.assertEqual(client.post('/api/start', json={'mode': 'bad'}).status_code, 422)

    def test_expert_cli_rank_count_preserved(self):
        from gta_autoclipper.__main__ import main
        with patch('gta_autoclipper.ranking.build_ranking_video', return_value=None) as build:
            main(['--ranking', '--niche', 'anime', '--rank-n', '7'])
            self.assertEqual(build.call_args.kwargs['n'], 7)
            self.assertFalse(build.call_args.kwargs['automatic'])

    def test_no_count_controls_or_payload(self):
        root = Path(__file__).resolve().parents[1] / 'gta_autoclipper/web'
        for file in (root / 'index.html', root / 'static/cutroom.js'):
            text = file.read_text()
            self.assertNotIn('maxVideos', text)
            self.assertNotIn('maxClips', text)
        self.assertIn('automaticPlan', (root / 'index.html').read_text())


if __name__ == '__main__':
    unittest.main()
