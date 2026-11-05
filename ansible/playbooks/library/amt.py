#!/usr/bin/python

from __future__ import annotations

import ssl
import uuid
import xml.etree.ElementTree as ET
from urllib.error import HTTPError, URLError
from urllib.request import (
    build_opener,
    ProxyHandler,
    HTTPSHandler,
    HTTPDigestAuthHandler,
    HTTPPasswordMgrWithDefaultRealm,
    Request,
)
from xml.sax.saxutils import escape

ADDRESSING = "http://schemas.xmlsoap.org/ws/2004/08/addressing"
ANONYMOUS = ADDRESSING + "/role/anonymous"
ENUMERATION = "http://schemas.xmlsoap.org/ws/2004/09/enumeration"
SOAP = "http://www.w3.org/2003/05/soap-envelope"
WSMAN = "http://schemas.dmtf.org/wbem/wsman/1/wsman.xsd"
CIM = "http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/"
TRANSFER = "http://schemas.xmlsoap.org/ws/2004/09/transfer"
KVM_SAP = "CIM_KVMRedirectionSAP"
KVM_SAP_SELECTORS = {
    "Name": "KVM Redirection Service Access Point",
    "CreationClassName": KVM_SAP,
    "SystemName": "ManagedSystem",
    "SystemCreationClassName": "CIM_ComputerSystem",
}

POWER_STATES = {
    2: "on",
    3: "sleep",
    4: "sleep",
    6: "off",
    7: "hibernate",
    8: "off",
    12: "off",
    13: "off",
}
# CIM_PowerManagementService.RequestPowerStateChange values.
POWER_ACTIONS = {"on": 2, "off": 8, "restart": 10, "cycle": 5}
POWER_SERVICE = {
    "CreationClassName": "CIM_PowerManagementService",
    "Name": "Intel(r) AMT Power Management Service",
    "SystemCreationClassName": "CIM_ComputerSystem",
    "SystemName": "Intel(r) AMT",
}


class AmtError(Exception):
    pass


class AmtUnreachable(AmtError):
    pass


class KvmRestoreError(AmtError):
    pass


def retryable(err: Exception, command_sent: bool) -> bool:
    # A command may have reached AMT before the error, and a repeated reset is not idempotent.
    return not command_sent and isinstance(err, (AmtUnreachable, TimeoutError, ConnectionError))


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def selector_set(selectors: dict[str, str] | None) -> str:
    if not selectors:
        return ""
    items = "".join(
        f'<w:Selector Name="{escape(k)}">{escape(v)}</w:Selector>' for k, v in selectors.items()
    )
    return f"<w:SelectorSet>{items}</w:SelectorSet>"


class PinnedCertificateSocket(ssl.SSLSocket):
    def do_handshake(self, block: bool = False) -> None:
        super().do_handshake(block)
        # Verify the exact host certificate before urllib can send any credentials.
        if self.getpeercert(binary_form=True) != self.context.amt_server_certificate:
            self.close()
            raise ssl.SSLCertVerificationError(
                "AMT server certificate does not match the configured pin"
            )


