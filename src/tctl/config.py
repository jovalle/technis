"""Project paths, environment loading, and host resolution."""

from __future__ import annotations

import os
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
STACKS_DIR = REPO_ROOT / "docker" / "stacks"
SERVICES_DIR = REPO_ROOT / "docker" / "services"
ENV_FILE = REPO_ROOT / "docker" / ".env"
CACHE_DIR = REPO_ROOT / "tmp" / ".tctl"

HOST_ALIASES: dict[str, str] = {"m": "mothership", "n": "nexus"}

SSH_OPTS: tuple[str, ...] = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=10")


def available_hosts() -> list[str]:
    """Return sorted list of host names from docker/stacks/."""
    if not STACKS_DIR.is_dir():
        return []
    return sorted(d.name for d in STACKS_DIR.iterdir() if d.is_dir())


def resolve_host(alias: str | None) -> str | None:
    """Resolve a host alias (m/n) or name to a full host name."""
    if alias is None:
        return None
    return HOST_ALIASES.get(alias, alias)


def stack_compose(host: str) -> Path:
    return STACKS_DIR / host / "compose.yaml"


def stack_env_file(host: str) -> Path:
    return STACKS_DIR / host / ".env"


def stack_services_dir(host: str) -> Path:
    return STACKS_DIR / host / "services"


# ── Env loading ──────────────────────────────────────────────────────

_VAR_RE = re.compile(r"\$\{([^}]+)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse a KEY=VALUE env file (handles single/double quotes)."""
    env: dict[str, str] = {}
    if not path.is_file():
        return env
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        env[key] = value
    return env


def _expand_vars(env: dict[str, str]) -> dict[str, str]:
    """Expand ${VAR} and $VAR references within env values."""
    for _ in range(10):
        changed = False
        for key, value in env.items():

            def _replace(m: re.Match[str]) -> str:
                var = m.group(1) or m.group(2)
                return env.get(var, m.group(0))

            new = _VAR_RE.sub(_replace, value)
            if new != value:
                env[key] = new
                changed = True
        if not changed:
            break
    return env


def load_env(host: str | None = None) -> dict[str, str]:
    """Load and merge global + stack env files. Returns merged dict."""
    env = _parse_env_file(ENV_FILE)
    if host:
        stack = _parse_env_file(stack_env_file(host))
        env.update(stack)
    return _expand_vars(env)


def subprocess_env(host: str | None = None) -> dict[str, str]:
    """Return a full environment dict for subprocess calls."""
    merged = dict(os.environ)
    merged.update(load_env(host))
    return merged


def base_dir(host: str) -> str:
    """Resolve the remote data base path for a host (STACK_ROOT_DIR or STACK_DATA_ROOT)."""
    env = load_env(host)
    path = env.get("STACK_ROOT_DIR") or env.get("STACK_DATA_ROOT")
    if not path:
        raise SystemExit(
            f"Missing STACK_ROOT_DIR (or STACK_DATA_ROOT) in {stack_env_file(host)}"
        )
    return path
