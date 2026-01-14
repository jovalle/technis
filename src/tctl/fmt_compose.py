"""Beautify docker compose output with Rich formatting."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from rich.markup import escape as rich_escape
from rich.table import Table
from rich.text import Text

from tctl.compose_args import compose_primary_subcommand
from tctl.output import console, err_console

# ── Symbols ──────────────────────────────────────────────────────────
_S = {
    "running": "●",
    "healthy": "✔",
    "unhealthy": "✖",
    "exited": "○",
    "restarting": "↻",
    "paused": "⏸",
    "created": "◌",
    "dead": "✖",
    "removing": "⌛",
    # lifecycle
    "started": "▶",
    "stopped": "■",
    "created_evt": "✦",
    "recreated": "↻",
    "pulled": "⬇",
    "removed": "✕",
    "waiting": "⏳",
    "skipped": "⏭",
    # log levels
    "info": "ℹ",
    "warn": "▲",
    "error": "✖",
    "debug": "⋯",
    "trace": "·",
}

# ── Color maps ───────────────────────────────────────────────────────
_STATE_STYLE = {
    "running": "bold green",
    "healthy": "bold green",
    "unhealthy": "bold red",
    "exited": "red",
    "restarting": "bold yellow",
    "paused": "yellow",
    "created": "dim",
    "dead": "bold red",
    "removing": "dim yellow",
}

_LOG_LEVEL_STYLE = {
    "info": "cyan",
    "warn": "bold yellow",
    "warning": "bold yellow",
    "error": "bold red",
    "fatal": "bold red",
    "debug": "dim",
    "trace": "dim",
}

# ── Age formatting ───────────────────────────────────────────────────


def _human_age(created_str: str) -> str:
    """Convert docker's CreatedAt string to a short human-readable age."""
    # Docker format: "2026-04-06 14:50:34 -0400 EDT"
    # Strip timezone name at end, parse offset.
    try:
        # Remove trailing tz name (EDT, UTC, etc.)
        cleaned = re.sub(r"\s+[A-Z]{2,5}$", "", created_str.strip())
        dt = datetime.strptime(cleaned, "%Y-%m-%d %H:%M:%S %z")
        delta = datetime.now(timezone.utc) - dt
        secs = int(delta.total_seconds())
        if secs < 0:
            return "just now"
        if secs < 60:
            return f"{secs}s"
        mins = secs // 60
        if mins < 60:
            return f"{mins}m"
        hours = mins // 60
        if hours < 24:
            return f"{hours}h {mins % 60}m"
        days = hours // 24
        if days < 30:
            return f"{days}d {hours % 24}h"
        return f"{days}d"
    except (ValueError, TypeError):
        return created_str


def _short_image(image: str) -> str:
    """Abbreviate image name: strip docker.io prefix, keep repo:tag."""
    image = re.sub(r"^(docker\.io/|ghcr\.io/|gcr\.io/)", "", image)
    # If it's library/foo:tag → foo:tag
    image = re.sub(r"^library/", "", image)
    return image


def _short_id(cid: str) -> str:
    """First 12 chars of container ID."""
    return cid[:12] if cid else ""


def _format_ports(publishers: list[dict]) -> str:
    """Format Publishers list into compact port mapping string, deduplicating IPv4/v6."""
    if not publishers:
        return ""
    seen: set[str] = set()
    parts: list[str] = []
    for p in publishers:
        url = p.get("URL", "")
        pub = p.get("PublishedPort", 0)
        tgt = p.get("TargetPort", 0)
        proto = p.get("Protocol", "tcp")

        # Skip pure IPv6 duplicates of the same mapping
        if url == "::":
            continue

        if pub and tgt:
            if url and url not in ("0.0.0.0", "::"):
                entry = f"{url}:{pub}→{tgt}"
            else:
                entry = f"{pub}→{tgt}"
        elif tgt:
            entry = f"{tgt}"
        else:
            continue

        if proto != "tcp":
            entry += f"/{proto}"

        if entry not in seen:
            seen.add(entry)
            parts.append(entry)
    return ", ".join(parts)