class Amt:
    def __init__(self, params: dict) -> None:
        self.url = f"https://{params['host']}:{params['port']}/wsman"
        self.username = params["username"]
        self.password = params["password"]
        self.timeout = params["timeout"]
        certificate = params["server_certificate"]
        self.context = ssl.create_default_context(cadata=certificate)
        self.context.amt_server_certificate = ssl.PEM_cert_to_DER_cert(certificate)
        # AMT's factory certificates have no hostname; the exact certificate identifies the host.
        self.context.check_hostname = False
        self.context.sslsocket_class = PinnedCertificateSocket
        if params["legacy_server_connect"]:
            # Older AMT firmware lacks secure renegotiation; limit compatibility to this client.
            self.context.options |= getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x4)

    def call(
        self, action: str, resource: str, body: str, selectors: dict[str, str] | None = None
    ) -> ET.Element:
        envelope = (
            f'<?xml version="1.0" encoding="utf-8"?><s:Envelope xmlns:s="{SOAP}" xmlns:a="{ADDRESSING}" '
            f'xmlns:w="{WSMAN}" xmlns:n="{ENUMERATION}"><s:Header>'
            f'<a:Action s:mustUnderstand="true">{action}</a:Action>'
            '<a:To s:mustUnderstand="true">/wsman</a:To>'
            f'<w:ResourceURI s:mustUnderstand="true">{resource}</w:ResourceURI>'
            f'<a:MessageID s:mustUnderstand="true">uuid:{uuid.uuid4()}</a:MessageID>'
            f"<a:ReplyTo><a:Address>{ANONYMOUS}</a:Address></a:ReplyTo>"
            # AMT requires at least 30 seconds here, independently of the socket timeout.
            f"<w:OperationTimeout>PT{max(30, self.timeout)}S</w:OperationTimeout>{selector_set(selectors)}"
            f"</s:Header><s:Body>{body}</s:Body></s:Envelope>"
        )
        try:
            credentials = HTTPPasswordMgrWithDefaultRealm()
            credentials.add_password(None, self.url, self.username, self.password)
            payload = (
                build_opener(
                    ProxyHandler({}),
                    HTTPSHandler(context=self.context),
                    HTTPDigestAuthHandler(credentials),
                )
                .open(
                    Request(
                        self.url,
                        data=envelope.encode("utf-8"),
                        headers={"Content-Type": "application/soap+xml;charset=UTF-8"},
                    ),
                    timeout=self.timeout,
                )
                .read()
            )
        except HTTPError as err:
            if err.code == 401:
                raise AmtError(f"AMT rejected the credentials at {self.url}") from None
            payload = err.read()
            if not payload:
                raise AmtError(f"HTTP {err.code} from {self.url}") from None
        except URLError as err:
            # TLS failures, including a pin mismatch, will not fix themselves on retry.
            error = AmtError if isinstance(err.reason, ssl.SSLError) else AmtUnreachable
            raise error(f"Cannot reach {self.url}: {err.reason}") from None

        try:
            root = ET.fromstring(payload)
        except ET.ParseError as err:
            raise AmtError(f"Invalid WS-Management response to {action}: {err}") from None
        fault = root.find(f".//{{{SOAP}}}Fault")
        if fault is not None:
            reason = " ".join(text.strip() for text in fault.itertext() if text.strip())
            raise AmtError(f"WS-Management fault for {action}: {reason}")
        body_element = root.find(f"{{{SOAP}}}Body")
        if body_element is None:
            raise AmtError(f"WS-Management response to {action} has no body")
        return body_element

    def enumerate_items(self, cim_class: str) -> list[ET.Element]:
        resource = CIM + cim_class
        body = self.call(f"{ENUMERATION}/Enumerate", resource, "<n:Enumerate/>")
        context = body.findtext(f".//{{{ENUMERATION}}}EnumerationContext")
        items: list[ET.Element] = []
        # 32 pulls of 32 items covers the small power classes read here.
        for _ in range(32):
            if not context:
                break
            body = self.call(
                f"{ENUMERATION}/Pull",
                resource,
                f"<n:Pull><n:EnumerationContext>{escape(context)}</n:EnumerationContext>"
                "<n:MaxElements>32</n:MaxElements></n:Pull>",
            )
            items.extend(body.iterfind(f".//{{{ENUMERATION}}}Items/*"))
            if body.find(f".//{{{ENUMERATION}}}EndOfSequence") is not None:
                break
            context = body.findtext(f".//{{{ENUMERATION}}}EnumerationContext")
        return items

    def enumerate(self, cim_class: str) -> list[dict[str, str]]:
        return [
            {local_name(child.tag): "".join(child.itertext()).strip() for child in node}
            for node in self.enumerate_items(cim_class)
        ]

    def power_state(self) -> int | None:
        for item in self.enumerate("CIM_AssociatedPowerManagementService"):
            if item.get("PowerState", "").isdigit():
                return int(item["PowerState"])
        return None

    def set_power(self, code: int) -> None:
        resource = CIM + "CIM_PowerManagementService"
        managed = (
            f"<a:Address>{ANONYMOUS}</a:Address><a:ReferenceParameters>"
            f"<w:ResourceURI>{CIM}CIM_ComputerSystem</w:ResourceURI>"
            f"{selector_set({'CreationClassName': 'CIM_ComputerSystem', 'Name': 'ManagedSystem'})}"
            "</a:ReferenceParameters>"
        )
        body = self.call(
            f"{resource}/RequestPowerStateChange",
            resource,
            f'<p:RequestPowerStateChange_INPUT xmlns:p="{resource}">'
            f"<p:PowerState>{code}</p:PowerState><p:ManagedElement>{managed}</p:ManagedElement>"
            "</p:RequestPowerStateChange_INPUT>",
            POWER_SERVICE,
        )
        value = body.findtext(f".//{{{resource}}}ReturnValue")
        if value != "0":
            raise AmtError(f"RequestPowerStateChange returned {value}")

    def kvm_state(self) -> int:
        resource = CIM + KVM_SAP
        body = self.call(f"{TRANSFER}/Get", resource, "", KVM_SAP_SELECTORS)
        state = body.findtext(f".//{{{resource}}}EnabledState", "")
        if state not in ("2", "3", "6"):
            raise AmtError(f"AMT returned an unsupported KVM state: {state}")
        return int(state)

    def request_kvm_state(self, state: int) -> None:
        resource = CIM + KVM_SAP
        body = self.call(
            f"{resource}/RequestStateChange",
            resource,
            f'<p:RequestStateChange_INPUT xmlns:p="{resource}">'
            f"<p:RequestedState>{state}</p:RequestedState></p:RequestStateChange_INPUT>",
            KVM_SAP_SELECTORS,
        )
        value = body.findtext(f".//{{{resource}}}ReturnValue")
        if value != "0":
            raise AmtError(f"KVM RequestStateChange returned {value}")

    def disconnect_kvm(self) -> bool:
        # Disabling KVM ends every redirection session, including other clients'.
        if self.kvm_state() == 3:
            return False
        try:
            self.request_kvm_state(3)
            if self.kvm_state() != 3:
                raise AmtError("AMT did not disable KVM; session termination is unverified")
        finally:
            # A timed-out disable may have applied; always restore KVM before reporting failure.
            try:
                self.request_kvm_state(2)
                if self.kvm_state() not in (2, 6):
                    raise AmtError("KVM is still disabled")
            except (AmtError, ValueError, OSError) as err:
                raise KvmRestoreError(f"Could not restore KVM after disconnect: {err}") from None
        return True


