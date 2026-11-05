#!/usr/bin/env python3
"""Store Technis secrets using the Infisical API."""

import argparse
import atexit
import codecs
import json
import os
import re
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import tempfile
import termios
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
PROJECT = os.environ.get("INFISICAL_PROJECT_ID", "c3bc611d-ea94-4e5c-beb1-9d69778aa1fa")
ENVIRONMENT = os.environ.get("INFISICAL_ENVIRONMENT", "prod")

# Output follows docker/scripts/stacks and jsh: decoration on stderr, colored only for a terminal unless JSH_COLOR says otherwise.
COLOR = (
    os.environ.get("JSH_PLAIN_OUTPUT") != "1"
    and os.environ.get("TERM") != "dumb"
    and "NO_COLOR" not in os.environ
    and {"always": True, "never": False}.get(os.environ.get("JSH_COLOR"), sys.stderr.isatty())
)


def sgr(code, text):
    return f"\033[{code}m{text}\033[0m" if COLOR else text


def say(text, end="\n"):
    print(text, end=end, file=sys.stderr, flush=True)


def heading(name):
    return sgr(1, f"{name:>7}:") + " "


def tunnel_ready():
    try:
        with socket.create_connection(("127.0.0.1", 18080), timeout=0.2):
            return True
    except OSError:
        return False


def open_tunnel():
    target = os.environ.get("INFISICAL_SSH_TARGET")
    if not target or tunnel_ready():
        return None
    host = os.environ.get("INFISICAL_SSH_HOST")
    args = ["ssh", "-F", str(Path.home() / ".ssh/config"), "-o", "BatchMode=yes",
            "-o", "ExitOnForwardFailure=yes", "-o", "ConnectTimeout=8"]
    if host:
        args += ["-o", "HostName=" + host, "-o", "HostKeyAlias=" + target]
    args += ["-N", "-L", "127.0.0.1:18080:127.0.0.1:18080", target]
    try:
        proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
    except OSError:
        raise ValueError("Cannot start Infisical SSH tunnel") from None
    for _ in range(100):
        if proc.poll() is not None:
            raise ValueError("Cannot establish Infisical SSH tunnel")
        if tunnel_ready():
            atexit.register(proc.terminate)
            return proc
        time.sleep(0.1)
    proc.terminate()
    raise ValueError("Timed out establishing Infisical SSH tunnel")


def request(method, path, payload=None):
    """Send JSON over the API without exposing request or response bodies in errors."""
    base = os.environ.get("INFISICAL_URL", "").rstrip("/")
    token = os.environ.get("INFISICAL_TOKEN", "")
    client_id = os.environ.get("INFISICAL_CLIENT_ID", "")
    client_secret = os.environ.get("INFISICAL_CLIENT_SECRET", "")
    if not base or not (token or (client_id and client_secret)):
        raise ValueError("Set INFISICAL_URL and INFISICAL_TOKEN or universal-auth credentials")

    def send(verb, url, data=None, bearer=None):
        headers = {"Content-Type": "application/json"}
        if bearer:
            headers["Authorization"] = "Bearer " + bearer
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(base + url, data=body, headers=headers, method=verb)
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            raise ValueError(f"Infisical API returned HTTP {error.code}") from None
        except urllib.error.URLError:
            raise ValueError("Cannot connect to Infisical") from None

    if not token:
        token = send("POST", "/api/v1/auth/universal-auth/login", {
            "clientId": client_id, "clientSecret": client_secret
        })["accessToken"]
    return send(method, path, payload, token)


def folder_path(title, name):
    return "/" + "/".join(x for x in (title, name) if x)


def ensure_folder(title, name):
    parent = "/"
    for part in (title, name):
        if not part:
            continue
        query = urllib.parse.urlencode({"projectId": PROJECT, "environment": ENVIRONMENT, "path": parent})
        folders = request("GET", "/api/v2/folders?" + query).get("folders", [])
        if not any(folder.get("name") == part for folder in folders):
            request("POST", "/api/v2/folders", {"projectId": PROJECT, "environment": ENVIRONMENT, "path": parent, "name": part})
        parent = parent.rstrip("/") + "/" + part


