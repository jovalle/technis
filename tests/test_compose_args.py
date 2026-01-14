from __future__ import annotations

from tctl.compose_args import compose_primary_subcommand, compose_subcommand
from tctl.fmt_compose import detect_compose_command


def test_compose_subcommand_skips_global_flags_with_values() -> None:
    args = (
        "--project-name",
        "demo",
        "-f",
        "docker/stacks/nexus/compose.yaml",
        "ps",
    )

    assert compose_subcommand(args) == ("ps", 4)
    assert compose_primary_subcommand(args) == "ps"


def test_compose_subcommand_handles_equals_style_global_flags() -> None:
    args = (
        "--project-name=demo",
        "--file=docker/stacks/nexus/compose.yaml",
        "logs",
        "blocky",
    )

    assert compose_subcommand(args) == ("logs", 2)
    assert compose_primary_subcommand(args) == "logs"


def test_detect_compose_command_with_prefixed_global_flags() -> None:
    args = (
        "--project-name",
        "demo",
        "--file",
        "docker/stacks/nexus/compose.yaml",
        "images",
    )

    assert detect_compose_command(args) == "images"