def plan(power: str | None, state: str) -> str | None:
    if power in ("on", "off"):
        return None if state == power else power
    if power in ("restart", "cycle"):
        # A reset needs a running host; boot one that is off instead.
        return power if state == "on" else "on"
    return None


def main() -> None:
    from ansible.module_utils.basic import AnsibleModule

    module = AnsibleModule(
        argument_spec={
            "host": {"type": "str", "required": True},
            "port": {"type": "int", "default": 16993},
            "server_certificate": {"type": "str", "required": True},
            "legacy_server_connect": {"type": "bool", "default": False},
            "username": {"type": "str", "default": "admin"},
            "password": {"type": "str", "required": True, "no_log": True},
            "power": {"type": "str", "choices": list(POWER_ACTIONS)},
            "timeout": {"type": "int", "default": 30},
        },
        supports_check_mode=True,
    )

    command_sent = False
    try:
        amt = Amt(module.params)
        code = amt.power_state()
        state = POWER_STATES.get(code, "unknown") if code is not None else "unknown"
        action = plan(module.params["power"], state)
        if action is not None and not module.check_mode:
            command_sent = True
            amt.set_power(POWER_ACTIONS[action])
    except (AmtError, ValueError, OSError) as err:
        module.fail_json(msg=str(err), retryable=retryable(err, command_sent))

    module.exit_json(
        changed=action is not None,
        power_state=state,
        power_state_code=code,
        power_action=action,
    )


if __name__ == "__main__":
    main()
