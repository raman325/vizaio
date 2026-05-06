"""Tests for ``vizaio.cli._format`` — pure renderers, no I/O."""

from __future__ import annotations

from enum import Enum
import json
import sys

from vizaio.cli._format import (
    OutputFormat,
    auto_format,
    render_message,
    render_rows,
    render_value,
)


class _Color(Enum):
    RED = "red"
    BLUE = "blue"


class TestAutoFormat:
    def test_pipe_returns_tsv(self, monkeypatch) -> None:
        monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
        assert auto_format() is OutputFormat.TSV

    def test_tty_returns_table(self, monkeypatch) -> None:
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
        assert auto_format() is OutputFormat.TABLE


class TestRenderValue:
    def test_plain_string(self) -> None:
        assert render_value("HDMI-1", fmt=OutputFormat.PLAIN) == "HDMI-1"

    def test_json_string(self) -> None:
        assert render_value("HDMI-1", fmt=OutputFormat.JSON) == '"HDMI-1"'

    def test_json_dict(self) -> None:
        assert render_value({"k": 1}, fmt=OutputFormat.JSON) == '{"k": 1}'

    def test_int(self) -> None:
        assert render_value(42, fmt=OutputFormat.PLAIN) == "42"

    def test_auto_when_fmt_none(self, monkeypatch) -> None:
        monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
        assert render_value("x", fmt=None) == "x"


class TestRenderMessage:
    def test_plain(self) -> None:
        assert render_message("ok", fmt=OutputFormat.PLAIN) == "ok"

    def test_json_wraps(self) -> None:
        out = render_message("ok", fmt=OutputFormat.JSON)
        assert json.loads(out) == {"message": "ok"}

    def test_table_returns_raw_message(self) -> None:
        # Table mode for status messages just emits the string — no tabular framing.
        assert render_message("ok", fmt=OutputFormat.TABLE) == "ok"


class TestRenderRowsEmpty:
    def test_empty_returns_empty_string(self) -> None:
        assert render_rows([], fmt=OutputFormat.JSON) == ""
        assert render_rows([], fmt=OutputFormat.TABLE) == ""


class TestRenderRowsTSV:
    def test_basic(self) -> None:
        out = render_rows(
            [{"a": 1, "b": "hi"}, {"a": 2, "b": "bye"}],
            fmt=OutputFormat.TSV,
        )
        assert out == "1\thi\n2\tbye"

    def test_explicit_columns_and_missing_keys(self) -> None:
        out = render_rows(
            [{"a": 1}, {"b": 2}],
            columns=["a", "b"],
            fmt=OutputFormat.TSV,
        )
        # Missing keys render as empty cells (per ``_str_cell(None)``).
        assert out == "1\t\n\t2"

    def test_bool_true_marker_and_none_blank(self) -> None:
        out = render_rows(
            [{"on": True, "off": False, "missing": None}],
            fmt=OutputFormat.TSV,
        )
        assert out == "*\t\t"

    def test_enum_value_extracted(self) -> None:
        out = render_rows([{"color": _Color.RED}], fmt=OutputFormat.TSV)
        assert out == "red"


class TestRenderRowsPlain:
    def test_space_separated(self) -> None:
        out = render_rows(
            [{"a": 1, "b": 2}, {"a": 3, "b": 4}],
            fmt=OutputFormat.PLAIN,
        )
        assert out == "1 2\n3 4"


class TestRenderRowsJSON:
    def test_compact_when_piped(self, monkeypatch) -> None:
        monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
        out = render_rows([{"a": 1}], fmt=OutputFormat.JSON)
        assert out == '[{"a": 1}]'

    def test_pretty_when_tty(self, monkeypatch) -> None:
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
        out = render_rows([{"a": 1}], fmt=OutputFormat.JSON)
        # Pretty form has indentation and newlines.
        assert "\n" in out
        assert json.loads(out) == [{"a": 1}]

    def test_enum_serialized_via_value(self, monkeypatch) -> None:
        monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
        out = render_rows([{"color": _Color.BLUE}], fmt=OutputFormat.JSON)
        assert json.loads(out) == [{"color": "blue"}]

    def test_unserializable_falls_back_to_str(self, monkeypatch) -> None:
        monkeypatch.setattr(sys.stdout, "isatty", lambda: False)

        class _Stringy:
            def __str__(self) -> str:
                return "stringy"

        out = render_rows([{"obj": _Stringy()}], fmt=OutputFormat.JSON)
        assert json.loads(out) == [{"obj": "stringy"}]


class TestRenderRowsTable:
    def test_table_contains_columns_and_values(self) -> None:
        out = render_rows(
            [{"name": "HDMI-1", "current": True}],
            fmt=OutputFormat.TABLE,
        )
        # The exact rich rendering is layout-dependent, but it must
        # contain the column headers and the values.
        assert "name" in out
        assert "current" in out
        assert "HDMI-1" in out
