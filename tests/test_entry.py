"""Console-script entry point: guards the optional ``[cli]`` extra."""

from __future__ import annotations

import builtins
from typing import Any
from unittest.mock import patch

import pytest

from vizaio import _entry


def test_main_runs_app_when_cli_installed() -> None:
    with patch("vizaio.cli.app") as mock_app:
        _entry.main()
    mock_app.assert_called_once_with()


@pytest.mark.parametrize("missing", ["typer", "rich", "tomlkit", "platformdirs"])
def test_main_missing_cli_extra_exits_with_hint(
    missing: str, capsys: pytest.CaptureFixture[str]
) -> None:
    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        # _entry uses a relative import, so __import__ sees name="cli"
        if name in ("cli", "vizaio.cli"):
            raise ModuleNotFoundError(f"No module named '{missing}'", name=missing)
        return real_import(name, *args, **kwargs)

    with (
        patch.object(builtins, "__import__", side_effect=fake_import),
        pytest.raises(SystemExit) as exc_info,
    ):
        _entry.main()
    assert exc_info.value.code == 1
    assert "vizaio[cli]" in capsys.readouterr().err


def test_main_reraises_unrelated_import_error() -> None:
    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name in ("cli", "vizaio.cli"):
            raise ModuleNotFoundError("No module named 'notanextra'", name="notanextra")
        return real_import(name, *args, **kwargs)

    with (
        patch.object(builtins, "__import__", side_effect=fake_import),
        pytest.raises(ModuleNotFoundError, match="notanextra"),
    ):
        _entry.main()