def _format_ports_full(publishers: list[dict]) -> list[str]:
    """Format Publishers list into full mapping lines."""
    if not publishers:
        return []

    parts: list[str] = []
    seen: set[str] = set()
    for p in publishers:
        url = p.get("URL", "")
        pub = p.get("PublishedPort", 0)
        tgt = p.get("TargetPort", 0)
        proto = p.get("Protocol", "tcp")
        mode = p.get("PublishMode", "")

        if pub and tgt:
            if url:
                entry = f"{url}:{pub}->{tgt}/{proto}"
            else:
                entry = f"{pub}->{tgt}/{proto}"
        elif tgt:
            entry = f"{tgt}/{proto}"
        else:
            continue

        if mode:
            entry += f" ({mode})"

        if entry not in seen:
            seen.add(entry)
            parts.append(entry)

    return parts


def _size_human(size_str: str) -> str:
    """Pass through docker's size string (already human-readable)."""
    return size_str


# ── ps formatter ─────────────────────────────────────────────────────


def _status_parts(container: dict) -> tuple[str, str, str]:
    """Resolve status icon, status style, and status text for a container."""
    state = container.get("State", "unknown").lower()
    health = container.get("Health", "").lower()

    if health == "healthy":
        sym = _S["healthy"]
        style = _STATE_STYLE["healthy"]
        status_text = "healthy"
    elif health == "unhealthy":
        sym = _S["unhealthy"]
        style = _STATE_STYLE["unhealthy"]
        status_text = "unhealthy"
    elif state in _STATE_STYLE:
        sym = _S.get(state, "?")
        style = _STATE_STYLE[state]
        status_text = state
    else:
        sym = "?"
        style = "white"
        status_text = state

    exit_code = container.get("ExitCode", 0)
    if state == "exited" and exit_code != 0:
        status_text = f"exit({exit_code})"
        sym = _S["dead"]
        style = "bold red"

    return sym, style, status_text


def _print_full_field(label: str, value: str, *, dim_if_empty: bool = True) -> None:
    """Print one aligned key/value field for full ps output."""
    line = Text(f"    {label:<9}: ", style="dim")
    if value:
        line.append(value)
    else:
        line.append("—", style="dim" if dim_if_empty else "")
    console.print(line)


def _format_ps_full(containers: list[dict]) -> None:
    """Render multiline full-length ps output for easier vertical scanning."""
    ordered = sorted(containers, key=lambda x: x.get("Service", ""))

    for i, c in enumerate(ordered):
        sym, style, status_text = _status_parts(c)
        service = c.get("Service", c.get("Name", "?"))

        header = Text()
        header.append(f"{sym} ", style=style)
        header.append(service, style="bold white")
        console.print(header)

        _print_full_field("status", status_text, dim_if_empty=False)
        _print_full_field("state", c.get("State", ""))
        _print_full_field("health", c.get("Health", ""))
        _print_full_field("age", _human_age(c.get("CreatedAt", "")))
        _print_full_field("created", c.get("CreatedAt", ""))
        _print_full_field("image", c.get("Image", ""))
        _print_full_field("container", c.get("Name", ""))
        _print_full_field("project", c.get("Project", ""))
        _print_full_field("id", c.get("ID", ""))
        _print_full_field("command", c.get("Command", ""))

        ports = _format_ports_full(c.get("Publishers", []))
        if ports:
            console.print(f"    [dim]{'ports':<9}[/]:")
            for port in ports:
                console.print(f"      {port}")
        else:
            _print_full_field("ports", "")

        if i < len(ordered) - 1:
            console.print()


