#!/usr/bin/env python3
"""Apply the reviewed qBittorrent exclusion policy through its Web API."""
import argparse
import hashlib
import http.cookiejar
import json
import os
from pathlib import Path
import runpy
import sys
import urllib.error
import urllib.parse
import urllib.request

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
KEYS = ('excluded_file_names', 'excluded_file_names_enabled')


class Error(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Error('API returned a redirect; check the configured URL')


class API:
    def __init__(self, url, username, password):
        self.url = url.rstrip('/')
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()), NoRedirect())
        if self.request('/api/v2/auth/login', {'username': username, 'password': password}) != b'Ok.':
            raise Error('qBittorrent authentication failed')

    def request(self, path, data=None):
        payload = None if data is None else urllib.parse.urlencode(data).encode()
        req = urllib.request.Request(self.url + path, data=payload,
                                     headers={'Origin': self.url, 'Referer': self.url + '/'})
        try:
            with self.opener.open(req, timeout=15) as response:
                body = response.read()
                if path == '/api/v2/auth/login' and getattr(response, 'status', None) == 204:
                    return b'Ok.'
                return body
        except (OSError, urllib.error.URLError):
            raise Error('qBittorrent API request failed; state may have changed') from None

    def read(self):
        try:
            prefs = json.loads(self.request('/api/v2/app/preferences'))
            result = {key: prefs[key] for key in KEYS}
            if not isinstance(result[KEYS[0]], str) or type(result[KEYS[1]]) is not bool:
                raise ValueError
            return result
        except (ValueError, KeyError, TypeError):
            raise Error('qBittorrent returned invalid exclusion settings') from None

    def write(self, state):
        self.request('/api/v2/app/setPreferences', {'json': json.dumps(state)})


def fingerprint(state):
    return hashlib.sha256(json.dumps(state, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def apply(api, desired, backup, url, expected=None):
    before = api.read()
    if before == desired:
        print('Exclusion policy already applied.')
        return
    existing = set(before[KEYS[0]].splitlines()) - {''}
    policy = set(desired[KEYS[0]].splitlines())
    if expected is not None and fingerprint(before) != expected:
        raise Error('Existing exclusions no longer match the reviewed fingerprint')
    if existing and existing != policy and expected is None:
        print('Existing exclusions differ from the reviewed policy:')
        for line in sorted(existing - policy):
            print('  existing only:', line)
        for line in sorted(policy - existing):
            print('  policy only:', line)
        print('Current settings fingerprint:', fingerprint(before))
        raise Error('Review the differences before applying with --expected-state')
    record = {'url': url, 'before': before, 'applied': desired}
    try:
        fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(fd, 'w') as output:
            json.dump(record, output)
            output.flush()
            os.fsync(output.fileno())
    except OSError:
        raise Error('Could not create a new private rollback record; no settings changed') from None
    if api.read() != before:
        raise Error('Settings changed concurrently; no settings written; rollback record retained')
    api.write(desired)
    if api.read() != desired:
        raise Error('Verification failed; state may have changed; rollback record retained')
    print('Exclusion policy applied and verified.')


def rollback(api, backup, url):
    try:
        if backup.is_symlink() or backup.stat().st_mode & 0o077:
            raise ValueError
        record = json.loads(backup.read_text())
        if record['url'] != url or set(record['before']) != set(KEYS) or set(record['applied']) != set(KEYS):
            raise ValueError
        before, applied = record['before'], record['applied']
        for state in (before, applied):
            if not isinstance(state[KEYS[0]], str) or type(state[KEYS[1]]) is not bool:
                raise ValueError
    except (OSError, ValueError, KeyError, TypeError):
        raise Error('Invalid or inaccessible private rollback record') from None
    if api.read() != applied:
        raise Error('Current exclusions differ from the recorded applied state; rollback stopped')
    api.write(before)
    if api.read() != before:
        raise Error('Rollback verification failed; state may have changed; record retained')
    print('Previous exclusion settings restored and verified.')


def resolve(value):
    try:
        vault = runpy.run_path(str(ROOT / 'bin/vault'))
        folder, name = vault['reference_parts'](value)
        query = urllib.parse.urlencode({'projectId': vault['PROJECT'],
                                        'environment': vault['ENVIRONMENT'],
                                        'secretPath': folder, 'expandSecretReferences': 'false'})
        result = vault['request']('GET', '/api/v4/secrets/' + urllib.parse.quote(name, safe='') + '?' + query)
        return vault['value'](result['secret'])
    except (OSError, ValueError, KeyError, TypeError):
        raise Error('Could not resolve connection settings from Infisical') from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['apply', 'rollback'])
    parser.add_argument('--record', type=Path, required=True, help='private rollback record path')
    parser.add_argument('--expected-state', help='reviewed existing exclusion settings fingerprint')
    args = parser.parse_args()
    try:
        url, username, password = (resolve(os.environ.get(key, '')) for key in
                                   ('QBITTORRENT_URL', 'QBITTORRENT_USERNAME', 'QBITTORRENT_PASSWORD'))
        url = url.rstrip('/')
        parsed = urllib.parse.urlsplit(url)
        if parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment or not parsed.hostname:
            raise Error('URL must be an origin without credentials or a path')
        if parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in ('localhost', '127.0.0.1', '::1')):
            raise Error('Use HTTPS, or HTTP only for a local connection')
        api = API(url, username, password)
        if args.action == 'apply':
            policy = (HERE / 'excluded-file-names.txt').read_text().strip()
            if not policy:
                raise Error('Exclusion policy is empty')
            apply(api, {KEYS[0]: policy, KEYS[1]: True}, args.record, url, args.expected_state)
        else:
            if args.expected_state is not None:
                raise Error('--expected-state is only valid with apply')
            rollback(api, args.record, url)
    except (Error, OSError, ValueError) as error:
        print(f'Error: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
