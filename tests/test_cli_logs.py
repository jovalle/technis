from __future__ import annotations

from click.testing import CliRunner

from tctl.cli import cli


def test_help_lists_logs_command() -> None:
    runner = CliRunner()

    result = runner.invoke(cli, ["--help"])

    assert result.exit_code == 0
    assert "logs" in result.output
    assert "Stream docker compose logs." in result.output


def test_logs_requires_service_without_stack() -> None:
    runner = CliRunner()

    result = runner.invoke(cli, ["logs"])

    assert result.exit_code == 2
    assert "Provide at least one service when no stack is selected." in result.output
    assert "Example: tctl logs blocky" in result.output


def test_passthrough_without_stack_shows_logs_tip() -> None:
    runner = CliRunner()

    result = runner.invoke(cli, ["ps"])

    assert result.exit_code == 2
    assert "Stack required. Use -m, -n, or -s <stack>." in result.output
    assert "Tip: use 'tctl logs <service>' to stream across stacks." in result.output


def test_logs_routes_to_cross_stack_helper(monkeypatch) -> None:
    runner = CliRunner()
    captured: dict[str, object] = {}

    def _fake_logs_across_stacks(args: tuple[str, ...]) -> int:
        captured["args"] = args
        return 7

    monkeypatch.setattr(
        "tctl.compose.compose_logs_across_stacks", _fake_logs_across_stacks
    )

    result = runner.invoke(cli, ["logs", "-f", "authelia"])

    assert result.exit_code == 7
    assert captured["args"] == ("logs", "-f", "authelia")


def test_logs_routes_to_compose_exec_when_stack_selected(monkeypatch) -> None:
    runner = CliRunner()
    captured: dict[str, object] = {}

    def _fake_compose_exec(
        host: str, args: tuple[str, ...], *, full: bool = False
    ) -> int:
        captured["host"] = host
        captured["args"] = args
        captured["full"] = full
        return 5

    monkeypatch.setattr("tctl.compose.compose_exec", _fake_compose_exec)

    result = runner.invoke(cli, ["-m", "logs", "--tail", "50", "authelia"])

    assert result.exit_code == 5
    assert captured["host"] == "mothership"
    assert captured["args"] == ("logs", "--tail", "50", "authelia")
    assert captured["full"] is False
