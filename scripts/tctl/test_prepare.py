"""Exercise the real installer with local release archives; no network or Docker."""
import hashlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile

SCRIPT = Path(__file__).with_name("prepare")
with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    (root / "scripts/tctl").mkdir(parents=True)
    shutil.copy2(SCRIPT, root / "scripts/tctl/prepare")
    tools = root / "tools"
    tools.mkdir()
    uname = tools / "uname"
    uname.write_text('#!/bin/sh\ncase "$1" in -s) echo "$TEST_OS";; -m) echo "$TEST_ARCH";; esac\n')
    uname.chmod(0o755)
    curl = tools / "curl"
    curl.write_text('''#!/usr/bin/env python3
import os, pathlib, shutil, sys
args = sys.argv[1:]
output = pathlib.Path(args[args.index('--output') + 1])
if args[-1].endswith('/SHA256SUMS'):
    shutil.copyfile(os.environ['MANIFEST'], output)
    print(os.environ.get('HTTP_CODE', '200') + ' https://example.invalid/SHA256SUMS', end='')
else:
    assert args[-1].endswith('/' + os.environ['ASSET']), args[-1]
    shutil.copyfile(os.environ['ARCHIVE'], output)
''')
    curl.chmod(0o755)
    env = {**os.environ, "PATH": str(tools) + os.pathsep + os.environ["PATH"],
           "TCTL_REPOSITORY": "example/technis", "TCTL_VERSION": "tctl-test"}
    binary = b'#!/bin/sh\necho "tctl installer-test"\n'
    archive = root / "release.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        entry = tarfile.TarInfo("tctl")
        entry.size = len(binary)
        entry.mode = 0o755
        tar.addfile(entry, io.BytesIO(binary))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    manifest = root / "SHA256SUMS"
    for system, architecture, target in [
        ("Darwin", "arm64", "aarch64-apple-darwin"),
        ("Darwin", "x86_64", "x86_64-apple-darwin"),
        ("Linux", "aarch64", "aarch64-unknown-linux-musl"),
        ("Linux", "x86_64", "x86_64-unknown-linux-musl"),
    ]:
        asset = f"tctl-{target}.tar.gz"
        manifest.write_text(f"# release: tctl-test\n{digest}  {asset}\n")
        current = {**env, "TEST_OS": system, "TEST_ARCH": architecture,
                   "ARCHIVE": str(archive), "MANIFEST": str(manifest), "ASSET": asset}
        result = subprocess.run(["bash", str(root / "scripts/tctl/prepare")], env=current,
                                text=True, capture_output=True)
        assert result.returncode == 0, result.stderr
        assert (root / "bin/tctl").read_bytes() == binary
    for failure in ["checksum", "http", "platform", "archive", "hardlink"]:
        manifest.write_text(f"# release: tctl-test\n{digest}  {asset}\n")
        current = {**current, "HTTP_CODE": "200", "TEST_OS": "Linux"}
        if failure == "checksum":
            manifest.write_text(f"# release: tctl-test\n{'0' * 64}  {asset}\n")
        elif failure == "http":
            current["HTTP_CODE"] = "500"
        elif failure == "platform":
            current["TEST_OS"] = "Unsupported"
        elif failure == "archive":
            with tarfile.open(archive, "w:gz") as tar:
                tar.addfile(tarfile.TarInfo("../escaped"))
            manifest.write_text(f"# release: tctl-test\n{hashlib.sha256(archive.read_bytes()).hexdigest()}  {asset}\n")
        else:
            outside = root / "outside"
            outside.write_bytes(b"untouched")
            with tarfile.open(archive, "w:gz") as tar:
                entry = tarfile.TarInfo("tctl")
                entry.type = tarfile.LNKTYPE
                entry.linkname = str(outside)
                tar.addfile(entry)
            manifest.write_text(f"# release: tctl-test\n{hashlib.sha256(archive.read_bytes()).hexdigest()}  {asset}\n")
        result = subprocess.run(["bash", str(root / "scripts/tctl/prepare")], env=current,
                                text=True, capture_output=True)
        assert result.returncode != 0, failure
        assert (root / "bin/tctl").read_bytes() == binary, failure
        assert not (root / "escaped").exists()
print("Installer: four platforms, checksums, atomic replacement, HTTP and archive rejection passed.")
