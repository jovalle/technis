#!/usr/bin/env python3
"""Read-only Tautulli API subset backed by Tracearr's public v2 API."""
import hmac
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen


def number(value):
    return int(value) if value is not None and str(value).isdigit() else 0


def translate(command, params, plays, users, server_id, now):
    accounts = {a['server_user_id']: number(a['external_user_id']) for u in users for a in u['accounts']
                if a['server_id'] == server_id and a['server_type'] == 'plex'}
    plays = [p for p in plays if p['server_id'] == server_id and p['server_type'] == 'plex']
    if command.startswith('get_item_'):
        key = params['rating_key']
        plays = [p for p in plays if key in (p['rating_key'], p['parent_rating_key'], p['grandparent_rating_key'])]
    else:
        user_id = number(params.get('user_id'))
        plays = [p for p in plays if user_id and accounts.get(p['user']['server_user_id']) == user_id]
    if command in ('get_item_watch_time_stats', 'get_user_watch_time_stats'):
        result = []
        for days in ([0, 7, 30] if command.startswith('get_item_') else [0]):
            selected = [p for p in plays if not days or (now - datetime.fromisoformat(p['started_at'].replace('Z', '+00:00'))).total_seconds() <= days * 86400]
            result.append({'query_days': days, 'total_plays': len(selected), 'total_time': sum(p['duration_ms'] for p in selected) // 1000})
        return result
    if command == 'get_item_user_stats':
        result = {}
        for p in plays:
            uid = accounts.get(p['user']['server_user_id'])
            if uid:
                row = result.setdefault(uid, {'user_id': uid, 'username': p['user']['username'], 'friendly_name': p['user']['username'], 'user_thumb': '', 'total_plays': 0, 'total_time': 0})
                row['total_plays'] += 1
                row['total_time'] += p['duration_ms'] // 1000
        return list(result.values())
    if command == 'get_history':
        plays = [p for p in plays if p['media_type'] in ('movie', 'episode')]
        start, length = number(params.get('start')), number(params.get('length', 100))
        records = [{'rating_key': number(p['rating_key']), 'parent_rating_key': number(p['parent_rating_key']),
                    'grandparent_rating_key': number(p['grandparent_rating_key']), 'media_type': p['media_type'],
                    'user_id': accounts[p['user']['server_user_id']]} for p in plays[start:start + length]]
        return {'data': records, 'recordsTotal': len(plays), 'recordsFiltered': len(plays)}
    raise ValueError('Unsupported command')


cache = {}
cache_lock = threading.Lock()


def fetch_pages(path, key, server_id=None, user_id=None, **filters):
    cache_key = (path, server_id, user_id, tuple(sorted(filters.items())))
    with cache_lock:
        cached = cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < 120:
            return cached[1]
        result = read_pages(path, key, server_id, user_id, **filters)
        if len(cache) >= 64:
            cache.pop(next(iter(cache)))
        cache[cache_key] = (time.monotonic(), result)
        return result


def read_pages(path, key, server_id=None, user_id=None, **filters):
    result, cursor, seen = [], None, set()
    while True:
        params = {'pageSize': 100}
        if cursor:
            params['cursor'] = cursor
        if server_id:
            params['server_id'] = server_id
        if user_id:
            params['user_id'] = user_id
        params.update(filters)
        request = Request('http://tracearr:3000/api/v2/public/' + path + '?' + urlencode(params), headers={'Authorization': 'Bearer ' + key})
        with urlopen(request, timeout=15) as response:
            page = json.load(response)
        result.extend(page['data'])
        cursor = page['meta']['nextCursor']
        if not cursor:
            return result
        if cursor in seen:
            raise ValueError('Tracearr repeated a pagination cursor')
        seen.add(cursor)


def title_history(key, server, rating_key):
    shows = fetch_pages('recently-added', key, server, media_type='show', include_removed='true')
    matches = [show for show in shows if show['rating_key'] == rating_key]
    if matches:
        ids = {show['media_id'] for show in matches}
        if len(ids) != 1 or None in ids:
            raise ValueError('Show has no unambiguous Tracearr media ID')
        return fetch_pages('history', key, server, media_id=ids.pop())
    return fetch_pages('history', key, server, rating_key=rating_key)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # The request URL contains Seerr's API key.
        pass

    def do_GET(self):
        path = urlsplit(self.path)
        if path.path == '/health':
            self.send_response(200)
            self.end_headers()
            return
        params = {k: v[0] for k, v in parse_qs(path.query).items()}
        supplied = params.get('apikey', '').encode()
        if path.path != '/api/v2' or not hmac.compare_digest(supplied, os.environ['ADAPTER_API_KEY'].encode()):
            self.send_error(403)
            return
        try:
            cmd = params.get('cmd', '')
            if cmd == 'get_tautulli_info':
                # Authenticate against Tracearr so Seerr's connection test is meaningful.
                fetch_pages('users', os.environ['TRACEARR_API_KEY'])
                data = {'tautulli_version': '2.9.0'}
            elif cmd in ('get_item_watch_time_stats', 'get_item_user_stats', 'get_user_watch_time_stats', 'get_history'):
                server = os.environ['TRACEARR_SERVER_ID']
                key = os.environ['TRACEARR_API_KEY']
                users = fetch_pages('users', key)
                if cmd in ('get_user_watch_time_stats', 'get_history'):
                    matches = [u['id'] for u in users if any(a['server_id'] == server and a['server_type'] == 'plex' and a['external_user_id'] == params.get('user_id') for a in u['accounts'])]
                    if len(matches) > 1:
                        raise ValueError('Ambiguous Plex user')
                    plays = fetch_pages('history', key, server, matches[0]) if matches else []
                else:
                    plays = title_history(key, server, params['rating_key'])
                data = translate(cmd, params, plays, users, server, datetime.now(timezone.utc))
            else:
                raise ValueError('Unsupported command')
            body = json.dumps({'response': {'result': 'success', 'message': None, 'data': data}}).encode()
        except Exception:
            logging.exception('Tracearr request failed')
            self.send_error(502, 'Tracearr request failed')
            return
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == '__main__':
    for name in ('ADAPTER_API_KEY', 'TRACEARR_API_KEY', 'TRACEARR_SERVER_ID'):
        if not os.environ.get(name):
            raise SystemExit(f'{name} is required')
    ThreadingHTTPServer(('0.0.0.0', 8181), Handler).serve_forever()
