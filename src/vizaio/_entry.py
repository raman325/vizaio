"""Console-script entry point that guards the optional ``[cli]`` extra."""

from __future__ import annotations

import sys

_CLI_EXTRA_MODULES = frozenset({"platformdirs", "rich", "tomlkit", "typer"})


def main() -> None:
    """Run the CLI, or explain how to install it if the extra is missing."""
    try:
        # Deferred so a missing [cli] extra is caught here, not at import time
        from .cli import app  # noqa: PLC0415
    except ModuleNotFoundError as err:
        if err.name is not None and err.name.split(".")[0] in _CLI_EXTRA_MODULES:
            print(
                "The vizaio CLI requires optional dependencies that are not"
                " installed.\nInstall them with: pip install 'vizaio[cli]'",
                file=sys.stderr,
            )
            raise SystemExit(1) from err
        raise
    app()
