"""Drive tctl through an actual PTY against an intentionally absent local daemon."""
import fcntl
import os
from pathlib import Path
import pty
import re
import select
import signal
import struct
import sys
import tempfile
import termios
import time

binary = Path(sys.argv[1] if len(sys.argv) > 1 else "target/debug/tctl").resolve()
with tempfile.TemporaryDirectory(prefix="tctl-terminal-", dir="/tmp") as temporary:
    root = Path(temporary)
    (root / "docker/stacks/demo").mkdir(parents=True)
    (root / "docker/stacks/demo/compose.yaml").write_text("name: demo\nservices: {}\n")
    (root / "tctl.toml").write_text(
        f'[hosts.demo]\nendpoint="unix://{root}/absent.sock"\nexpected_name="demo"\n')
    pid, master = pty.fork()
    if pid == 0:
        os.environ["TERM"] = "xterm-256color"
        os.execv(str(binary), [str(binary), "--root", str(root)])
    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 35, 120, 0, 0))
    transcript = bytearray()
    exited = False

    def screen():
        cells = [[" "] * 120 for _ in range(35)]
        row = col = 0
        text = transcript.decode("utf-8", errors="replace")
        tokens = re.finditer(r"\x1b\[([0-9;?]*)([@-~])|([^\x1b])", text)
        for token in tokens:
            if token.group(2):
                values = [int(v) if v.isdigit() else 0 for v in token.group(1).split(";")]
                command = token.group(2)
                if command in "Hf":
                    row = max(0, (values[0] or 1) - 1)
                    col = max(0, (values[1] if len(values) > 1 else 1) - 1)
                elif command == "J" and values[0] == 2:
                    cells = [[" "] * 120 for _ in range(35)]
                elif command == "K" and row < 35:
                    for c in range(col, 120): cells[row][c] = " "
                elif command == "A": row = max(0, row - (values[0] or 1))
                elif command == "B": row += values[0] or 1
                elif command == "C": col += values[0] or 1
                elif command == "D": col = max(0, col - (values[0] or 1))
            else:
                char = token.group(3)
                if char == "\r": col = 0
                elif char == "\n": row += 1
                elif char.isprintable():
                    if row < 35 and col < 120: cells[row][col] = char
                    col += 1
        return "\n".join("".join(line) for line in cells)

    def wait_for(needle):
        deadline = time.monotonic() + 8
        while needle.decode() not in screen():
            if time.monotonic() > deadline:
                raise AssertionError(f"Missing {needle!r}:\n{screen()}")
            ready, _, _ = select.select([master], [], [], 0.2)
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    chunk = b""
                if not chunk:
                    raise AssertionError(f"tctl exited early: {bytes(transcript[-2000:])!r}")
                transcript.extend(chunk)
                # Respond to terminal feature/cursor queries if requested.
                if b"\x1b[6n" in chunk:
                    os.write(master, b"\x1b[1;1R")
                if b"\x1b[c" in chunk:
                    os.write(master, b"\x1b[?1;2c")

    try:
        wait_for(b"tctl")
        wait_for(b"OFFLINE")
        wait_for(b"Services [0]")
        os.write(master, b"b")
        wait_for(b"Stack scope")
        os.write(master, b"demo")
        wait_for(b"Search: demo")
        os.write(master, b"\r")
        wait_for(b" tctl  demo ")
        os.write(master, b":ctx all\r")
        wait_for(b" tctl  ALL STACKS ")
        os.write(master, b"?")
        wait_for(b"NAVIGATION")
        os.write(master, b"?")
        # Command input uses a literal tab name, then a live fuzzy-filter prompt.
        os.write(master, b":containers\r")
        wait_for(b"Containers [0]")
        os.write(master, b"/app")
        wait_for(b"/app")
        os.write(master, b"\r:q\r")
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            waited, status = os.waitpid(pid, os.WNOHANG)
            if waited:
                exited = True
                assert os.waitstatus_to_exitcode(status) == 0, status
                break
            ready, _, _ = select.select([master], [], [], 0.05)
            if ready:
                try: transcript.extend(os.read(master, 65536))
                except OSError: pass
        assert exited, "tctl did not quit cleanly"
    finally:
        os.close(master)
        if not exited:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
print("Terminal: offline startup, scope picker, all-stack scope, help, command navigation, filter and clean exit passed.")
