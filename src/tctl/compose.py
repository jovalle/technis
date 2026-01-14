"""Docker Compose operations: service listing, dependency closure, reconcile, passthrough."""

from __future__ import annotations

import hashlib
import json
import queue
import re
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

from tctl import cache
from tctl import output as out
from tctl.compose_args import compose_subcommand
from tctl.config import (
    SSH_OPTS,
    STACKS_DIR,
    available_hosts,
    base_dir,
    stack_compose,
    stack_env_file,
    subprocess_env,
)

# ── Service listing ──────────────────────────────────────────────────

_INCLUDE_SVC_RE = re.compile(r"(?:\.\./\.\./services|services)/([^/]+)/compose\.ya?ml")


@dataclass(frozen=True)
class _IncludeEntry:
    paths: tuple[str, ...]


@dataclass(frozen=True)
class _BindMountRef:
    service: str
    source: str
    target: str


def _repo_display(host: str, include_path: str) -> str:
    if include_path.startswith("../../services/"):
        return f"docker/services/{include_path.removeprefix('../../services/')}"
    if include_path.startswith("services/"):
        return f"docker/stacks/{host}/{include_path}"
    return include_path


def _service_name(include_path: str) -> str:
    path = Path(include_path)
    if len(path.parts) >= 2:
        return path.parts[-2]
    return include_path


def _parse_include_section(lines: list[str]) -> tuple[list[_IncludeEntry], int, int]:
    include_index = 0
    while include_index < len(lines):
        stripped = lines[include_index].strip()
        if not stripped or stripped.startswith("#"):
            include_index += 1
            continue
        break

    if include_index >= len(lines) or lines[include_index].rstrip("\n") != "include:":
        raise ValueError("stack compose must contain a top-level include section")

    entries: list[_IncludeEntry] = []
    index = include_index + 1
    while index < len(lines):
        line = lines[index].rstrip("\n")
        if not line:
            index += 1
            continue
        if not line.startswith(" "):
            break
        if not line.startswith("  - path:"):
            raise ValueError(f"unexpected include entry: {line}")

        remainder = line.removeprefix("  - path:").strip()
        if remainder:
            entries.append(_IncludeEntry(paths=(remainder,)))
            index += 1
            continue

        index += 1
        child_paths: list[str] = []
        while index < len(lines):
            child = lines[index].rstrip("\n")
            if child.startswith("      - "):
                child_paths.append(child.removeprefix("      - ").strip())
                index += 1
                continue
            break

        if not child_paths:
            raise ValueError("include block path entry had no child paths")
        entries.append(_IncludeEntry(paths=tuple(child_paths)))

    return entries, include_index, index


def _render_include_section(entries: list[_IncludeEntry]) -> list[str]:
    rendered = ["include:\n"]
    for entry in entries:
        if len(entry.paths) == 1:
            rendered.append(f"  - path: {entry.paths[0]}\n")
            continue
        rendered.append("  - path:\n")
        for include_path in entry.paths:
            rendered.append(f"      - {include_path}\n")
    return rendered


def _reconcile_include_entries(
    host: str, stack_compose_path: Path, entries: list[_IncludeEntry]
) -> tuple[list[_IncludeEntry], list[str]]:
    stack_dir = stack_compose_path.parent
    kept_entries: list[_IncludeEntry] = []
    changes: list[str] = []

    for entry in entries:
        existing = [path for path in entry.paths if (stack_dir / path).is_file()]
        missing = [path for path in entry.paths if path not in existing]
        if not missing:
            kept_entries.append(entry)
            continue

        had_shared = any(path.startswith("../../services/") for path in entry.paths)
        shared_remaining = any(path.startswith("../../services/") for path in existing)

        if not existing or (had_shared and not shared_remaining):
            svc = _service_name(entry.paths[0])
            changes.append(f"Removed stale include entry for '{svc}'.")
            for include_path in entry.paths:
                changes.append(f"  - {_repo_display(host, include_path)}")
            continue

        kept_entries.append(_IncludeEntry(paths=tuple(existing)))
        for include_path in missing:
            changes.append(
                f"Removed missing include path: {_repo_display(host, include_path)}"
            )

    return kept_entries, changes


