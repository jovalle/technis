#!/usr/bin/env python3
"""Redirect the status host to Plex's current direct public connection."""

import ipaddress
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

PREFERENCES = Path('/plex/Preferences.xml')


def public_url(resources, machine_id):
    for device in ET.fromstring(resources):
        if (device.get('clientIdentifier') != machine_id or device.get('owned') != '1'
                or 'server' not in device.get('provides', '').split(',')):
            continue
        for connection in device.findall('Connection'):
            url = urllib.parse.urlsplit(connection.get('uri', ''))
            if (connection.get('local') == '0' and connection.get('relay') != '1'
                    and url.scheme == 'https' and url.hostname
                    and url.hostname.endswith('.plex.direct') and not url.username
                    and not url.password and not url.path and not url.query and not url.fragment
                    and url.port and ipaddress.ip_address(connection.get('address', '')).is_global):
                return connection.get('uri') + '/identity'
    raise ValueError('No direct public Plex connection')


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        # Only the status host may use this private discovery endpoint.
        if self.client_address[0] != '100.100.1.1' or self.path != '/':
            self.send_error(404)
            return
        try:
            prefs = ET.parse(PREFERENCES).getroot().attrib
            request = urllib.request.Request('https://plex.tv/api/resources?includeHttps=1',
                                             headers={'X-Plex-Token': prefs['PlexOnlineToken']})
            with urllib.request.urlopen(request, timeout=8) as response:
                url = public_url(response.read(), prefs['ProcessedMachineIdentifier'])
        except (OSError, KeyError, ValueError, ET.ParseError):
            self.send_error(503)
            return
        self.send_response(302)
        self.send_header('Location', url)
        self.end_headers()

    def log_message(self, format, *args):
        pass


if __name__ == '__main__':
    HTTPServer(('100.100.1.3', 8765), Handler).serve_forever()
