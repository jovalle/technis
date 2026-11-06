import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('pilot_pr_guard', Path(__file__).with_name('pilot_pr_guard.py'))
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class GuardTest(unittest.TestCase):
    def test_paused_policy_blocks_a_digest_update(self):
        old = 'services:\n  flaresolverr:\n    image: ghcr.io/flaresolverr/flaresolverr:latest@sha256:' + 'a' * 64 + '\n'
        new = old.replace('a' * 64, 'b' * 64)
        rule = {'matchManagers': ['docker-compose'],
                'matchFileNames': ['docker/services/flaresolverr/compose.yaml'],
                'matchPackageNames': ['ghcr.io/flaresolverr/flaresolverr'], 'automerge': True}
        guard.check(old, new, json.dumps({'enabled': True, 'packageRules': [rule]}))
        rule['automerge'] = False
        with self.assertRaises(ValueError):
            guard.check(old, new, json.dumps({'enabled': True, 'packageRules': [rule]}))

    def test_unchanged_service_does_not_block_other_pull_requests(self):
        guard.check('unchanged', 'unchanged', '{"enabled": false}')

    def test_pull_request_cannot_override_a_paused_base_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def git(*args):
                return subprocess.run(['git', *args], cwd=root, check=True, text=True, capture_output=True).stdout.strip()
            git('init', '-q')
            git('config', 'user.email', 'pilot@example.invalid')
            git('config', 'user.name', 'Pilot Test')
            service = root / guard.PATH
            service.parent.mkdir(parents=True)
            old = 'services:\n  flaresolverr:\n    image: ghcr.io/flaresolverr/flaresolverr:latest@sha256:' + 'a' * 64 + '\n'
            service.write_text(old)
            (root / 'renovate.json').write_text('{"enabled":false}')
            git('add', '.')
            git('commit', '-qm', 'baseline')
            base = git('rev-parse', 'HEAD')
            service.write_text(old.replace('a' * 64, 'b' * 64))
            (root / 'renovate.json').write_text('{"enabled":true}')
            event = root / 'event.json'
            event.write_text(json.dumps({'pull_request': {'base': {'sha': base}}}))
            result = subprocess.run(['python3', str(Path(__file__).with_name('pilot_pr_guard.py'))], cwd=root,
                                    env={**os.environ, 'GITHUB_EVENT_NAME': 'pull_request', 'GITHUB_EVENT_PATH': str(event)},
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)


if __name__ == '__main__':
    unittest.main()