def stack_services(host: str) -> list[str]:
    """List service names included in a stack compose file (cached)."""
    compose = stack_compose(host)
    if not compose.is_file():
        raise SystemExit(f"No stack compose file at {compose}")

    key = cache.cache_key("stack_services", compose)
    cached = cache.get(key)
    if cached is not None:
        return cached

    services: list[str] = []
    for match in _INCLUDE_SVC_RE.finditer(compose.read_text()):
        name = match.group(1)
        if name not in services:
            services.append(name)

    services.sort()
    cache.put(key, services)
    return services


def service_stacks(service: str) -> list[str]:
    """Return hosts whose stack includes the given service."""
    hosts = []
    for host_dir in sorted(STACKS_DIR.iterdir()):
        if not host_dir.is_dir():
            continue
        if service in stack_services(host_dir.name):
            hosts.append(host_dir.name)
    if not hosts:
        raise SystemExit(f"No stacks include service: {service}")
    return hosts


# ── Dependency closure ───────────────────────────────────────────────


def _parse_compose_config(host: str) -> dict:
    """Run `docker compose config` and return parsed YAML."""
    compose = stack_compose(host)
    env = subprocess_env(host)

    for extra in (["--no-interpolate"], []):
        result = subprocess.run(
            ["docker", "compose", "-f", str(compose), "config"] + extra,
            capture_output=True,
            text=True,
            env=env,
        )
        if result.returncode == 0:
            return yaml.safe_load(result.stdout) or {}
    raise SystemExit(f"Unable to render compose config for '{host}'.\n{result.stderr}")


def _parse_compose_config_interpolated(host: str) -> dict:
    """Run `docker compose config` with interpolation enabled and return parsed YAML."""
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
            f"Unable to render interpolated compose config for '{host}'.\n{result.stderr}"
        )
    return yaml.safe_load(result.stdout) or {}


_SEMANTIC_COMPOSE_KEYS = (
    "name",
    "services",
    "networks",
    "volumes",
    "secrets",
    "configs",
)


def _render_compose_text(host: str) -> str:
    compose = stack_compose(host)
    env = subprocess_env(host)
    rendered = subprocess.run(
        ["docker", "compose", "-f", str(compose), "config"],
        capture_output=True,
        text=True,
        env=env,
    )
    if rendered.returncode != 0:
        raise SystemExit(
            f"Unable to render final compose config for '{host}'.\n{rendered.stderr.strip()}"
        )
    return rendered.stdout


