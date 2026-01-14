"""Allow running as `python -m tctl`."""

from tctl.cli import cli


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
