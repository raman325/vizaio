"""
CLI output formatting: rich tables (TTY), TSV (pipe), JSON, plain.

Auto-detection: stdout TTY → table, otherwise → tsv.
Override with ``--format {table,tsv,json,plain}`` in any command.

Rationale: piping ``vizaio input list`` into ``awk`` / ``cut`` / ``grep``
needs a stable, parseable shape. Watching ``vizaio input list`` in a
terminal wants pretty colors and headers.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import StrEnum
import io
import json
import sys
from typing import Any

from rich.console import Console
from rich.table import Table


class OutputFormat(StrEnum):
    TABLE = "table"
    TSV = "tsv"
    JSON = "json"
    PLAIN = "plain"


def auto_format() -> OutputFormat:
    """Pick a sensible default based on whether stdout is a TTY."""
    return OutputFormat.TABLE if sys.stdout.isatty() else OutputFormat.TSV


def render_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    columns: Sequence[str] | None = None,
    fmt: OutputFormat | None = None,
) -> str:
    """
    Render a list of dicts as the chosen format.

    ``columns`` defaults to the keys of the first row, in their dict order.
    """
    fmt = fmt or auto_format()
    if not rows:
        return ""
    cols = list(columns) if columns is not None else list(rows[0].keys())

    if fmt is OutputFormat.JSON:
        return _render_json(list(rows))
    if fmt is OutputFormat.TSV:
        return _render_tsv(rows, cols)
    if fmt is OutputFormat.PLAIN:
        return _render_plain(rows, cols)
    return _render_table(rows, cols)


def render_value(value: Any, *, fmt: OutputFormat | None = None) -> str:
    """Render a single scalar (e.g., ``vizaio info model``)."""
    fmt = fmt or auto_format()
    if fmt is OutputFormat.JSON:
        return json.dumps(value)
    return str(value)


def render_message(message: str, *, fmt: OutputFormat | None = None) -> str:
    """Status messages — no formatting in tabular modes; plain in others."""
    fmt = fmt or auto_format()
    if fmt is OutputFormat.JSON:
        return json.dumps({"message": message})
    return message


# ---------------------------------------------------------------------------
# Per-format renderers
# ---------------------------------------------------------------------------


def _render_json(rows: list[Mapping[str, Any]]) -> str:
    # Pretty JSON for TTY (caller may also pass --format json directly).
    if sys.stdout.isatty():
        return json.dumps(rows, indent=2, default=_json_default)
    return json.dumps(rows, default=_json_default)


def _json_default(obj: Any) -> Any:
    if hasattr(obj, "value"):
        return obj.value
    return str(obj)


def _render_tsv(rows: Sequence[Mapping[str, Any]], cols: list[str]) -> str:
    lines = []
    for row in rows:
        cells = [_str_cell(row.get(c, "")) for c in cols]
        lines.append("\t".join(cells))
    return "\n".join(lines)


def _render_plain(rows: Sequence[Mapping[str, Any]], cols: list[str]) -> str:
    lines = []
    for row in rows:
        cells = [_str_cell(row.get(c, "")) for c in cols]
        lines.append(" ".join(cells))
    return "\n".join(lines)


def _render_table(rows: Sequence[Mapping[str, Any]], cols: list[str]) -> str:
    # ``record=True`` captures rendered output for export_text(); the
    # file=io.StringIO() avoids writing the rendered table to stdout
    # twice (we want export_text(), not the live print).
    console = Console(record=True, file=io.StringIO())
    table = Table(*cols)
    for row in rows:
        table.add_row(*[_str_cell(row.get(c, "")) for c in cols])
    console.print(table)
    return console.export_text(clear=True)


def _str_cell(value: Any) -> str:
    if value is True:
        return "*"
    if value is False or value is None:
        return ""
    if hasattr(value, "value"):  # enum-ish
        return str(value.value)
    return str(value)
