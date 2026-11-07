import importlib.util
import pathlib
import unittest
from unittest.mock import Mock, patch
import io
import json
from datetime import datetime, timezone

spec = importlib.util.spec_from_file_location('adapter', pathlib.Path(__file__).parents[1] / 'files' / 'adapter.py')
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 1, 31, tzinfo=timezone.utc)
        self.users = [{'id': 'u1', 'accounts': [{'server_id': 's1', 'server_type': 'plex', 'server_user_id': 'su1', 'external_user_id': '42'}]}]
        self.plays = [
            {'server_id': 's1', 'server_type': 'plex', 'user': {'server_user_id': 'su1', 'username': 'jay'}, 'rating_key': '101', 'parent_rating_key': '10', 'grandparent_rating_key': '1', 'media_type': 'episode', 'started_at': '2026-01-30T00:00:00Z', 'duration_ms': 600000},
            {'server_id': 's1', 'server_type': 'plex', 'user': {'server_user_id': 'su1', 'username': 'jay'}, 'rating_key': '2', 'parent_rating_key': None, 'grandparent_rating_key': None, 'media_type': 'movie', 'started_at': '2025-12-01T00:00:00Z', 'duration_ms': 300000},
        ]

    def test_show_stats_match_grandparent_and_windows(self):
        stats = adapter.translate('get_item_watch_time_stats', {'rating_key': '1'}, self.plays, self.users, 's1', self.now)
        self.assertEqual([(s['query_days'], s['total_plays']) for s in stats], [(0, 1), (7, 1), (30, 1)])

    def test_history_maps_plex_user_and_numeric_keys(self):
        result = adapter.translate('get_history', {'user_id': '42', 'start': '0', 'length': '100'}, self.plays, self.users, 's1', self.now)
        self.assertEqual(result['data'][0]['grandparent_rating_key'], 1)
        self.assertEqual(result['data'][0]['user_id'], 42)
        self.assertEqual(len(result['data']), 2)

    def test_watchers_and_user_stats(self):
        watchers = adapter.translate('get_item_user_stats', {'rating_key': '1'}, self.plays, self.users, 's1', self.now)
        self.assertEqual(watchers[0]['user_id'], 42)
        stats = adapter.translate('get_user_watch_time_stats', {'user_id': '42'}, self.plays, self.users, 's1', self.now)
        self.assertEqual(stats[0]['total_plays'], 2)

    def test_unknown_user_does_not_leak_history(self):
        result = adapter.translate('get_history', {'user_id': '999'}, self.plays, self.users, 's1', self.now)
        self.assertEqual(result['data'], [])

    def test_endpoint_authenticates_and_returns_tautulli_envelope(self):
        def request(path):
            sock = Mock()
            sock.makefile.return_value = io.BytesIO(f'GET {path} HTTP/1.0\r\n\r\n'.encode())
            adapter.Handler(sock, ('127.0.0.1', 1234), Mock())
            return b''.join(call.args[0] for call in sock.sendall.call_args_list)

        with patch.dict(adapter.os.environ, {'ADAPTER_API_KEY': 'secret', 'TRACEARR_API_KEY': 'trace', 'TRACEARR_SERVER_ID': 's1'}), patch.object(adapter, 'fetch_pages', side_effect=[self.users, self.plays]) as fetch:
            self.assertIn(b'403', request('/api/v2?apikey=wrong&cmd=get_history'))
            response = request('/api/v2?apikey=secret&cmd=get_history&user_id=42')
            headers, body = response.split(b'\r\n\r\n', 1)
            self.assertIn(b'HTTP/1.0 200 OK', headers)
            self.assertEqual(len(json.loads(body)['response']['data']['data']), 2)
            self.assertEqual(fetch.call_args_list[1].args, ('history', 'trace', 's1', 'u1'))

    def test_connection_check_reports_supported_tautulli_version(self):
        sock = Mock()
        sock.makefile.return_value = io.BytesIO(b'GET /api/v2?apikey=secret&cmd=get_tautulli_info HTTP/1.0\r\n\r\n')
        with patch.dict(adapter.os.environ, {'ADAPTER_API_KEY': 'secret', 'TRACEARR_API_KEY': 'trace'}), patch.object(adapter, 'fetch_pages', return_value=[]):
            adapter.Handler(sock, ('127.0.0.1', 1234), Mock())
        response = b''.join(call.args[0] for call in sock.sendall.call_args_list)
        body = json.loads(response.split(b'\r\n\r\n', 1)[1])
        self.assertEqual(body['response']['data']['tautulli_version'], '2.9.0')

    def test_cache_reuses_successes_but_not_failures(self):
        adapter.cache.clear()
        with patch.object(adapter, 'read_pages', side_effect=[ValueError('failed'), self.plays]) as read:
            with self.assertRaises(ValueError):
                adapter.fetch_pages('history', 'trace', 's1', 'u1')
            self.assertEqual(adapter.fetch_pages('history', 'trace', 's1', 'u1'), self.plays)
            self.assertEqual(adapter.fetch_pages('history', 'trace', 's1', 'u1'), self.plays)
            self.assertEqual(read.call_count, 2)

    def test_title_history_uses_show_media_id_or_movie_rating_key(self):
        shows = [{'rating_key': '1', 'media_id': 'show-id'}]
        with patch.object(adapter, 'fetch_pages', side_effect=[shows, self.plays[:1]]) as fetch:
            self.assertEqual(adapter.title_history('trace', 's1', '1'), self.plays[:1])
            self.assertEqual(fetch.call_args_list[1].kwargs, {'media_id': 'show-id'})
        with patch.object(adapter, 'fetch_pages', side_effect=[shows, self.plays[1:]]) as fetch:
            self.assertEqual(adapter.title_history('trace', 's1', '2'), self.plays[1:])
            self.assertEqual(fetch.call_args_list[1].kwargs, {'rating_key': '2'})

    def test_unmapped_show_fails_instead_of_reporting_zero(self):
        with patch.object(adapter, 'fetch_pages', return_value=[{'rating_key': '1', 'media_id': None}]):
            with self.assertRaises(ValueError):
                adapter.title_history('trace', 's1', '1')

    def test_title_filters_are_sent_to_tracearr(self):
        response = io.BytesIO(json.dumps({'data': [], 'meta': {'nextCursor': None}}).encode())
        with patch.object(adapter, 'urlopen', return_value=response) as urlopen:
            adapter.read_pages('history', 'trace', 's1', media_id='show-id')
        self.assertIn('media_id=show-id', urlopen.call_args.args[0].full_url)
        self.assertIn('server_id=s1', urlopen.call_args.args[0].full_url)

    def test_title_history_paginates_with_same_filters(self):
        pages = [
            io.BytesIO(json.dumps({'data': [{'id': 'one'}], 'meta': {'nextCursor': 'next'}}).encode()),
            io.BytesIO(json.dumps({'data': [{'id': 'two'}], 'meta': {'nextCursor': None}}).encode()),
        ]
        with patch.object(adapter, 'urlopen', side_effect=pages) as urlopen:
            result = adapter.read_pages('history', 'trace', 's1', rating_key='2')
        self.assertEqual([item['id'] for item in result], ['one', 'two'])
        self.assertIn('rating_key=2', urlopen.call_args_list[1].args[0].full_url)
        self.assertIn('cursor=next', urlopen.call_args_list[1].args[0].full_url)


if __name__ == '__main__':
    unittest.main()
