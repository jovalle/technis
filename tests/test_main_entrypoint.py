from __future__ import annotations

import importlib
import sys
import types


def test_main_module_import_has_no_side_effects(monkeypatch) -> None:
    calls = {"count": 0}

    fake_cli_module = types.ModuleType("tctl.cli")

    def _fake_cli() -> None:
        calls["count"] += 1

    fake_cli_module.cli = _fake_cli
    monkeypatch.setitem(sys.modules, "tctl.cli", fake_cli_module)
    sys.modules.pop("tctl.__main__", None)

    main_module = importlib.import_module("tctl.__main__")
    assert calls["count"] == 0

    main_module.main()
    assert calls["count"] == 1
