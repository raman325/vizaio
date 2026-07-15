"""SmartCast app catalog: bundled JSON + remote refresh with fallback.

Tests cover three concerns:
1. ``find_app_name`` correctly resolves ``AppConfig`` against a catalog,
   including the documented namespace quirks (#4 NAME_SPACE 2↔4 equivalence,
   #5 NAME_SPACE 0 → Cast sentinel).
2. The bundled catalog is loadable and structured correctly.
3. ``fetch_app_catalog`` falls back to the bundled list when the remote
   URL fails (per the agreed-on bundle+remote strategy — see
   docs/protocol-notes.md, F).
"""

from __future__ import annotations

from pathlib import Path

import aiohttp
from aioresponses import aioresponses
import pytest

from vizaio import AppConfig, AppRecord, VizioConnectionError, VizioResponseError
from vizaio.apps import (
    APP_HOME,
    BUNDLED_APPS,
    NO_APP_RUNNING,
    REMOTE_CATALOG_URL,
    fetch_app_catalog,
    fetch_remote_app_catalog,
    find_app_name,
    is_app_input,
)

# ---------------------------------------------------------------------------
# find_app_name
# ---------------------------------------------------------------------------


class TestFindAppNameExactMatch:
    """Direct (app_id, name_space) match against a catalog entry."""

    def test_match(self) -> None:
        apps = [
            AppRecord(
                name="Netflix",
                country=("*",),
                config=(AppConfig(app_id="1", name_space=3),),
            ),
        ]
        assert find_app_name(AppConfig(app_id="1", name_space=3), apps) == "Netflix"

    def test_unknown_returns_none(self) -> None:
        """Unknown (app_id, name_space) returns None — caller can decide
        whether to map None to 'Unknown App' or surface as missing."""
        apps = [
            AppRecord(
                name="Netflix",
                country=("*",),
                config=(AppConfig(app_id="1", name_space=3),),
            ),
        ]
        assert find_app_name(AppConfig(app_id="999", name_space=99), apps) is None

    def test_multi_config_match(self) -> None:
        """An app catalog entry may have multiple launch configs (different
        firmwares may report different IDs for the same app). Any matching
        config wins."""
        apps = [
            AppRecord(
                name="HBO Max",
                country=("usa",),
                config=(
                    AppConfig(app_id="42", name_space=2),
                    AppConfig(app_id="43", name_space=3),
                ),
            ),
        ]
        assert find_app_name(AppConfig(app_id="43", name_space=3), apps) == "HBO Max"


class TestNamespaceEquivalence:
    """Quirk #4: NAME_SPACE 2 and 4 are interchangeable — pyvizio commit
    219260d (PR #97). vizaio preserves this rule."""

    def test_2_matches_4(self) -> None:
        apps = [
            AppRecord(
                name="Prime Video",
                country=("*",),
                config=(AppConfig(app_id="42", name_space=2),),
            ),
        ]
        assert (
            find_app_name(AppConfig(app_id="42", name_space=4), apps) == "Prime Video"
        )

    def test_4_matches_2(self) -> None:
        apps = [
            AppRecord(
                name="Prime Video",
                country=("*",),
                config=(AppConfig(app_id="42", name_space=4),),
            ),
        ]
        assert (
            find_app_name(AppConfig(app_id="42", name_space=2), apps) == "Prime Video"
        )

    def test_other_namespaces_not_equivalent(self) -> None:
        """Namespace 3 is NOT equivalent to namespace 2 or 4. The rule is
        narrow: only 2↔4."""
        apps = [
            AppRecord(
                name="Prime Video",
                country=("*",),
                config=(AppConfig(app_id="42", name_space=2),),
            ),
        ]
        assert find_app_name(AppConfig(app_id="42", name_space=3), apps) is None


