"""Coverage for ``apps`` private functions and tolerant parsing paths."""

from __future__ import annotations

import json
from pathlib import Path

from vizio_smartcast import apps as apps_module
from vizio_smartcast.apps import _parse_catalog
from vizio_smartcast.types import AppConfig


class TestParseCatalogTolerance:
    def test_non_list_root_returns_empty(self) -> None:
        # Catalogs are typically arrays — non-list payload → empty tuple.
        assert _parse_catalog({"not": "a list"}) == ()
        assert _parse_catalog("string") == ()
        assert _parse_catalog(None) == ()

    def test_skips_non_dict_entries(self) -> None:
        assert _parse_catalog([1, "x", None]) == ()

    def test_skips_entry_without_name(self) -> None:
        assert _parse_catalog([{"config": [{"APP_ID": "1", "NAME_SPACE": 2}]}]) == ()

    def test_skips_entry_with_non_string_name(self) -> None:
        assert (
            _parse_catalog([{"name": 42, "config": [{"APP_ID": "1", "NAME_SPACE": 2}]}])
            == ()
        )

    def test_country_non_list_defaults_to_wildcard(self) -> None:
        catalog = _parse_catalog(
            [
                {
                    "name": "Foo",
                    "country": "not-a-list",
                    "config": [{"APP_ID": "1", "NAME_SPACE": 2}],
                }
            ]
        )
        assert catalog[0].country == ("*",)

    def test_config_dict_promoted_to_list(self) -> None:
        catalog = _parse_catalog(
            [
                {
                    "name": "Foo",
                    "config": {"APP_ID": "1", "NAME_SPACE": 2},
                }
            ]
        )
        assert catalog[0].name == "Foo"
        assert catalog[0].config == (AppConfig(app_id="1", name_space=2, message=None),)

    def test_config_list_with_non_dict_entries_skipped(self) -> None:
        catalog = _parse_catalog(
            [
                {
                    "name": "Foo",
                    "config": [
                        "not a dict",
                        {"APP_ID": "1", "NAME_SPACE": 2},
                    ],
                }
            ]
        )
        assert len(catalog[0].config) == 1

    def test_config_lowercase_keys_accepted(self) -> None:
        # Older catalog format uses lowercase config keys.
        catalog = _parse_catalog(
            [
                {
                    "name": "Foo",
                    "config": [{"app_id": "5", "name_space": 3}],
                }
            ]
        )
        assert catalog[0].config[0].app_id == "5"
        assert catalog[0].config[0].name_space == 3

    def test_config_missing_app_id_or_namespace_skipped(self) -> None:
        catalog = _parse_catalog(
            [
                {
                    "name": "Foo",
                    "config": [
                        {"APP_ID": "1"},  # no namespace
                        {"NAME_SPACE": 2},  # no app_id
                    ],
                }
            ]
        )
        # Both configs invalid → entry has no usable configs → skipped entirely.
        assert catalog == ()

    def test_entry_with_no_valid_config_skipped(self) -> None:
        # config is a dict but doesn't carry the required fields.
        assert _parse_catalog([{"name": "Foo", "config": {}}]) == ()

    def test_message_string_preserved(self) -> None:
        catalog = _parse_catalog(
            [
                {
                    "name": "Foo",
                    "config": [{"APP_ID": "5", "NAME_SPACE": 3, "MESSAGE": "hello"}],
                }
            ]
        )
        assert catalog[0].config[0].message == "hello"


class TestLoadBundled:
    """Exercise the missing-file and corrupt-file fallback branches of
    ``_load_bundled``. The bundled file path is computed from
    ``__file__`` at import; we monkeypatch the module's ``__file__``
    attribute so the function looks at a different location."""

    def test_missing_file_returns_empty(self, tmp_path: Path, monkeypatch) -> None:
        # Move __file__ to a directory with no data/apps.json.
        fake_file = tmp_path / "apps.py"
        fake_file.write_text("# empty stand-in")
        monkeypatch.setattr(apps_module, "__file__", str(fake_file))
        # Re-run the loader.
        result = apps_module._load_bundled()
        assert result == ()

    def test_corrupt_file_returns_empty(self, tmp_path: Path, monkeypatch) -> None:
        # Build the directory layout the loader expects.
        fake_file = tmp_path / "apps.py"
        fake_file.write_text("# empty stand-in")
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "apps.json").write_text("not valid json {{{")
        monkeypatch.setattr(apps_module, "__file__", str(fake_file))
        result = apps_module._load_bundled()
        assert result == ()

    def test_valid_file_loads(self, tmp_path: Path, monkeypatch) -> None:
        fake_file = tmp_path / "apps.py"
        fake_file.write_text("# empty stand-in")
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "apps.json").write_text(
            json.dumps(
                [
                    {
                        "name": "TestApp",
                        "country": ["*"],
                        "config": [{"APP_ID": "9", "NAME_SPACE": 4}],
                    }
                ]
            )
        )
        monkeypatch.setattr(apps_module, "__file__", str(fake_file))
        result = apps_module._load_bundled()
        assert result[0].name == "TestApp"
        assert result[0].config[0] == AppConfig(app_id="9", name_space=4)