def _semantic_compose_digest(text: str, *, source: str) -> str:
    try:
        parsed = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise SystemExit(f"Unable to parse compose YAML from {source}.\n{exc}") from exc

    if not isinstance(parsed, dict):
        raise SystemExit(
            f"Unexpected compose document type from {source}; expected mapping."
        )

    semantic = {key: parsed.get(key) for key in _SEMANTIC_COMPOSE_KEYS if key in parsed}
    payload = json.dumps(
        semantic, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _read_remote_compose_text(host: str, remote_compose: str) -> str:
    read_cmd = f"cat {shlex.quote(remote_compose)}"
    try:
        read_result = subprocess.run(
            ["ssh", *SSH_OPTS, host, "bash", "-lc", shlex.quote(read_cmd)],
            text=True,
            capture_output=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        raise SystemExit(
            f"Timed out reading remote compose from {host}:{remote_compose}"
        )
    if read_result.returncode != 0:
        raise SystemExit(
            "Unable to read remote compose from stack root.\n"
            f"Host: {host}\n"
            f"Path: {remote_compose}\n"
            f"{read_result.stderr.strip()}"
        )
    return read_result.stdout


def _remote_compose_matches_rendered(
    host: str,
    remote_compose: str,
    rendered_text: str,
) -> tuple[bool, str, str]:
    local_digest = _semantic_compose_digest(
        rendered_text, source=f"local render for '{host}'"
    )
    remote_text = _read_remote_compose_text(host, remote_compose)
    remote_digest = _semantic_compose_digest(
        remote_text,
        source=f"{host}:{remote_compose}",
    )
    return local_digest == remote_digest, local_digest, remote_digest


def compose_drift(host: str) -> tuple[bool, str, str, str]:
    """Compare local rendered compose with remote stack-root compose.

    Returns (has_drift, remote_compose_path, local_digest, remote_digest).
    """
    rendered_text = _render_compose_text(host)
    remote_root = base_dir(host).rstrip("/") or "/"
    remote_compose = (
        f"{remote_root}/compose.yaml" if remote_root != "/" else "/compose.yaml"
    )
    matches, local_digest, remote_digest = _remote_compose_matches_rendered(
        host,
        remote_compose,
        rendered_text,
    )
    return (not matches), remote_compose, local_digest, remote_digest


def write_rendered_compose_to_stack_root(host: str) -> str:
    """Render final compose config and write it to the remote stack data root as compose.yaml."""
    out.info("Rendering compose config...")
    rendered_text = _render_compose_text(host)

    remote_root = base_dir(host).rstrip("/") or "/"
    remote_compose = (
        f"{remote_root}/compose.yaml" if remote_root != "/" else "/compose.yaml"
    )
    remote_compose_q = shlex.quote(remote_compose)

    write_cmd = (
        f"set -euo pipefail; "
        f"dest={remote_compose_q}; "
        f'tmp="${{dest}}.tctl.tmp"; '
        f'mkdir -p "$(dirname "${{dest}}")"; '
        f'cat > "${{tmp}}"; '
        f'mv "${{tmp}}" "${{dest}}"; '
        f'sha256sum "${{dest}}" | cut -d" " -f1'
    )
    out.info(f"Writing to {host}:{remote_compose}...")
    try:
        write_result = subprocess.run(
            ["ssh", *SSH_OPTS, host, "bash", "-lc", shlex.quote(write_cmd)],
            input=rendered_text,
            text=True,
            capture_output=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        raise SystemExit(
            f"Timed out writing rendered compose to {host}:{remote_compose}"
        )
    if write_result.returncode != 0:
        raise SystemExit(
            "Unable to write rendered compose to remote stack root.\n"
            f"Host: {host}\n"
            f"Path: {remote_compose}\n"
            f"{write_result.stderr.strip()}"
        )

    remote_digest = write_result.stdout.strip()
    local_digest = hashlib.sha256(rendered_text.encode("utf-8")).hexdigest()
    if remote_digest != local_digest:
        raise SystemExit(
            "Rendered compose write verification failed.\n"
            f"Host: {host}\n"
            f"Path: {remote_compose}\n"
            f"local_digest={local_digest}\n"
            f"remote_digest={remote_digest}"
        )

    return remote_compose


def service_closure(host: str, service: str) -> list[str]:
    """Resolve transitive depends_on for a service. Returns topological order."""
    compose = stack_compose(host)
    env_file = stack_env_file(host)
    key = cache.cache_key("closure", compose, env_file)
    full_graph = cache.get(key)

    if full_graph is None:
        cfg = _parse_compose_config(host)
        svcs = cfg.get("services", {})
        full_graph = {}
        for svc_name, svc_def in svcs.items():
            deps = svc_def.get("depends_on", {})
            if isinstance(deps, list):
                full_graph[svc_name] = deps
            elif isinstance(deps, dict):
                full_graph[svc_name] = list(deps.keys())
            else:
                full_graph[svc_name] = []
        cache.put(key, full_graph)

    if service not in full_graph:
        available = stack_services(host)
        if service not in available:
            raise SystemExit(f"Service '{service}' is not in stack '{host}'.")
        return [service]

    seen: set[str] = set()
    ordered: list[str] = []

    def _resolve(svc: str) -> None:
        if svc in seen:
            return
        seen.add(svc)
        for dep in full_graph.get(svc, []):
            _resolve(dep)
        ordered.append(svc)

    _resolve(service)
    return ordered


# ── Reconcile ────────────────────────────────────────────────────────


def reconcile(host: str) -> list[str]:
    """Prune stale include entries. Returns list of change descriptions."""
    compose = stack_compose(host)
    if not compose.is_file():
        raise SystemExit(f"No stack compose file at {compose}")

    lines = compose.read_text(encoding="utf-8").splitlines(keepends=True)
    try:
        entries, include_index, remainder_index = _parse_include_section(lines)
    except ValueError as exc:
        raise SystemExit(
            f"Unable to reconcile stack compose for '{host}'.\n{compose}: {exc}"
        )

    reconciled_entries, changes = _reconcile_include_entries(host, compose, entries)
    if not changes:
        return []

    updated = (
        lines[:include_index]
        + _render_include_section(reconciled_entries)
        + lines[remainder_index:]
    )
    compose.write_text("".join(updated), encoding="utf-8")

    # Invalidate cache after reconcile modifies the file.
    cache.invalidate_all()
    return changes


def detect_orphan_containers(host: str) -> list[str]:
    """Detect running containers that are no longer defined in compose.

    Returns list of orphaned container/service names.
    """
    compose = stack_compose(host)
    if not compose.is_file():
        return []

    # Get all running containers with container name and service label
    cmd = _base_cmd(host) + ["ps", "--format", "json"]
    env = subprocess_env(host)

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, env=env, timeout=10
        )
        if result.returncode != 0:
            return []

        containers = json.loads(result.stdout) if result.stdout.strip() else []
    except (json.JSONDecodeError, subprocess.TimeoutExpired):
        return []

    # Get defined services from current compose config
    try:
        config = _parse_compose_config(host)
        defined_services = set(config.get("services", {}).keys())
    except Exception:
        return []

    # Find containers with services not in defined set
    orphans = []
    for container in containers:
        # The Service key in compose ps output corresponds to the service name
        service = container.get("Service", "")
        if service and service not in defined_services:
            orphans.append(service)

    return sorted(set(orphans))


# ── Compose passthrough ──────────────────────────────────────────────


def _base_cmd(host: str) -> list[str]:
    compose = stack_compose(host)
    return ["docker", "--context", host, "compose", "-f", str(compose)]


_PREFLIGHT_SUBCOMMANDS = {"up", "create", "run"}
_LOGS_FLAGS_WITH_VALUE = {"--since", "--until", "--tail", "--index", "-n"}


def _has_follow_flag(args: tuple[str, ...]) -> bool:
    for arg in args:
        if arg in {"-f", "--follow"} or arg.startswith("--follow="):
            return True
    return False


def _logs_service_filters(args: tuple[str, ...], subcommand_index: int) -> list[str]:
    services: list[str] = []
    tokens = args[subcommand_index + 1 :] if subcommand_index >= 0 else ()

    index = 0
    while index < len(tokens):
        token = tokens[index]

        if token == "--":
            services.extend(
                t for t in tokens[index + 1 :] if t and not t.startswith("-")
            )
            break

        if token in _LOGS_FLAGS_WITH_VALUE:
            index += 2
            continue

        if any(
            token.startswith(f"{flag}=")
            for flag in _LOGS_FLAGS_WITH_VALUE
            if flag.startswith("--")
        ):
            index += 1
            continue

        if token.startswith("-n") and token != "-n":
            index += 1
            continue

        if token.startswith("-"):
            index += 1
            continue

        services.append(token)
        index += 1

    return list(dict.fromkeys(services))


def _matching_stacks_for_services(services: list[str]) -> list[str]:
    matches: list[str] = []
    for host in available_hosts():
        host_services = set(stack_services(host))
        if all(service in host_services for service in services):
            matches.append(host)
    return matches


def _selected_services(
    cfg: dict, args: tuple[str, ...], subcommand_index: int
) -> list[str]:
    services = cfg.get("services", {})
    if not isinstance(services, dict):
        return []

    tokens = args[subcommand_index + 1 :] if subcommand_index >= 0 else ()
    explicit = [
        token for token in tokens if not token.startswith("-") and token in services
    ]
    if explicit:
        return list(dict.fromkeys(explicit))
    return sorted(services.keys())


def _split_short_volume_spec(spec: str) -> tuple[str | None, str | None]:
    parts = spec.split(":")
    if len(parts) < 2:
        return None, None
    return parts[0], parts[1]


def _collect_bind_mounts(cfg: dict, services: list[str]) -> list[_BindMountRef]:
    resolved: list[_BindMountRef] = []
    service_defs = cfg.get("services", {})

    for service in services:
        svc_def = service_defs.get(service, {})
        volumes = svc_def.get("volumes", [])
        if not isinstance(volumes, list):
            continue

        for volume in volumes:
            if isinstance(volume, str):
                source, target = _split_short_volume_spec(volume)
                if source and target and source.startswith("/"):
                    resolved.append(
                        _BindMountRef(service=service, source=source, target=target)
                    )
                continue

            if not isinstance(volume, dict):
                continue
            if volume.get("type") != "bind":
                continue

            source = str(volume.get("source") or "")
            target = str(volume.get("target") or "")
            if source.startswith("/") and target:
                resolved.append(
                    _BindMountRef(service=service, source=source, target=target)
                )

    # Preserve first-seen ordering while deduplicating refs.
    return list(dict.fromkeys(resolved))


def _looks_like_file_path(path: str) -> bool:
    return Path(path).suffix != ""


def _remote_path_types(host: str, paths: list[str]) -> dict[str, str]:
    if not paths:
        return {}

    script = """
set -euo pipefail
for p in "$@"; do
  t="missing"
  if [ -d "$p" ]; then
    t="dir"
  elif [ -f "$p" ]; then
    t="file"
  elif [ -S "$p" ]; then
    t="socket"
  elif [ -L "$p" ]; then
    t="symlink"
  elif [ -e "$p" ]; then
    t="other"
  fi
  printf "%s\t%s\n" "$p" "$t"
done
""".strip()

    result = subprocess.run(
        [
            "ssh",
            *SSH_OPTS,
            host,
            "bash",
            "-s",
            "--",
            *paths,
        ],
        input=script,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise SystemExit(
            "Preflight failed: unable to inspect remote bind paths over SSH.\n"
            f"{result.stderr.strip()}"
        )

    types: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "\t" not in line:
            continue
        path, kind = line.split("\t", 1)
        types[path] = kind
    return types


def _run_bind_mount_preflight(host: str, args: tuple[str, ...]) -> None:
    subcommand, subcommand_index = compose_subcommand(args)
    if subcommand not in _PREFLIGHT_SUBCOMMANDS:
        return

    cfg = _parse_compose_config_interpolated(host)
    services = _selected_services(cfg, args, subcommand_index)
    if not services:
        return

    bind_mounts = _collect_bind_mounts(cfg, services)
    file_like_mounts = [
        mount for mount in bind_mounts if _looks_like_file_path(mount.source)
    ]
    if not file_like_mounts:
        return

    out.phase("Preflight: checking file-like bind mount source paths...")
    path_types = _remote_path_types(
        host, sorted({mount.source for mount in file_like_mounts})
    )

    issues: list[str] = []
    warnings: list[str] = []
    success_count = 0

    for mount in file_like_mounts:
        kind = path_types.get(mount.source, "unknown")
        if kind in {"file", "socket", "symlink"}:
            success_count += 1
            continue
        if kind == "dir":
            issues.append(
                f"{mount.service}: {mount.source} is a directory but looks like a file path (mount target: {mount.target})"
            )
            continue
        if kind == "missing":
            issues.append(
                f"{mount.service}: {mount.source} is missing and looks like a file path (mount target: {mount.target})"
            )
            continue
        warnings.append(
            f"{mount.service}: {mount.source} has remote type '{kind}' (mount target: {mount.target})"
        )

    for warning in warnings:
        out.warn(warning)

    if issues:
        for issue in issues:
            out.err(issue)
        if success_count:
            out.ok(
                f"Preflight partial pass: {success_count} file-like bind path(s) verified."
            )
        raise SystemExit(
            "Preflight failed: fix invalid bind source path types and retry."
        )

    out.ok(f"Preflight passed: {success_count} file-like bind path(s) verified.")


def compose_logs_across_stacks(args: tuple[str, ...]) -> int:
    """Stream docker compose logs from all stacks that include requested services."""
    from tctl.fmt_compose import extract_log_timestamp, format_log_line

    subcommand, subcommand_index = compose_subcommand(args)
    if subcommand != "logs":
        raise SystemExit(
            "compose_logs_across_stacks only supports docker compose logs."
        )

    services = _logs_service_filters(args, subcommand_index)
    if not services:
        raise SystemExit(
            "Stack required for this logs invocation. Use -m/-n, or specify one or more services "
            "(example: tctl logs blocky)."
        )

    hosts = _matching_stacks_for_services(services)
    if not hosts:
        raise SystemExit(f"No stacks include service filter(s): {', '.join(services)}")

    effective_args = list(args)
    if not _has_follow_flag(args):
        effective_args.insert(subcommand_index + 1, "-f")
        out.meta("No follow flag provided; defaulting to streaming mode (-f).")

    out.phase(f"docker compose {' '.join(effective_args)}")
    out.meta(f"Streaming from stacks: {', '.join(hosts)}")

    line_queue: queue.Queue[tuple[str, str | None]] = queue.Queue()
    procs: dict[str, subprocess.Popen[str]] = {}
    readers: list[threading.Thread] = []
    # Hold lines briefly so inter-host network jitter does not scramble chronology.
    pending: list[tuple[float, int, float, str, str]] = []
    sequence = 0
    reorder_window_seconds = 0.75

    def _enqueue_line(host: str, line: str) -> None:
        nonlocal sequence
        parsed = extract_log_timestamp(line)
        event_ts = parsed.timestamp() if parsed is not None else time.time()
        pending.append((event_ts, sequence, time.monotonic(), host, line))
        sequence += 1

    def _flush_ready(*, force: bool = False) -> None:
        nonlocal pending
        if not pending:
            return

        if force:
            ready = pending
            pending = []
        else:
            cutoff = time.monotonic() - reorder_window_seconds
            ready = [entry for entry in pending if entry[2] <= cutoff]
            pending = [entry for entry in pending if entry[2] > cutoff]

        if not ready:
            return

        for _, _, _, ready_host, ready_line in sorted(
            ready, key=lambda item: (item[0], item[1])
        ):
            format_log_line(ready_line, stack=ready_host)

    def _pump_output(host: str, proc: subprocess.Popen[str]) -> None:
        if proc.stdout is None:
            line_queue.put((host, None))
            return
        for raw in proc.stdout:
            line_queue.put((host, raw.rstrip("\n")))
        line_queue.put((host, None))

    try:
        for host in hosts:
            cmd = _base_cmd(host) + effective_args
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=subprocess_env(host),
                bufsize=1,
            )
            procs[host] = proc
            thread = threading.Thread(
                target=_pump_output, args=(host, proc), daemon=True
            )
            thread.start()
            readers.append(thread)

        completed_hosts: set[str] = set()
        while len(completed_hosts) < len(hosts):
            try:
                host, line = line_queue.get(timeout=0.2)
            except queue.Empty:
                _flush_ready()
                continue
            if line is None:
                completed_hosts.add(host)
                _flush_ready()
                continue
            if line:
                _enqueue_line(host, line)
                _flush_ready()

        _flush_ready(force=True)

        rc = 0
        for host, proc in procs.items():
            proc.wait()
            if proc.returncode not in (0, None):
                out.warn(f"[{host}] logs exited with code {proc.returncode}.")
                if rc == 0:
                    rc = proc.returncode
        return rc

    except KeyboardInterrupt:
        for proc in procs.values():
            if proc.poll() is None:
                proc.terminate()
        return 130
    finally:
        for thread in readers:
            thread.join(timeout=0.2)


def compose_exec(host: str, args: tuple[str, ...], *, full: bool = False) -> int:
    """Run a docker compose command with beautified output for known subcommands."""
    from tctl.fmt_compose import (
        detect_compose_command,
        format_images,
        format_lifecycle_line,
        format_log_line,
        format_ps,
    )

    compose = stack_compose(host)
    if not compose.is_file():
        raise SystemExit(f"No stack compose file at {compose}")

    env = subprocess_env(host)
    formatter = detect_compose_command(args)

    _run_bind_mount_preflight(host, args)

    out.phase(f"docker compose {' '.join(args)}")

    # ── ps: inject --format json, capture + format ───────────────
    if formatter == "ps":
        cmd = _base_cmd(host) + list(args) + ["--format", "json"]
        result = subprocess.run(cmd, capture_output=True, text=True, env=env)
        format_ps(result.stdout, result.stderr, full=full)
        return result.returncode

    # ── images: inject --format json, capture + format ───────────
    if formatter == "images":
        cmd = _base_cmd(host) + list(args) + ["--format", "json"]
        result = subprocess.run(cmd, capture_output=True, text=True, env=env)
        format_images(result.stdout, result.stderr)
        return result.returncode

    # ── logs: stream and format each line ────────────────────────
    if formatter == "logs":
        cmd = _base_cmd(host) + list(args)
        proc: subprocess.Popen[str] | None = None
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
                bufsize=1,
            )
            for line in proc.stdout:  # type: ignore[union-attr]
                format_log_line(line.rstrip("\n"))
            proc.wait()
            return proc.returncode
        except KeyboardInterrupt:
            if proc is not None and proc.poll() is None:
                proc.terminate()
            return 130

    # ── lifecycle (up/down/restart/etc): capture + format ────────
    if formatter == "lifecycle":
        cmd = _base_cmd(host) + list(args)
        dry_run = "--dry-run" in args
        proc: subprocess.Popen[str] | None = None
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
                bufsize=1,
            )
            for line in proc.stdout:  # type: ignore[union-attr]
                format_lifecycle_line(line.rstrip("\n"), dry_run=dry_run)
            proc.wait()
            return proc.returncode
        except KeyboardInterrupt:
            if proc is not None and proc.poll() is None:
                proc.terminate()
            return 130

    # ── Unrecognized: raw passthrough ────────────────────────────
    cmd = _base_cmd(host) + list(args)
    result = subprocess.run(cmd, env=env)
    return result.returncode
