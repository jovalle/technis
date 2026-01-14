"""Repository-wide .env normalization and .env.example generation."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from tctl.config import REPO_ROOT

_IGNORED_DIRS = {
    ".git",
    ".idea",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    ".vscode",
    "__pycache__",
    "node_modules",
}


@dataclass(frozen=True)
class EnvSyncSummary:
    scanned_env_files: int
    sorted_env_files: int
    updated_example_files: int

    @property
    def changed(self) -> bool:
        return self.sorted_env_files > 0 or self.updated_example_files > 0

    def describe(self) -> str:
        return (
            f"Env sync: scanned {self.scanned_env_files} .env file(s), "
            f"sorted {self.sorted_env_files}, updated {self.updated_example_files} .env.example file(s)."
        )


def _discover_env_files(root: Path) -> list[Path]:
    env_files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _IGNORED_DIRS)
        if ".env" in filenames:
            env_files.append(Path(dirpath) / ".env")
    return sorted(env_files)


def _parse_env_file(path: Path) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    seen: set[str] = set()

    for line_num, raw in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in raw:
            raise ValueError(f"{path}:{line_num}: expected KEY=VALUE")

        key, _, raw_value = raw.partition("=")
        key = key.strip()
        value = raw_value.strip()
        if not key:
            raise ValueError(f"{path}:{line_num}: empty key in env assignment")
        if key in seen:
            raise ValueError(f"{path}:{line_num}: duplicate key '{key}'")

        seen.add(key)
        entries.append((key, value))

    return sorted(entries, key=lambda item: item[0])


def _render_env(entries: list[tuple[str, str]]) -> str:
    if not entries:
        return ""
    return "".join(f"{key}={value}\n" for key, value in entries)


def _render_example(entries: list[tuple[str, str]]) -> str:
    if not entries:
        return ""
    return "".join(f"{key}=''\n" for key, _ in entries)


def sync_all_env_files(root: Path | None = None) -> EnvSyncSummary:
    target_root = root or REPO_ROOT
    env_files = _discover_env_files(target_root)

    sorted_env_files = 0
    updated_example_files = 0

    for env_path in env_files:
        entries = _parse_env_file(env_path)

        normalized_env = _render_env(entries)
        current_env = env_path.read_text(encoding="utf-8")
        if current_env != normalized_env:
            env_path.write_text(normalized_env, encoding="utf-8")
            sorted_env_files += 1

        example_path = env_path.with_name(".env.example")
        example_content = _render_example(entries)
        current_example = (
            example_path.read_text(encoding="utf-8") if example_path.is_file() else None
        )
        if current_example != example_content:
            example_path.write_text(example_content, encoding="utf-8")
            updated_example_files += 1

    return EnvSyncSummary(
        scanned_env_files=len(env_files),
        sorted_env_files=sorted_env_files,
        updated_example_files=updated_example_files,
    )
