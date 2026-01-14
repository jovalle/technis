"""Render .template files using envsubst with merged environment."""

from __future__ import annotations

import subprocess
from pathlib import Path

from tctl import output as out
from tctl.compose import stack_services
from tctl.config import (
    SERVICES_DIR,
    STACKS_DIR,
    subprocess_env,
)


def _render_one(template: Path, env: dict[str, str]) -> bool:
    """Render a single .template file. Returns True if content changed."""
    output_path = template.with_suffix("")
    result = subprocess.run(
        ["envsubst"],
        input=template.read_text(),
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode != 0:
        out.err(f"envsubst failed for {template}: {result.stderr}")
        return False

    rendered = result.stdout
    if output_path.is_file() and output_path.read_text() == rendered:
        return False

    output_path.write_text(rendered)

    # Ensure rendered file is gitignored.
    gitignore = template.parent / ".gitignore"
    base = output_path.name
    if not gitignore.is_file() or base not in gitignore.read_text().splitlines():
        with gitignore.open("a") as f:
            f.write(f"{base}\n")

    return True


def render(host: str | None = None) -> int:
    """Render .template files under docker/. Returns count of updated files."""
    search_root = STACKS_DIR / host if host else STACKS_DIR
    if host and not search_root.is_dir():
        raise SystemExit(f"Unknown stack or missing directory: docker/{host}")

    scanned_count = 0
    changed_count = 0

    # Render stack-level templates.
    for template in sorted(search_root.rglob("*.template")):
        scanned_count += 1
        rel = template.relative_to(STACKS_DIR)
        stack_name = rel.parts[0]
        env = subprocess_env(stack_name)
        if _render_one(template, env):
            out.info(
                f"Rendered: {template.relative_to(STACKS_DIR.parent.parent)} (stack: {stack_name})"
            )
            changed_count += 1

    # In all-stacks mode, also render shared service templates using global env.
    if not host:
        env = subprocess_env()
        if "DOMAIN" not in env:
            for fallback in ("TRAEFIK_DOMAIN", "AUTHELIA_DOMAIN"):
                if fallback in env:
                    env["DOMAIN"] = env[fallback]
                    break

        for template in sorted(SERVICES_DIR.rglob("*.template")):
            scanned_count += 1
            if _render_one(template, env):
                out.info(
                    f"Rendered: {template.relative_to(STACKS_DIR.parent.parent)} (global)"
                )
                changed_count += 1

    # In host mode, also render shared service templates for included services.
    if host:
        env = subprocess_env(host)
        # Derive DOMAIN from existing keys if absent.
        if "DOMAIN" not in env:
            for fallback in ("TRAEFIK_DOMAIN", "AUTHELIA_DOMAIN"):
                if fallback in env:
                    env["DOMAIN"] = env[fallback]
                    break

        for svc in stack_services(host):
            svc_dir = SERVICES_DIR / svc
            if not svc_dir.is_dir():
                continue
            for template in sorted(svc_dir.rglob("*.template")):
                scanned_count += 1
                if _render_one(template, env):
                    out.info(
                        f"Rendered: {template.relative_to(STACKS_DIR.parent.parent)} (host: {host})"
                    )
                    changed_count += 1

    if scanned_count == 0:
        scope = f"docker/{host}/" if host else "docker/"
        out.info(f"No .template files found under {scope}.")
    elif changed_count == 0:
        out.ok(f"Templates already up to date ({scanned_count} scanned).")
    else:
        out.ok(f"Rendered {changed_count} template(s) ({scanned_count} scanned).")

    return changed_count