class TestNamespaceZeroIsCast:
    """Quirk #5: NAME_SPACE 0 means the Cast/SmartCast Home screen,
    regardless of app_id. Sentinel returned even with empty catalog."""

    def test_namespace_zero_with_empty_catalog(self) -> None:
        result = find_app_name(AppConfig(app_id="anything", name_space=0), [])
        assert result == "Cast"

    def test_namespace_zero_with_full_catalog(self) -> None:
        # Even with a full catalog, namespace 0 short-circuits.
        result = find_app_name(AppConfig(app_id="x", name_space=0), list(BUNDLED_APPS))
        assert result == "Cast"


class TestNoAppRunning:
    """When the device reports no app, find_app_name returns the
    NO_APP_RUNNING sentinel — see protocol-notes #9."""

    def test_none_config(self) -> None:
        # ``None`` represents "no app running" per parse_current_app_config.
        assert find_app_name(None, list(BUNDLED_APPS)) == NO_APP_RUNNING

    def test_empty_app_config(self) -> None:
        empty = AppConfig(app_id="", name_space=0)
        # app_id="" with name_space=0 is "Cast" by quirk #5 — not no-app.
        # NO_APP_RUNNING fires only on a None config or one with no
        # meaningful identification.
        assert find_app_name(empty, []) == "Cast"


class TestSmartCastHome:
    """The SmartCast Home screen has a documented constant entry."""

    def test_home_in_apps(self) -> None:
        """APP_HOME is exposed for callers who want to recognize it."""
        assert APP_HOME.name == "SmartCast Home"

    def test_home_resolves(self) -> None:
        """Looking up the home screen's config should find APP_HOME by name."""
        # The HOME app's namespace and id are stable across firmware. We
        # don't hardcode them here — the catalog entry is the source of
        # truth.
        config = APP_HOME.config[0]
        assert find_app_name(config, [APP_HOME]) == "SmartCast Home"


# ---------------------------------------------------------------------------
# Bundled catalog
# ---------------------------------------------------------------------------


class TestBundledCatalog:
    """The bundled catalog is loaded at import time and is well-formed."""

    def test_non_empty(self) -> None:
        # If this drops to zero we've lost the bundled file.
        assert len(BUNDLED_APPS) > 0

    def test_all_records_typed(self) -> None:
        for record in BUNDLED_APPS:
            assert isinstance(record, AppRecord)

    def test_known_apps_present(self) -> None:
        """Sanity: a few well-known apps must be in the bundle."""
        names = {r.name for r in BUNDLED_APPS}
        assert "Netflix" in names

    def test_data_file_exists(self) -> None:
        from vizaio import apps as apps_module

        data_dir = Path(apps_module.__file__).parent / "data"
        assert (data_dir / "apps.json").exists()


# ---------------------------------------------------------------------------
# fetch_app_catalog with remote + bundled fallback
# ---------------------------------------------------------------------------


@pytest.fixture
async def session():
    async with aiohttp.ClientSession() as s:
        yield s


class TestFetchAppCatalogRemote:
    """The remote URL can return a fresh catalog. We parse and return it."""

    async def test_remote_success(self, session: aiohttp.ClientSession) -> None:
        # Build a tiny remote-shaped response. The real remote returns a
        # Vizio-defined JSON structure; we mirror enough of it to parse.
        remote_payload = [
            {
                "name": "Future App",
                "country": ["*"],
                "config": [{"APP_ID": "999", "NAME_SPACE": 2}],
            }
        ]
        with aioresponses() as m:
            m.get(REMOTE_CATALOG_URL, payload=remote_payload)
            catalog = await fetch_app_catalog(session=session)
        names = {r.name for r in catalog}
        assert "Future App" in names