def format_ps(stdout: str, stderr: str, *, full: bool = False) -> None:
    """Parse `docker compose ps --format json` JSONL and render a Rich table."""
    containers = _parse_jsonl(stdout)
    if not containers:
        if stderr.strip():
            err_console.print(f"  [bold red]✖[/] {stderr.strip()}")
        else:
            console.print("  [dim]│[/]     No containers found.")
        return

    if full:
        _format_ps_full(containers)
        return

    table = Table(
        show_header=True,
        header_style="bold",
        show_lines=False,
        pad_edge=False,
        expand=False,
    )
    table.add_column("Service", style="bold white", min_width=14)
    table.add_column("Status", min_width=8)
    table.add_column("Age", min_width=6, justify="right")
    table.add_column("Image", style="dim")
    table.add_column("Ports", style="cyan")
    table.add_column("ID", style="dim", min_width=12)

    for c in sorted(containers, key=lambda x: x.get("Service", "")):
        service = c.get("Service", c.get("Name", "?"))
        image = _short_image(c.get("Image", ""))
        age = _human_age(c.get("CreatedAt", ""))
        ports = _format_ports(c.get("Publishers", []))
        cid = _short_id(c.get("ID", ""))

        sym, style, status_text = _status_parts(c)

        service_text = Text()
        service_text.append(f"{sym} ", style=style)
        service_text.append(service, style="bold white")

        table.add_row(
            service_text,
            Text(status_text, style=style),
            age,
            image,
            ports if ports else Text("—", style="dim"),
            cid,
        )

    console.print(table)


# ── images formatter ─────────────────────────────────────────────────


def format_images(stdout: str, stderr: str) -> None:
    """Parse `docker compose images --format json` and render a Rich table."""
    try:
        images = json.loads(stdout) if stdout.strip() else []
    except json.JSONDecodeError:
        images = _parse_jsonl(stdout)

    if not images:
        if stderr.strip():
            err_console.print(f"  [bold red]✖[/] {stderr.strip()}")
        else:
            console.print("  [dim]│[/]     No images found.")
        return

    table = Table(
        show_header=True,
        header_style="bold",
        show_lines=False,
        pad_edge=False,
        expand=False,
    )
    table.add_column("Container", style="bold white")
    table.add_column("Repository", style="cyan")
    table.add_column("Tag", style="yellow")
    table.add_column("Size", justify="right", style="green")
    table.add_column("Platform", style="dim")
    table.add_column("ID", style="dim")

    for img in sorted(images, key=lambda x: x.get("ContainerName", "")):
        size_bytes = img.get("Size", 0)
        if isinstance(size_bytes, int) and size_bytes > 0:
            if size_bytes >= 1_073_741_824:
                size = f"{size_bytes / 1_073_741_824:.1f} GB"
            elif size_bytes >= 1_048_576:
                size = f"{size_bytes / 1_048_576:.1f} MB"
            elif size_bytes >= 1024:
                size = f"{size_bytes / 1024:.1f} KB"
            else:
                size = f"{size_bytes} B"
        else:
            size = str(size_bytes)

        table.add_row(
            img.get("ContainerName", ""),
            _short_image(img.get("Repository", "")),
            img.get("Tag", "latest"),
            size,
            img.get("Platform", ""),
            _short_id(img.get("ID", "").replace("sha256:", "")),
        )

    console.print(table)


# ── up/down/restart lifecycle formatter ──────────────────────────────

_LIFECYCLE_RE = re.compile(
    r"^\s*(?:DRY-RUN MODE\s*-\s*)?"
    r"(?:Container|Network|Volume|Image)\s+"
    r"(\S+)\s+"
    r"(Creat(?:ed|ing)|Start(?:ed|ing)|Stopp(?:ed|ing)|Remov(?:ed|ing)|"
    r"Recreat(?:ed|ing)|Pull(?:ed|ing)|Running|Waiting|Healthy|Skipped|Stopped|"
    r"Error|Killed|Restarted|Started|Removed)\s*$",
    re.IGNORECASE,
)

