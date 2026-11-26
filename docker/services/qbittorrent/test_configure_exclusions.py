import importlib.util
import json
import io
import urllib.parse
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).parent
spec = importlib.util.spec_from_file_location('configure_exclusions', HERE / 'configure_exclusions.py')
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


class FakeAPI:
    def __init__(self, state):
        self.state = state.copy()
        self.posts = []
        self.reads = 0
        self.change_on_second_read = False
        self.fail_write = False
        self.bad_readback = False

    def read(self):
        self.reads += 1
        if self.change_on_second_read and self.reads == 2:
            self.state['excluded_file_names'] = '*.changed'
        return self.state.copy()

    def write(self, state):
        self.posts.append(state.copy())
        if self.fail_write:
            raise app.Error('write failed; state may have changed')
        if not self.bad_readback:
            self.state = state.copy()


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.backup = Path(self.tmp.name) / 'rollback.json'
        self.empty = {'excluded_file_names': '', 'excluded_file_names_enabled': False}
        self.desired = {'excluded_file_names': '*.exe', 'excluded_file_names_enabled': True}
        self.api = FakeAPI(self.empty)

    def test_apply_and_rollback(self):
        app.apply(self.api, self.desired, self.backup, 'https://example.test')
        self.assertEqual(self.api.state, self.desired)
        self.assertEqual(self.backup.stat().st_mode & 0o777, 0o600)
        self.assertEqual(set(self.api.posts[0]), set(self.empty))
        app.rollback(self.api, self.backup, 'https://example.test')
        self.assertEqual(self.api.state, self.empty)

    def test_idempotent(self):
        self.api.state = self.desired.copy()
        app.apply(self.api, self.desired, self.backup, 'https://example.test')
        self.assertFalse(self.api.posts)
        self.assertFalse(self.backup.exists())

    def test_existing_conflict_and_concurrent_change_stop(self):
        self.api.state['excluded_file_names'] = '*.srt'
        with self.assertRaises(app.Error):
            app.apply(self.api, self.desired, self.backup, 'https://example.test')
        self.assertFalse(self.api.posts)
        self.api.state = self.empty.copy()
        self.api.change_on_second_read = True
        with self.assertRaises(app.Error):
            app.apply(self.api, self.desired, self.backup, 'https://example.test')
        self.assertFalse(self.api.posts)

    def test_reviewed_replacement_can_apply_and_roll_back_existing_rules(self):
        self.api.state = {'excluded_file_names': '*.zip\n*.exe', 'excluded_file_names_enabled': True}
        previous = self.api.state.copy()
        app.apply(self.api, self.desired, self.backup, 'https://example.test', expected=app.fingerprint(previous))
        self.assertEqual(self.api.state, self.desired)
        app.rollback(self.api, self.backup, 'https://example.test')
        self.assertEqual(self.api.state, previous)

    def test_replacement_refuses_stale_fingerprint(self):
        self.api.state['excluded_file_names'] = '*.zip'
        with self.assertRaises(app.Error):
            app.apply(self.api, self.desired, self.backup, 'https://example.test', expected=app.fingerprint(self.empty))
        self.assertFalse(self.api.posts)

    def test_failures_preserve_backup(self):
        for attr in ('fail_write', 'bad_readback'):
            with self.subTest(attr=attr), tempfile.TemporaryDirectory() as directory:
                api = FakeAPI(self.empty)
                setattr(api, attr, True)
                backup = Path(directory) / 'rollback.json'
                with self.assertRaises(app.Error):
                    app.apply(api, self.desired, backup, 'https://example.test')
                self.assertTrue(backup.exists())

    def test_rollback_refuses_changed_target_and_other_server(self):
        app.apply(self.api, self.desired, self.backup, 'https://example.test')
        with self.assertRaises(app.Error):
            app.rollback(self.api, self.backup, 'https://other.test')
        self.api.state['excluded_file_names'] = '*.other'
        with self.assertRaises(app.Error):
            app.rollback(self.api, self.backup, 'https://example.test')
        self.assertEqual(len(self.api.posts), 1)

    def test_no_overwrite_of_rollback_record(self):
        self.backup.write_text('keep')
        with self.assertRaises(app.Error):
            app.apply(self.api, self.desired, self.backup, 'https://example.test')
        self.assertEqual(self.backup.read_text(), 'keep')
        self.assertFalse(self.api.posts)

    def test_api_auth_failure_does_not_expose_credentials(self):
        with patch.object(app.urllib.request, 'build_opener') as opener:
            opener.return_value.open.side_effect = OSError('secret')
            with self.assertRaises(app.Error) as error:
                app.API('https://example.test', 'user', 'secret')
            self.assertNotIn('secret', str(error.exception))

    def test_resolve_requests_only_named_secret(self):
        calls = []
        vault = {'PROJECT': 'project', 'ENVIRONMENT': 'prod',
                 'reference_parts': lambda value: ('/service', 'KEY'),
                 'request': lambda method, path: calls.append((method, path)) or {'secret': {'secretValue': 'value'}},
                 'value': lambda entry: entry['secretValue']}
        with patch.object(app.runpy, 'run_path', return_value=vault):
            self.assertEqual(app.resolve('vault://technis/service/key'), 'value')
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0][1].startswith('/api/v4/secrets/KEY?'))
        self.assertIn('secretPath=%2Fservice', calls[0][1])

    def test_media_policy(self):
        import fnmatch
        rules = (HERE / 'excluded-file-names.txt').read_text().splitlines()
        def excluded(name):
            return any(fnmatch.fnmatchcase(name.lower(), rule.lower()) for rule in rules)
        for name in ['movie.mkv.exe', 'file.EXE', 'payload.Bat']:
            self.assertTrue(excluded(name), name)
        for name in ['Up.To.Bat.mkv', 'film.mp4', 'film.webm', 'track.flac', 'track.mp3', 'track.m4a', 'sub.srt', 'sub.ass', 'sub.sub', 'cover.jpg', 'cover.png', 'cover.webp', 'comic.cbz', 'comic.cbr', 'comic.cb7', 'book.epub', 'book.pdf', 'book.mobi', 'book.azw3', 'release.rar', 'release.7z', 'release.zip', 'release.r00', 'release.part01.rar']:
            self.assertFalse(excluded(name), name)


