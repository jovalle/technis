"""Push/pull local ↔ remote service files via rsync."""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal

import yaml

from tctl import output as out
from tctl.compose import stack_services
from tctl.config import (
    REPO_ROOT,
    SERVICES_DIR,
    SSH_OPTS,
    base_dir,
    stack_compose,
    stack_services_dir,
    subprocess_env,
)

RSYNC_EXCLUDES = [
    "--exclude=/compose.yaml",
    "--exclude=/compose.yml",
    "--exclude=.gitignore",
    "--exclude=.tignore",
    "--exclude=.env",
    "--exclude=*.template",
    "--exclude=*.example",
]


# ── Rsync change parsing ────────────────────────────────────────────

_TYPE_MAP = {"f": "file", "d": "dir", "L": "symlink", "D": "device", "S": "special"}
_ACTION_MAP = {"<": "send", ">": "receive", "c": "create", "h": "hardlink", ".": "meta"}

_ACTION_GLYPH = {
    "send": ("+", "green"),
    "create": ("+", "green"),
    "receive": ("+", "cyan"),
    "delete": ("-", "red"),
    "meta": ("~", "yellow"),
    "update": ("~", "yellow"),
    "hardlink": ("+", "magenta"),
}

_TYPE_GLYPH = {
    "file": "📄",
    "symlink": "🔗",
    "device": "◫",
    "special": "◇",
    "dir": "📁",
    "item": "•",
}

_ATTR_GLYPH = {
    "checksum": "#",
    "size": "s",
    "mtime": "t",
    "perms": "p",
    "owner": "u",
    "group": "g",
    "new": "+",
    "content": "c",
    "metadata": "~",
}

_PERMS_STATE_DIR = REPO_ROOT / "tmp" / ".tctl" / "permissions"
_EXTENSION_POLICY_FILE = REPO_ROOT / "tmp" / ".tctl" / "extension-policy.json"
_CHANGE_PREVIEW_MAX_ITEMS = 50

_CONFIG_AUTO_APPROVE_SUFFIXES = {
    ".yaml",
    ".yml",
    ".json",
    ".toml",
    ".ini",
    ".conf",
    ".cfg",
    ".ico",
}
_CONFIG_AUTO_APPROVE_NAMES = {
    ".env",
}

_STATE_IGNORE_SUFFIXES = {
    ".db",
    ".sqlite",
    ".sqlite3",
    ".shm",
    ".wal",
    ".pid",
    ".sock",
    ".lock",
    ".log",
    ".cache",
}

ExtensionDecision = Literal["config", "state", "manual"]
_EXTENSION_DECISION_VALUES = {"config", "state", "manual"}


@dataclass
class RsyncChange:
    path: str
    type_label: str
    action: str
    changes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ManagedBindMount:
    owner_service: str
    compose_service: str
    remote_abs: str
    remote_rel: str
    container_target: str
    file_like: bool


def _load_extension_policy() -> dict[str, ExtensionDecision]:
    if not _EXTENSION_POLICY_FILE.is_file():
        return {}

    try:
        payload = json.loads(_EXTENSION_POLICY_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}

    decisions_raw = payload.get("decisions", {}) if isinstance(payload, dict) else {}
    if not isinstance(decisions_raw, dict):
        return {}

    decisions: dict[str, ExtensionDecision] = {}
    for ext, decision in decisions_raw.items():
        if not isinstance(ext, str) or not isinstance(decision, str):
            continue
        normalized_ext = ext.strip().lower()
        normalized_decision = decision.strip().lower()
        if not normalized_ext.startswith("."):
            continue
        if normalized_decision not in _EXTENSION_DECISION_VALUES:
            continue
        decisions[normalized_ext] = normalized_decision  # type: ignore[assignment]

    return decisions


def _save_extension_policy(decisions: dict[str, ExtensionDecision]) -> None:
    _EXTENSION_POLICY_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "decisions": {ext: decisions[ext] for ext in sorted(decisions)},
    }
    _EXTENSION_POLICY_FILE.write_text(json.dumps(payload, indent=2, sort_keys=True))


def _state_filter_args(extension_policy: dict[str, ExtensionDecision]) -> list[str]:
    suffixes = {
        *_STATE_IGNORE_SUFFIXES,
        *{
            suffix
            for suffix, decision in extension_policy.items()
            if suffix.startswith(".") and decision == "state"
        },
    }
    return [f"--exclude=*{suffix}" for suffix in sorted(suffixes)]


def _with_state_filters(
    filter_args: list[str],
    extension_policy: dict[str, ExtensionDecision],
) -> list[str]:
    merged = list(filter_args)
    for state_arg in _state_filter_args(extension_policy):
        if state_arg not in merged:
            merged.append(state_arg)
    return merged


def _prompt_extension_decision(extension: str, sample_path: str) -> ExtensionDecision:
    if not out.console.is_interactive:
        out.warn(
            "Unknown diff extension "
            f"{extension} ({sample_path}); defaulting to manual review in non-interactive mode."
        )
        return "manual"

    prompt_text = (
        "Unknown diff extension "
        f"{extension} ({sample_path}). "
        "Classify for future syncs: [c]onfig auto-approve / [i]gnore as state / [m]anual review [m] "
    )

    while True:
        answer = out.prompt(prompt_text).strip().lower()
        if not answer or answer in {"m", "manual"}:
            return "manual"
        if answer in {"c", "config"}:
            return "config"
        if answer in {"i", "ignore", "s", "state"}:
            return "state"

        out.warn("Please answer c/config, i/ignore/state, or m/manual.")


def _classify_change_for_policy(
    change: RsyncChange,
    extension_policy: dict[str, ExtensionDecision],
) -> ExtensionDecision:
    name = PurePosixPath(change.path).name.lower()
    if name in _CONFIG_AUTO_APPROVE_NAMES:
        return "config"

    suffix = PurePosixPath(name).suffix.lower()
    if not suffix:
        return "manual"

    if suffix in _CONFIG_AUTO_APPROVE_SUFFIXES:
        return "config"
    if suffix in _STATE_IGNORE_SUFFIXES:
        return "state"

    decision = extension_policy.get(suffix)
    if decision:
        return decision

    decision = _prompt_extension_decision(suffix, change.path)
    extension_policy[suffix] = decision
    _save_extension_policy(extension_policy)
    out.meta(f"Remembering {suffix} as {decision} for future syncs.")
    return decision


def _partition_policy_changes(
    changes: list[RsyncChange],
    extension_policy: dict[str, ExtensionDecision],
) -> tuple[list[RsyncChange], int, int]:
    actionable: list[RsyncChange] = []
    ignored_state = 0
    manual_review = 0

    for change in changes:
        decision = _classify_change_for_policy(change, extension_policy)
        if decision == "state":
            ignored_state += 1
            continue
        actionable.append(change)
        if decision == "manual":
            manual_review += 1

    return actionable, ignored_state, manual_review


