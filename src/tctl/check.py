"""Comprehensive service health checks for tctl."""

from __future__ import annotations

import ipaddress
import json
import re
import socket
import ssl
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib import error as urllib_error
from urllib import request as urllib_request

import yaml

from tctl import output as out
from tctl.compose import service_stacks, stack_services
from tctl.config import SSH_OPTS, available_hosts, stack_compose, subprocess_env

_SCOPE_SERVICE = "service"

_STATUS_PASS = "pass"
_STATUS_WARN = "warn"
_STATUS_FAIL = "fail"
_STATUS_SKIP = "skip"

_STATUS_SYMBOL = {
    _STATUS_PASS: out.SYM_OK,
    _STATUS_WARN: out.SYM_WARN,
    _STATUS_FAIL: out.SYM_ERR,
    _STATUS_SKIP: "•",
}

_STATUS_STYLE = {
    _STATUS_PASS: "ok",
    _STATUS_WARN: "warn",
    _STATUS_FAIL: "err",
    _STATUS_SKIP: "dim",
}

_HOST_RULE_RE = re.compile(r"Host(?:SNI)?\(([^)]*)\)", flags=re.IGNORECASE)

_PORT_TIMEOUT_SECONDS = 1.5
_ROUTE_TIMEOUT_SECONDS = 2.5

_DEFAULT_SSL = ssl.create_default_context()
_UNVERIFIED_SSL = ssl.create_default_context()
_UNVERIFIED_SSL.check_hostname = False
_UNVERIFIED_SSL.verify_mode = ssl.CERT_NONE

_TAILNET_NETWORK = ipaddress.IPv4Network("100.64.0.0/10")


def _is_tailnet_ip(host: str) -> bool:
    try:
        return ipaddress.IPv4Address(host) in _TAILNET_NETWORK
    except ValueError:
        return False


@dataclass(frozen=True)
class CheckOutcome:
    name: str
    status: str
    summary: str
    details: tuple[str, ...] = ()


@dataclass(frozen=True)
class ServiceCheckResult:
    host: str
    service: str
    outcomes: tuple[CheckOutcome, ...]

    @property
    def by_name(self) -> dict[str, CheckOutcome]:
        return {outcome.name: outcome for outcome in self.outcomes}

    @property
    def overall_status(self) -> str:
        statuses = [outcome.status for outcome in self.outcomes]
        if _STATUS_FAIL in statuses:
            return _STATUS_FAIL
        if _STATUS_WARN in statuses:
            return _STATUS_WARN
        return _STATUS_PASS


@dataclass(frozen=True)
class _BindMountRef:
    source: str
    target: str


def _resolve_targets(
    *,
    explicit_stack: str | None,
    scope: str | None,
    service: str | None,
) -> list[tuple[str, list[str] | None]]:
    hosts = available_hosts()
    normalized_scope = scope.strip() if scope else None
    normalized_service = service.strip() if service else None

    if explicit_stack:
        if (
            normalized_scope
            and normalized_scope in hosts
            and normalized_scope != explicit_stack
        ):
            raise ValueError(
                f"Conflicting stack selectors: option selected '{explicit_stack}' but argument selected '{normalized_scope}'."
            )

        if not normalized_scope:
            if normalized_service:
                return [(explicit_stack, [normalized_service])]
            return [(explicit_stack, None)]

        if normalized_scope == _SCOPE_SERVICE:
            if not normalized_service:
                raise ValueError(
                    "Provide a service name: tctl check service <service>."
                )
            return [(explicit_stack, [normalized_service])]

        if normalized_scope in hosts:
            if normalized_service:
                return [(explicit_stack, [normalized_service])]
            return [(explicit_stack, None)]

        if normalized_service:
            raise ValueError(
                "Too many arguments for explicit stack mode. Use tctl check <service> with -m/-n/-s, or drop the second argument."
            )

        return [(explicit_stack, [normalized_scope])]

    if not normalized_scope:
        return [(host, None) for host in hosts]

    if normalized_scope == _SCOPE_SERVICE:
        if not normalized_service:
            raise ValueError("Provide a service name: tctl check service <service>.")
        try:
            stacks = service_stacks(normalized_service)
        except SystemExit as exc:
            raise ValueError(str(exc)) from exc
        return [(host, [normalized_service]) for host in stacks]

    if normalized_scope in hosts:
        if normalized_service:
            return [(normalized_scope, [normalized_service])]
        return [(normalized_scope, None)]

    if normalized_service:
        raise ValueError(
            f"Unknown scope '{normalized_scope}'. Use one of: {', '.join(hosts + [_SCOPE_SERVICE])}."
        )

    # Convenience mode: treat `tctl check <service>` as service-wide check across stacks.
    try:
        stacks = service_stacks(normalized_scope)
    except SystemExit as exc:
        raise ValueError(
            f"Unknown scope or service '{normalized_scope}'. Use one of: {', '.join(hosts + [_SCOPE_SERVICE])}."
        ) from exc
    return [(host, [normalized_scope]) for host in stacks]