class TestFetchAppCatalogFallback:
    """When the remote URL fails (down, malformed, timeout), we fall back
    silently to the bundled catalog. The remote URL is unreliable per
    historical pyvizio experience — fallback is the contract."""

    async def test_remote_down(self, session: aiohttp.ClientSession) -> None:
        with aioresponses() as m:
            m.get(REMOTE_CATALOG_URL, status=500)
            catalog = await fetch_app_catalog(session=session)
        # Bundled fallback used.
        assert tuple(catalog) == BUNDLED_APPS

    async def test_remote_404(self, session: aiohttp.ClientSession) -> None:
        with aioresponses() as m:
            m.get(REMOTE_CATALOG_URL, status=404)
            catalog = await fetch_app_catalog(session=session)
        assert tuple(catalog) == BUNDLED_APPS

    async def test_remote_malformed_json(self, session: aiohttp.ClientSession) -> None:
        with aioresponses() as m:
            m.get(REMOTE_CATALOG_URL, body="not json")
            catalog = await fetch_app_catalog(session=session)
        assert tuple(catalog) == BUNDLED_APPS

    async def test_remote_unexpected_shape(
        self, session: aiohttp.ClientSession
    ) -> None:
        """If the remote returns valid JSON but a shape we don't
        understand, fall back rather than crashing."""
        with aioresponses() as m:
            m.get(REMOTE_CATALOG_URL, payload={"some_key": "some_value"})
            catalog = await fetch_app_catalog(session=session)
        assert tuple(catalog) == BUNDLED_APPS

    async def test_remote_timeout(self, session: aiohttp.ClientSession) -> None:
        with aioresponses() as m:
            m.get(REMOTE_CATALOG_URL, exception=TimeoutError())
            catalog = await fetch_app_catalog(session=session)
        assert tuple(catalog) == BUNDLED_APPS

    async def test_remote_connection_error(
        self, session: aiohttp.ClientSession
    ) -> None:
        with aioresponses() as m:
            m.get(
                REMOTE_CATALOG_URL,
                exception=aiohttp.ClientConnectionError(),
            )
            catalog = await fetch_app_catalog(session=session)
        assert tuple(catalog) == BUNDLED_APPS


class TestFetchAppCatalogNeverRaises:
    """fetch_app_catalog is invoked from app polling — it must NEVER raise.
    The bundled fallback ensures availability is preserved even if the
    remote URL is permanently dead."""

    async def test_never_raises(self, session: aiohttp.ClientSession) -> None:
        with aioresponses() as m:
            m.get(REMOTE_CATALOG_URL, exception=RuntimeError("unexpected"))
            # Must not propagate. We log debug, fall back to bundled.
            catalog = await fetch_app_catalog(session=session)
        assert len(catalog) > 0


class TestFetchAppCatalogUrlOverride:
    """Caller can point ``fetch_app_catalog`` at a different URL — useful
    for region-specific catalogs, internal mirrors, or testing. The
    default remains ``REMOTE_CATALOG_URL``."""

    async def test_url_override_is_fetched(
        self, session: aiohttp.ClientSession
    ) -> None:
        custom_url = "https://example.invalid/custom_apps.json"
        remote_payload = [
            {
                "name": "Mirror App",
                "country": ["*"],
                "config": [{"APP_ID": "777", "NAME_SPACE": 2}],
            }
        ]
        with aioresponses() as m:
            m.get(custom_url, payload=remote_payload)
            catalog = await fetch_app_catalog(session=session, url=custom_url)
        assert any(r.name == "Mirror App" for r in catalog)

    async def test_url_override_falls_back_on_failure(
        self, session: aiohttp.ClientSession
    ) -> None:
        custom_url = "https://example.invalid/custom_apps.json"
        with aioresponses() as m:
            m.get(custom_url, status=500)
            catalog = await fetch_app_catalog(session=session, url=custom_url)
        assert tuple(catalog) == BUNDLED_APPS

    async def test_default_url_constant_unchanged(self) -> None:
        # Guard against accidentally rebinding the default in a refactor.
        import inspect

        sig = inspect.signature(fetch_app_catalog)
        assert sig.parameters["url"].default == REMOTE_CATALOG_URL


class TestFetchAppCatalogTopLevelExport:
    """``fetch_app_catalog`` is part of the documented public API and
    importable from the top-level package."""

    def test_importable_from_vizaio(self) -> None:
        import vizaio

        assert vizaio.fetch_app_catalog is fetch_app_catalog


# ---------------------------------------------------------------------------
# is_app_input
# ---------------------------------------------------------------------------


