import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("discovery", Path(__file__).parents[1] / "files/discovery.py")
discovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(discovery)


class DiscoveryTests(unittest.TestCase):
    def test_selects_only_the_servers_public_direct_secure_connection(self):
        resources = b'''<MediaContainer>
          <Device clientIdentifier="other" provides="server" owned="1"><Connection uri="https://wrong.plex.direct:443" address="8.8.8.8" local="0" /></Device>
          <Device clientIdentifier="target" provides="server" owned="1">
            <Connection uri="https://local.plex.direct:32400" address="192.168.1.2" local="1" />
            <Connection uri="https://relay.plex.direct:443" address="8.8.8.8" local="0" relay="1" />
            <Connection uri="https://custom.example:443" address="8.8.8.8" local="0" />
            <Connection uri="https://real.plex.direct:443" address="8.8.8.8" local="0" />
          </Device></MediaContainer>'''
        self.assertEqual(discovery.public_url(resources, "target"), "https://real.plex.direct:443/identity")

    def test_missing_or_unsafe_connection_fails_closed(self):
        resources = b'''<MediaContainer><Device clientIdentifier="target" provides="server" owned="1">
          <Connection uri="http://example.plex.direct:443" address="8.8.8.8" local="0" />
          <Connection uri="https://example.plex.direct.evil.test:443" address="8.8.8.8" local="0" />
          <Connection uri="https://private.plex.direct:443" address="192.168.1.2" local="0" />
        </Device></MediaContainer>'''
        with self.assertRaises(ValueError):
            discovery.public_url(resources, "target")


if __name__ == "__main__":
    unittest.main()
