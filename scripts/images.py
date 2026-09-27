#!/usr/bin/env python3
"""Convert site images to WebP and check that image references use the right extension."""

import argparse
import os
import re
import shutil
import subprocess
import sys
from fnmatch import fnmatch
from functools import cache
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent.parent
REPOS = [ROOT, ROOT / "docs", ROOT / "web"]
# Repositories whose files are served to browsers; only images they reference get converted.
SITES = [ROOT / "docs", ROOT / "web"]
CONVERTIBLE = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff"}
IMAGE_EXTS = CONVERTIBLE | {".webp", ".avif", ".ico", ".svg"}
TEXT_EXTS = {
    ".md",
    ".mdx",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".css",
    ".scss",
    ".html",
    ".json",
    ".jsonc",
    ".yaml",
    ".yml",
    ".toml",
    ".svg",
    ".xml",
    ".webmanifest",
}
# Browsers and build tools need these as-is: favicons, Next.js app-dir images, and the next/og logo input.
KEEP = [
    "favicon*",
    "apple-touch-icon*",
    "android-chrome*",
    "mstile*",
    "web/app/*",
    "web/public/logo.png",
]
QUALITY = "82"
MAX_SIZE = "2560x2560>"
REF = re.compile(
    r"(?<![\w.@~/%-])((?:\.{1,2}/|[/@~])?[\w@~./%-]*[\w-]\.(?:png|jpe?g|bmp|gif|tiff?|webp|avif|ico|svg))(?![\w.-])",
    re.IGNORECASE,
)
SAME_FORMAT = {".jpeg": ".jpg", ".tiff": ".tif"}


def rel(path):
    return path.relative_to(ROOT).as_posix()


def repo_files(repo):
    # A missing .git would make git fall back to the parent repository.
    if not (repo / ".git").exists():
        return []
    out = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        check=True,
        capture_output=True,
    ).stdout
    paths = (repo / os.fsdecode(name) for name in out.split(b"\0") if name)
    return [p for p in paths if p.is_file() and not p.is_symlink()]


def skipped(path):
    return "samples" in path.relative_to(ROOT).parts


def read(path):
    with path.open(encoding="utf-8", newline="") as f:
        return f.read()


def references(repos):
    for repo in repos:
        for path in repo_files(repo):
            if (
                skipped(path)
                or path.suffix.lower() not in TEXT_EXTS
                or path.stat().st_size > 1_000_000
            ):
                continue
            try:
                lines = read(path).splitlines(keepends=True)
            except UnicodeDecodeError:
                continue
            for lineno, line in enumerate(lines, 1):
                for match in REF.finditer(line):
                    ref = match.group(1)
                    if (
                        ref.startswith("//")
                        or line[max(match.start() - 1, 0) : match.start()] == ":"
                    ):
                        continue
                    yield path, lineno, match, candidates(ref, path, repo)


def sniff(path):
    with path.open("rb") as f:
        head = f.read(16)
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    if head[:2] == b"BM":
        return ".bmp"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return ".tif"
    if head[:4] == b"\x00\x00\x01\x00":
        return ".ico"
    if head[4:8] == b"ftyp" and head[8:12] in (b"avif", b"avis"):
        return ".avif"
    return None


@cache
def listing(directory):
    try:
        return frozenset(os.listdir(directory))
    except OSError:
        return frozenset()


def exists_exact(path):
    # Exact-case match: macOS resolves wrong case, Cloudflare Pages does not.
    return path.name in listing(path.parent)


def same_stem(path):
    stem = path.stem.casefold()
    return sorted(
        name
        for name in listing(path.parent)
        if name != path.name
        and Path(name).stem.casefold() == stem
        and Path(name).suffix.lower() in IMAGE_EXTS
    )


def candidates(ref, source, repo):
    ref = unquote(ref)
    if ref.startswith("/"):
        bases, ref = [repo, repo / "public"], ref.lstrip("/")
    elif ref[:2] in ("@/", "~/"):
        bases, ref = [repo], ref[2:]
    else:
        bases = [source.parent, repo, repo / "public"]
    return [Path(os.path.normpath(base / ref)) for base in bases]


