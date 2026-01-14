"""Shared docker compose argument parsing helpers."""

from __future__ import annotations

COMPOSE_GLOBAL_FLAGS_WITH_VALUE = {
    "-f",
    "--file",
    "-p",
    "--project-name",
    "--profile",
    "--env-file",
    "--project-directory",
    "--parallel",
    "--progress",
    "--ansi",
}


def compose_subcommand(args: tuple[str, ...]) -> tuple[str | None, int]:
    """Return the compose subcommand and its index in args.

    Global compose flags (and their values) are skipped before detecting the
    first non-flag token.
    """
    index = 0
    while index < len(args):
        arg = args[index]

        if arg in COMPOSE_GLOBAL_FLAGS_WITH_VALUE:
            index += 2
            continue
        if arg.startswith("--"):
            if any(
                arg.startswith(f"{flag}=") for flag in COMPOSE_GLOBAL_FLAGS_WITH_VALUE
            ):
                index += 1
                continue
            index += 1
            continue
        if arg.startswith("-"):
            index += 1
            continue

        return arg, index

    return None, -1


def compose_primary_subcommand(args: tuple[str, ...]) -> str | None:
    """Return just the compose subcommand token, if present."""
    subcommand, _ = compose_subcommand(args)
    return subcommand