class APITests(unittest.TestCase):
    def test_login_accepts_204_and_rejects_empty_200(self):
        for status, succeeds in ((204, True), (200, False)):
            with self.subTest(status=status):
                response = io.BytesIO(b'')
                response.status = status
                with patch.object(app.urllib.request, 'build_opener') as builder:
                    builder.return_value.open.return_value = response
                    if succeeds:
                        app.API('https://example.test', 'user', 'pass')
                    else:
                        with self.assertRaises(app.Error):
                            app.API('https://example.test', 'user', 'pass')

    def test_login_preferences_and_write_requests(self):
        class Opener:
            state = {'excluded_file_names': '', 'excluded_file_names_enabled': False}
            writes = []

            def open(self, req, timeout):
                self.assert_origin(req)
                if req.full_url.endswith('/api/v2/auth/login'):
                    self.login = urllib.parse.parse_qs(req.data.decode())
                    return io.BytesIO(b'Ok.')
                if req.full_url.endswith('/api/v2/app/preferences'):
                    return io.BytesIO(json.dumps({**self.state, 'password': 'private'}).encode())
                if req.full_url.endswith('/api/v2/app/setPreferences'):
                    self.state = json.loads(urllib.parse.parse_qs(req.data.decode())['json'][0])
                    self.writes.append(self.state)
                    return io.BytesIO(b'Ok.')
                raise AssertionError('unexpected request')

            def assert_origin(self, req):
                assert req.get_header('Origin') == 'https://example.test'
                assert req.get_header('Referer') == 'https://example.test/'

        opener = Opener()
        with patch.object(app.urllib.request, 'build_opener', return_value=opener):
            api = app.API('https://example.test', 'user', 'pass')
            self.assertEqual(opener.login, {'username': ['user'], 'password': ['pass']})
            with tempfile.TemporaryDirectory() as directory:
                desired = {'excluded_file_names': '*.exe', 'excluded_file_names_enabled': True}
                app.apply(api, desired, Path(directory) / 'backup', 'https://example.test')
                self.assertEqual(api.read(), desired)
                self.assertEqual(opener.writes, [desired])


if __name__ == '__main__':
    unittest.main()