_LIFECYCLE_STYLE: dict[str, tuple[str, str]] = {
    "created": (_S["created_evt"], "bold cyan"),
    "creating": (_S["created_evt"], "dim cyan"),
    "started": (_S["started"], "bold green"),
    "starting": (_S["started"], "dim green"),
    "stopped": (_S["stopped"], "yellow"),
    "stopping": (_S["stopped"], "dim yellow"),
    "removed": (_S["removed"], "red"),
    "removing": (_S["removed"], "dim red"),
    "recreated": (_S["recreated"], "bold blue"),
    "recreating": (_S["recreated"], "dim blue"),
    "pulled": (_S["pulled"], "bold magenta"),
    "pulling": (_S["pulled"], "dim magenta"),
    "running": (_S["running"], "green"),
    "waiting": (_S["waiting"], "dim"),
    "healthy": (_S["healthy"], "bold green"),
    "skipped": (_S["skipped"], "dim"),
    "error": (_S["error"], "bold red"),
    "killed": (_S["dead"], "bold red"),
    "restarted": (_S["recreated"], "bold green"),
}


def format_lifecycle(combined: str, dry_run: bool = False) -> None:
    """Parse docker compose up/down/restart output and render styled lines."""
    for raw_line in combined.splitlines():
        format_lifecycle_line(raw_line, dry_run=dry_run)


def format_lifecycle_line(raw_line: str, dry_run: bool = False) -> None:
    """Parse and render a single lifecycle output line."""
    line = raw_line.strip()
    if not line:
        return

    prefix = "[dim]DRY[/] " if dry_run else ""
    m = _LIFECYCLE_RE.match(line)
    if m:
        resource = m.group(1)
        action = m.group(2).lower()
        sym, style = _LIFECYCLE_STYLE.get(action, ("·", "white"))
        console.print(
            f"  ├─ {prefix}[{style}]{sym}[/] [{style}]{resource}[/] [dim]{action}[/]"
        )
    else:
        # Fallback: print as-is with dim styling
        console.print(f"  [dim]│[/]     {line}")


# ── logs formatter ───────────────────────────────────────────────────

_LOG_PREFIX_RE = re.compile(r"^(\S+)\s+\|\s*(.*)$")
_JSON_LOG_RE = re.compile(r"^\{.*\}$")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_KV_LEVEL_RE = re.compile(r"\blevel=(\w+)")
_TEMP_CONTAINER_PREFIX_RE = re.compile(r"^[0-9a-f]{12}_(.+)$")
_KV_TIME_RE = re.compile(
    r"\bt=(\d{4}-\d{2}-\d{2}T[\d:.]+(?:Z|[+-]\d{2}:?\d{2})?)"
    r"|\btime=\"?(\d{4}-\d{2}-\d{2}T[\d:.]+(?:Z|[+-]\d{2}:?\d{2})?)"
)
_KV_MSG_RE = re.compile(r'\bmsg="([^"]*)"')
_ISO_TS_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T[\d:.]+(?:Z|[+-]\d{2}:?\d{2})?)\b")
_LOCAL_TZ = datetime.now().astimezone().tzinfo or timezone.utc

# Assign deterministic colors to service names.
_SVC_COLORS = [
    "cyan",
    "green",
    "yellow",
    "blue",
    "magenta",
    "red",
    "bright_cyan",
    "bright_green",
    "bright_yellow",
    "bright_blue",
    "bright_magenta",
]
_svc_color_map: dict[str, str] = {}

_STACK_COLORS = [
    "bright_cyan",
    "bright_green",
    "bright_yellow",
    "bright_blue",
    "bright_magenta",
    "bright_red",
    "cyan",
    "green",
    "yellow",
    "blue",
    "magenta",
]
_stack_color_map: dict[str, str] = {}


def _svc_color(name: str) -> str:
    if name not in _svc_color_map:
        _svc_color_map[name] = _SVC_COLORS[len(_svc_color_map) % len(_SVC_COLORS)]
    return _svc_color_map[name]


def _stack_color(name: str) -> str:
    if name not in _stack_color_map:
        _stack_color_map[name] = _STACK_COLORS[
            len(_stack_color_map) % len(_STACK_COLORS)
        ]
    return _stack_color_map[name]


def _stack_prefix(stack: str | None) -> str:
    if not stack:
        return ""
    style = _stack_color(stack)
    return f"[{style}]{stack}[/] "


