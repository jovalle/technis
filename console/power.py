"""Reuse the inventory module's pinned AMT transport; secrets arrive only on stdin."""

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ansible/playbooks/library"))
from amt import Amt, AmtUnreachable, KvmRestoreError, POWER_ACTIONS, POWER_STATES, CIM

ACTIONS = {**POWER_ACTIONS, "shutdown": 12}


def connect(config):
    return Amt(
        {
            "host": config["host"],
            "port": config.get("powerPort", 16993),
            "server_certificate": config["certificate"],
            "legacy_server_connect": config.get("legacy", False),
            "username": config["username"],
            "password": config["password"],
            "timeout": 15,
        }
    )


def power(config, action=None):
    amt = connect(config)
    if action == "disconnect":
        amt.disconnect_kvm()
        return {"disconnected": True}
    if action is not None and action != "shutdown":
        amt.set_power(ACTIONS[action])
        return {"accepted": True}
    resource = CIM + "CIM_AssociatedPowerManagementService"
    items = amt.enumerate_items("CIM_AssociatedPowerManagementService")
    code = next((item.findtext(f"{{{resource}}}PowerState") for item in items), None)
    available = {
        node.text
        for item in items
        for node in item.findall(f"{{{resource}}}AvailableRequestedPowerStates")
    }
    if action == "shutdown":
        if "12" not in available:
            raise ValueError("Orderly shutdown is unavailable on this host")
        amt.set_power(ACTIONS[action])
        return {"accepted": True}
    return {
        "state": POWER_STATES.get(int(code), "unknown") if code and code.isdigit() else "unknown",
        "actions": [name for name in ACTIONS if name != "shutdown" or "12" in available],
    }


if __name__ == "__main__":
    try:
        request = json.load(sys.stdin)
        print(json.dumps(power(request["config"], request.get("action"))))
    except KvmRestoreError:
        print(
            json.dumps(
                {"error": "KVM may still be disabled on this host; re-enable AMT KVM redirection."}
            )
        )
        sys.exit(1)
    except AmtUnreachable:
        print(
            json.dumps(
                {
                    "error": "AMT is unreachable; check the host's standby power, network link and LAN/VPN."
                }
            )
        )
        sys.exit(1)
    except Exception:
        # Firmware errors can contain authentication details; never echo them to the relay.
        print(
            json.dumps(
                {"error": "AMT request failed; check LAN/VPN, credentials and certificate pin."}
            )
        )
        sys.exit(1)
