import base64
import json
import unittest

import pilot_gateway
import pilot_prepare
import pilot_recovery
import pilot_update


class FlowTest(unittest.TestCase):
    def test_failed_deployment_recovers_git_reports_and_releases_lock(self):
        old = 'services:\n  flaresolverr:\n    image: ghcr.io/flaresolverr/flaresolverr:latest@sha256:' + 'a' * 64 + '\n'
        new = old.replace('a' * 64, 'b' * 64)
        previous, candidate = pilot_prepare.digests(old, new)
        rule = {'matchManagers': ['docker-compose'],
                'matchFileNames': ['docker/services/flaresolverr/compose.yaml'],
                'matchPackageNames': ['ghcr.io/flaresolverr/flaresolverr'], 'automerge': True}
        policy = json.dumps({'enabled': True, 'packageRules': [rule]})
        pilot_gateway.verify_desired('ghcr.io/flaresolverr/flaresolverr@sha256:' + candidate, new)
        pilot_gateway.verify_policy(policy)
        pilot_gateway.verify_installed_config(old, new)
        deployed = []

        def deploy(image):
            deployed.append(image)
            if image == candidate:
                raise RuntimeError('unhealthy')

        with self.assertRaises(pilot_update.Recovered):
            pilot_update.apply_images(candidate, previous, deploy)
        self.assertEqual(deployed, [candidate, previous])
        state = {'head': '2' * 40, 'service': new, 'policy': policy, 'issues': []}

        def api(method, route, payload=None):
            if route.endswith('/git/ref/heads/main'):
                return {'object': {'sha': state['head']}}
            if '/contents/' in route:
                text = (state['policy'] if 'renovate.json' in route else old if 'ref=' + '1' * 40 in route else state['service'])
                return {'encoding': 'base64', 'content': base64.b64encode(text.encode()).decode()}
            if route == '/graphql':
                commit = payload['variables']['input']
                self.assertEqual(commit['expectedHeadOid'], state['head'])
                for item in commit['fileChanges']['additions']:
                    text = base64.b64decode(item['contents']).decode()
                    state['policy' if item['path'] == 'renovate.json' else 'service'] = text
                state['head'] = '3' * 40
                return {'data': {'createCommitOnBranch': {'commit': {'oid': state['head']}}}}
            if route.endswith('/issues'):
                state['issues'].append(payload)
                return {'number': 1}
            raise AssertionError(route)

        pilot_recovery.reconcile(api, '1' * 40, candidate)
        pilot_recovery.report(api, candidate, '42', 'restored', 'reconciled')
        self.assertEqual(state['service'], old)
        with self.assertRaises(ValueError):
            pilot_gateway.verify_policy(state['policy'])
        self.assertEqual(len(state['issues']), 1)
        prior_image = 'sha256:' + 'c' * 64
        digest_ref = 'ghcr.io/flaresolverr/flaresolverr@sha256:' + previous
        calls = []

        def docker(*args):
            calls.append(args)
            if args[0] == 'inspect' and args[2] == '{{.Image}}':
                return prior_image
            if args[0] == 'inspect':
                return json.dumps({'Status': 'running', 'Health': {'Status': 'healthy'}})
            if args[:2] == ('image', 'inspect'):
                return json.dumps([digest_ref])
            return ''

        pilot_gateway.verify_desired(digest_ref, state['service'])
        pilot_gateway.unlock(digest_ref, docker)
        self.assertEqual(calls[-1], ('rm', 'technis-update-lock-flaresolverr'))


if __name__ == '__main__':
    unittest.main()
