"""Compose manifest formatting for tctl."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from tctl import output as out

_COMPOSE_GLOBS = ("docker/services/**/compose.y*ml", "docker/stacks/**/compose.y*ml")
_KEY_VALUE_LIST_KEYS = frozenset({"environment", "extra_hosts", "labels", "sysctls"})
_SORTED_STRING_LIST_KEYS = frozenset(
    {
        "cap_add",
        "cap_drop",
        "depends_on",
        "dns",
        "extra_hosts",
        "networks",
        "security_opt",
        "tmpfs",
    }
)


class _ComposeDumper(yaml.SafeDumper):
    def increase_indent(self, flow: bool = False, indentless: bool = False) -> Any:
        return super().increase_indent(flow, False)


def _represent_str(dumper: _ComposeDumper, data: str) -> yaml.ScalarNode:
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


_ComposeDumper.add_representer(str, _represent_str)


def _current_key(path: tuple[str, ...]) -> str | None:
    for part in reversed(path):
        if part != "[]":
            return part
    return None


def _ancestor_keys(path: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(part for part in path if part != "[]")


def _sort_key(value: str) -> tuple[str, str]:
    return value.casefold(), value


def _compose_scalar(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _split_key_value(item: str) -> tuple[str, str | None]:
    key, sep, value = item.partition("=")
    if not sep:
        return item, None
    return key, value


def _normalize_key_value_list(value: Any) -> Any:
    if isinstance(value, dict):
        items: list[str] = []
        for key, raw in value.items():
            rendered = _compose_scalar(raw)
            items.append(key if rendered is None else f"{key}={rendered}")
        return sorted(items, key=lambda item: _sort_key(_split_key_value(item)[0]))

    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return sorted(value, key=lambda item: _sort_key(_split_key_value(item)[0]))

    return value


def _include_sort_key(entry: Any) -> tuple[str, str]:
    if not isinstance(entry, dict):
        text = str(entry)
        return _sort_key(text)

    raw_path = entry.get("path", "")
    if isinstance(raw_path, list) and raw_path:
        primary = str(raw_path[0])
    else:
        primary = str(raw_path)

    service_name = Path(primary).parts[-2] if len(Path(primary).parts) >= 2 else primary
    return service_name.casefold(), primary


def _normalize_list(value: list[Any], path: tuple[str, ...]) -> list[Any]:
    key = _current_key(path)
    normalized = [_normalize_value(item, path + ("[]",)) for item in value]

    if key == "include":
        return sorted(normalized, key=_include_sort_key)

    if (
        key == "path"
        and "include" in _ancestor_keys(path)
        and all(isinstance(item, str) for item in normalized)
    ):
        return sorted(normalized, key=_sort_key)

    if key in _SORTED_STRING_LIST_KEYS and all(
        isinstance(item, str) for item in normalized
    ):
        return sorted(normalized, key=_sort_key)

    return normalized


def _normalize_mapping(value: dict[str, Any], path: tuple[str, ...]) -> dict[str, Any]:
    items = [
        (key, _normalize_value(item, path + (str(key),)))
        for key, item in sorted(value.items(), key=lambda pair: _sort_key(str(pair[0])))
    ]
    return dict(items)


def _normalize_value(value: Any, path: tuple[str, ...] = ()) -> Any:
    key = _current_key(path)

    if key in _KEY_VALUE_LIST_KEYS:
        value = _normalize_key_value_list(value)

    if isinstance(value, dict):
        return _normalize_mapping(value, path)

    if isinstance(value, list):
        return _normalize_list(value, path)

    return value


def _add_spacing(text: str) -> str:
    lines = text.splitlines()
    if not lines:
        return ""

    output: list[str] = []
    top_level: str | None = None
    first_service = True

    for line in lines:
        if re.match(r"^[A-Za-z0-9_-]+:", line):
            if output and output[-1] != "":
                output.append("")
            top_level = line.split(":", 1)[0]
            first_service = True
            output.append(line)
            continue

        if top_level == "services" and re.match(r"^  [A-Za-z0-9_.-]+:$", line):
            if not first_service and output and output[-1] != "":
                output.append("")
            first_service = False

        output.append(line)

    return "\n".join(output).rstrip() + "\n"


def format_compose_text(text: str, *, source: str = "<memory>") -> str:
    try:
        parsed = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"Unable to parse compose YAML from {source}.\n{exc}") from exc

    if not isinstance(parsed, dict):
        raise ValueError(
            f"Compose document at {source} must be a mapping at the top level."
        )

    normalized = _normalize_value(parsed)
    rendered = yaml.dump(
        normalized,
        Dumper=_ComposeDumper,
        allow_unicode=False,
        default_flow_style=False,
        sort_keys=False,
        width=1000,
    )
    return _add_spacing(rendered)


def _discover_compose_files(repo_root: Path, targets: tuple[Path, ...]) -> list[Path]:
    if not targets:
        files: set[Path] = set()
        for pattern in _COMPOSE_GLOBS:
            files.update(
                path.resolve() for path in repo_root.glob(pattern) if path.is_file()
            )
        return sorted(files)

    files: set[Path] = set()
    for target in targets:
        resolved = target.resolve()
        if resolved.is_file():
            files.add(resolved)
            continue
        if not resolved.is_dir():
            raise ValueError(f"Path not found: {target}")
        files.update(
            path.resolve() for path in resolved.rglob("compose.y*ml") if path.is_file()
        )

    return sorted(files)


def format_compose_files(
    repo_root: Path,
    *,
    targets: tuple[Path, ...] = (),
    check: bool = False,
) -> tuple[list[Path], list[Path]]:
    files = _discover_compose_files(repo_root, targets)
    changed: list[Path] = []

    for path in files:
        original = path.read_text()
        formatted = format_compose_text(original, source=str(path))
        if formatted == original:
            continue
        changed.append(path)
        if not check:
            path.write_text(formatted)

    return files, changed


def fmt_compose_manifests(
    *, targets: tuple[Path, ...] = (), check: bool = False
) -> None:
    repo_root = Path.cwd()
    files, changed = format_compose_files(repo_root, targets=targets, check=check)

    out.header("Formatting compose manifests")
    out.meta(f"Scanned {len(files)} manifest(s)")

    if changed:
        for path in changed:
            display = path.relative_to(repo_root)
            verb = "Would format" if check else "Formatted"
            out.info(f"{verb} {display}")

        if check:
            out.warn(f"{len(changed)} manifest(s) need formatting.")
            raise SystemExit(1)

        out.ok(f"Formatted {len(changed)} manifest(s).")
        return

    message = "All compose manifests are already formatted."
    if check:
        out.ok(message)
        return

    out.ok(message)
