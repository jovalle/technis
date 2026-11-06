import importlib.util
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import sys
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location('pilot_update', __file__.replace('test_pilot_update.py', 'pilot_update.py'))
update = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = update
spec.loader.exec_module(update)


class PilotUpdateTest(unittest.TestCase):
    def test_just_update_pilot_previews_selected_image(self):
        candidate = 'ghcr.io/flaresolverr/flaresolverr@sha256:' + 'a' * 64
        result = subprocess.run(
            ['just', 'update', 'pilot', 'nexus/flaresolverr', candidate],
            cwd=update.ROOT, text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Preview only', result.stdout)
        self.assertIn('Preview only', result.stdout)
        self.assertIn(candidate, result.stdout)

    def test_just_update_still_forwards_regular_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'justfile').write_text((update.ROOT / 'justfile').read_text())
            runner = root / 'docker/scripts/stacks'
            runner.parent.mkdir(parents=True)
            runner.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
            runner.chmod(0o755)
            result = subprocess.run(['just', 'update', 'nexus/immich'], cwd=root,
                                    text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ['upgrade', 'nexus/immich'])

    def test_pilot_model_refuses_unresolved_secrets(self):
        model = {'services': {'flaresolverr': {'environment': {'PASSWORD': 'TECHNIS_PILOT_SECRET'}}}}
        with patch.object(update, 'run', return_value=json.dumps(model)):
            with self.assertRaisesRegex(RuntimeError, 'secret'):
                update.pilot_model()

    def test_pilot_model_is_scoped_to_service_without_fetching_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / 'vault-invoked'
            python = Path(tmp) / 'python3'
            python.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 99\n')
            python.chmod(0o755)
            with patch.dict(os.environ, {'PATH': tmp + os.pathsep + os.environ['PATH']}):
                model = update.pilot_model()
            self.assertFalse(marker.exists())
        self.assertEqual(set(model['services']), {'flaresolverr'})
        self.assertEqual(model['services']['flaresolverr']['environment']['LOG_LEVEL'], 'info')
        update.validate('nexus/flaresolverr', model['services']['flaresolverr']['image'])

    def test_pilot_model_refuses_added_configuration(self):
        model = update.pilot_model()
        model['services']['flaresolverr']['environment']['PASSWORD'] = 'a literal'
        with patch.object(update, 'run', return_value=json.dumps(model)):
            with self.assertRaisesRegex(RuntimeError, 'environment changed'):
                update.pilot_model()

    def test_pilot_model_refuses_unreviewed_environment_values(self):
        model = update.pilot_model()
        model['services']['flaresolverr']['environment']['LOG_LEVEL'] = 'unexpected'
        with patch.object(update, 'run', return_value=json.dumps(model)):
            with self.assertRaisesRegex(RuntimeError, 'environment changed'):
                update.pilot_model()

    def test_host_local_model_renders_without_a_nexus_context(self):
        model = update.pilot_model()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'pilot-model.json').write_text(json.dumps(model))
            with patch.object(update, 'CONTEXT', 'default'), patch.object(update, 'ROOT', root):
                model = update.pilot_model()
        self.assertEqual(set(model['services']), {'flaresolverr'})

    def test_rejects_unqualified_target_and_mutable_candidate(self):
        with self.assertRaises(ValueError):
            update.validate('nexus/boxarr', 'ghcr.io/flaresolverr/flaresolverr@sha256:' + 'a' * 64)
        with self.assertRaises(ValueError):
            update.validate('nexus/flaresolverr', 'ghcr.io/flaresolverr/flaresolverr:latest')

    def test_accepts_tag_and_digest_together(self):
        update.validate('nexus/flaresolverr', 'ghcr.io/flaresolverr/flaresolverr:latest@sha256:' + 'a' * 64)

    def test_context_checks_distinguish_remote_and_host_local_deployments(self):
        update.validate_endpoint('ssh://nexus', local=False)
        update.validate_endpoint('unix:///var/run/docker.sock', local=True)
        with self.assertRaises(ValueError):
            update.validate_endpoint('ssh://other-host', local=False)
        with self.assertRaises(ValueError):
            update.validate_endpoint('ssh://nexus', local=True)

    def test_failed_update_restores_and_checks_previous_image(self):
        actions = []

        def deploy(image):
            actions.append(image)
            if len(actions) == 1:
                raise RuntimeError('candidate failed')

        with self.assertRaises(update.Recovered):
            update.apply_images('new', 'old', deploy)
        self.assertEqual(actions, ['new', 'old'])

    def test_failed_rollback_is_distinct_from_successful_recovery(self):
        def deploy(image):
            raise RuntimeError('unhealthy')

        with self.assertRaises(update.RecoveryFailed):
            update.apply_images('new', 'old', deploy)

    def test_success_never_runs_rollback(self):
        actions = []
        update.apply_images('new', 'old', actions.append)
        self.assertEqual(actions, ['new'])

    def test_observation_catches_a_service_that_fails_after_startup(self):
        with patch.object(update, 'current', side_effect=['sha256:one', RuntimeError('unhealthy')]), \
             patch.object(update.time, 'sleep'):
            with self.assertRaises(RuntimeError):
                update.observe('sha256:one')

    def test_git_change_during_deployment_fails_validation(self):
        with self.assertRaisesRegex(RuntimeError, 'Git'):
            update.verify_head('1' * 40, lambda: ('2' * 40, '', ''))
        update.verify_head('1' * 40, lambda: ('1' * 40, '', ''))

    def test_failed_rollback_keeps_the_remote_lock(self):
        calls = []
        old = 'sha256:' + 'b' * 64
        candidate = 'ghcr.io/flaresolverr/flaresolverr@sha256:' + 'a' * 64

        def fake_docker(*args):
            calls.append(args)
            if args[:2] == ('image', 'inspect'):
                return old
            return ''

        with patch.object(sys, 'argv', ['pilot_update.py', 'nexus/flaresolverr', candidate, '--apply']), \
             patch.object(update, 'pilot_model', return_value={}), \
             patch.object(update, 'run', return_value='ssh://nexus'), \
             patch.object(update, 'docker', side_effect=fake_docker), \
             patch.object(update, 'current', return_value=old), \
             patch.object(update, 'deploy', side_effect=RuntimeError('failed')), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(update.main(), 11)
        self.assertTrue(any(call[0] == 'create' for call in calls))
        self.assertFalse(any(call[0] == 'rm' for call in calls))

    def test_recovered_remote_failure_keeps_lock_until_git_is_reconciled(self):
        calls = []
        old = 'sha256:' + 'b' * 64
        candidate = 'ghcr.io/flaresolverr/flaresolverr@sha256:' + 'a' * 64

        def fake_docker(*args):
            calls.append(args)
            return old if args[:2] == ('image', 'inspect') else ''

        def fail_candidate(image, expected, model):
            if image != old:
                raise RuntimeError('unhealthy')

        with patch.object(sys, 'argv', ['pilot_update.py', 'nexus/flaresolverr', candidate, '--apply', '--hold-lock']), \
             patch.object(update, 'pilot_model', return_value={}), \
             patch.object(update, 'run', return_value='ssh://nexus'), \
             patch.object(update, 'docker', side_effect=fake_docker), \
             patch.object(update, 'current', return_value=old), \
             patch.object(update, 'deploy', side_effect=fail_candidate), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(update.main(), 10)
        self.assertTrue(any(call[0] == 'create' for call in calls))
        self.assertFalse(any(call[0] == 'rm' for call in calls))


if __name__ == '__main__':
    unittest.main()
