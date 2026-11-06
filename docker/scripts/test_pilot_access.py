import importlib.util
import contextlib
import io
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('pilot_access', Path(__file__).with_name('pilot_access.py'))
access = importlib.util.module_from_spec(spec)
spec.loader.exec_module(access)


class AccessTest(unittest.TestCase):
    def test_fetches_only_the_pilot_key(self):
        calls = []

        def request(url, bearer, payload=None):
            calls.append((url, bearer, payload))
            if 'actions.githubusercontent.com' in url:
                return {'value': 'oidc-token'}
            if url.endswith('/api/v1/auth/oidc-auth/login'):
                return {'accessToken': 'infisical-token'}
            return {'secret': {'secretValue': 'test-private-key'}}

        env = {'ACTIONS_ID_TOKEN_REQUEST_URL': 'https://pipelines.actions.githubusercontent.com/token',
               'ACTIONS_ID_TOKEN_REQUEST_TOKEN': 'request-token',
               'INFISICAL_IDENTITY_ID': 'identity', 'INFISICAL_PROJECT_ID': 'project'}
        self.assertEqual(access.pilot_key(request, env), 'test-private-key')
        self.assertEqual(len(calls), 3)
        self.assertIn('/api/v4/secrets/PILOT_DEPLOY_SSH_KEY?', calls[-1][0])
        self.assertIn('secretPath=%2Fci%2Fpilot', calls[-1][0])

    def test_recovery_identity_requests_only_its_app_key(self):
        calls = []

        def request(url, bearer, payload=None):
            calls.append(url)
            if 'actions.githubusercontent.com' in url:
                return {'value': 'oidc-token'}
            if url.endswith('/api/v1/auth/oidc-auth/login'):
                return {'accessToken': 'infisical-token'}
            return {'secret': {'secretValue': 'test-app-key'}}

        env = {'ACTIONS_ID_TOKEN_REQUEST_URL': 'https://pipelines.actions.githubusercontent.com/token',
               'ACTIONS_ID_TOKEN_REQUEST_TOKEN': 'request-token',
               'INFISICAL_IDENTITY_ID': 'recovery-identity', 'INFISICAL_PROJECT_ID': 'project'}
        self.assertEqual(access.infisical_secret(request, env, '/ci/pilot-recovery', 'PILOT_GITHUB_APP_KEY'), 'test-app-key')
        self.assertIn('/api/v4/secrets/PILOT_GITHUB_APP_KEY?', calls[-1])
        self.assertIn('secretPath=%2Fci%2Fpilot-recovery', calls[-1])

    def test_rejects_untrusted_oidc_url(self):
        env = {'ACTIONS_ID_TOKEN_REQUEST_URL': 'https://example.org/token',
               'ACTIONS_ID_TOKEN_REQUEST_TOKEN': 'request-token',
               'INFISICAL_IDENTITY_ID': 'identity', 'INFISICAL_PROJECT_ID': 'project'}
        with self.assertRaises(ValueError):
            access.pilot_key(lambda *args: {}, env)

    def test_accepts_regional_github_oidc_endpoint(self):
        env = {'ACTIONS_ID_TOKEN_REQUEST_URL': 'https://pipelinesghubeus13.actions.githubusercontent.com/token',
               'ACTIONS_ID_TOKEN_REQUEST_TOKEN': 'request-token',
               'INFISICAL_IDENTITY_ID': 'identity', 'INFISICAL_PROJECT_ID': 'project'}
        def request(url, bearer, payload=None):
            if 'actions.githubusercontent.com' in url:
                return {'value': 'jwt'}
            if url.endswith('/api/v1/auth/oidc-auth/login'):
                return {'accessToken': 'token'}
            return {'secret': {'secretValue': 'key'}}
        self.assertEqual(access.pilot_key(request, env), 'key')

    def test_rejects_untrusted_remote_command_and_host(self):
        for operation, digest, host in [('shell', 'a' * 64, 'nexus'),
                                        ('deploy', 'a' * 64 + ';id', 'nexus'),
                                        ('deploy', 'a' * 64, 'nexus -o ProxyCommand=id')]:
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                access.validate_request(operation, digest, host)

    def test_key_is_passed_to_agent_stdin_not_command_arguments(self):
        calls = []
        key = '-----BEGIN ' 'OPENSSH PRIVATE KEY-----\ntest-only\n-----END OPENSSH PRIVATE KEY-----\n'

        def run(args, **kwargs):
            calls.append((args, kwargs))
            if args[:2] == ['/usr/bin/ssh-agent', '-a']:
                self.assertIn('/pilot-agent-', args[2])
                return SimpleNamespace(stdout='SSH_AUTH_SOCK=' + args[2] + ';\nSSH_AGENT_PID=123;\n')
            return SimpleNamespace(returncode=0, stdout='')

        with patch.object(access.subprocess, 'run', side_effect=run):
            self.assertEqual(access.ssh_call('deploy', 'a' * 64, 'nexus', 'ssh-ed25519 dGVzdA==', key), 0)
        self.assertEqual(calls[1][1]['input'], key)
        self.assertTrue(all(key not in str(args) for args, _ in calls))

    def test_identity_failure_does_not_print_sensitive_error_details(self):
        env = {'PILOT_HOST': 'nexus'}
        output = io.StringIO()
        with patch.dict(access.os.environ, env, clear=True), \
             patch.object(access, 'pilot_key', side_effect=RuntimeError('sensitive detail')), \
             contextlib.redirect_stderr(output):
            self.assertEqual(access.main(['deploy', 'a' * 64]), 1)
        self.assertNotIn('sensitive detail', output.getvalue())


if __name__ == '__main__':
    unittest.main()
