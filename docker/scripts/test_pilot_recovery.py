import importlib.util
import json
import base64
import os
import subprocess
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('pilot_recovery', Path(__file__).with_name('pilot_recovery.py'))
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


class RecoveryTest(unittest.TestCase):
    def test_app_jwt_is_signed_without_writing_a_private_key(self):
        key = subprocess.run(['/usr/bin/openssl', 'genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:2048'],
                             capture_output=True, check=True).stdout
        public = subprocess.run(['/usr/bin/openssl', 'pkey', '-pubout'], input=key,
                                capture_output=True, check=True).stdout
        token = recovery.app_jwt('123', key.decode(), now=2000000000)
        header, payload, signature = token.split('.')
        decode = lambda value: base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))
        self.assertEqual(json.loads(decode(payload))['iss'], '123')
        pub_r, pub_w = os.pipe()
        sig_r, sig_w = os.pipe()
        try:
            os.write(pub_w, public)
            os.write(sig_w, decode(signature))
            os.close(pub_w)
            os.close(sig_w)
            result = subprocess.run(['/usr/bin/openssl', 'dgst', '-sha256', '-verify', f'/dev/fd/{pub_r}',
                                     '-signature', f'/dev/fd/{sig_r}'], input=(header + '.' + payload).encode(),
                                    pass_fds=(pub_r, sig_r), capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        finally:
            os.close(pub_r)
            os.close(sig_r)

    def test_recovery_app_token_requests_only_contents_write(self):
        calls = []

        def api(method, route, payload, token):
            calls.append((method, route, payload, token))
            return {'token': 'short-lived-test-token'}

        with patch.object(recovery, 'app_jwt', return_value='test-jwt'):
            result = recovery.installation_token(api, '123', '456', 'test-key')
        self.assertEqual(result, 'short-lived-test-token')
        self.assertEqual(calls, [('POST', '/app/installations/456/access_tokens',
                                  {'permissions': {'contents': 'write'}, 'repositories': ['technis']}, 'test-jwt')])

    def setUp(self):
        self.before = 'services:\n  flaresolverr:\n    image: ghcr.io/flaresolverr/flaresolverr:latest@sha256:' + 'a' * 64 + '\n'
        self.after = self.before.replace('a' * 64, 'b' * 64)

    def test_reverts_only_the_failed_candidate(self):
        self.assertEqual(recovery.restore_image(self.before, self.after, 'b' * 64), self.before)

    def test_refuses_an_advanced_image_or_configuration_change(self):
        with self.assertRaises(ValueError):
            recovery.restore_image(self.before, self.after.replace('b' * 64, 'c' * 64), 'b' * 64)
        with self.assertRaises(ValueError):
            recovery.restore_image(self.before, self.after + '    user: changed\n', 'b' * 64)

    def test_rejects_ambiguous_images(self):
        with self.assertRaises(ValueError):
            recovery.restore_image(self.before, self.after + self.after, 'b' * 64)

    def test_recovery_disables_pilot_automerge_in_same_change(self):
        result = recovery.recovery_files(self.before, self.after, '{"enabled": true, "foo": 1}', 'b' * 64)
        self.assertEqual(result['docker/services/flaresolverr/compose.yaml'], self.before)
        config = json.loads(result['renovate.json'])
        self.assertTrue(config['enabled'])
        self.assertEqual(config['packageRules'][-1]['matchPackageNames'], ['ghcr.io/flaresolverr/flaresolverr'])
        self.assertFalse(config['packageRules'][-1]['automerge'])

    def test_pause_changes_only_pilot_automerge(self):
        config = json.loads(recovery.pause_automerge('{"enabled": true, "foo": 1}'))
        self.assertTrue(config['enabled'])
        self.assertEqual(config['foo'], 1)
        self.assertFalse(config['packageRules'][-1]['automerge'])

    def test_pause_commit_uses_head_precondition(self):
        calls = []

        def api(method, route, payload=None):
            calls.append((method, route, payload))
            if route.endswith('/git/ref/heads/main'):
                return {'object': {'sha': '2' * 40}}
            if '/contents/renovate.json' in route:
                return {'encoding': 'base64', 'content': base64.b64encode(b'{"enabled":true}').decode()}
            if route == '/graphql':
                return {'data': {'createCommitOnBranch': {'commit': {'oid': '5' * 40}}}}
            raise AssertionError(route)

        recovery.pause(api)
        commit = calls[-1][2]['variables']['input']
        self.assertEqual(commit['expectedHeadOid'], '2' * 40)
        self.assertEqual([item['path'] for item in commit['fileChanges']['additions']], ['renovate.json'])

    def test_git_recovery_uses_atomic_head_precondition(self):
        calls = []
        before_sha = '1' * 40
        head_sha = '2' * 40

        def api(method, route, payload=None):
            calls.append((method, route, payload))
            if route.endswith('/git/ref/heads/main'):
                return {'object': {'sha': head_sha}}
            if '/contents/' in route:
                text = ('{"enabled":true}' if 'renovate.json' in route else
                        self.before if 'ref=' + before_sha in route else self.after)
                return {'encoding': 'base64', 'content': base64.b64encode(text.encode()).decode()}
            if route == '/graphql':
                return {'data': {'createCommitOnBranch': {'commit': {'oid': '5' * 40}}}}
            raise AssertionError(route)

        recovery.reconcile(api, before_sha, 'b' * 64)
        commit = calls[-1][2]['variables']['input']
        self.assertEqual(commit['expectedHeadOid'], head_sha)
        self.assertEqual({entry['path'] for entry in commit['fileChanges']['additions']},
                         {'renovate.json', 'docker/services/flaresolverr/compose.yaml'})

    def test_recovery_never_force_updates_a_changed_main(self):
        attempted = []

        def api(method, route, payload=None):
            if route.endswith('/git/ref/heads/main'):
                return {'object': {'sha': '2' * 40}}
            if '/contents/' in route:
                text = ('{"enabled":true}' if 'renovate.json' in route else
                        self.before if 'ref=' + '1' * 40 in route else self.after)
                return {'encoding': 'base64', 'content': base64.b64encode(text.encode()).decode()}
            if route == '/graphql':
                attempted.append(payload['variables']['input']['expectedHeadOid'])
                raise RuntimeError('branch advanced')
            raise AssertionError(route)

        with self.assertRaisesRegex(RuntimeError, 'branch advanced'):
            recovery.reconcile(api, '1' * 40, 'b' * 64)
        self.assertEqual(attempted, ['2' * 40])

    def test_issue_reports_only_validated_identifiers_and_fixed_status(self):
        calls = []

        def api(method, route, payload=None):
            calls.append((method, route, payload))
            return {'number': 123}

        recovery.report(api, 'b' * 64, '456', 'restored', 'reconciled')
        body = calls[0][2]['body']
        self.assertIn('b' * 64, body)
        self.assertIn('https://github.com/jovalle/technis/actions/runs/456', body)
        self.assertIn('Git was reconciled', body)
        with self.assertRaises(ValueError):
            recovery.report(api, 'b' * 64, '456', 'untrusted logs', 'reconciled')


if __name__ == '__main__':
    unittest.main()
