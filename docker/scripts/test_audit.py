import importlib.util
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

PATH = Path(__file__).with_name('audit.py')


def load():
    spec = importlib.util.spec_from_file_location('audit', PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


HARDENED = {'cap_drop': ['ALL'], 'security_opt': ['no-new-privileges:true'], 'user': '568:568'}


class FindingsTest(unittest.TestCase):
    def setUp(self):
        self.audit = load()

    def test_hardened_service_is_clean(self):
        self.assertEqual(self.audit.findings(HARDENED), [])

    def test_each_rule(self):
        cases = {
            'privileged': {**HARDENED, 'privileged': True},
            'no-cap-drop': {**HARDENED, 'cap_drop': []},
            'no-new-privileges': {**HARDENED, 'security_opt': []},
            'docker-socket': {**HARDENED, 'volumes': ['/var/run/docker.sock:/var/run/docker.sock:ro']},
            'implicit-user': {k: v for k, v in HARDENED.items() if k != 'user'},
            'root': {**HARDENED, 'user': 'root'},
            'uid-1000': {**HARDENED, 'user': '${PUID:-1000}:${PGID:-1000}'},
        }
        for finding, svc in cases.items():
            with self.subTest(finding):
                self.assertEqual(self.audit.findings(svc), [finding])

    def test_long_volume_syntax_and_equals_option(self):
        svc = {**HARDENED, 'security_opt': ['no-new-privileges=true'],
               'volumes': [{'type': 'bind', 'source': '/run/docker.sock', 'target': '/s'}]}
        self.assertEqual(self.audit.findings(svc), ['docker-socket'])

    def test_variable_default_resolves(self):
        self.assertEqual(self.audit.uid('${PUID:-568}:${PGID:-568}'), '568')
        self.assertEqual(self.audit.uid(568), '568')


class StackTest(unittest.TestCase):
    def test_overrides_merge_and_exceptions_apply(self):
        audit = load()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stack = root / 'demo'
            (stack / 'services').mkdir(parents=True)
            (root / 'base.yaml').write_text(textwrap.dedent('''
                services:
                  app:
                    cap_drop: [ALL]
                    security_opt: [no-new-privileges:true]
                    user: "568:568"
                  hawser:
                    cap_drop: [ALL]
                    security_opt: [no-new-privileges:true]
                    user: "568:568"
                    volumes: [/var/run/docker.sock:/var/run/docker.sock]
            '''))
            (stack / 'services' / 'app.yaml').write_text('services:\n  app:\n    privileged: true\n')
            (stack / 'compose.yaml').write_text(textwrap.dedent('''
                include:
                  - path:
                      - ../base.yaml
                      - services/app.yaml
            '''))
            audit.STACKS = root
            self.assertEqual(audit.audit([stack / 'compose.yaml']), ['demo/app: privileged'])

    def test_repository_passes(self):
        self.assertEqual(load().main(), 0)


if __name__ == '__main__':
    unittest.main()
