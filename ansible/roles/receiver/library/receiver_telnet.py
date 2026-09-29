#!/usr/bin/python

from __future__ import annotations

import re
import socket
import time

from ansible.module_utils.basic import AnsibleModule


def exchange(host: str, port: int, command: str, timeout: float = 1.2) -> str:
    chunks: list[bytes] = []
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(0.25)
        sock.sendall((command + "\r").encode("ascii"))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                chunk = sock.recv(4096)
            except TimeoutError:
                continue
            if not chunk:
                break
            chunks.append(chunk)
    return b"".join(chunks).decode("ascii", errors="replace")


def read_state(host: str, port: int, timeout: float) -> dict[str, object]:
    delay_raw = exchange(host, port, "PSDELAY ?", timeout)
    status_raw = exchange(host, port, "SI?", timeout)
    delay_match = re.search(r"PSDELAY\s+(\d{1,3})", delay_raw)
    source_match = re.search(r"(?:^|\r)SI([^\r]+)", status_raw)
    lip_sync_match = re.search(r"(?:^|\r)OPALSSET\s+(ON|OFF)", status_raw)
    lip_sync_value_match = re.search(r"(?:^|\r)OPALSVAL\s+(\d{3})", status_raw)
    if not delay_match:
        raise RuntimeError(f"Unable to parse PSDELAY response: {delay_raw!r}")
    return {
        "delay_ms": int(delay_match.group(1)),
        "source": source_match.group(1) if source_match else None,
        "auto_lip_sync": (lip_sync_match.group(1) == "ON" if lip_sync_match else None),
        "auto_lip_sync_value_ms": (
            int(lip_sync_value_match.group(1)) if lip_sync_value_match else None
        ),
        "raw": status_raw,
    }


def main() -> None:
    module = AnsibleModule(
        argument_spec={
            "host": {"type": "str", "required": True},
            "port": {"type": "int", "default": 23},
            "delay_ms": {"type": "int"},
            "auto_lip_sync": {"type": "bool"},
            "query_only": {"type": "bool", "default": False},
            "timeout": {"type": "float", "default": 1.2},
        },
        supports_check_mode=True,
    )
    host = module.params["host"]
    port = module.params["port"]
    timeout = module.params["timeout"]
    desired_delay = module.params["delay_ms"]
    desired_lip_sync = module.params["auto_lip_sync"]

    try:
        before = read_state(host, port, timeout)
        if module.params["query_only"]:
            module.exit_json(changed=False, state=before)

        delay_change = desired_delay is not None and before["delay_ms"] != desired_delay
        lip_sync_change = (
            desired_lip_sync is not None
            and before["auto_lip_sync"] is not None
            and before["auto_lip_sync"] != desired_lip_sync
        )
        changed = delay_change or lip_sync_change
        if module.check_mode or not changed:
            module.exit_json(changed=changed, before=before, after=before)

        if delay_change:
            exchange(host, port, f"PSDELAY {desired_delay:03d}", timeout)
        if lip_sync_change:
            exchange(
                host,
                port,
                "OPALSSET ON" if desired_lip_sync else "OPALSSET OFF",
                timeout,
            )
        time.sleep(0.3)
        after = read_state(host, port, timeout)
        if desired_delay is not None and after["delay_ms"] != desired_delay:
            raise RuntimeError(f"Audio delay verification failed: {after!r}")
        if (
            desired_lip_sync is not None
            and after["auto_lip_sync"] is not None
            and after["auto_lip_sync"] != desired_lip_sync
        ):
            raise RuntimeError(f"Auto Lip Sync verification failed: {after!r}")
        module.exit_json(changed=changed, before=before, after=after)
    except Exception as exc:
        module.fail_json(msg=str(exc))


if __name__ == "__main__":
    main()
