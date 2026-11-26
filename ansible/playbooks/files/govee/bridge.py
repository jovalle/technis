"""Expose power control for explicitly configured Govee lights."""

import ipaddress
import json
import socket
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


PORT = 8765
GOVEE_COMMAND = 4003
GOVEE_REPLY = 4002


class Govee:
    def __init__(self, channel):
        self.channel = channel

    def send(self, address, command, data):
        message = json.dumps({"msg": {"cmd": command, "data": data}}).encode()
        self.channel.sendto(message, (address, GOVEE_COMMAND))

    def status(self, address):
        self.channel.setblocking(False)
        for _ in range(16):
            try:
                self.channel.recv(4096)
            except BlockingIOError:
                break
        self.send(address, "devStatus", {})
        deadline = time.monotonic() + 0.7
        while (remaining := deadline - time.monotonic()) > 0:
            self.channel.settimeout(remaining)
            packet, (sender, _) = self.channel.recvfrom(4096)
            if sender != address:
                continue
            try:
                message = json.loads(packet)["msg"]
                state = message["data"]["onOff"]
                if message["cmd"] == "devStatus" and type(state) is int and state in (0, 1):
                    return bool(state)
            except (ValueError, KeyError, TypeError):
                continue
        raise TimeoutError("light did not respond")

    def power(self, address, on):
        self.send(address, "turn", {"value": int(on)})
        for _ in range(3):
            time.sleep(0.15)
            try:
                if self.status(address) == on:
                    return on
            except TimeoutError:
                continue
        raise TimeoutError("light did not confirm the requested state")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        # Access logs would record private addresses.
        pass

    def reply(self, status, value):
        data = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def light(self):
        if self.client_address[0] != self.server.assistant:
            self.reply(403, {"error": "forbidden"})
            return None
        address = self.server.lights.get(self.path.removeprefix("/"))
        if address is None:
            self.reply(404, {"error": "unknown light"})
        return address

    def do_GET(self):
        address = self.light()
        if address:
            self.perform(self.server.govee.status, address)

    def do_PUT(self):
        address = self.light()
        if not address:
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 1 <= length <= 32:
                raise ValueError
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict) or body.keys() != {"on"} or type(body["on"]) is not bool:
                raise ValueError
        except (ValueError, TimeoutError):
            self.reply(400, {"error": "expected a boolean on field"})
            return
        self.perform(self.server.govee.power, address, body["on"])

    def perform(self, operation, *args):
        try:
            state = operation(*args)
        except OSError:
            self.reply(503, {"error": "light unavailable"})
        else:
            self.reply(200, {"on": state})


class Server(HTTPServer):
    def get_request(self):
        stream, address = super().get_request()
        stream.settimeout(4)
        return stream, address


def make_server(bind, assistant, lights, govee):
    server = Server(bind, Handler)
    server.assistant = assistant
    server.lights = lights
    server.govee = govee
    return server


def main(config_path):
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    bind = str(ipaddress.IPv4Interface(config["bridge"]).ip)
    assistant = str(ipaddress.IPv4Address(config["assistant"]))
    downlights = str(ipaddress.IPv4Address(config["lights"]["downlights"]))
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as channel:
        channel.bind((bind, GOVEE_REPLY))
        with make_server((bind, PORT), assistant, {"downlights": downlights}, Govee(channel)) as server:
            server.serve_forever()


if __name__ == "__main__":
    main(sys.argv[1])
