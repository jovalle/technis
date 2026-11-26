import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


class BridgeTest(unittest.TestCase):
    def test_backlight_is_excluded_from_the_lan_bridge(self):
        from bridge import main

        config = {
            "bridge": "192.0.2.2/24",
            "assistant": "198.51.100.2",
            "lights": {"tv": "192.0.2.3", "downlights": "192.0.2.4"},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            with patch("bridge.socket.socket"), patch("bridge.make_server") as listen:
                main(path)
        self.assertEqual(listen.call_args.args[2], {"downlights": "192.0.2.4"})

    def test_only_configured_lights_and_valid_power_requests_reach_the_lan(self):
        self.assertTrue(Path(__file__).with_name("bridge.py").exists(), "bridge is missing")
        from bridge import make_server

        class Lights:
            def __init__(self):
                self.calls = []

            def status(self, address):
                self.calls.append(("status", address))
                return True

            def power(self, address, on):
                self.calls.append(("power", address, on))
                return on

        lights = Lights()
        with make_server(("127.0.0.1", 0), "127.0.0.1", {"tv": "192.0.2.10"}, lights) as server:
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            connection = http.client.HTTPConnection(*server.server_address, timeout=2)
            try:
                connection.request("GET", "/tv")
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read()), {"on": True})

                connection.request("PUT", "/tv", '{"on":false}')
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read()), {"on": False})

                for path, body, status in [
                    ("/192.0.2.11", '{"on":true}', 404),
                    ("/tv", '{"on":true,"cmd":"scan"}', 400),
                    ("/tv", '{"on":1}', 400),
                ]:
                    connection.request("PUT", path, body)
                    response = connection.getresponse()
                    self.assertEqual(response.status, status)
                    response.read()

                server.assistant = "192.0.2.20"
                connection.request("GET", "/tv")
                response = connection.getresponse()
                self.assertEqual(response.status, 403)
                response.read()
            finally:
                connection.close()
                server.shutdown()
                thread.join()

        self.assertEqual(lights.calls, [("status", "192.0.2.10"), ("power", "192.0.2.10", False)])