def _compose_config(host: str) -> dict:
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
            f"Unable to render compose config for '{host}'.\n{result.stderr.strip()}"
        )
    return yaml.safe_load(result.stdout) or {}


def _parse_json_objects(text: str) -> list[dict]:
    stripped = text.strip()
    if not stripped:
        return []

    try:
        data = json.loads(stripped)
        if isinstance(data, dict):
            return [data]
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
    except json.JSONDecodeError:
        pass

    rows: list[dict] = []
    for line in stripped.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _compose_ps(host: str) -> list[dict]:
    compose = stack_compose(host)
    env = subprocess_env(host)
    result = subprocess.run(
        [
            "docker",
            "--context",
            host,
            "compose",
            "-f",
            str(compose),
            "ps",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode != 0:
        raise SystemExit(
            f"Unable to read compose status for '{host}'.\n{result.stderr.strip()}"
        )
    return _parse_json_objects(result.stdout)


def _service_definitions(cfg: dict) -> dict[str, dict]:
    services = cfg.get("services", {})
    if not isinstance(services, dict):
        return {}
    typed: dict[str, dict] = {}
    for name, raw in services.items():
        if isinstance(raw, dict):
            typed[str(name)] = raw
    return typed


def _selected_services(host: str, requested: list[str] | None) -> list[str]:
    services = stack_services(host)
    if not requested:
        return services

    missing = [name for name in requested if name not in services]
    if missing:
        available = ", ".join(services)
        raise ValueError(
            f"Service(s) not in stack '{host}': {', '.join(missing)}. Available: {available}"
        )
    return requested


def _labels_dict(service_cfg: dict) -> dict[str, str]:
    labels = service_cfg.get("labels", {})
    result: dict[str, str] = {}

    if isinstance(labels, dict):
        for key, value in labels.items():
            result[str(key)] = str(value)
        return result

    if isinstance(labels, list):
        for entry in labels:
            if not isinstance(entry, str):
                continue
            if "=" in entry:
                key, value = entry.split("=", 1)
                result[key.strip()] = value.strip()
            else:
                result[entry.strip()] = "true"

    return result


def _as_bool(raw: str | None) -> bool | None:
    if raw is None:
        return None
    lowered = raw.strip().lower()
    if lowered in {"true", "1", "yes", "on"}:
        return True
    if lowered in {"false", "0", "no", "off"}:
        return False
    return None


def _extract_traefik_routes(service_cfg: dict) -> list[tuple[str, tuple[str, ...]]]:
    labels = _labels_dict(service_cfg)
    routes: dict[str, set[str]] = {}

    for key, value in labels.items():
        if not key.startswith("traefik.http.routers.") or not key.endswith(".rule"):
            continue

        router = key.removeprefix("traefik.http.routers.").removesuffix(".rule")
        entrypoint_key = f"traefik.http.routers.{router}.entrypoints"
        entrypoints = [
            part.strip().lower()
            for part in labels.get(entrypoint_key, "").split(",")
            if part.strip()
        ]

        schemes: set[str] = set()
        if any("secure" in entry for entry in entrypoints):
            schemes.add("https")
        if "web" in entrypoints:
            schemes.add("http")
        if not schemes:
            schemes = {"https", "http"}

        for host_match in _HOST_RULE_RE.findall(value):
            for raw in host_match.split(","):
                candidate = raw.strip().strip("`\"' ")
                if not candidate:
                    continue
                if "{" in candidate or "}" in candidate or "*" in candidate:
                    continue
                if "." not in candidate:
                    continue
                routes.setdefault(candidate, set()).update(schemes)

    return [(host, tuple(sorted(schemes))) for host, schemes in sorted(routes.items())]


def _split_short_volume_spec(spec: str) -> tuple[str | None, str | None, str]:
    parts = spec.split(":")
    if len(parts) < 2:
        return None, None, ""
    source = parts[0]
    target = parts[1]
    options = ":".join(parts[2:]) if len(parts) > 2 else ""
    return source, target, options


def _collect_bind_mounts(service_cfg: dict) -> list[_BindMountRef]:
    mounts: list[_BindMountRef] = []
    volumes = service_cfg.get("volumes", [])
    if not isinstance(volumes, list):
        return mounts

    for volume in volumes:
        if isinstance(volume, str):
            source, target, _options = _split_short_volume_spec(volume)
            if source and target and source.startswith("/"):
                mounts.append(_BindMountRef(source=source, target=target))
            continue

        if not isinstance(volume, dict):
            continue
        if volume.get("type") != "bind":
            continue
        source = str(volume.get("source") or "")
        target = str(volume.get("target") or "")
        if source.startswith("/") and target:
            mounts.append(_BindMountRef(source=source, target=target))

    deduped: dict[tuple[str, str], _BindMountRef] = {}
    for mount in mounts:
        deduped[(mount.source, mount.target)] = mount
    return list(deduped.values())


def _looks_like_file_path(path: str) -> bool:
    return Path(path).suffix != ""


def _remote_path_types(
    host: str, paths: list[str]
) -> tuple[dict[str, str] | None, str | None]:
    if not paths:
        return {}, None

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
        stderr = result.stderr.strip() or "ssh failed"
        return None, stderr

    types: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "\t" not in line:
            continue
        path, kind = line.split("\t", 1)
        types[path] = kind
    return types, None


def _tcp_probe(host: str, port: int, timeout: float) -> tuple[bool, str]:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, "reachable"
    except OSError as exc:
        return False, str(exc)


def _http_probe(url: str, timeout: float, *, verify_tls: bool) -> tuple[bool, str]:
    req = urllib_request.Request(url=url, headers={"User-Agent": "tctl-check/0.1"})
    context = _DEFAULT_SSL if verify_tls else _UNVERIFIED_SSL
    try:
        with urllib_request.urlopen(req, timeout=timeout, context=context) as resp:
            status = getattr(resp, "status", 0)
            reason = getattr(resp, "reason", "")
            return True, f"{status} {reason}".strip()
    except urllib_error.HTTPError as exc:
        if exc.code < 500:
            return True, f"{exc.code} {exc.reason}".strip()
        return False, f"{exc.code} {exc.reason}".strip()
    except urllib_error.URLError as exc:
        return False, str(exc.reason)
    except OSError as exc:
        return False, str(exc)


def _check_containers(containers: list[dict]) -> CheckOutcome:
    if not containers:
        return CheckOutcome("containers", _STATUS_FAIL, "no compose containers found")

    non_running: list[str] = []
    running = 0
    for container in containers:
        state = str(container.get("State", "unknown")).lower()
        name = str(container.get("Name", container.get("Service", "container")))
        if state == "running":
            running += 1
            continue
        non_running.append(f"{name}={state}")

    if not non_running:
        return CheckOutcome(
            "containers",
            _STATUS_PASS,
            f"{running}/{len(containers)} running",
        )

    return CheckOutcome(
        "containers",
        _STATUS_FAIL,
        f"{running}/{len(containers)} running",
        tuple(non_running),
    )


def _check_health(containers: list[dict]) -> CheckOutcome:
    if not containers:
        return CheckOutcome("health", _STATUS_SKIP, "skipped: container check failed")

    health_states: list[tuple[str, str]] = []
    for container in containers:
        raw_health = container.get("Health")
        if raw_health is None or raw_health == "":
            continue
        name = str(container.get("Name", container.get("Service", "container")))
        health_states.append((name, str(raw_health).lower()))

    if not health_states:
        return CheckOutcome("health", _STATUS_WARN, "no docker healthcheck configured")

    unhealthy = [
        f"{name}={state}" for name, state in health_states if state == "unhealthy"
    ]
    if unhealthy:
        return CheckOutcome(
            "health",
            _STATUS_FAIL,
            "at least one container is unhealthy",
            tuple(unhealthy),
        )

    unknown = [f"{name}={state}" for name, state in health_states if state != "healthy"]
    if unknown:
        return CheckOutcome(
            "health",
            _STATUS_WARN,
            "health state is not yet healthy",
            tuple(unknown),
        )

    return CheckOutcome(
        "health", _STATUS_PASS, f"{len(health_states)} healthy container(s)"
    )


def _check_ports(host: str, containers: list[dict]) -> CheckOutcome:
    publishers: list[dict] = []
    for container in containers:
        raw_publishers = container.get("Publishers", [])
        if isinstance(raw_publishers, list):
            publishers.extend(p for p in raw_publishers if isinstance(p, dict))

    if not publishers:
        return CheckOutcome("ports", _STATUS_PASS, "no published host ports")

    endpoints: list[tuple[str, int]] = []
    skipped: list[str] = []

    for publisher in publishers:
        protocol = str(publisher.get("Protocol", "tcp")).lower()
        if protocol != "tcp":
            maybe_port = publisher.get("PublishedPort")
            if isinstance(maybe_port, int):
                skipped.append(f"{maybe_port}/{protocol} (tcp probe not applicable)")
            continue

        port = publisher.get("PublishedPort")
        if not isinstance(port, int) or port <= 0:
            continue

        bind_host = str(publisher.get("URL") or "")
        target_host = (
            bind_host
            if bind_host and bind_host not in {"0.0.0.0", "::", "[::]"}
            else host
        )

        # Remote loopback-only bindings cannot be verified from local execution context.
        if target_host in {"127.0.0.1", "::1"} and host not in {
            "localhost",
            "127.0.0.1",
        }:
            skipped.append(f"{target_host}:{port} (remote loopback binding)")
            continue

        # Tailnet CGNAT addresses are only reachable from within the tailnet.
        if _is_tailnet_ip(target_host):
            skipped.append(f"{target_host}:{port} (tailnet address)")
            continue

        # Resolve target; skip if it maps to tailnet or doesn't resolve locally.
        try:
            resolved = socket.gethostbyname(target_host)
        except OSError:
            skipped.append(f"{target_host}:{port} (not resolvable locally)")
            continue

        if _is_tailnet_ip(resolved):
            skipped.append(f"{target_host}:{port} (resolves to tailnet)")
            continue

        endpoints.append((target_host, port))

    endpoints = list(dict.fromkeys(endpoints))
    if not endpoints:
        if skipped:
            return CheckOutcome(
                "ports",
                _STATUS_PASS,
                "no externally probeable tcp ports",
                tuple(skipped),
            )
        return CheckOutcome("ports", _STATUS_PASS, "no probeable tcp ports")

    failures: list[str] = []
    for endpoint_host, endpoint_port in endpoints:
        ok, detail = _tcp_probe(endpoint_host, endpoint_port, _PORT_TIMEOUT_SECONDS)
        if not ok:
            failures.append(f"{endpoint_host}:{endpoint_port} -> {detail}")

    if failures:
        details = tuple(failures + skipped)
        return CheckOutcome(
            "ports",
            _STATUS_FAIL,
            f"{len(endpoints) - len(failures)}/{len(endpoints)} tcp endpoint(s) reachable",
            details,
        )

    details = tuple(skipped)
    return CheckOutcome(
        "ports", _STATUS_PASS, f"{len(endpoints)} tcp endpoint(s) reachable", details
    )


def _check_traefik(service_cfg: dict, *, verify_tls: bool) -> CheckOutcome:
    labels = _labels_dict(service_cfg)
    traefik_enabled = _as_bool(labels.get("traefik.enable"))
    routes = _extract_traefik_routes(service_cfg)

    if not routes:
        if traefik_enabled is True:
            return CheckOutcome(
                "traefik", _STATUS_PASS, "traefik enabled but no Host(...) rules found"
            )
        return CheckOutcome(
            "traefik", _STATUS_PASS, "no traefik Host(...) route labels"
        )

    failures: list[str] = []
    successes = 0

    for host, schemes in routes:
        ordered_schemes = list(schemes) or ["https", "http"]
        preferred = [
            scheme for scheme in ("https", "http") if scheme in ordered_schemes
        ]
        for scheme in ordered_schemes:
            if scheme not in preferred:
                preferred.append(scheme)

        passed = False
        last_error = "no response"
        for scheme in preferred:
            url = f"{scheme}://{host}/"
            ok, detail = _http_probe(url, _ROUTE_TIMEOUT_SECONDS, verify_tls=verify_tls)
            if ok:
                successes += 1
                passed = True
                break
            last_error = f"{url} -> {detail}"

        if not passed:
            failures.append(last_error)

    if failures:
        return CheckOutcome(
            "traefik",
            _STATUS_FAIL,
            f"{successes}/{len(routes)} traefik route(s) reachable",
            tuple(failures),
        )

    return CheckOutcome(
        "traefik", _STATUS_PASS, f"{successes}/{len(routes)} traefik route(s) reachable"
    )


def _check_bind_sources(
    bind_mounts: list[_BindMountRef],
    path_types: dict[str, str] | None,
    path_types_error: str | None,
) -> CheckOutcome:
    if not bind_mounts:
        return CheckOutcome("binds", _STATUS_PASS, "no bind mounts")

    if path_types is None:
        return CheckOutcome(
            "binds",
            _STATUS_WARN,
            "unable to inspect bind mount paths over ssh",
            (path_types_error or "ssh check failed",),
        )

    failures: list[str] = []
    warnings: list[str] = []
    verified = 0

    for mount in bind_mounts:
        kind = path_types.get(mount.source, "missing")
        if _looks_like_file_path(mount.source):
            if kind in {"file", "socket", "symlink"}:
                verified += 1
                continue
            if kind == "dir":
                failures.append(
                    f"{mount.source} is dir but target '{mount.target}' looks file-like"
                )
                continue
            failures.append(f"{mount.source} missing for target '{mount.target}'")
            continue

        if kind == "missing":
            warnings.append(f"{mount.source} is missing (docker may create it)")
            continue
        verified += 1

    if failures:
        return CheckOutcome(
            "binds",
            _STATUS_FAIL,
            f"{verified}/{len(bind_mounts)} bind mount source(s) verified",
            tuple(failures + warnings),
        )

    if warnings:
        return CheckOutcome(
            "binds",
            _STATUS_WARN,
            f"{verified}/{len(bind_mounts)} bind mount source(s) verified",
            tuple(warnings),
        )

    return CheckOutcome(
        "binds",
        _STATUS_PASS,
        f"{verified}/{len(bind_mounts)} bind mount source(s) verified",
    )


def _service_result(
    *,
    host: str,
    service: str,
    service_cfg: dict,
    containers: list[dict],
    bind_mounts: list[_BindMountRef],
    path_types: dict[str, str] | None,
    path_types_error: str | None,
    verify_tls: bool,
) -> ServiceCheckResult:
    outcomes = (
        _check_containers(containers),
        _check_health(containers),
        _check_ports(host, containers),
        _check_traefik(service_cfg, verify_tls=verify_tls),
        _check_bind_sources(bind_mounts, path_types, path_types_error),
    )
    return ServiceCheckResult(host=host, service=service, outcomes=outcomes)


def _status_text(status: str) -> str:
    symbol = _STATUS_SYMBOL.get(status, "?")
    style = _STATUS_STYLE.get(status, "dim")
    label = status.upper()
    return f"[{style}]{symbol} {label}[/]"


def _print_verbose_details(results: list[ServiceCheckResult]) -> None:
    for result in results:
        out.meta(f"{result.host}/{result.service}")
        for outcome in result.outcomes:
            out.console.print(
                f"    {_status_text(outcome.status)} [bold]{outcome.name}[/]: {outcome.summary}"
            )
            for detail in outcome.details:
                out.info(detail)


def _print_summary(results: list[ServiceCheckResult]) -> None:
    from rich.table import Table

    table = Table(title="Service Checks", show_lines=True)
    table.add_column("Stack", style="bold cyan")
    table.add_column("Service", style="bold white")
    table.add_column("Containers")
    table.add_column("Health")
    table.add_column("Ports")
    table.add_column("Traefik")
    table.add_column("Binds")
    table.add_column("Overall")

    for result in results:
        by_name = result.by_name
        table.add_row(
            result.host,
            result.service,
            _status_text(by_name["containers"].status),
            _status_text(by_name["health"].status),
            _status_text(by_name["ports"].status),
            _status_text(by_name["traefik"].status),
            _status_text(by_name["binds"].status),
            _status_text(result.overall_status),
        )

    out.console.print(table)


def run_checks(
    *,
    explicit_stack: str | None,
    scope: str | None,
    service: str | None,
    verbose: bool,
    verify_tls: bool,
) -> bool:
    """Run service checks and return True when any service has failed checks."""
    targets = _resolve_targets(
        explicit_stack=explicit_stack, scope=scope, service=service
    )
    if not targets:
        raise ValueError("No target stacks were resolved.")

    all_results: list[ServiceCheckResult] = []

    for host, requested_services in targets:
        out.phase(f"Evaluating stack '{host}'...")

        cfg = _compose_config(host)
        service_defs = _service_definitions(cfg)
        selected = _selected_services(host, requested_services)

        ps_rows = _compose_ps(host)
        by_service: dict[str, list[dict]] = {}
        for row in ps_rows:
            service_name = str(row.get("Service") or "")
            if service_name:
                by_service.setdefault(service_name, []).append(row)

        bind_mounts_by_service: dict[str, list[_BindMountRef]] = {}
        bind_paths: set[str] = set()

        for service_name in selected:
            bind_mounts = _collect_bind_mounts(service_defs.get(service_name, {}))
            bind_mounts_by_service[service_name] = bind_mounts
            for bind_mount in bind_mounts:
                bind_paths.add(bind_mount.source)

        path_types, path_types_error = _remote_path_types(host, sorted(bind_paths))

        for service_name in selected:
            result = _service_result(
                host=host,
                service=service_name,
                service_cfg=service_defs.get(service_name, {}),
                containers=by_service.get(service_name, []),
                bind_mounts=bind_mounts_by_service.get(service_name, []),
                path_types=path_types,
                path_types_error=path_types_error,
                verify_tls=verify_tls,
            )
            all_results.append(result)

    _print_summary(all_results)

    if verbose:
        _print_verbose_details(all_results)

    fail_count = sum(
        1 for result in all_results if result.overall_status == _STATUS_FAIL
    )
    warn_count = sum(
        1 for result in all_results if result.overall_status == _STATUS_WARN
    )
    pass_count = sum(
        1 for result in all_results if result.overall_status == _STATUS_PASS
    )

    out.meta(
        f"Summary: {pass_count} pass, {warn_count} warning, {fail_count} failing service(s) across {len(all_results)} checked service(s)."
    )

    if fail_count:
        out.err("Health check failed. Resolve failing checks before deploy.")
        return True

    out.ok("Health check completed without failures.")
    return False
