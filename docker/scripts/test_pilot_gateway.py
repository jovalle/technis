import importlib.util
import json
import pathlib
import unittest

spec = importlib.util.spec_from_file_location('pilot_gateway', pathlib.Path(__file__).with_name('pilot_gateway.py'))
gateway = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway)


class GatewayTest(unittest.TestCase):
    def test_accepts_only_pilot_digest_deployment(self):
        digest = 'a' * 64
        self.assertEqual(gateway.candidate('deploy sha256:' + digest),
                         'ghcr.io/flaresolverr/flaresolverr@sha256:' + digest)

    def test_rejects_shell_syntax_and_other_commands(self):
        for value in ('', 'shell', 'deploy sha256:' + 'a' * 64 + '; id',
                      'deploy sha256:' + 'a' * 63, 'deploy sha256:' + 'A' * 64,
                      'deploy sha256:' + 'a' * 64 + '\nwhoami'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                gateway.candidate(value)

    def test_only_current_git_digest_can_be_deployed(self):
        requested = 'ghcr.io/flaresolverr/flaresolverr@sha256:' + 'a' * 64
        good = 'services:\n  flaresolverr:\n    image: ghcr.io/flaresolverr/flaresolverr:latest@sha256:' + 'a' * 64
        gateway.verify_desired(requested, good)
        with self.assertRaises(ValueError):
            gateway.verify_desired(requested, good.replace('a' * 64, 'b' * 64))
        with self.assertRaises(ValueError):
            gateway.verify_desired(requested, good + '\n    image: ' + requested)

    def test_rejects_installed_configuration_drift(self):
        old = 'services:\n  flaresolverr:\n    image: ghcr.io/flaresolverr/flaresolverr:latest@sha256:' + 'a' * 64 + '\n'
        new = old.replace('a' * 64, 'b' * 64)
        gateway.verify_installed_config(old, new)
        with self.assertRaises(ValueError):
            gateway.verify_installed_config(old, new + '    user: changed\n')

    def test_unlock_requires_healthy_previous_image_and_matching_digest(self):
        image = 'sha256:' + 'b' * 64
        reference = 'ghcr.io/flaresolverr/flaresolverr@sha256:' + 'a' * 64
        calls = []

        def docker(*args):
            calls.append(args)
            if args[0] == 'inspect' and args[2] == '{{.Image}}':
                return image
            if args[0] == 'inspect':
                return json.dumps({'Status': 'running', 'Health': {'Status': 'healthy'}})
            if args[:2] == ('image', 'inspect'):
                return json.dumps([reference])
            return ''

        gateway.unlock(reference, docker)
        self.assertEqual(calls[-1], ('rm', 'technis-update-lock-flaresolverr'))
        calls.clear()
        with self.assertRaises(ValueError):
            gateway.unlock(reference.replace('a' * 64, 'c' * 64), docker)
        self.assertFalse(any(args[0] == 'rm' for args in calls))

    def test_deployment_rejects_paused_automerge_policy(self):
        rule = {'matchManagers': ['docker-compose'],
                'matchFileNames': ['docker/services/flaresolverr/compose.yaml'],
                'matchPackageNames': ['ghcr.io/flaresolverr/flaresolverr'], 'automerge': True}
        gateway.verify_policy(json.dumps({'enabled': True, 'packageRules': [rule]}))
        rule['automerge'] = False
        with self.assertRaises(ValueError):
            gateway.verify_policy(json.dumps({'enabled': True, 'packageRules': [rule]}))
        with self.assertRaises(ValueError):
            gateway.verify_policy(json.dumps({'enabled': False, 'packageRules': [rule]}))

    def test_snapshot_rejects_a_changed_git_head(self):
        calls = []

        def read(url):
            calls.append(url)
            if '/git/ref/heads/main' in url:
                sha = '1' * 40 if sum('/git/ref/heads/main' in x for x in calls) == 1 else '2' * 40
                return json.dumps({'object': {'sha': sha}})
            return 'file'

        with self.assertRaisesRegex(ValueError, 'changed'):
            gateway.snapshot(read)

    def test_snapshot_returns_one_consistent_revision(self):
        def read(url):
            if '/git/ref/heads/main' in url:
                return json.dumps({'object': {'sha': '1' * 40}})
            return 'content'
        self.assertEqual(gateway.snapshot(read), ('1' * 40, 'content', 'content'))

if __name__ == '__main__':
    unittest.main()