REFERENCE = re.compile(r"{{\s*(vault://[^}\s]+)\s*}}")


def key_name(label):
    key = re.sub(r"[^A-Za-z0-9_]", "_", label)
    return "_" + key if not key or key[0].isdigit() else key


def read_folder(path):
    query = urllib.parse.urlencode({"projectId": PROJECT, "environment": ENVIRONMENT,
                                    "secretPath": path, "expandSecretReferences": "false",
                                    "includeImports": "false"})
    result = request("GET", "/api/v4/secrets?" + query)
    return {entry["secretKey"]: entry["secretValue"] for entry in result.get("secrets", [])}


def reference_parts(reference):
    prefix = "vault://technis/"
    if not reference.startswith(prefix):
        raise ValueError("Invalid Infisical reference: " + reference)
    parts = reference[len(prefix):].split("/")
    if len(parts) not in (2, 3) or any(not part or part in (".", "..") for part in parts):
        raise ValueError("Invalid Infisical reference: " + reference)
    return "/" + "/".join(parts[:-1]), key_name(parts[-1])


def inject(text):
    references = {m.group(1) for m in REFERENCE.finditer(text)}
    folders = {}
    missing = []
    values = {}
    for reference in sorted(references):
        path, key = reference_parts(reference)
        if path not in folders:
            folders[path] = read_folder(path)
        if key not in folders[path]:
            missing.append(reference)
        else:
            values[reference] = folders[path][key]
    if missing:
        raise ValueError("missing Infisical references: " + ", ".join(missing))
    return REFERENCE.sub(lambda match: values[match.group(1)], text)


def read_reference(path):
    folder, key = reference_parts("vault://technis/" + path)
    entries = read_folder(folder)
    if key not in entries:
        raise ValueError("missing Infisical reference: " + path)
    return entries[key]


def page(text):
    """Show text in less's alternate screen so nothing stays in the terminal scrollback."""
    env = {**os.environ, "LESS": "R", "LESSHISTFILE": "-", "LESSSECURE": "1"}
    subprocess.run(["less"], input=text, text=True, env=env, check=False)


def confirm(prompt):
    with open("/dev/tty", "r+") as tty:
        tty.write(prompt)
        tty.flush()
        return tty.readline().strip().lower() in ("y", "yes")