def _parse_rsync_itemize(output: str) -> list[RsyncChange]:
    """Parse rsync --itemize-changes output into structured changes."""
    results = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue

        if line.startswith("*deleting "):
            path = line.removeprefix("*deleting ").strip()
            if path and path != "./" and not path.endswith("/"):
                results.append(RsyncChange(path, "file", "delete", ["content"]))
            continue

        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        code, path = parts

        # Keep only real rsync itemize codes (e.g. ">f.st......").
        if len(code) < 11 or code[0] not in _ACTION_MAP or code[1] not in _TYPE_MAP:
            continue

        # Skip directory-only metadata entries
        if path == "./" or path.endswith("/") or code[1] == "d":
            continue

        type_label = _TYPE_MAP.get(code[1], "item")
        action = _ACTION_MAP.get(code[0], "update")

        attrs = []
        if code[2] == "c":
            attrs.append("checksum")
        if code[3] == "s":
            attrs.append("size")
        if code[4] in {"t", "T"}:
            attrs.append("mtime")
        if code[5] == "p":
            attrs.append("perms")
        if code[6] == "o":
            attrs.append("owner")
        if code[7] == "g":
            attrs.append("group")
        if not attrs and "+" in code:
            attrs.append("new")

        results.append(RsyncChange(path, type_label, action, attrs or ["metadata"]))
    return results


def _summarize_changes(changes: list[RsyncChange]) -> str:
    """Build a short, human-readable summary of parsed rsync changes."""
    if not changes:
        return "no changes"

    action_counts = Counter(c.action for c in changes)
    parts = [f"{len(changes)} change{'s' if len(changes) != 1 else ''}"]
    for action in ("send", "create", "delete", "receive", "meta", "update"):
        count = action_counts.get(action, 0)
        if count:
            parts.append(f"{count} {action}")
    return ", ".join(parts)


def _is_mtime_only_metadata_change(change: RsyncChange) -> bool:
    return change.action == "meta" and change.changes == ["mtime"]


def _partition_actionable_changes(
    changes: list[RsyncChange],
) -> tuple[list[RsyncChange], int]:
    actionable: list[RsyncChange] = []
    ignored_mtime_only = 0
    for change in changes:
        if _is_mtime_only_metadata_change(change):
            ignored_mtime_only += 1
            continue
        actionable.append(change)
    return actionable, ignored_mtime_only


def _has_pushable_files(local_dir: Path) -> bool:
    """Check if directory has non-compose, non-template files."""
    if not local_dir.is_dir():
        return False
    skip = {"compose.yaml", "compose.yml", ".gitignore", ".tignore", ".env"}
    skip_ext = {".template", ".example"}
    for child in local_dir.rglob("*"):
        if child.is_file() and child.name not in skip and child.suffix not in skip_ext:
            return True
    return False


def _is_pushable_file(path: Path) -> bool:
    if not path.is_file():
        return False
    skip = {"compose.yaml", "compose.yml", ".gitignore", ".tignore", ".env"}
    skip_ext = {".template", ".example"}
    return path.name not in skip and path.suffix not in skip_ext


def _has_pushable_path(path: Path, file_like: bool) -> bool:
    if file_like:
        return _is_pushable_file(path)
    return _has_pushable_files(path)


def _mount_local_path_exists(path: Path, file_like: bool) -> bool:
    if file_like:
        return path.is_file()
    return path.is_dir()


def _rsync_source_dir(path: Path) -> str:
    """Return a path string with a trailing slash so rsync copies directory contents."""
    raw = str(path)
    return raw if raw.endswith("/") else f"{raw}/"


def _rsync_diff(
    src: str,
    dst: str,
    *,
    filter_args: list[str] | None = None,
    copy_unsafe_links: bool = False,
) -> str:
    """Dry-run rsync to get itemize-changes output."""
    args = ["rsync", "-rlptn", "--checksum", "--itemize-changes", *RSYNC_EXCLUDES]
    if copy_unsafe_links:
        args.append("--copy-unsafe-links")
    if filter_args:
        args.extend(filter_args)
    args.extend([src, dst])
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _rsync_exec(
    src: str,
    dst: str,
    *,
    filter_args: list[str] | None = None,
    copy_unsafe_links: bool = False,
) -> None:
    """Execute rsync transfer."""
    args = ["rsync", "-rlpt", "--checksum", *RSYNC_EXCLUDES]
    if copy_unsafe_links:
        args.append("--copy-unsafe-links")
    if filter_args:
        args.extend(filter_args)
    args.extend([src, dst])
    subprocess.run(
        args,
        check=True,
    )


def _mount_path(base: Path | str, item_path: str, file_like: bool) -> str:
    if file_like:
        return str(base)
    rel = item_path.lstrip("./")
    if not rel:
        return str(base)
    return str(Path(base) / rel)


def _service_root(path: Path, owner_service: str) -> Path:
    parts = path.parts
    for idx in range(len(parts) - 1):
        if parts[idx] == "services" and parts[idx + 1] == owner_service:
            return Path(*parts[: idx + 2])
    return path.parent


def _service_relative_local_path(local_path: str, owner_service: str) -> str:
    path = Path(local_path)
    base = _service_root(path, owner_service)
    try:
        rel = path.relative_to(base)
    except ValueError:
        return path.as_posix()
    return rel.as_posix() if rel != Path(".") else "."


def _attr_marks(change: RsyncChange) -> str:
    marks = "".join(_ATTR_GLYPH.get(attr, "?") for attr in change.changes)
    return marks or "~"


def _show_changes(
    *,
    host: str,
    owner_service: str,
    changes: list[RsyncChange],
    local_base: Path,
    remote_base: str,
    file_like: bool,
    direction: str,
) -> None:
    def _preview_path(path: str) -> str:
        text = path.strip()
        if text in {"", ".", "./"}:
            return "."

        absolute = text.startswith("/")
        parts = [
            part for part in PurePosixPath(text).parts if part not in {"", ".", "/"}
        ]
        if not parts:
            return "/" if absolute else "."

        joined = "/".join(parts)
        return f"/{joined}" if absolute else joined

    grouped: dict[tuple[str, str, str, str, str], int] = {}

    for c in changes:
        sym, color = _ACTION_GLYPH.get(c.action, ("~", "white"))
        type_glyph = _TYPE_GLYPH.get(c.type_label, "•")
        attrs = _attr_marks(c)

        local_path = _service_relative_local_path(
            _mount_path(local_base, c.path, file_like), owner_service
        )
        remote_path = _mount_path(remote_base, c.path, file_like)
        preview_local = _preview_path(local_path)
        preview_remote = _preview_path(remote_path)

        if direction == "push":
            flow = f"{preview_local} [dim]→[/] {host}:{preview_remote}"
        else:
            flow = f"{host}:{preview_remote} [dim]→[/] {preview_local}"

        key = (sym, color, type_glyph, flow, attrs)
        grouped[key] = grouped.get(key, 0) + 1

    grouped_items = list(grouped.items())
    for idx, (row, count) in enumerate(grouped_items):
        if idx >= _CHANGE_PREVIEW_MAX_ITEMS:
            break

        sym, color, type_glyph, flow, attrs = row
        multiplicity = f" [dim]x{count}[/]" if count > 1 else ""
        out.console.print(
            f"[{color}]{sym}[/] {type_glyph} {flow} [dim]{attrs}[/]{multiplicity}"
        )

    if len(grouped_items) > _CHANGE_PREVIEW_MAX_ITEMS:
        hidden_groups = len(grouped_items) - _CHANGE_PREVIEW_MAX_ITEMS
        hidden_changes = sum(
            count for _, count in grouped_items[_CHANGE_PREVIEW_MAX_ITEMS:]
        )
        out.info(
            f"Preview limited to {_CHANGE_PREVIEW_MAX_ITEMS} entries; "
            f"{hidden_groups} more grouped entries ({hidden_changes} changes) hidden."
        )