def check(_args):
    errors = []
    for repo in REPOS:
        for path in repo_files(repo):
            suffix = path.suffix.lower()
            if skipped(path) or suffix not in IMAGE_EXTS - {".svg"}:
                continue
            actual = sniff(path)
            if actual and actual != SAME_FORMAT.get(suffix, suffix):
                errors.append(
                    f"{rel(path)}: content is {actual[1:].upper()}, extension is {path.suffix}"
                )
    refs = 0
    for path, lineno, match, cands in references(REPOS):
        refs += 1
        ref = match.group(1)
        where = f"{rel(path)}:{lineno}"
        found = next((c for c in cands if exists_exact(c)), None)
        if found:
            webp = found.with_suffix(".webp")
            if found.suffix.lower() in CONVERTIBLE and exists_exact(webp):
                errors.append(f"{where}: {ref} has a WebP version; use {webp.name}")
            continue
        for cand in cands:
            if alts := same_stem(cand):
                errors.append(f"{where}: {ref} does not exist; found {', '.join(alts)}")
                break
    for error in errors:
        print(error, file=sys.stderr)
    print(f"images: checked {refs} references, {len(errors)} problems")
    return 1 if errors else 0


def convert(args):
    magick = shutil.which("magick")
    if not magick:
        sys.exit("images: ImageMagick 'magick' is not on PATH")
    base = Path(args.base).resolve()
    targets = [(base / p).resolve() for p in args.paths]
    for target in targets:
        if not target.exists():
            sys.exit(f"images: {target} does not exist")
    uses = {}
    for path, lineno, match, cands in references(SITES):
        found = next((c for c in cands if exists_exact(c)), None)
        if found and found.suffix.lower() in CONVERTIBLE:
            uses.setdefault(found, []).append((path, lineno, match))
    failed = 0
    edits = {}
    for src in sorted(uses):
        if skipped(src) or not any(t == src or t in src.parents for t in targets):
            continue
        name = rel(src)
        if any(fnmatch(src.name.lower(), k) or fnmatch(name, k) for k in KEEP):
            continue
        dst = src.with_suffix(".webp")
        if dst.exists():
            print(f"images: skipped {name}: {rel(dst)} already exists", file=sys.stderr)
            failed += 1
            continue
        tmp = dst.with_name(f".{dst.name}.tmp")
        result = subprocess.run(
            [
                magick,
                str(src),
                "-coalesce",
                "-auto-orient",
                "-strip",
                "-resize",
                MAX_SIZE,
                "-quality",
                QUALITY,
                "-define",
                "webp:method=6",
                "-define",
                "webp:alpha-quality=100",
                f"webp:{tmp}",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0 or not tmp.is_file() or sniff(tmp) != ".webp":
            tmp.unlink(missing_ok=True)
            print(f"images: failed {name}: {result.stderr.strip()}", file=sys.stderr)
            failed += 1
            continue
        before = src.stat().st_size
        os.replace(tmp, dst)
        src.unlink()
        print(
            f"images: {name} -> {dst.name} ({before / 1024:.1f} KiB -> {dst.stat().st_size / 1024:.1f} KiB)"
        )
        for path, lineno, match in uses[src]:
            ref = match.group(1)
            edits.setdefault(path, []).append(
                (lineno, match.span(1), ref[: -len(Path(ref).suffix)] + ".webp")
            )
    for path, changes in edits.items():
        lines = read(path).splitlines(keepends=True)
        for lineno, (start, end), new in sorted(changes, reverse=True):
            line = lines[lineno - 1]
            lines[lineno - 1] = line[:start] + new + line[end:]
        with path.open("w", encoding="utf-8", newline="") as f:
            f.write("".join(lines))
        print(f"images: updated {len(changes)} references in {rel(path)}")
    return 1 if failed else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(required=True)
    to_webp = commands.add_parser(
        "convert",
        help="convert site-referenced rasters under PATHS to WebP, update references, delete originals",
    )
    to_webp.add_argument("--base", default=".", help="directory that relative PATHS start from")
    to_webp.add_argument("paths", nargs="+")
    to_webp.set_defaults(func=convert)
    commands.add_parser("check", help="check image formats and references").set_defaults(func=check)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
