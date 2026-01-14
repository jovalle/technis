from __future__ import annotations

from typing import Literal

from rich.console import Console
from rich.theme import Theme

_theme = Theme(
    {
        "header": "bold cyan",
        "phase": "cyan",
        "meta": "bold blue",
        "ok": "bold green",
        "warn": "bold yellow",
        "err": "bold red",
        "dim": "dim",
    }
)

console = Console(theme=_theme, highlight=False)
err_console = Console(theme=_theme, stderr=True, highlight=False)

# ── Symbols ──────────────────────────────────────────────────────────
SYM_HEAD = "●"
SYM_OK = "✓"
SYM_ERR = "✗"
SYM_WARN = "▲"
SYM_PHASE = "◇"
SYM_META = "◆"


def header(text: str) -> None:
    rule = "─" * 40
    console.print(f"[header]{SYM_HEAD}[/] [bold]{text}[/] [dim]{rule}[/]")


def phase(text: str) -> None:
    console.print(f"  ├─ [phase]{SYM_PHASE}[/] {text}")


def meta(text: str) -> None:
    console.print(f"  ├─ [meta]{SYM_META}[/] {text}")


def ok(text: str) -> None:
    console.print(f"  └─ [ok]{SYM_OK} {text}[/]")


def warn(text: str) -> None:
    console.print(f"  ├─ [warn]{SYM_WARN} {text}[/]")


def err(text: str) -> None:
    err_console.print(f"  [err]{SYM_ERR} {text}[/]")


def info(text: str) -> None:
    console.print(f"  [dim]│[/]     {text}")


def info_block(block: str) -> None:
    for line in block.splitlines():
        if line.strip():
            info(line)


def prompt(text: str) -> str:
    return console.input(f"  ├─ [bold]{text}[/]")


def confirm(text: str, default: bool = False) -> bool:
    suffix = " [Y/n] " if default else " [y/N] "
    answer = prompt(text + suffix).strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes")


ServicePromptDecision = Literal["approve", "skip", "ignore"]


def confirm_or_ignore_service(
    text: str,
    *,
    service: str,
    default: bool = False,
) -> ServicePromptDecision:
    suffix = " [Y/n/i] " if default else " [y/N/i] "

    while True:
        answer = prompt(text + suffix).strip().lower()
        if not answer:
            return "approve" if default else "skip"
        if answer in ("y", "yes"):
            return "approve"
        if answer in ("n", "no"):
            return "skip"

        if answer in ("i", "ignore"):
            while True:
                confirm_ignore = (
                    prompt(
                        f"Ignore {service} data for future syncs by creating .tignore? [y/N] "
                    )
                    .strip()
                    .lower()
                )
                if not confirm_ignore:
                    return "skip"
                if confirm_ignore in ("y", "yes"):
                    return "ignore"
                if confirm_ignore in ("n", "no"):
                    return "skip"
                warn("Please answer y/yes or n/no.")

        warn("Please answer y/yes, n/no, or i/ignore.")
