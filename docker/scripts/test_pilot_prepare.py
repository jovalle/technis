import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('pilot_prepare', Path(__file__).with_name('pilot_prepare.py'))
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)


class PrepareTest(unittest.TestCase):
    def test_allows_only_a_pinned_image_change(self):
        old = 'services:\n  flaresolverr:\n    image: ghcr.io/flaresolverr/flaresolverr:latest@sha256:' + 'a' * 64 + '\n'
        new = old.replace('a' * 64, 'b' * 64)
        self.assertEqual(prepare.digests(old, new), ('a' * 64, 'b' * 64))
        with self.assertRaises(ValueError):
            prepare.digests(old, new + '    user: changed\n')
        with self.assertRaises(ValueError):
            prepare.digests(old, old)


if __name__ == '__main__':
    unittest.main()
