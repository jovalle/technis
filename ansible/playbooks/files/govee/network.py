"""Put the Govee bridge on its own IPv4-only Isolated interface."""

import ipaddress
import json
import re
import subprocess
import sys
from pathlib import Path


NAME = "govee"


def run(*args, stdin=None):
    result = subprocess.run(args, input=stdin, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(f"{args[0]} failed while preparing the bridge network")


def configure(path):
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    bridge = ipaddress.IPv4Interface(config["bridge"])
    gateway = ipaddress.IPv4Address(config["gateway"])
    assistant = ipaddress.IPv4Address(config["assistant"])
    downlights = ipaddress.IPv4Address(config["lights"]["downlights"])
    mac = config["mac"]
    local = {gateway, downlights, bridge.ip}
    if not (
        all(address in bridge.network for address in local)
        and not local & {bridge.network.network_address, bridge.network.broadcast_address}
        and len(local) == 3
        and assistant not in bridge.network
        and re.fullmatch(r"02(?::[0-9a-f]{2}){5}", mac)
        and re.fullmatch(r"[a-zA-Z0-9_.-]+", config["link"])
        and type(config["vlan"]) is int
        and 1 <= config["vlan"] <= 4094
    ):
        raise ValueError("invalid bridge network configuration")

    run("ip", "netns", "add", NAME)
    link_on_host = False
    try:
        run("ip", "link", "add", "link", config["link"], "name", NAME, "type", "vlan", "id", str(config["vlan"]))
        link_on_host = True
        run("ip", "link", "set", NAME, "netns", NAME)
        link_on_host = False
        run(
            "ip", "netns", "exec", NAME, "sysctl", "-qw",
            "net.ipv4.ip_forward=0",
            "net.ipv6.conf.all.disable_ipv6=1",
            "net.ipv6.conf.default.disable_ipv6=1",
        )
        run("ip", "-n", NAME, "link", "set", NAME, "address", mac)
        run("ip", "-n", NAME, "address", "add", str(bridge), "dev", NAME)
        run("ip", "netns", "exec", NAME, "nft", "-f", "-", stdin=f"""
table inet govee {{
    chain input {{
        type filter hook input priority 0; policy drop;
        ct state invalid drop
        ip saddr {assistant} tcp dport 8765 ct state {{ new, established }} accept
        ip saddr {downlights} udp dport 4002 accept
    }}
    chain output {{
        type filter hook output priority 0; policy drop;
        ip daddr {assistant} tcp sport 8765 ct state established accept
        ip daddr {downlights} udp dport 4003 accept
    }}
}}
""")
        run("ip", "-n", NAME, "link", "set", NAME, "up")
        run("ip", "-n", NAME, "route", "add", f"{assistant}/32", "via", str(gateway), "dev", NAME)
    except BaseException:
        if link_on_host:
            subprocess.run(["ip", "link", "del", NAME], check=False, capture_output=True)
        subprocess.run(["ip", "netns", "del", NAME], check=False, capture_output=True)
        raise


if __name__ == "__main__":
    configure(sys.argv[1])