def edit():
    """Value written in $VISUAL/$EDITOR (vim by default) through a private temporary file."""
    editor = shlex.split(os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vim")
    descriptor, name = tempfile.mkstemp(prefix="vault-")
    os.close(descriptor)
    try:
        if subprocess.run([*editor, name], check=False).returncode:
            raise ValueError("Editor exited with an error")
        return Path(name).read_text().removesuffix("\n")
    finally:
        os.unlink(name)


def masked(prompt):
    """Read a line from the terminal, echoing a bullet per character."""
    with open("/dev/tty", "r+b", buffering=0) as tty:
        fd = tty.fileno()
        saved, mode = termios.tcgetattr(fd), termios.tcgetattr(fd)
        mode[3] &= ~(termios.ECHO | termios.ICANON)
        mode[6][termios.VMIN], mode[6][termios.VTIME] = 1, 0
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        chars = []
        tty.write(prompt.encode())
        termios.tcsetattr(fd, termios.TCSAFLUSH, mode)
        try:
            while True:
                data = os.read(fd, 1)
                if not data or (data == b"\x04" and not chars):
                    raise EOFError
                char = decoder.decode(data)
                if char in ("\r", "\n"):
                    return "".join(chars)
                if char in ("\x7f", "\b") and chars:
                    chars.pop()
                    tty.write(b"\b \b")
                elif char == "\x15":  # Ctrl-U
                    tty.write(b"\b \b" * len(chars))
                    chars.clear()
                elif char.isprintable() and char:
                    chars.append(char)
                    tty.write("•".encode())
        finally:
            termios.tcsetattr(fd, termios.TCSAFLUSH, saved)
            tty.write(b"\n")


def secret(mode):
    if not sys.stdin.isatty():
        say(heading("value") + sgr("2;37", "from stdin"))
        value = sys.stdin.read().removesuffix("\n")
    elif mode == "edit":
        say(heading("value") + sgr("2;37", "from editor"))
        value = edit()
    elif mode == "password":
        value = masked(heading("value"))
        if value != masked(heading("confirm")):
            raise ValueError("Values do not match")
    else:
        say(heading("value"), end="")
        value = input()
    say("")
    if not value:
        raise ValueError("Value cannot be empty")
    return value


def store(title, name, values):
    """Save one or more fields, asking before overwriting a changed value."""
    ensure_folder(title, name)
    path = folder_path(title, name)
    query = urllib.parse.urlencode({"projectId": PROJECT, "environment": ENVIRONMENT,
                                    "secretPath": path, "expandSecretReferences": "false"})
    result = request("GET", "/api/v4/secrets?" + query)
    current = {entry["secretKey"]: entry["secretValue"] for entry in result.get("secrets", [])}
    for label, value in values.items():
        key = key_name(label)
        old = current.get(key)
        where = path.lstrip("/") + "/" + label
        if old == value:
            say(f"{sgr('2;37', '–')} {sgr(1, where)} {sgr('2;37', 'already up to date')}")
            continue
        if old is not None:
            if not sys.stdin.isatty():
                raise ValueError(f"{where} already exists; overwrite requires a terminal")
            if not confirm(f"{sgr(33, '!')} {sgr(1, where)} differs. Overwrite? [y/N] "):
                say(f"{sgr('2;37', '–')} {sgr(1, where)} {sgr('2;37', 'left unchanged')}")
                continue
        endpoint = "/api/v4/secrets/" + urllib.parse.quote(key, safe="")
        request("PATCH" if old is not None else "POST", endpoint, {
            "projectId": PROJECT, "environment": ENVIRONMENT, "secretPath": path,
            "secretValue": value, "skipMultilineEncoding": True,
        })
        say(f"{sgr(32, '✓')} Wrote {sgr(1, where)}")


def put(path, mode):
    """Store one value at ITEM/[SECTION/]FIELD, or field `value` of item KEY."""
    parts = path.split("/")
    if len(parts) > 3 or not all(parts):
        raise ValueError(USAGE)
    if len(parts) == 1:
        parts.append("value")
    title, name, key = parts if len(parts) == 3 else (parts[0], None, parts[1])
    say(heading("vault") + sgr(36, "Infisical / " + ENVIRONMENT))
    say(heading("path") + sgr(36, "/".join(p for p in (title, name) if p) + "/"))
    say(heading("key") + sgr(36, key))
    say("")
    store(title, name, {key: secret(mode)})


USAGE = "Usage: just vault [-e | -p] KEY | ITEM/FIELD | ITEM/SECTION/FIELD"


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("inject", "get", "section"):
        action = sys.argv[1]
        if action == "inject" and len(sys.argv) == 2:
            content = sys.stdin.read()
            if REFERENCE.search(content):
                open_tunnel()
            sys.stdout.write(inject(content))
            return
        if action in ("get", "section") and len(sys.argv) == 3:
            open_tunnel()
            if action == "get":
                sys.stdout.write(read_reference(sys.argv[2]))
            else:
                path = sys.argv[2].strip("/")
                if not path or any(part in ("", ".", "..") for part in path.split("/")):
                    raise ValueError("Invalid Infisical folder")
                json.dump(read_folder("/" + path), sys.stdout)
            return
        raise ValueError("Usage: vault.py inject | get ITEM/[SECTION/]FIELD | section ITEM/SECTION")
    parser = argparse.ArgumentParser(prog="just vault", description="Store an Infisical value.")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "-e", dest="mode", action="store_const", const="edit", help="write the value in an editor"
    )
    modes.add_argument(
        "-p",
        dest="mode",
        action="store_const",
        const="password",
        help="hidden input, entered twice",
    )
    parser.add_argument("path", metavar="KEY|ITEM/FIELD|ITEM/SECTION/FIELD")
    args = parser.parse_args()
    open_tunnel()
    put(args.path, args.mode)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, EOFError, KeyboardInterrupt) as error:
        say(sgr(31, f"✗ {error or 'Cancelled'}"))
        sys.exit(1)