def _normalize_log_source(raw: str) -> str:
    """Strip temporary compose recreate prefixes like '<12hex>_<name>'."""
    m = _TEMP_CONTAINER_PREFIX_RE.match(raw)
    if m:
        return m.group(1)
    return raw


def _parse_log_timestamp(raw: str) -> datetime | None:
    """Parse an ISO-like timestamp and return a timezone-aware datetime."""
    if not raw:
        return None

    value = str(raw).strip().strip('"')
    if not value:
        return None

    if value.endswith("Z"):
        value = f"{value[:-1]}+00:00"
    if re.search(r"[+-]\d{4}$", value):
        value = f"{value[:-5]}{value[-5:-2]}:{value[-2:]}"

    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_LOCAL_TZ)
    return dt


def _format_short_local_time(raw: str) -> str:
    """Render an incoming timestamp as HH:MM:SS in local timezone."""
    dt = _parse_log_timestamp(raw)
    if dt is not None:
        return dt.astimezone(_LOCAL_TZ).strftime("%H:%M:%S")

    cleaned = str(raw).strip().strip('"')
    if "T" in cleaned:
        _, _, tail = cleaned.partition("T")
        return tail[:8] if len(tail) >= 8 else cleaned
    return cleaned[-8:] if len(cleaned) >= 8 else cleaned


def _extract_timestamp_from_body(body: str) -> datetime | None:
    """Extract best-effort timestamp from known log formats."""
    if not body:
        return None

    if body.startswith("{") and _JSON_LOG_RE.match(body):
        try:
            log_json = json.loads(body)
        except json.JSONDecodeError:
            log_json = None
        if isinstance(log_json, dict):
            for key in ("time", "timestamp", "t", "ts"):
                parsed = _parse_log_timestamp(str(log_json.get(key, "")))
                if parsed is not None:
                    return parsed

    time_m = _KV_TIME_RE.search(body)
    if time_m:
        raw = time_m.group(1) or time_m.group(2)
        parsed = _parse_log_timestamp(raw)
        if parsed is not None:
            return parsed

    iso_m = _ISO_TS_PREFIX_RE.match(body)
    if iso_m:
        return _parse_log_timestamp(iso_m.group(1))

    return None


def extract_log_timestamp(line: str) -> datetime | None:
    """Extract timestamp from a full compose log line for cross-host ordering."""
    m = _LOG_PREFIX_RE.match(line)
    body = m.group(2).strip() if m else line.strip()
    return _extract_timestamp_from_body(body)


def _format_json_log(svc: str, log_json: dict, stack: str | None = None) -> None:
    """Format a structured JSON log line beautifully."""
    level = str(log_json.get("level", log_json.get("severity", "info"))).lower()
    msg = str(log_json.get("msg", log_json.get("message", "")))
    time_str = log_json.get("time", log_json.get("timestamp", ""))

    # Compact time: just HH:MM:SS
    short_time = ""
    if time_str:
        short_time = _format_short_local_time(str(time_str))

    sym = _S.get(level, _S.get("info"))
    level_style = _LOG_LEVEL_STYLE.get(level, "white")
    svc_style = _svc_color(svc)

    # Build extra fields (skip msg, level, time, timestamp)
    skip_keys = {"msg", "message", "level", "severity", "time", "timestamp", "t", "ts"}
    extras = []
    for k, v in log_json.items():
        if k in skip_keys:
            continue
        extras.append(f"[dim]{rich_escape(str(k))}=[/]{rich_escape(str(v))}")

    time_part = f"[dim]{short_time}[/] " if short_time else ""
    extra_part = f" {' '.join(extras)}" if extras else ""
    safe_svc = rich_escape(svc)
    safe_msg = rich_escape(msg)

    stack_part = _stack_prefix(stack)
    console.print(
        f"{stack_part}[{svc_style}]{safe_svc}[/] {time_part}"
        f"[{level_style}]{sym}[/] {safe_msg}{extra_part}"
    )