def _print_change_key() -> None:
    out.meta(
        "Key: + send/create, - delete, ~ metadata | "
        "📄 file, 🔗 symlink, ◫ device, ◇ special | "
        "attrs: # checksum, s size, t mtime, p perms, u owner, g group, + new, c content"
    )


def _split_short_volume_spec(spec: str) -> tuple[str | None, str | None, str]:
    parts = spec.split(":")
    if len(parts) < 2:
        return None, None, ""
    source = parts[0]
    target = parts[1]
    options = ":".join(parts[2:]) if len(parts) > 2 else ""
    return source, target, options


def _volume_is_read_only(options: str) -> bool:
    return any(opt.strip() == "ro" for opt in options.split(",") if opt.strip())


def _looks_like_file_path(path: str) -> bool:
    return Path(path).suffix != ""


def _load_rendered_compose_config(host: str) -> dict:
    compose = stack_compose(host)
    env = subprocess_env(host)
    result = subprocess.run(
        ["docker", "compose", "-f", str(compose), "config"],
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode != 0:
        raise SystemExit(
            f"Unable to render compose config for '{host}'.\n{result.stderr}"
        )
    return yaml.safe_load(result.stdout) or {}


def _service_owner_map(host: str) -> dict[str, str]:
    mapping: dict[str, str] = {}
    stack_dir = stack_services_dir(host)

    for owner in stack_services(host):
        mapping.setdefault(owner, owner)
        for compose_file in (
            SERVICES_DIR / owner / "compose.yaml",
            stack_dir / owner / "compose.yaml",
        ):
            if not compose_file.is_file():
                continue
            data = yaml.safe_load(compose_file.read_text()) or {}
            svc_defs = data.get("services", {})
            if not isinstance(svc_defs, dict):
                continue
            for name in svc_defs:
                mapping.setdefault(str(name), owner)
    return mapping


def _resolve_requested_owner_services(host: str, service: str | None) -> list[str]:
    owners = stack_services(host)
    if service is None:
        return owners

    if service in owners:
        return [service]

    owner_map = _service_owner_map(host)
    owner = owner_map.get(service)
    if owner:
        out.warn(f"{service}: compose service maps to sync scope '{owner}'.")
        return [owner]

    available = ", ".join(owners)
    raise SystemExit(
        f"Service '{service}' is not in stack '{host}'. Available sync scopes: {available}"
    )


def _collect_managed_bind_mounts(
    host: str,
    owners: list[str],
) -> tuple[list[ManagedBindMount], list[str]]:
    owner_filter = set(owners)
    owner_map = _service_owner_map(host)
    cfg = _load_rendered_compose_config(host)
    svcs = cfg.get("services", {})
    if not isinstance(svcs, dict):
        return [], []

    remote_root = base_dir(host).rstrip("/")
    mounts: list[ManagedBindMount] = []
    warnings: list[str] = []

    for compose_service, svc_def in svcs.items():
        owner = owner_map.get(str(compose_service))
        if not owner or owner not in owner_filter:
            continue

        volumes = svc_def.get("volumes", [])
        if not isinstance(volumes, list):
            continue

        for volume in volumes:
            source = ""
            target = ""

            if isinstance(volume, str):
                source, target, _options = _split_short_volume_spec(volume)
                if not source or not target:
                    continue
            elif isinstance(volume, dict):
                source = str(volume.get("source") or "")
                target = str(volume.get("target") or "")
                volume_type = str(volume.get("type") or "")
                if volume_type and volume_type != "bind":
                    continue
                if not source.startswith("/") or not target:
                    continue
            else:
                continue

            if not source.startswith("/"):
                continue

            if source != remote_root and not source.startswith(f"{remote_root}/"):
                continue

            rel = source[len(remote_root) :].lstrip("/")
            if not rel:
                warnings.append(
                    f"{owner}: mount {source}:{target} points at STACK_DATA_ROOT directly; skipping."
                )
                continue

            mounts.append(
                ManagedBindMount(
                    owner_service=owner,
                    compose_service=str(compose_service),
                    remote_abs=source,
                    remote_rel=rel,
                    container_target=target,
                    file_like=_looks_like_file_path(source),
                )
            )

    deduped: dict[tuple[str, str], ManagedBindMount] = {}
    for mount in mounts:
        deduped.setdefault((mount.owner_service, mount.remote_abs), mount)

    ordered = sorted(deduped.values(), key=lambda m: (m.owner_service, m.remote_abs))
    return ordered, warnings


def _service_local_mount_rel(
    owner_service: str,
    remote_rel: str,
) -> Path:
    owner_prefix = f"{owner_service}/"

    if remote_rel == owner_service:
        return Path("_")

    if remote_rel.startswith(owner_prefix):
        remainder = remote_rel[len(owner_prefix) :].strip("/")
        return (Path("_") / remainder) if remainder else Path("_")

    normalized = remote_rel.strip("/")
    return (Path("_") / normalized) if normalized else Path("_")


def _local_mount_path(
    root: Path,
    owner_service: str,
    remote_rel: str,
    _container_target: str,
) -> Path:
    return root / owner_service / _service_local_mount_rel(owner_service, remote_rel)


def _service_relative_mount_path(
    owner_service: str,
    remote_rel: str,
    _container_target: str,
) -> Path:
    return _service_local_mount_rel(owner_service, remote_rel)


def _load_tignore_file(
    path: Path, seen: set[Path] | None = None
) -> tuple[bool, list[str]]:
    if not path.is_file():
        return False, []

    resolved = path.resolve()
    active = seen or set()
    if resolved in active:
        return True, []

    active.add(resolved)
    patterns: list[str] = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line:
            continue

        # Syncthing-style include directive.
        if line.startswith("#include "):
            include_rel = line.removeprefix("#include ").strip()
            include_path = (path.parent / include_rel).resolve()
            _, included = _load_tignore_file(include_path, active)
            patterns.extend(included)
            continue

        # Comments in common Syncthing style.
        if line.startswith("#") or line.startswith("//"):
            continue

        patterns.append(line)

    active.remove(resolved)
    return True, patterns


def _load_service_tignore_patterns(owner_service: str) -> tuple[bool, list[str]]:
    return _load_tignore_file(SERVICES_DIR / owner_service / ".tignore")


def _service_root_tignore_exists(owner_service: str) -> bool:
    return (SERVICES_DIR / owner_service / ".tignore").is_file()


def _touch_service_root_tignore(owner_service: str) -> tuple[Path, bool]:
    tignore = SERVICES_DIR / owner_service / ".tignore"
    existed = tignore.is_file()
    tignore.parent.mkdir(parents=True, exist_ok=True)
    tignore.touch(exist_ok=True)
    return tignore, existed


def _parse_syncthing_pattern(raw_pattern: str) -> tuple[bool, bool, str]:
    pattern = raw_pattern.strip()
    case_insensitive = False

    while pattern.startswith("(?"):
        close = pattern.find(")")
        if close <= 2:
            break
        opts = pattern[2:close]
        if "i" in opts:
            case_insensitive = True
        pattern = pattern[close + 1 :]

    negated = pattern.startswith("!")
    if negated:
        pattern = pattern[1:]

    return negated, case_insensitive, pattern.strip()


def _pattern_matches_path(rel_path: str, pattern: str) -> bool:
    path = rel_path.strip("/")
    pat = pattern.strip()
    if not pat:
        return False

    anchored = pat.startswith("/")
    if anchored:
        pat = pat.lstrip("/")

    dir_only = pat.endswith("/")
    if dir_only:
        pat = pat.rstrip("/")

    if not pat:
        return False

    probes: list[str] = [path]
    probe_child = f"{path}/.tctl-sync-probe" if path else ".tctl-sync-probe"
    probes.append(probe_child)

    checks = [pat]
    if dir_only:
        checks.append(f"{pat}/**")
    if not anchored and "/" not in pat:
        checks.append(f"**/{pat}")
        if dir_only:
            checks.append(f"**/{pat}/**")

    for probe in probes:
        candidate = PurePosixPath(probe)
        for check in checks:
            if candidate.match(check):
                return True
    return False


def _path_excluded_by_tignore(rel_path: str, patterns: list[str]) -> bool:
    excluded = False
    for raw_pattern in patterns:
        is_negated, case_insensitive, check = _parse_syncthing_pattern(raw_pattern)
        if not check:
            continue

        candidate_rel = rel_path.lower() if case_insensitive else rel_path
        candidate_check = check.lower() if case_insensitive else check

        if _pattern_matches_path(candidate_rel, candidate_check):
            excluded = not is_negated
    return excluded


def _mount_tignore_rules(
    mount_path: Path, *, file_like: bool
) -> tuple[bool, list[str]]:
    """Return (skip_mount, patterns) for a mount-local .tignore file."""
    tignore = (
        (mount_path.parent / ".tignore")
        if mount_path.suffix
        else (mount_path / ".tignore")
    )
    exists, patterns = _load_tignore_file(tignore)
    if not exists:
        return False, []

    # Empty .tignore means exempt the mount entirely.
    if not patterns:
        return True, []

    # Preserve existing opt-out behavior for whole-mount skipping.
    if _path_excluded_by_tignore(".tctl-sync-probe", patterns):
        return True, patterns

    # File-like mounts can be excluded explicitly by filename.
    if file_like and _path_excluded_by_tignore(mount_path.name, patterns):
        return True, patterns

    return False, patterns


def _rsync_filter_args_from_tignore(patterns: list[str]) -> list[str]:
    """Translate Syncthing-style patterns into rsync filter arguments.

    Rsync uses first-match-wins; .tignore uses last-match-wins, so we reverse
    pattern order to preserve effective precedence.
    """
    if not patterns:
        return []

    rules: list[str] = ["+ */"]
    for raw_pattern in reversed(patterns):
        is_negated, _case_insensitive, check = _parse_syncthing_pattern(raw_pattern)
        if not check:
            continue
        op = "+" if is_negated else "-"
        rules.append(f"{op} {check}")

    args: list[str] = []
    for rule in rules:
        args.extend(["--filter", rule])
    return args


def _perms_state_file(host: str) -> Path:
    _PERMS_STATE_DIR.mkdir(parents=True, exist_ok=True)
    return _PERMS_STATE_DIR / f"{host}.json"


def _load_perms_state(host: str) -> dict[str, list[dict[str, str]]]:
    state_file = _perms_state_file(host)
    if not state_file.is_file():
        return {}

    try:
        payload = json.loads(state_file.read_text())
    except (json.JSONDecodeError, OSError):
        return {}

    mounts = payload.get("mounts", {}) if isinstance(payload, dict) else {}
    if not isinstance(mounts, dict):
        return {}

    normalized: dict[str, list[dict[str, str]]] = {}
    for mount_path, entries in mounts.items():
        if not isinstance(mount_path, str) or not isinstance(entries, list):
            continue
        clean_entries: list[dict[str, str]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            rel = str(entry.get("rel", "."))
            item_type = str(entry.get("type", "f"))
            mode = str(entry.get("mode", ""))
            uid = str(entry.get("uid", ""))
            gid = str(entry.get("gid", ""))
            if not mode or not uid or not gid:
                continue
            clean_entries.append(
                {"rel": rel, "type": item_type, "mode": mode, "uid": uid, "gid": gid}
            )
        if clean_entries:
            normalized[mount_path] = clean_entries
    return normalized


def _save_perms_state(host: str, mounts: dict[str, list[dict[str, str]]]) -> None:
    state_file = _perms_state_file(host)
    payload = {"version": 1, "mounts": mounts}
    state_file.write_text(json.dumps(payload, indent=2, sort_keys=True))


def _capture_remote_permissions(
    host: str, remote_abs: str, file_like: bool
) -> list[dict[str, str]]:
    if file_like:
        script = """
set -euo pipefail
target="$1"
if [ -L "$target" ]; then
  printf '.\tl\t000\t0\t0\n'
elif [ -d "$target" ]; then
  printf '.\td\t%s\t%s\t%s\n' "$(stat -c '%a' "$target")" "$(stat -c '%u' "$target")" "$(stat -c '%g' "$target")"
elif [ -e "$target" ]; then
  printf '.\tf\t%s\t%s\t%s\n' "$(stat -c '%a' "$target")" "$(stat -c '%u' "$target")" "$(stat -c '%g' "$target")"
fi
""".strip()
    else:
        script = """
set -euo pipefail
target="$1"
if [ -d "$target" ]; then
  find "$target" -printf '%P\t%y\t%m\t%U\t%G\n'
fi
""".strip()

    result = subprocess.run(
        ["ssh", *SSH_OPTS, host, "bash", "-s", "--", remote_abs],
        input=script,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        out.warn(
            f"Unable to snapshot remote permissions for {remote_abs}: {result.stderr.strip()}"
        )
        return []

    entries: list[dict[str, str]] = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 5:
            continue
        rel, item_type, mode, uid, gid = parts
        rel_path = rel if rel else "."
        if not mode or not uid or not gid:
            continue
        entries.append(
            {"rel": rel_path, "type": item_type, "mode": mode, "uid": uid, "gid": gid}
        )

    entries.sort(key=lambda item: item["rel"])
    return entries


def _record_remote_permissions(host: str, mount: ManagedBindMount) -> None:
    entries = _capture_remote_permissions(host, mount.remote_abs, mount.file_like)
    if not entries:
        return

    state = _load_perms_state(host)
    state[mount.remote_abs] = entries
    _save_perms_state(host, state)


def _snapshot_remote_permissions_for_mounts(
    host: str,
    mounts: list[ManagedBindMount],
) -> dict[str, list[dict[str, str]]]:
    snapshots: dict[str, list[dict[str, str]]] = {}
    state = _load_perms_state(host)

    for mount in mounts:
        entries = _capture_remote_permissions(host, mount.remote_abs, mount.file_like)
        if not entries:
            continue
        snapshots[mount.remote_abs] = entries
        state[mount.remote_abs] = entries

    if snapshots:
        _save_perms_state(host, state)
    return snapshots


def _apply_permissions_entries(
    host: str,
    remote_abs: str,
    entries: list[dict[str, str]],
) -> None:
    if not entries:
        return

    payload = "\n".join(
        f"{entry['rel']}\t{entry['type']}\t{entry['mode']}\t{entry['uid']}\t{entry['gid']}"
        for entry in entries
    )
    if not payload:
        return

    script = (
        "set -euo pipefail; "
        'base="$1"; '
        "while IFS=$'\\t' read -r rel item_type mode uid gid; do "
        '[ -z "$rel" ] && continue; '
        'if [ "$rel" = "." ]; then path="$base"; else path="$base/$rel"; fi; '
        '[ -e "$path" ] || continue; '
        '[ "$item_type" = "l" ] && continue; '
        'chown "$uid:$gid" "$path" 2>/dev/null || true; '
        'chmod "$mode" "$path" 2>/dev/null || true; '
        "done"
    )
    remote_cmd = f"bash -lc {shlex.quote(script)} -- {shlex.quote(remote_abs)}"

    result = subprocess.run(
        ["ssh", *SSH_OPTS, host, remote_cmd],
        input=f"{payload}\n",
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        out.warn(
            f"Unable to re-apply saved permissions for {remote_abs}: {result.stderr.strip()}"
        )


def _apply_saved_permissions(host: str, mount: ManagedBindMount) -> None:
    state = _load_perms_state(host)
    entries = state.get(mount.remote_abs)
    if not entries:
        return
    _apply_permissions_entries(host, mount.remote_abs, entries)


def _owner_from_entries(entries: list[dict[str, str]]) -> tuple[str, str] | None:
    for entry in entries:
        if entry.get("rel") == "." and entry.get("uid") and entry.get("gid"):
            return entry["uid"], entry["gid"]
    for entry in entries:
        if entry.get("uid") and entry.get("gid"):
            return entry["uid"], entry["gid"]
    return None


def _parent_owner_hint(host: str, mount: ManagedBindMount) -> tuple[str, str] | None:
    probe = str(Path(mount.remote_abs).parent)
    script = """
set -euo pipefail
target="$1"
if [ -e "$target" ]; then
  stat -c '%u\t%g' "$target"
fi
""".strip()
    result = subprocess.run(
        ["ssh", *SSH_OPTS, host, "bash", "-s", "--", probe],
        input=script,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        return None
    parts = result.stdout.strip().split("\t")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None
    return parts[0], parts[1]


def _owner_hint_for_mount(
    host: str,
    mount: ManagedBindMount,
    pre_push_entries: list[dict[str, str]] | None,
) -> tuple[str, str] | None:
    if pre_push_entries:
        hint = _owner_from_entries(pre_push_entries)
        if hint:
            return hint

    state = _load_perms_state(host)
    saved = state.get(mount.remote_abs)
    if saved:
        hint = _owner_from_entries(saved)
        if hint:
            return hint

    return _parent_owner_hint(host, mount)


def _changed_remote_paths(
    mount: ManagedBindMount,
    changes: list[RsyncChange],
) -> list[str]:
    if mount.file_like:
        return [mount.remote_abs]

    paths: list[str] = []
    seen: set[str] = set()
    for change in changes:
        if change.action == "delete":
            continue
        path = _mount_path(mount.remote_abs, change.path, file_like=False)
        if path in seen:
            continue
        seen.add(path)
        paths.append(path)
    return paths


def _apply_owner_hint_to_paths(
    host: str,
    paths: list[str],
    uid: str,
    gid: str,
) -> None:
    if not paths:
        return

    script = """
set -euo pipefail
uid="$1"
gid="$2"
shift 2
for path in "$@"; do
  [ -e "$path" ] || continue
  [ -L "$path" ] && continue
  chown "$uid:$gid" "$path" 2>/dev/null || true
done
""".strip()
    result = subprocess.run(
        ["ssh", *SSH_OPTS, host, "bash", "-s", "--", uid, gid, *paths],
        input=script,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        out.warn(
            f"Unable to normalize ownership for updated paths: {result.stderr.strip()}"
        )


def _env_value(env: object, key: str) -> str | None:
    if isinstance(env, dict):
        value = env.get(key)
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    if isinstance(env, list):
        prefix = f"{key}="
        for item in env:
            if not isinstance(item, str):
                continue
            if not item.startswith(prefix):
                continue
            value = item[len(prefix) :].strip()
            return value or None

    return None


def _numeric_id(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip()
    if text.isdigit():
        return text
    return None


def _numeric_uid_gid_from_user(value: object) -> tuple[str, str] | None:
    if value is None:
        return None

    text = str(value).strip()
    if not text or ":" not in text:
        return None

    uid, gid = text.split(":", 1)
    uid = uid.strip()
    gid = gid.strip()
    if uid.isdigit() and gid.isdigit():
        return uid, gid
    return None


def _compose_service_uid_gid_map(host: str) -> dict[str, tuple[str, str]]:
    cfg = _load_rendered_compose_config(host)
    services = cfg.get("services", {})
    if not isinstance(services, dict):
        return {}

    resolved: dict[str, tuple[str, str]] = {}
    for compose_service, svc_def in services.items():
        if not isinstance(svc_def, dict):
            continue

        # Prefer explicit runtime user when available.
        user_uid_gid = _numeric_uid_gid_from_user(svc_def.get("user"))
        if user_uid_gid:
            resolved[str(compose_service)] = user_uid_gid
            continue

        env = svc_def.get("environment")
        puid = _numeric_id(_env_value(env, "PUID"))
        pgid = _numeric_id(_env_value(env, "PGID"))
        if puid and pgid:
            resolved[str(compose_service)] = (puid, pgid)
    return resolved


def _is_config_like_mount(mount: ManagedBindMount) -> bool:
    target = mount.container_target.rstrip("/")
    if target == "/config" or target.startswith("/config/"):
        return True

    rel = mount.remote_rel.strip("/")
    return rel.endswith("/config") or "/config/" in rel


def _apply_recursive_owner(host: str, remote_abs: str, uid: str, gid: str) -> bool:
    script = """
set -euo pipefail
uid="$1"
gid="$2"
path="$3"

[ -e "$path" ] || exit 0
[ -L "$path" ] && exit 0

failed=0

if ! chown "$uid:$gid" "$path" 2>/dev/null; then
    failed=1
fi

if [ -d "$path" ]; then
    while IFS= read -r entry; do
        if ! chown "$uid:$gid" "$entry" 2>/dev/null; then
            failed=1
        fi
    done < <(find "$path" -mindepth 1 -not -type l -print)
fi

exit "$failed"
""".strip()
    result = subprocess.run(
        ["ssh", *SSH_OPTS, host, "bash", "-s", "--", uid, gid, remote_abs],
        input=script,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        out.warn(
            f"Unable to normalize ownership for {remote_abs}: {result.stderr.strip()}"
        )
        return False
    return True


def _normalize_mount_owner_from_compose(
    host: str,
    mount: ManagedBindMount,
    uid_gid_map: dict[str, tuple[str, str]],
) -> bool:
    if not _is_config_like_mount(mount):
        return False

    uid_gid = uid_gid_map.get(mount.compose_service)
    if not uid_gid:
        return False

    return _apply_recursive_owner(host, mount.remote_abs, uid_gid[0], uid_gid[1])


def _rsync_src(path: Path, file_like: bool) -> str:
    return str(path) if file_like else _rsync_source_dir(path)


def _rsync_push_src(path: Path, file_like: bool) -> str:
    """Build rsync source for push operations.

    For file-like mounts, follow local symlinks so we compare and transfer the
    target file content instead of syncing symlink metadata.
    """
    if not file_like:
        return _rsync_source_dir(path)

    if path.is_symlink():
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            return str(path)
        if resolved.is_file():
            return str(resolved)

    return str(path)


def _rsync_dst(host: str, remote_abs: str, file_like: bool) -> str:
    if file_like:
        return f"{host}:{remote_abs}"
    remote_dir = remote_abs if remote_abs.endswith("/") else f"{remote_abs}/"
    return f"{host}:{remote_dir}"


def _ensure_remote_bind_targets(host: str, mounts: list[ManagedBindMount]) -> None:
    dirs = sorted(
        {
            str(Path(mount.remote_abs).parent) if mount.file_like else mount.remote_abs
            for mount in mounts
        }
    )
    if not dirs:
        return

    script = """
set -euo pipefail
for d in "$@"; do
  mkdir -p "$d"
done
""".strip()
    subprocess.run(
        ["ssh", *SSH_OPTS, host, "bash", "-s", "--", *dirs],
        input=script,
        text=True,
        check=True,
    )


def _safe_diff_stub(remote_path: str) -> str:
    stem = PurePosixPath(remote_path).name or "root"
    return stem.replace("/", "_").replace(" ", "_")


def _read_remote_file_for_diff(host: str, remote_path: str) -> tuple[bytes, bool]:
    path_q = shlex.quote(remote_path)
    script = (
        "set -euo pipefail; "
        f"p={path_q}; "
        'if [ -f "$p" ]; then cat -- "$p"; exit 0; fi; '
        'if [ -e "$p" ]; then exit 3; fi; '
        "exit 4"
    )
    result = subprocess.run(
        ["ssh", *SSH_OPTS, host, "bash", "-lc", script],
        capture_output=True,
    )
    if result.returncode == 0:
        return result.stdout, True
    if result.returncode == 4:
        return b"", False

    message = result.stderr.decode("utf-8", errors="replace").strip() or "unknown error"
    out.warn(f"Unable to read remote file {host}:{remote_path} ({message}).")
    return b"", False


def _open_vimdiff(host: str, local_path: Path, remote_path: str) -> None:
    tmp_dir = REPO_ROOT / "tmp" / ".tctl" / "diff"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    remote_bytes, remote_exists = _read_remote_file_for_diff(host, remote_path)
    remote_stub = _safe_diff_stub(remote_path)
    remote_tmp = tmp_dir / f"remote_{host}_{remote_stub}"
    remote_tmp.write_bytes(remote_bytes)

    if local_path.is_file():
        left_path = local_path
    else:
        left_path = tmp_dir / f"local_missing_{remote_stub}"
        left_path.write_text("\n")

    if not remote_exists:
        out.meta(
            f"Remote file missing: {host}:{remote_path} (diffing against empty file)."
        )

    if not local_path.is_file():
        out.meta(f"Local file missing: {local_path} (diffing against empty file).")

    vimdiff_cmd = shutil.which("vimdiff")
    if vimdiff_cmd:
        cmd = [vimdiff_cmd, "-R", str(left_path), str(remote_tmp)]
    else:
        vim_cmd = shutil.which("vim")
        if not vim_cmd:
            out.warn("Neither vimdiff nor vim is available in PATH.")
            return
        cmd = [vim_cmd, "-d", "-R", str(left_path), str(remote_tmp)]

    subprocess.run(cmd)


def _collect_push_diff_targets(
    shared_diffs: list[tuple[ManagedBindMount, Path, list[str], list[RsyncChange]]],
    stack_diffs: list[tuple[ManagedBindMount, Path, list[str], list[RsyncChange]]],
) -> list[tuple[str, Path, str]]:
    targets: list[tuple[str, Path, str]] = []
    seen: set[tuple[str, str]] = set()

    combined = [*shared_diffs, *stack_diffs]
    for mount, local_base, _filter_args, changes in combined:
        for change in changes:
            if change.type_label != "file":
                continue

            local_abs = Path(_mount_path(local_base, change.path, mount.file_like))
            remote_abs = _mount_path(mount.remote_abs, change.path, mount.file_like)
            dedupe = (str(local_abs), remote_abs)
            if dedupe in seen:
                continue
            seen.add(dedupe)

            label = f"{change.action}: {change.path}"
            targets.append((label, local_abs, remote_abs))

    return targets


def _preview_push_diffs(
    host: str,
    targets: list[tuple[str, Path, str]],
) -> None:
    if not out.console.is_interactive:
        out.warn("Diff preview requires an interactive terminal.")
        return

    if not targets:
        out.warn("No file-content diffs available to open in vimdiff.")
        return

    out.phase("Diff files available for vimdiff:")
    for idx, (label, local_abs, remote_abs) in enumerate(targets[:20], start=1):
        out.info(f"{idx:>2}. {label} | {local_abs} -> {host}:{remote_abs}")

    if len(targets) > 20:
        out.info(f"... and {len(targets) - 20} more file(s).")

    while True:
        choice = (
            out.prompt(
                f"Open vimdiff for file [1-{len(targets)}], [a]ll, or [q]uit preview: "
            )
            .strip()
            .lower()
        )
        if not choice or choice in {"q", "quit"}:
            return
        if choice in {"a", "all"}:
            for _, local_abs, remote_abs in targets:
                _open_vimdiff(host, local_abs, remote_abs)
            return
        if choice.isdigit():
            idx = int(choice)
            if 1 <= idx <= len(targets):
                _, local_abs, remote_abs = targets[idx - 1]
                _open_vimdiff(host, local_abs, remote_abs)
                continue

        out.warn("Please enter a valid index, a/all, or q/quit.")


def _confirm_push_with_diff(
    host: str,
    service: str,
    summary: str,
    shared_diffs: list[tuple[ManagedBindMount, Path, list[str], list[RsyncChange]]],
    stack_diffs: list[tuple[ManagedBindMount, Path, list[str], list[RsyncChange]]],
) -> Literal["approve", "skip", "ignore"]:
    targets = _collect_push_diff_targets(shared_diffs, stack_diffs)

    while True:
        answer = (
            out.prompt(f"Push {service} data? ({summary}) [y/N/i/d] ").strip().lower()
        )
        if not answer or answer in {"n", "no"}:
            return "skip"
        if answer in {"y", "yes"}:
            return "approve"
        if answer in {"d", "diff"}:
            _preview_push_diffs(host, targets)
            continue
        if answer in {"i", "ignore"}:
            while True:
                confirm_ignore = (
                    out.prompt(
                        f"Ignore {service} data for future syncs by creating .tignore? [y/N] "
                    )
                    .strip()
                    .lower()
                )
                if not confirm_ignore or confirm_ignore in {"n", "no"}:
                    return "skip"
                if confirm_ignore in {"y", "yes"}:
                    return "ignore"
                out.warn("Please answer y/yes or n/no.")

        out.warn("Please answer y/yes, n/no, i/ignore, or d/diff.")


# ── Push ─────────────────────────────────────────────────────────────


def push(
    host: str,
    service: str | None = None,
    *,
    yes: bool = False,
    no: bool = False,
    dry_run: bool = False,
    pushed_services: set[str] | None = None,
) -> bool:
    """Push local service files to a remote host.

    Returns True when one or more diffs were detected.
    """
    svc_dir = stack_services_dir(host)
    has_diff = False
    extension_policy = _load_extension_policy()

    if not svc_dir.is_dir():
        raise SystemExit(
            f"No services directory at {svc_dir.relative_to(svc_dir.parents[3])}/"
        )

    services = _resolve_requested_owner_services(host, service)
    if not services:
        raise SystemExit("No services found.")

    mounts, mount_warnings = _collect_managed_bind_mounts(host, services)
    for warning in mount_warnings:
        out.warn(warning)
    compose_uid_gid = _compose_service_uid_gid_map(host)

    _print_change_key()

    if yes and no:
        raise SystemExit("Use either --yes or --no, not both.")

    if yes:
        out.meta("Auto-approving push confirmations (--yes).")
    if no:
        out.meta("Auto-skipping push confirmations (--no).")
    if dry_run:
        out.meta("Diff mode only; no files will be pushed.")

    for svc in services:
        out.header(f"{svc}: push preview ({host})")
        service_mounts = [mount for mount in mounts if mount.owner_service == svc]
        if not service_mounts:
            out.warn("No STACK_DATA_ROOT bind mounts found.")
            continue

        if _service_root_tignore_exists(svc):
            out.warn(f"Skipping {svc} (.tignore present in service root)")
            continue

        shared_diffs: list[
            tuple[ManagedBindMount, Path, list[str], list[RsyncChange]]
        ] = []
        stack_diffs: list[
            tuple[ManagedBindMount, Path, list[str], list[RsyncChange]]
        ] = []
        ignored_mtime_only = 0
        ignored_state_only = 0
        manual_review_changes = 0
        candidate_mounts: list[ManagedBindMount] = []
        candidate_mount_keys: set[tuple[str, str]] = set()

        for mount in service_mounts:
            shared_local = _local_mount_path(
                SERVICES_DIR,
                svc,
                mount.remote_rel,
                mount.container_target,
            )
            stack_local = _local_mount_path(
                svc_dir,
                svc,
                mount.remote_rel,
                mount.container_target,
            )

            shared_skip, shared_patterns = _mount_tignore_rules(
                shared_local,
                file_like=mount.file_like,
            )
            stack_skip, stack_patterns = _mount_tignore_rules(
                stack_local,
                file_like=mount.file_like,
            )

            shared_exists = _mount_local_path_exists(shared_local, mount.file_like)
            stack_exists = _mount_local_path_exists(stack_local, mount.file_like)

            has_shared = shared_exists and _has_pushable_path(
                shared_local, mount.file_like
            )
            has_stack = stack_exists and _has_pushable_path(
                stack_local, mount.file_like
            )

            if shared_skip:
                has_shared = False
            if stack_skip:
                has_stack = False

            if (shared_skip or stack_skip) and not has_shared and not has_stack:
                out.warn(f"Skipping {mount.remote_rel} (excluded by .tignore rules)")
                continue

            if not shared_exists and not stack_exists:
                out.warn(
                    f"Missing local path for mount {mount.remote_abs} -> {mount.container_target}"
                )
                continue

            mount_key = (mount.owner_service, mount.remote_abs)
            if mount_key not in candidate_mount_keys:
                candidate_mount_keys.add(mount_key)
                candidate_mounts.append(mount)

            dst = _rsync_dst(host, mount.remote_abs, mount.file_like)

            if has_shared:
                shared_filter_args_base = (
                    _rsync_filter_args_from_tignore(shared_patterns)
                    if not mount.file_like
                    else []
                )
                shared_filter_args = _with_state_filters(
                    shared_filter_args_base, extension_policy
                )
                shared_changes_raw = _parse_rsync_itemize(
                    _rsync_diff(
                        _rsync_push_src(shared_local, mount.file_like),
                        dst,
                        filter_args=shared_filter_args,
                        copy_unsafe_links=not mount.file_like,
                    )
                )
                shared_changes, ignored = _partition_actionable_changes(
                    shared_changes_raw
                )
                ignored_mtime_only += ignored
                shared_changes, ignored_state, manual_review = (
                    _partition_policy_changes(
                        shared_changes,
                        extension_policy,
                    )
                )
                ignored_state_only += ignored_state
                manual_review_changes += manual_review
                if shared_changes:
                    shared_diffs.append(
                        (mount, shared_local, shared_filter_args, shared_changes)
                    )

            if has_stack:
                stack_filter_args_base = (
                    _rsync_filter_args_from_tignore(stack_patterns)
                    if not mount.file_like
                    else []
                )
                stack_filter_args = _with_state_filters(
                    stack_filter_args_base, extension_policy
                )
                stack_changes_raw = _parse_rsync_itemize(
                    _rsync_diff(
                        _rsync_push_src(stack_local, mount.file_like),
                        dst,
                        filter_args=stack_filter_args,
                        copy_unsafe_links=not mount.file_like,
                    )
                )
                stack_changes, ignored = _partition_actionable_changes(
                    stack_changes_raw
                )
                ignored_mtime_only += ignored
                stack_changes, ignored_state, manual_review = _partition_policy_changes(
                    stack_changes,
                    extension_policy,
                )
                ignored_state_only += ignored_state
                manual_review_changes += manual_review
                if stack_changes:
                    stack_diffs.append(
                        (mount, stack_local, stack_filter_args, stack_changes)
                    )

        all_changes = [
            *[change for _, _, _, changes in shared_diffs for change in changes],
            *[change for _, _, _, changes in stack_diffs for change in changes],
        ]

        if not all_changes:
            if ignored_mtime_only:
                out.info(
                    "Ignoring "
                    f"{ignored_mtime_only} mtime-only metadata change"
                    f"{'s' if ignored_mtime_only != 1 else ''}."
                )

            if ignored_state_only:
                out.info(
                    "Ignoring "
                    f"{ignored_state_only} persistent/state change"
                    f"{'s' if ignored_state_only != 1 else ''}."
                )

            if dry_run:
                out.info("Already in sync.")
                continue

            _ensure_remote_bind_targets(host, candidate_mounts)

            normalized = 0
            for mount in candidate_mounts:
                if _normalize_mount_owner_from_compose(host, mount, compose_uid_gid):
                    normalized += 1
            if normalized:
                out.meta(
                    f"Normalized ownership from compose user/PUID/PGID on {normalized} config mount(s)."
                )
            out.info("Already in sync.")
            continue

        for mount, local_path, _filter_args, changes in shared_diffs:
            out.phase(f"Shared local → remote ({mount.remote_abs})")
            _show_changes(
                host=host,
                owner_service=svc,
                changes=changes,
                local_base=local_path,
                remote_base=mount.remote_abs,
                file_like=mount.file_like,
                direction="push",
            )

        for mount, local_path, _filter_args, changes in stack_diffs:
            out.phase(f"Stack overlay local → remote ({mount.remote_abs})")
            _show_changes(
                host=host,
                owner_service=svc,
                changes=changes,
                local_base=local_path,
                remote_base=mount.remote_abs,
                file_like=mount.file_like,
                direction="push",
            )

        summary = _summarize_changes(all_changes)
        has_diff = True
        out.meta(f"Diff summary: {summary}")
        if ignored_state_only:
            out.info(
                "Ignoring "
                f"{ignored_state_only} persistent/state change"
                f"{'s' if ignored_state_only != 1 else ''}."
            )

        config_only = manual_review_changes == 0

        if dry_run:
            continue

        if config_only and not no:
            out.meta(f"Config-only diff detected for {svc}; auto-approving push.")
            decision = "approve"
        elif yes:
            out.warn(
                f"Diff detected for {svc}; auto-approving push. "
                f"Review with: tctl -s {host} diff {svc}"
            )
            decision = "approve"
        elif no:
            decision = "skip"
        else:
            decision = _confirm_push_with_diff(
                host,
                svc,
                summary,
                shared_diffs,
                stack_diffs,
            )
        if decision == "ignore":
            tignore_path, existed = _touch_service_root_tignore(svc)
            if existed:
                out.warn(
                    f"{svc}: .tignore already exists at {tignore_path} (future syncs skipped)."
                )
            else:
                out.warn(f"{svc}: Created {tignore_path} (future syncs skipped).")
            out.warn("Skipped.")
            continue

        if decision != "approve":
            out.warn("Skipped.")
            continue

        _ensure_remote_bind_targets(
            host,
            [
                *{mount for mount, _, _, _ in shared_diffs},
                *{mount for mount, _, _, _ in stack_diffs},
            ],
        )

        affected_mounts = [
            *{mount for mount, _, _, _ in shared_diffs},
            *{mount for mount, _, _, _ in stack_diffs},
        ]
        pre_push_permissions = _snapshot_remote_permissions_for_mounts(
            host, affected_mounts
        )

        for mount, local_path, filter_args, changes in shared_diffs:
            exec_filter_args = _with_state_filters(filter_args, extension_policy)
            _rsync_exec(
                _rsync_push_src(local_path, mount.file_like),
                _rsync_dst(host, mount.remote_abs, mount.file_like),
                filter_args=exec_filter_args,
                copy_unsafe_links=not mount.file_like,
            )
            entries = pre_push_permissions.get(mount.remote_abs)
            if entries:
                _apply_permissions_entries(host, mount.remote_abs, entries)
            else:
                _apply_saved_permissions(host, mount)

            owner_hint = _owner_hint_for_mount(host, mount, entries)
            if owner_hint:
                changed_paths = _changed_remote_paths(mount, changes)
                _apply_owner_hint_to_paths(
                    host,
                    changed_paths,
                    owner_hint[0],
                    owner_hint[1],
                )

        for mount, local_path, filter_args, changes in stack_diffs:
            exec_filter_args = _with_state_filters(filter_args, extension_policy)
            _rsync_exec(
                _rsync_push_src(local_path, mount.file_like),
                _rsync_dst(host, mount.remote_abs, mount.file_like),
                filter_args=exec_filter_args,
                copy_unsafe_links=not mount.file_like,
            )
            entries = pre_push_permissions.get(mount.remote_abs)
            if entries:
                _apply_permissions_entries(host, mount.remote_abs, entries)
            else:
                _apply_saved_permissions(host, mount)

            owner_hint = _owner_hint_for_mount(host, mount, entries)
            if owner_hint:
                changed_paths = _changed_remote_paths(mount, changes)
                _apply_owner_hint_to_paths(
                    host,
                    changed_paths,
                    owner_hint[0],
                    owner_hint[1],
                )

        normalized = 0
        for mount in affected_mounts:
            if _normalize_mount_owner_from_compose(host, mount, compose_uid_gid):
                normalized += 1
        if normalized:
            out.meta(
                f"Normalized ownership from compose user/PUID/PGID on {normalized} config mount(s)."
            )

        if pushed_services is not None:
            pushed_services.add(svc)

        out.ok("Pushed.")

    return has_diff


# ── Pull ─────────────────────────────────────────────────────────────


def pull(host: str, service: str | None = None, *, yes: bool = False) -> None:
    """Pull remote service files to local copy."""
    svc_dir = stack_services_dir(host)

    services = _resolve_requested_owner_services(host, service)

    if not services:
        raise SystemExit("No services found.")

    mounts, mount_warnings = _collect_managed_bind_mounts(host, services)
    for warning in mount_warnings:
        out.warn(warning)

    if yes:
        out.meta("Auto-approving pull confirmations (--yes).")

    _print_change_key()

    for svc in services:
        out.header(f"{svc}: pull preview ({host})")
        service_mounts = [mount for mount in mounts if mount.owner_service == svc]
        if not service_mounts:
            out.warn("No STACK_DATA_ROOT bind mounts found.")
            continue

        if _service_root_tignore_exists(svc):
            out.warn(f"Skipping {svc} (.tignore present in service root)")
            continue

        stack_service_root = svc_dir / svc
        prefer_stack_overlay = stack_service_root.is_dir()
        ignored_mtime_only = 0

        pull_diffs: list[
            tuple[ManagedBindMount, Path, str, list[str], list[RsyncChange]]
        ] = []

        for mount in service_mounts:
            shared_local = _local_mount_path(
                SERVICES_DIR,
                svc,
                mount.remote_rel,
                mount.container_target,
            )
            stack_local = _local_mount_path(
                svc_dir,
                svc,
                mount.remote_rel,
                mount.container_target,
            )

            if prefer_stack_overlay:
                target = stack_local
                scope = "stack overlay"
            else:
                target = shared_local
                scope = "shared service"

            mount_skip, mount_patterns = _mount_tignore_rules(
                target,
                file_like=mount.file_like,
            )
            if mount_skip:
                out.warn(f"Skipping {mount.remote_rel} (excluded by .tignore rules)")
                continue

            if mount.file_like:
                target.parent.mkdir(parents=True, exist_ok=True)
            else:
                target.mkdir(parents=True, exist_ok=True)

            src = _rsync_dst(host, mount.remote_abs, mount.file_like)
            dst = _rsync_src(target, mount.file_like)
            filter_args = (
                _rsync_filter_args_from_tignore(mount_patterns)
                if not mount.file_like
                else []
            )
            changes_raw = _parse_rsync_itemize(
                _rsync_diff(src, dst, filter_args=filter_args)
            )
            changes, ignored = _partition_actionable_changes(changes_raw)
            ignored_mtime_only += ignored
            if changes:
                pull_diffs.append((mount, target, scope, filter_args, changes))

        if not pull_diffs:
            if ignored_mtime_only:
                out.info(
                    "Ignoring "
                    f"{ignored_mtime_only} mtime-only metadata change"
                    f"{'s' if ignored_mtime_only != 1 else ''}."
                )
            out.info("Already in sync.")
            continue

        for mount, target, scope, _filter_args, changes in pull_diffs:
            out.phase(f"Remote → {scope} local ({mount.remote_abs})")
            _show_changes(
                host=host,
                owner_service=svc,
                changes=changes,
                local_base=target,
                remote_base=mount.remote_abs,
                file_like=mount.file_like,
                direction="pull",
            )

        if not yes:
            decision = out.confirm_or_ignore_service(
                f"Pull {svc} data?",
                service=svc,
            )
            if decision == "ignore":
                tignore_path, existed = _touch_service_root_tignore(svc)
                if existed:
                    out.warn(
                        f"{svc}: .tignore already exists at {tignore_path} (future syncs skipped)."
                    )
                else:
                    out.warn(f"{svc}: Created {tignore_path} (future syncs skipped).")
                out.warn(f"{svc}: Skipped.")
                continue

            if decision != "approve":
                out.warn(f"{svc}: Skipped.")
                continue

        for mount, target, scope, filter_args, _ in pull_diffs:
            _record_remote_permissions(host, mount)
            src = _rsync_dst(host, mount.remote_abs, mount.file_like)
            dst = _rsync_src(target, mount.file_like)
            _rsync_exec(src, dst, filter_args=filter_args)
            out.ok(f"Pulled {mount.remote_rel} into {scope} local path.")
