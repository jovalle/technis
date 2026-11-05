import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('nexus_startup', Path(__file__).with_name('nexus-startup.py'))
startup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(startup)


class StartupTest(unittest.TestCase):
    def fixture(self, root, address=0x90140000, framebuffer=1):
        def write(path, value):
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(value)
        write('class/dmi/id/board_name', 'Pro WS W680-ACE IPMI\n')
        write(f'class/graphics/fb{framebuffer}/name', 'astdrmfb\n')
        pci = 'bus/pci/devices/0000:0d:01.0'
        for name, value in [('vendor', '0x1a03'), ('device', '0x2402'), ('class', '0x0c0701')]:
            write(f'{pci}/{name}', value)
        write(f'{pci}/resource', f'0 0 0\n{address:x} {address + 0x3ffff:x} 40200\n')
        return root / pci

    def test_detects_relocated_bar_and_renumbered_framebuffer(self):
        for address, framebuffer in [(0x90140000, 1), (0x82040000, 0), (0xa0140000, 3)]:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.fixture(root, address, framebuffer)
                self.assertEqual(startup.hardware(root), (address + 0xe88, framebuffer))

    def test_rejects_other_boards_and_invalid_memory_resources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pci = self.fixture(root)
            for resource in ['0 0 0\n0 0 200\n', '0 0 0\n1000 13ff 200\n', '0 0 0\n1000 41000 100\n']:
                (pci / 'resource').write_text(resource)
                with self.assertRaisesRegex(ValueError, 'BAR1'):
                    startup.hardware(root)
            (root / 'class/dmi/id/board_name').write_text('Different board')
            with self.assertRaisesRegex(ValueError, 'restricted'):
                startup.hardware(root)

    def test_refuses_ambiguous_or_missing_hardware(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            extra = root / 'class/graphics/fb2/name'
            extra.parent.mkdir()
            extra.write_text('astdrmfb')
            with self.assertRaisesRegex(ValueError, 'Ambiguous'):
                startup.hardware(root)
            extra.unlink()
            extra.parent.rmdir()
            (root / 'class/graphics/fb1/name').write_text('i915drmfb')
            with self.assertRaises(LookupError):
                startup.hardware(root)


if __name__ == '__main__':
    unittest.main()
