"""Fail-closed preparation of the pilot's Git recovery."""

import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

IMAGE = re.compile(
    r'(?m)^(?P<prefix>[ \t]*image:[ \t]*ghcr\.io/flaresolverr/flaresolverr'
    r'(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?@sha256:)'
    r'(?P<digest>[0-9a-f]{64})(?P<suffix>[ \t]*)$'
)


def app_jwt(app_id, private_key, *, now=None):
    if not re.fullmatch(r'[0-9]+', app_id) or not isinstance(private_key, str):
        raise ValueError('invalid GitHub App identity')
    key = private_key.encode()
    if len(key) > 4096 or not key.startswith(b'-----BEGIN'):
        raise ValueError('invalid GitHub App key')
    now = int(time.time()) if now is None else int(now)
    encode = lambda value: base64.urlsafe_b64encode(value).decode().rstrip('=')
    header = encode(json.dumps({'alg': 'RS256', 'typ': 'JWT'}, separators=(',', ':')).encode())
    payload = encode(json.dumps({'iat': now - 60, 'exp': now + 540, 'iss': app_id}, separators=(',', ':')).encode())
    message = (header + '.' + payload).encode()
    reader, writer = os.pipe()
    try:
        os.write(writer, key)
        os.close(writer)
        writer = -1
        result = subprocess.run(['/usr/bin/openssl', 'dgst', '-sha256', '-sign', f'/dev/fd/{reader}'],
                                input=message, pass_fds=(reader,), capture_output=True, check=True, timeout=20)
    finally:
        os.close(reader)
        if writer != -1:
            os.close(writer)
    return header + '.' + payload + '.' + encode(result.stdout)


def installation_token(api, app_id, installation_id, private_key):
    if not re.fullmatch(r'[0-9]+', installation_id):
        raise ValueError('invalid GitHub App installation')
    result = api('POST', '/app/installations/' + installation_id + '/access_tokens',
                 {'permissions': {'contents': 'write'}, 'repositories': ['technis']}, app_jwt(app_id, private_key))
    token = result['token']
    if not isinstance(token, str) or not token:
        raise ValueError('GitHub App token is unavailable')
    return token


def restore_image(previous, current, failed_digest):
    old = list(IMAGE.finditer(previous))
    new = list(IMAGE.finditer(current))
    if len(old) != 1 or len(new) != 1 or new[0]['digest'] != failed_digest:
        raise ValueError('pilot image changed or is ambiguous')
    restored = current[:new[0].start('digest')] + old[0]['digest'] + current[new[0].end('digest'):]
    if restored != previous:
        raise ValueError('pilot configuration changed; automatic recovery is unsafe')
    return restored


def pause_automerge(renovate):
    config = json.loads(renovate)
    if not isinstance(config, dict):
        raise ValueError('invalid Renovate configuration')
    rules = config.setdefault('packageRules', [])
    if not isinstance(rules, list):
        raise ValueError('invalid Renovate package rules')
    rules.append({
        'matchManagers': ['docker-compose'],
        'matchFileNames': ['docker/services/flaresolverr/compose.yaml'],
        'matchPackageNames': ['ghcr.io/flaresolverr/flaresolverr'],
        'automerge': False,
    })
    return json.dumps(config, indent=2) + '\n'


def recovery_files(previous, current, renovate, failed_digest):
    return {
        'docker/services/flaresolverr/compose.yaml': restore_image(previous, current, failed_digest),
        'renovate.json': pause_automerge(renovate),
    }


def current_head(api):
    head = api('GET', '/repos/jovalle/technis/git/ref/heads/main')['object']['sha']
    if not re.fullmatch(r'[0-9a-f]{40}', head):
        raise ValueError('invalid main branch head')
    return head


def contents(api, path, ref):
    result = api('GET', '/repos/jovalle/technis/contents/' + path + '?ref=' + ref)
    if result.get('encoding') != 'base64':
        raise ValueError('unexpected GitHub file encoding')
    return base64.b64decode(result['content']).decode('utf-8')