def _format_kv_log(svc: str, text: str, stack: str | None = None) -> None:
    """Format a key=value structured log line (Grafana/logfmt style)."""
    svc_style = _svc_color(svc)

    # Extract level
    level_m = _KV_LEVEL_RE.search(text)
    level = level_m.group(1).lower() if level_m else "info"

    # Extract time
    time_m = _KV_TIME_RE.search(text)
    short_time = ""
    if time_m:
        raw = time_m.group(1) or time_m.group(2)
        short_time = _format_short_local_time(raw)

    # Extract msg
    msg_m = _KV_MSG_RE.search(text)
    msg = msg_m.group(1) if msg_m else ""

    # Collect remaining key=value pairs (skip extracted ones)
    clean = _ANSI_RE.sub("", text)
    # Remove the keys we already extracted for a cleaner extras line
    extras_text = clean
    for pattern in (_KV_LEVEL_RE, _KV_TIME_RE, _KV_MSG_RE):
        extras_text = pattern.sub("", extras_text)
    # Clean up leftover logger= , t= prefixes from logfmt
    extras = " ".join(extras_text.split()).strip()

    sym = _S.get(level, _S.get("info"))
    level_style = _LOG_LEVEL_STYLE.get(level, "white")
    time_part = f"[dim]{short_time}[/] " if short_time else ""
    msg_part = f"{rich_escape(msg)} " if msg else ""
    extra_part = f"[dim]{rich_escape(extras)}[/]" if extras else ""
    safe_svc = rich_escape(svc)
    stack_part = _stack_prefix(stack)

    console.print(
        f"{stack_part}[{svc_style}]{safe_svc}[/] {time_part}"
        f"[{level_style}]{sym}[/] {msg_part}{extra_part}"
    )


def _format_plain_log(svc: str, text: str, stack: str | None = None) -> None:
    """Format a plain-text log line."""
    svc_style = _svc_color(svc)
    # Strip ANSI codes for cleaner output
    clean = _ANSI_RE.sub("", text)
    safe_svc = rich_escape(svc)
    safe_clean = rich_escape(clean)
    stack_part = _stack_prefix(stack)
    console.print(f"{stack_part}[{svc_style}]{safe_svc}[/] {safe_clean}")


def format_log_line(line: str, stack: str | None = None) -> None:
    """Format a single log line (used for streaming)."""
    m = _LOG_PREFIX_RE.match(line)
    if m:
        svc = _normalize_log_source(m.group(1))
        body = m.group(2).strip()
    else:
        svc = "?"
        body = line.strip()

    if not body:
        return

    # Try JSON log
    if body.startswith("{"):
        jm = _JSON_LOG_RE.match(body)
        if jm:
            try:
                log_json = json.loads(body)
                _format_json_log(svc, log_json, stack=stack)
                return
            except json.JSONDecodeError:
                pass

    # Try logfmt/key=value log (has level= or t= patterns)
    if _KV_LEVEL_RE.search(body):
        _format_kv_log(svc, body, stack=stack)
        return

    _format_plain_log(svc, body, stack=stack)


def format_logs_block(output: str, stack: str | None = None) -> None:
    """Format a block of log output (non-streaming)."""
    for line in output.splitlines():
        if line.strip():
            format_log_line(line, stack=stack)


# ── Helpers ──────────────────────────────────────────────────────────


def _parse_jsonl(text: str) -> list[dict]:
    """Parse newline-delimited JSON (JSONL)."""
    results = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                results.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return results


# ── Command detection ────────────────────────────────────────────────

# Commands where we intercept output for formatting.
_FORMATTED_COMMANDS: dict[str, str] = {
    "ps": "ps",
    "images": "images",
    "up": "lifecycle",
    "down": "lifecycle",
    "restart": "lifecycle",
    "start": "lifecycle",
    "stop": "lifecycle",
    "create": "lifecycle",
    "rm": "lifecycle",
    "pull": "lifecycle",
    "logs": "logs",
}


def detect_compose_command(args: tuple[str, ...]) -> str | None:
    """Detect the primary compose subcommand from args. Returns formatter key or None."""
    subcommand = compose_primary_subcommand(args)
    if subcommand in _FORMATTED_COMMANDS:
        return _FORMATTED_COMMANDS[subcommand]
    return None