class TestIsAppInput:
    """``is_app_input`` identifies the SmartCast app input in both the
    display-name form (``"CAST"``) and the meta-name / current-input
    form (``"SMARTCAST"``), case-insensitively."""

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("CAST", True),
            ("SMARTCAST", True),
            ("cast", True),
            ("SmartCast", True),
            ("HDMI-1", False),
            ("", False),
        ],
    )
    def test_is_app_input(self, name: str, expected: bool) -> None:
        assert is_app_input(name) is expected

    def test_importable_from_vizaio(self) -> None:
        import vizaio

        assert vizaio.is_app_input is is_app_input


# ---------------------------------------------------------------------------
# fetch_remote_app_catalog (strict variant: no bundled fallback)
# ---------------------------------------------------------------------------


class TestFetchRemoteAppCatalog:
    """``fetch_remote_app_catalog`` raises instead of falling back so
    callers that keep their own cache (e.g. the Home Assistant apps
    coordinator) can distinguish failure from a fresh catalog."""

    async def test_remote_success(self, session: aiohttp.ClientSession) -> None:
        remote_payload = [
            {
                "name": "Future App",
                "country": ["*"],
                "config": [{"APP_ID": "999", "NAME_SPACE": 2}],
            }
        ]
        with aioresponses() as m:
            m.get(REMOTE_CATALOG_URL, payload=remote_payload)
            catalog = await fetch_remote_app_catalog(session=session)
        assert {r.name for r in catalog} == {"Future App"}

    async def test_http_error_raises(self, session: aiohttp.ClientSession) -> None:
        with aioresponses() as m:
            m.get(REMOTE_CATALOG_URL, status=500)
            with pytest.raises(VizioResponseError):
                await fetch_remote_app_catalog(session=session)

    async def test_connection_error_raises(
        self, session: aiohttp.ClientSession
    ) -> None:
        with aioresponses() as m:
            m.get(REMOTE_CATALOG_URL, exception=aiohttp.ClientConnectionError())
            with pytest.raises(VizioConnectionError):
                await fetch_remote_app_catalog(session=session)

    async def test_timeout_raises(self, session: aiohttp.ClientSession) -> None:
        with aioresponses() as m:
            m.get(REMOTE_CATALOG_URL, exception=TimeoutError())
            with pytest.raises(VizioConnectionError):
                await fetch_remote_app_catalog(session=session)

    async def test_malformed_json_raises(self, session: aiohttp.ClientSession) -> None:
        with aioresponses() as m:
            m.get(REMOTE_CATALOG_URL, body="not json")
            with pytest.raises(VizioResponseError):
                await fetch_remote_app_catalog(session=session)

    async def test_unexpected_error_raises_vizio_error(
        self, session: aiohttp.ClientSession
    ) -> None:
        with aioresponses() as m:
            m.get(REMOTE_CATALOG_URL, exception=RuntimeError("unexpected"))
            with pytest.raises(VizioResponseError):
                await fetch_remote_app_catalog(session=session)

    async def test_empty_parse_raises(self, session: aiohttp.ClientSession) -> None:
        with aioresponses() as m:
            m.get(REMOTE_CATALOG_URL, payload=[])
            with pytest.raises(VizioResponseError):
                await fetch_remote_app_catalog(session=session)

    async def test_url_override_is_fetched(
        self, session: aiohttp.ClientSession
    ) -> None:
        custom_url = "https://example.invalid/custom_apps.json"
        remote_payload = [
            {
                "name": "Mirror App",
                "country": ["*"],
                "config": [{"APP_ID": "1", "NAME_SPACE": 2}],
            }
        ]
        with aioresponses() as m:
            m.get(custom_url, payload=remote_payload)
            catalog = await fetch_remote_app_catalog(session=session, url=custom_url)
        assert {r.name for r in catalog} == {"Mirror App"}

    def test_importable_from_vizaio(self) -> None:
        import vizaio

        assert vizaio.fetch_remote_app_catalog is fetch_remote_app_catalog
