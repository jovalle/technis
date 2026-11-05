#!/usr/bin/python3
"""Install the TrueNAS-managed ACME certificate into Plex after renewal."""

from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
from pathlib import Path
import socket
import ssl
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

DOMAIN = "plex.direct.techn.is"
URL = f"https://{DOMAIN}:443"
CERTIFICATE = "plex_direct"


def connections(value):
    urls = [url for url in value.split(",") if url]
    if URL not in urls:
        urls.append(URL)
    return ",".join(urls)


def bundle(record, password):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.serialization import pkcs12

    chain = x509.load_pem_x509_certificates(record["certificate"].encode())
    leaf = chain[0]
    names = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    if DOMAIN not in names.get_values_for_type(x509.DNSName):
        raise ValueError("Certificate does not cover the Plex hostname")
    if leaf.not_valid_after_utc < datetime.now(timezone.utc) + timedelta(days=7):
        raise ValueError("Certificate expires in less than seven days")
    key = serialization.load_pem_private_key(record["privatekey"].encode(), password=None)
    if key.public_key() != leaf.public_key():
        raise ValueError("Certificate and private key do not match")
    data = pkcs12.serialize_key_and_certificates(
        DOMAIN.encode(), key, leaf, chain[1:],
        serialization.BestAvailableEncryption(password.encode()),
    )
    return data, leaf.fingerprint(hashes.SHA256()).hex()


def write(path, data, uid, gid, mode=0o600):
    import os

    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        name = Path(temporary.name)
        try:
            temporary.write(data)
            temporary.flush()
            os.fsync(temporary.fileno())
            os.fchmod(temporary.fileno(), mode)
            os.fchown(temporary.fileno(), uid, gid)
            os.replace(name, path)
        finally:
            name.unlink(missing_ok=True)


def request(path, token, values=None):
    url = "http://127.0.0.1:32400" + path
    if values is not None:
        url += "?" + urllib.parse.urlencode(values)
    req = urllib.request.Request(url, headers={"X-Plex-Token": token},
                                 method="PUT" if values is not None else "GET")
    with urllib.request.urlopen(req, timeout=10) as response:
        return response.read()


def fingerprint():
    context = ssl.create_default_context()
    with socket.create_connection(("127.0.0.1", 32400), timeout=5) as connection:
        with context.wrap_socket(connection, server_hostname=DOMAIN) as tls:
            return hashlib.sha256(tls.getpeercert(binary_form=True)).hexdigest()


def wait_until(check, message):
    deadline = time.monotonic() + 120
    while True:
        try:
            if check():
                return
        except (OSError, ssl.SSLError, ET.ParseError):
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError(message)
        time.sleep(2)


def deploy(record, directory):
    import secrets

    preferences = directory.parent / "Library/Application Support/Plex Media Server/Preferences.xml"
    original = preferences.read_bytes()
    prefs = ET.fromstring(original)
    token = prefs.attrib["PlexOnlineToken"]
    password = prefs.get("customCertificateKey") or secrets.token_urlsafe(32)
    data, expected = bundle(record, password)
    target = directory / "plex.p12"
    settings = {"customCertificatePath": "/config/tls/plex.p12",
                "customCertificateKey": password, "customCertificateDomain": DOMAIN}
    try:
        current = fingerprint()
    except (OSError, ssl.SSLError):
        current = None
    if current == expected and target.exists() and all(prefs.get(k) == v for k, v in settings.items()):
        request("/:/prefs", token, {"customConnections": connections(prefs.get("customConnections", ""))})
        print("Plex certificate is current; no restart")
        return
    sessions = ET.fromstring(request("/status/sessions", token))
    if int(sessions.attrib["size"]):
        print("Certificate deployment deferred until Plex has no active streams")
        return

    backup = directory / "backups" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup.mkdir(parents=True, mode=0o700)
    backup.parent.chmod(0o700)
    write(backup / "Preferences.xml", original, 0, 0)
    if target.exists():
        write(backup / "plex.p12", target.read_bytes(), 0, 0)
    owner = preferences.stat()
    subprocess.run(["docker", "stop", "--timeout", "30", "plex"], check=True)
    try:
        # Re-read after stopping so concurrent Plex preference updates are preserved.
        original = preferences.read_bytes()
        write(backup / "Preferences.xml", original, 0, 0)
        prefs = ET.fromstring(original)
        prefs.attrib.update(settings)
        prefs.set("customConnections", connections(prefs.get("customConnections", "")))
        write(target, data, owner.st_uid, owner.st_gid)
        write(preferences, ET.tostring(prefs, encoding="utf-8", xml_declaration=True),
              owner.st_uid, owner.st_gid, owner.st_mode & 0o777)
        subprocess.run(["docker", "start", "plex"], check=True)
        wait_until(lambda: fingerprint() == expected, "Plex did not load the new certificate")
    except Exception:
        subprocess.run(["docker", "stop", "--timeout", "30", "plex"], check=True)
        write(preferences, original, owner.st_uid, owner.st_gid, owner.st_mode & 0o777)
        if (backup / "plex.p12").exists():
            write(target, (backup / "plex.p12").read_bytes(), owner.st_uid, owner.st_gid)
        else:
            target.unlink(missing_ok=True)
        subprocess.run(["docker", "start", "plex"], check=True)
        wait_until(lambda: ET.fromstring(request("/identity", token)).get("machineIdentifier"),
                   "Plex did not recover after certificate rollback")
        raise
    # The official image reapplies ADVERTISE_IP on start; preserve its URLs too.
    # A discovery update failure must not roll back a successfully loaded certificate.
    started = ET.fromstring(preferences.read_bytes())
    request("/:/prefs", token, {"customConnections": connections(started.get("customConnections", ""))})
    print(f"Installed Plex certificate {expected}; rollback files: {backup}")


def main():
    from truenas_api_client import Client

    directory = Path(__file__).resolve().parent
    with (directory / "deploy.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with Client() as client:
            record = client.call("certificate.query", [["name", "=", CERTIFICATE]], {"get": True})
        deploy(record, directory)


if __name__ == "__main__":
    main()