def commit_files(api, head, files, headline):
    entries = [{'path': path, 'contents': base64.b64encode(text.encode()).decode()}
               for path, text in files.items()]
    result = api('POST', '/graphql', {
        'query': 'mutation($input:CreateCommitOnBranchInput!){createCommitOnBranch(input:$input){commit{oid}}}',
        'variables': {'input': {
            'branch': {'repositoryNameWithOwner': 'jovalle/technis', 'branchName': 'main'},
            'expectedHeadOid': head,
            'message': {'headline': headline},
            'fileChanges': {'additions': entries},
        }},
    })
    if result.get('errors'):
        raise RuntimeError('GitHub refused the recovery commit')
    oid = result['data']['createCommitOnBranch']['commit']['oid']
    if not isinstance(oid, str) or not re.fullmatch(r'[0-9a-f]{40}', oid):
        raise ValueError('invalid recovery commit')
    return oid


def reconcile(api, before, failed_digest):
    if not re.fullmatch(r'[0-9a-f]{40}', before) or not re.fullmatch(r'[0-9a-f]{64}', failed_digest):
        raise ValueError('invalid commit or candidate digest')
    head = current_head(api)
    path = 'docker/services/flaresolverr/compose.yaml'
    files = recovery_files(contents(api, path, before), contents(api, path, head),
                           contents(api, 'renovate.json', head), failed_digest)
    return commit_files(api, head, files, 'Revert failed FlareSolverr update and pause automerge')


def pause(api):
    head = current_head(api)
    config = pause_automerge(contents(api, 'renovate.json', head))
    return commit_files(api, head, {'renovate.json': config}, 'Pause FlareSolverr automerge')


def report(api, failed_digest, run_id, status, git_status):
    messages = {
        'restored': 'The previous image was restored and verified healthy.',
        'recovery-failed': 'Automatic recovery failed; the deployment lock remains in place.',
        'unknown': 'The deployment outcome is unknown; inspect the host and deployment lock.',
        'not-deployed': 'The candidate was not deployed.',
        'lock-held': 'The previous image recovered, but its deployment lock could not be cleared.',
    }
    git_messages = {'reconciled': 'Git was reconciled and pilot automerge was paused.',
                    'paused': 'Pilot automerge was paused; Git image recovery was not attempted.',
                    'failed': 'Git recovery failed; the failed candidate may still be desired.',
                    'not-attempted': 'Git recovery was not attempted.'}
    if (not (failed_digest == 'unknown' or re.fullmatch(r'[0-9a-f]{64}', failed_digest)) or not re.fullmatch(r'[0-9]+', run_id)
            or status not in messages or git_status not in git_messages):
        raise ValueError('invalid issue details')
    result = api('POST', '/repos/jovalle/technis/issues', {
        'title': 'FlareSolverr pilot update failed',
        'body': f'Candidate: `{("sha256:" + failed_digest) if failed_digest != "unknown" else "unknown"}`\n\n{messages[status]}\n\n{git_messages[git_status]}\n\n'
                f'Workflow: https://github.com/jovalle/technis/actions/runs/{run_id}\n',
    })
    number = result['number']
    if type(number) is not int or number < 1:
        raise ValueError('invalid issue number')
    return number


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def github_api(method, path, payload=None, token=None):
    token = token or os.environ.get('GH_TOKEN', '')
    if not token:
        raise RuntimeError('GitHub token is unavailable')
    request = urllib.request.Request('https://api.github.com' + path, method=method,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
                 'Content-Type': 'application/json', 'User-Agent': 'technis-pilot'})
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=20) as response:
            body = response.read(2_000_001)
        if len(body) > 2_000_000:
            raise RuntimeError('GitHub response is too large')
        return json.loads(body)
    except (OSError, ValueError) as error:
        raise RuntimeError('GitHub request failed') from error


def main(args):
    try:
        if (len(args) == 3 and args[0] == 'reconcile') or args == ['pause']:
            from pilot_access import infisical_secret, request
            key = infisical_secret(request, os.environ, '/ci/pilot-recovery', 'PILOT_GITHUB_APP_KEY')
            token = installation_token(github_api, os.environ['PILOT_GITHUB_APP_ID'],
                                       os.environ['PILOT_GITHUB_INSTALLATION_ID'], key)
            app_api = lambda method, path, payload=None: github_api(method, path, payload, token)
            print(reconcile(app_api, args[1], args[2]) if args[0] == 'reconcile' else pause(app_api))
        elif len(args) == 5 and args[0] == 'report':
            print(report(github_api, args[1], args[2], args[3], args[4]))
        else:
            raise ValueError('expected reconcile <before> <digest> or report <digest> <run> <status> <git-status>')
        return 0
    except (ValueError, RuntimeError, KeyError, TypeError, OSError, subprocess.SubprocessError):
        print('Pilot GitHub recovery or reporting failed; manual investigation is required.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
