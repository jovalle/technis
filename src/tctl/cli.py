"""tctl — Technis infrastructure control CLI.

Usage examples:
    tctl -m ps authelia           # compose passthrough (mothership)
    tctl -n logs -f immich        # stream logs on nexus
    tctl logs blocky              # follow blocky logs across all matching stacks
    tctl check                    # comprehensive checks across all stacks/services
    tctl check mothership         # comprehensive checks for one stack
    tctl check service immich     # comprehensive checks for one service across stacks
    tctl -n deploy                # deploy full nexus stack
    tctl -m deploy authelia       # render + push + up
    tctl -m diff authelia         # preview local/remote drift for a service
    tctl deploy authelia          # deploy to all stacks containing it
    tctl -n push                  # push all nexus services
    tctl render -m                # render mothership templates
    tctl ls                       # list stacks and services
"""

from __future__ import annotations

import shutil
import subprocess

import click

from tctl import __version__
from tctl import output as out
from tctl.compose_args import compose_primary_subcommand
from tctl.config import available_hosts, resolve_host, stack_compose
from tctl.envsync import sync_all_env_files

# ── Custom group that routes unknown commands to compose passthrough ──


class PassthroughGroup(click.Group):
    """Click group that treats unknown subcommands as docker compose args."""

    def resolve_command(self, ctx: click.Context, args: list[str]) -> tuple:
        cmd_name = args[0] if args else None
        if cmd_name and cmd_name in self.commands:
            return super().resolve_command(ctx, args)
        # Unknown command → compose passthrough.
        return "do", self.get_command(ctx, "do"), args  # type: ignore[return-value]

    def format_usage(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        formatter.write("Usage: tctl [-m|-n|-s STACK] <command> [args...]\n")
        formatter.write("       tctl -m <compose-cmd> [args...]   (passthrough)\n")


# ── Stack option (shared across group) ───────────────────────────────


_STACK_REQUIRED_MESSAGE = "Stack required. Use -m, -n, or -s <stack>."


def _raise_stack_required(*, mention_logs_shortcut: bool = False) -> None:
    message = _STACK_REQUIRED_MESSAGE
    if mention_logs_shortcut:
        message = f"{message}\nTip: use 'tctl logs <service>' to stream across stacks."
    raise click.UsageError(message)


def _optional_stack(ctx: click.Context) -> str | None:
    return ctx.obj.get("stack")


_COMPOSE_MUTATING_SUBCOMMANDS = {
    "build",
    "cp",
    "create",
    "down",
    "exec",
    "kill",
    "pause",
    "pull",
    "push",
    "restart",
    "rm",
    "run",
    "start",
    "stop",
    "unpause",
    "up",
}


def _is_mutating_compose_call(args: tuple[str, ...]) -> bool:
    subcommand = compose_primary_subcommand(args)
    if not subcommand:
        return False
    return subcommand in _COMPOSE_MUTATING_SUBCOMMANDS


def _run_env_sync() -> None:
    try:
        summary = sync_all_env_files()
    except ValueError as exc:
        raise SystemExit(f"Unable to normalize environment files.\n{exc}")
    if summary.changed:
        out.phase(summary.describe())


# ── CLI group ────────────────────────────────────────────────────────


@click.group(cls=PassthroughGroup, invoke_without_command=True)
@click.option("-m", "stack", flag_value="mothership", help="Target mothership.")
@click.option("-n", "stack", flag_value="nexus", help="Target nexus.")
@click.option("-s", "--stack", "stack", default=None, help="Target stack by name.")
@click.option(
    "--full",
    "full_length",
    is_flag=True,
    default=False,
    help="Full-length formatted output for supported compose commands (currently: ps).",
)
@click.version_option(__version__, prog_name="tctl")
@click.pass_context
def cli(ctx: click.Context, stack: str | None, full_length: bool) -> None:
    """Technis infrastructure control."""
    ctx.ensure_object(dict)
    ctx.obj["stack"] = resolve_host(stack)
    ctx.obj["full_length"] = full_length
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


# ── do: compose passthrough ──────────────────────────────────────────


@cli.command(
    "do",
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
    add_help_option=False,
)
@click.option(
    "--full",
    "full_length",
    is_flag=True,
    help="Full-length formatted output for supported compose commands (currently: ps).",
)
@click.argument("compose_args", nargs=-1, type=click.UNPROCESSED)
@click.pass_context
def do_cmd(
    ctx: click.Context, full_length: bool, compose_args: tuple[str, ...]
) -> None:
    """Docker compose passthrough (also the default for unknown commands)."""
    from tctl.compose import compose_exec, compose_logs_across_stacks

    if not compose_args:
        raise click.UsageError("No compose command given. Example: tctl -m ps")

    host = _optional_stack(ctx)
    subcommand = compose_primary_subcommand(compose_args)

    if not host:
        if subcommand == "logs":
            rc = compose_logs_across_stacks(compose_args)
            ctx.exit(rc)
        _raise_stack_required(mention_logs_shortcut=True)

    if _is_mutating_compose_call(compose_args):
        _run_env_sync()

    full_mode = full_length or bool(ctx.obj.get("full_length"))
    rc = compose_exec(host, compose_args, full=full_mode)
    ctx.exit(rc)


@cli.command(
    "logs",
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.argument("logs_args", nargs=-1, type=click.UNPROCESSED)
@click.pass_context
def logs_cmd(ctx: click.Context, logs_args: tuple[str, ...]) -> None:
    """Stream docker compose logs.

    \b
    tctl -m logs -f authelia      stream logs on one stack
    tctl logs authelia            stream logs across matching stacks
    tctl logs -f authelia traefik stream multiple services across stacks
    """
    from tctl.compose import compose_exec, compose_logs_across_stacks

    host = _optional_stack(ctx)
    compose_args = ("logs", *logs_args)

    if host:
        rc = compose_exec(host, compose_args, full=bool(ctx.obj.get("full_length")))
        ctx.exit(rc)

    if not logs_args:
        raise click.UsageError(
            "Provide at least one service when no stack is selected.\n"
            "Example: tctl logs blocky"
        )

    rc = compose_logs_across_stacks(compose_args)
    ctx.exit(rc)


# ── deploy ───────────────────────────────────────────────────────────


@cli.command()
@click.option(
    "-f",
    "--follow",
    "follow_logs",
    is_flag=True,
    default=False,
    help="Follow logs after deploy for deployed service(s).",
)
@click.option(
    "-y",
    "--yes",
    is_flag=True,
    default=False,
    help="Auto-approve push confirmations during deploy.",
)
@click.option(
    "--no",
    "auto_no",
    is_flag=True,
    default=False,
    help="Auto-skip push confirmations during deploy.",
)
@click.argument("target", required=False, default=None)
@click.argument("service", required=False, default=None)
@click.pass_context
def deploy(
    ctx: click.Context,
    follow_logs: bool,
    yes: bool,
    auto_no: bool,
    target: str | None,
    service: str | None,
) -> None:
    """Deploy a service or full stack.

    \b
    tctl deploy                        deploy full stack on all hosts
    tctl deploy <service>              deploy to all stacks containing it
    tctl -m deploy <service>           deploy on mothership
    tctl -m deploy                     deploy full mothership stack
    tctl -m deploy -f <service>        deploy on mothership and follow logs
    tctl -m deploy -y <service>        deploy on mothership (explicit auto-approve)
    tctl -m deploy --no <service>      deploy on mothership and skip pushes
    tctl deploy <host>                 deploy full stack
    """
    from tctl.compose import (
        compose_exec,
        reconcile,
        service_closure,
        service_stacks,
        write_rendered_compose_to_stack_root,
    )
    from tctl.render import render as do_render
    from tctl.sync import push as sync_push

    _run_env_sync()

    if yes and auto_no:
        raise click.UsageError("Use either --yes or --no, not both.")

    # Deploy prompts by default; --yes and --no make behavior non-interactive.
    auto_yes = yes

    explicit_stack = _optional_stack(ctx)

    if explicit_stack and not target and not service:
        # `tctl -m deploy` → full deploy on explicit stack.
        _deploy_full_stack(
            explicit_stack,
            follow_logs,
            reconcile,
            do_render,
            sync_push,
            auto_yes,
            auto_no,
            compose_exec,
            write_rendered_compose_to_stack_root,
        )
        return

    if explicit_stack and not service:
        # `tctl -m deploy <target>` → host is explicit, target is service.
        service = target
        host = explicit_stack
        _deploy_one(
            host,
            service,
            follow_logs,
            reconcile,
            service_closure,
            do_render,
            sync_push,
            auto_yes,
            auto_no,
            compose_exec,
            write_rendered_compose_to_stack_root,
        )
        return

    if explicit_stack and service:
        # `tctl -m deploy <target> <service>` → target is ignored, use explicit stack.
        _deploy_one(
            explicit_stack,
            service,
            follow_logs,
            reconcile,
            service_closure,
            do_render,
            sync_push,
            auto_yes,
            auto_no,
            compose_exec,
            write_rendered_compose_to_stack_root,
        )
        return

    # No explicit stack.
    if service:
        # `tctl deploy <host> <service>`
        _deploy_one(
            target,
            service,
            follow_logs,
            reconcile,
            service_closure,
            do_render,
            sync_push,
            auto_yes,
            auto_no,
            compose_exec,
            write_rendered_compose_to_stack_root,
        )
        return

    if not target:
        hosts = available_hosts()
        if not hosts:
            raise click.UsageError("No stack hosts found under docker/stacks.")

        out.header("Deploying full stack on all hosts")
        for host in hosts:
            _deploy_full_stack(
                host,
                follow_logs,
                reconcile,
                do_render,
                sync_push,
                auto_yes,
                auto_no,
                compose_exec,
                write_rendered_compose_to_stack_root,
            )
        out.ok("Deployed full stack on all hosts.")
        return

    # `tctl deploy <target>` — could be a host (full stack) or a service.
    if stack_compose(target).is_file():
        _deploy_full_stack(
            target,
            follow_logs,
            reconcile,
            do_render,
            sync_push,
            auto_yes,
            auto_no,
            compose_exec,
            write_rendered_compose_to_stack_root,
        )
        return

    # Service-only mode: deploy to all stacks containing it.
    for host in service_stacks(target):
        _deploy_one(
            host,
            target,
            follow_logs,
            reconcile,
            service_closure,
            do_render,
            sync_push,
            auto_yes,
            auto_no,
            compose_exec,
            write_rendered_compose_to_stack_root,
        )


def _deploy_full_stack(
    host,
    follow_logs,
    reconcile_fn,
    render_fn,
    push_fn,
    auto_yes,
    auto_no,
    exec_fn,
    write_compose_fn,
):
    out.header(f"Deploying full stack on {host}")

    changes = reconcile_fn(host)
    for c in changes:
        out.info(c)

    _handle_orphan_containers(host, exec_fn, auto_yes, auto_no)

    out.phase("Rendering templates...")
    render_fn(host)

    out.phase("Pushing files for all stack services...")
    pushed_services: set[str] = set()
    push_fn(host, yes=auto_yes, no=auto_no, pushed_services=pushed_services)

    out.phase("Writing rendered compose.yaml to remote stack data root...")
    remote_compose = write_compose_fn(host)
    out.info(f"Rendered compose written: {host}:{remote_compose}")

    out.phase("Running docker compose up for full stack...")
    rc = exec_fn(host, ("up", "-d"))
    if rc != 0:
        raise SystemExit(f"Docker compose up failed for stack '{host}'.")

    _restart_services_after_push(host, pushed_services, exec_fn)

    if follow_logs:
        out.phase("Following stack logs...")
        log_rc = exec_fn(host, ("logs", "-f", "--tail", "50"))
        if log_rc not in (0, 130):
            out.warn(f"docker compose logs exited with code {log_rc}.")

    out.ok(f"Deployed full stack '{host}'.")


def _handle_orphan_containers(
    host: str, exec_fn, auto_yes: bool, auto_no: bool
) -> None:
    """Detect and prompt for removal of orphaned containers."""
    from tctl.compose import detect_orphan_containers

    orphans = detect_orphan_containers(host)
    if not orphans:
        return

    out.warn(f"Found orphaned container(s): {', '.join(orphans)}")

    if auto_no:
        out.info("Skipping orphan removal (--no flag set)")
        return

    if auto_yes:
        out.phase(f"Removing orphaned container(s): {', '.join(orphans)}")
        rc = exec_fn(host, ("rm", "-f", *orphans))
        if rc != 0:
            out.warn("Failed to remove all orphaned containers")
        return

    # Interactive prompt
    should_remove = click.confirm(
        f"Remove {len(orphans)} orphaned container(s)?", default=False
    )
    if should_remove:
        out.phase(f"Removing orphaned container(s): {', '.join(orphans)}")
        rc = exec_fn(host, ("rm", "-f", *orphans))
        if rc != 0:
            out.warn("Failed to remove all orphaned containers")
    else:
        out.info("Skipped orphan removal")


def _restart_services_after_push(host: str, pushed_services: set[str], exec_fn) -> None:
    if not pushed_services:
        return

    restart_targets = sorted(pushed_services)
    out.phase("Restarting services with pushed file changes...")
    rc = exec_fn(host, ("restart", *restart_targets))
    if rc != 0:
        raise SystemExit(
            "Docker compose restart failed for pushed services "
            f"on '{host}': {' '.join(restart_targets)}"
        )


def _deploy_one(
    host,
    service,
    follow_logs,
    reconcile_fn,
    closure_fn,
    render_fn,
    push_fn,
    auto_yes,
    auto_no,
    exec_fn,
    write_compose_fn,
):
    out.header(f"Deploying {service} on {host}")

    changes = reconcile_fn(host)
    for c in changes:
        out.info(c)

    _handle_orphan_containers(host, exec_fn, auto_yes, auto_no)

    try:
        deploy_services = closure_fn(host, service)
    except SystemExit:
        out.warn(f"Dependency resolution failed; falling back to '{service}' only.")
        deploy_services = [service]

    out.meta(f"Deployment set: {' '.join(deploy_services)}")

    out.phase("Rendering templates...")
    render_fn(host)

    out.phase("Pushing files (service + dependencies)...")
    pushed_services: set[str] = set()
    for dep in deploy_services:
        push_fn(host, dep, yes=auto_yes, no=auto_no, pushed_services=pushed_services)

    out.phase("Writing rendered compose.yaml to remote stack data root...")
    remote_compose = write_compose_fn(host)
    out.info(f"Rendered compose written: {host}:{remote_compose}")

    out.phase("Running docker compose up...")
    rc = exec_fn(host, ("up", "-d", *deploy_services))
    if rc != 0:
        raise SystemExit(f"Docker compose up failed for stack '{host}'.")

    _restart_services_after_push(host, pushed_services, exec_fn)

    if follow_logs:
        out.phase("Following deployed service logs...")
        log_rc = exec_fn(host, ("logs", "-f", "--tail", "50", *deploy_services))
        if log_rc not in (0, 130):
            out.warn(f"docker compose logs exited with code {log_rc}.")

    out.ok("Deployed.")


# ── push ─────────────────────────────────────────────────────────────


@cli.command()
@click.argument("target", required=False, default=None)
@click.pass_context
def push(ctx: click.Context, target: str | None) -> None:
    """Push local service files to a remote host.

    \b
    tctl -m push                push all mothership services
    tctl -m push authelia       push authelia to mothership
    """
    from tctl.sync import push as sync_push

    _run_env_sync()

    host = _optional_stack(ctx)

    if host:
        out.header(f"Push → {host}" + (f" ({target})" if target else ""))
        sync_push(host, target)
        return

    if target and stack_compose(target).is_file():
        # `tctl push nexus` — target is a host.
        out.header(f"Push → {target}")
        sync_push(target)
        return

    _raise_stack_required()


# ── diff ─────────────────────────────────────────────────────────────


@cli.command("diff")
@click.argument("target", required=False, default=None)
@click.pass_context
def diff_cmd(ctx: click.Context, target: str | None) -> None:
    """Show local/remote drift without pushing files.

    \b
    tctl -m diff                diff all mothership services
    tctl -m diff authelia       diff authelia on mothership
    tctl diff nexus             diff all services on nexus
    """
    from tctl.compose import compose_drift
    from tctl.sync import push as sync_push

    host = _optional_stack(ctx)

    if host:
        out.header(f"Diff ↔ {host}" + (f" ({target})" if target else ""))
        sync_push(host, target, dry_run=True)
        has_drift, remote_compose, local_digest, remote_digest = compose_drift(host)
        if has_drift:
            out.warn(
                "Remote compose drift detected: "
                f"{host}:{remote_compose} (local {local_digest[:12]} != remote {remote_digest[:12]})"
            )
        else:
            out.ok(f"Remote compose in sync: {host}:{remote_compose}")
        return

    if target and stack_compose(target).is_file():
        # `tctl diff nexus` — target is a host.
        out.header(f"Diff ↔ {target}")
        sync_push(target, dry_run=True)
        has_drift, remote_compose, local_digest, remote_digest = compose_drift(target)
        if has_drift:
            out.warn(
                "Remote compose drift detected: "
                f"{target}:{remote_compose} (local {local_digest[:12]} != remote {remote_digest[:12]})"
            )
        else:
            out.ok(f"Remote compose in sync: {target}:{remote_compose}")
        return

    _raise_stack_required()


# ── pull ─────────────────────────────────────────────────────────────


@cli.command()
@click.option(
    "-y",
    "--yes",
    is_flag=True,
    default=False,
    help="Auto-approve all pull confirmations.",
)
@click.argument("target", required=False, default=None)
@click.pass_context
def pull(ctx: click.Context, yes: bool, target: str | None) -> None:
    """Pull remote service files to local copy.

    \b
    tctl -m pull                pull all mothership services
    tctl -m pull -y             pull all mothership services without prompts
    tctl -m pull authelia       pull authelia from mothership
    """
    from tctl.sync import pull as sync_pull

    _run_env_sync()

    host = _optional_stack(ctx)

    if host:
        out.header(f"Pull ← {host}" + (f" ({target})" if target else ""))
        sync_pull(host, target, yes=yes)
        return

    if target and stack_compose(target).is_file():
        out.header(f"Pull ← {target}")
        sync_pull(target, yes=yes)
        return

    _raise_stack_required()


# ── render ───────────────────────────────────────────────────────────


@cli.command()
@click.argument("host", required=False, default=None)
@click.pass_context
def render(ctx: click.Context, host: str | None) -> None:
    """Render .template files using global + per-stack environment.

    \b
    tctl render             render all stack templates
    tctl render nexus       render nexus templates
    tctl -m render          render mothership templates
    """
    from tctl.render import render as do_render

    _run_env_sync()

    target = _optional_stack(ctx) or host
    scope = target or "all stacks"
    out.header(f"Rendering templates ({scope})")
    do_render(target)


# ── fmt ──────────────────────────────────────────────────────────────


@cli.command("fmt")
@click.option(
    "--check",
    is_flag=True,
    default=False,
    help="Report compose manifests that would change without rewriting them.",
)
@click.option(
    "--yaml",
    "format_yaml",
    is_flag=True,
    default=False,
    help="Also format YAML files across the repository with Prettier.",
)
@click.argument("targets", nargs=-1, type=click.Path(path_type=str))
def fmt_cmd(check: bool, format_yaml: bool, targets: tuple[str, ...]) -> None:
    """Format compose manifests and optionally repository YAML files."""
    from pathlib import Path

    from tctl.fmt import fmt_compose_manifests

    try:
        fmt_compose_manifests(
            targets=tuple(Path(target) for target in targets), check=check
        )
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc

    if not format_yaml:
        return

    prettier = shutil.which("prettier")
    if prettier is None:
        raise click.ClickException(
            "Prettier is not installed or not available on PATH."
        )

    mode = "--check" if check else "--write"
    result = subprocess.run(
        [prettier, mode, "**/*.{yaml,yml}"],
        cwd=Path.cwd(),
        check=False,
        text=True,
        capture_output=False,
    )

    if result.returncode != 0:
        raise SystemExit(result.returncode)


# ── ls ───────────────────────────────────────────────────────────────


@cli.command()
@click.pass_context
def ls(ctx: click.Context) -> None:
    """List stacks and their services."""
    from rich.table import Table

    from tctl.compose import stack_services

    host = _optional_stack(ctx)
    hosts = [host] if host else available_hosts()

    table = Table(title="Stacks & Services", show_lines=True)
    table.add_column("Stack", style="bold cyan", min_width=12)
    table.add_column("Services", style="white")

    for h in hosts:
        services = stack_services(h)
        table.add_row(h, ", ".join(services))

    out.console.print(table)


# ── check ───────────────────────────────────────────────────────────


@cli.command("check")
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    default=False,
    help="Show detailed per-check output for each service.",
)
@click.option(
    "--insecure-routes",
    is_flag=True,
    default=False,
    help="Skip TLS certificate verification for HTTPS route probes.",
)
@click.argument("scope", required=False, default=None)
@click.argument("service", required=False, default=None)
@click.pass_context
def check_cmd(
    ctx: click.Context,
    verbose: bool,
    insecure_routes: bool,
    scope: str | None,
    service: str | None,
) -> None:
    """Run comprehensive preflight-like service checks.

    \b
    tctl check                          check all services in all stacks
    tctl check mothership               check all services in mothership
    tctl check nexus blocky             check one service in a stack
    tctl check service blocky           check one service across matching stacks
    """
    from tctl.check import run_checks

    explicit_stack = _optional_stack(ctx)
    try:
        has_failures = run_checks(
            explicit_stack=explicit_stack,
            scope=scope,
            service=service,
            verbose=verbose,
            verify_tls=not insecure_routes,
        )
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc

    if has_failures:
        raise SystemExit(1)


# ── setup ────────────────────────────────────────────────────────────


@cli.command()
def setup() -> None:
    """Install local prerequisites (macOS/Homebrew)."""
    import platform

    if platform.system() != "Darwin":
        out.err("This setup command supports macOS only.")
        out.err("Install docker, docker compose, and gettext manually.")
        raise SystemExit(1)

    if not shutil.which("brew"):
        out.phase("Installing Homebrew...")
        subprocess.run(
            [
                "/bin/bash",
                "-c",
                "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)",
            ],
            check=True,
        )

    out.phase("Installing/upgrading tooling via Homebrew...")
    subprocess.run(
        ["brew", "install", "docker", "docker-compose", "gettext"], check=True
    )

    out.ok("Setup complete.")
    for cmd in [
        "docker --version",
        "docker compose version",
        "envsubst --version | head -1",
    ]:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        out.info(result.stdout.strip())
