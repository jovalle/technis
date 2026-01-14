from __future__ import annotations

from tctl.compose import _parse_include_section


def test_parse_include_section_allows_leading_comments_and_blank_lines() -> None:
    lines = [
        "# managed by tooling\n",
        "\n",
        "include:\n",
        "  - path: ../../services/blocky/compose.yaml\n",
        "services:\n",
        "  app:\n",
        "    image: demo\n",
    ]

    entries, include_index, remainder_index = _parse_include_section(lines)

    assert include_index == 2
    assert remainder_index == 4
    assert entries[0].paths == ("../../services/blocky/compose.yaml",)
