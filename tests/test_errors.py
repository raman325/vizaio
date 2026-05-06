"""Exception hierarchy invariants.

Callers depend on being able to:

- Catch ``VizioError`` to handle "any device problem" uniformly
- Catch specific subclasses (``VizioAuthError`` etc.) for targeted handling
- Inspect error messages via ``str()`` / ``repr()``

These tests lock in the inheritance graph and message preservation so a
future refactor can't accidentally re-parent an exception class.
"""

from __future__ import annotations

import pytest

from vizaio import (
    VizioAuthError,
    VizioBusyError,
    VizioConnectionError,
    VizioError,
    VizioInvalidInputError,
    VizioInvalidParameterError,
    VizioNotFoundError,
    VizioResponseError,
    VizioUnsupportedError,
)

ALL_ERRORS = [
    VizioAuthError,
    VizioBusyError,
    VizioConnectionError,
    VizioInvalidInputError,
    VizioInvalidParameterError,
    VizioNotFoundError,
    VizioResponseError,
    VizioUnsupportedError,
]


class TestHierarchy:
    """All exceptions inherit from VizioError and ultimately Exception."""

    def test_base_inherits_exception(self) -> None:
        assert issubclass(VizioError, Exception)

    @pytest.mark.parametrize("cls", ALL_ERRORS)
    def test_all_inherit_from_vizio_error(self, cls: type[Exception]) -> None:
        assert issubclass(cls, VizioError)

    def test_invalid_input_is_invalid_parameter(self) -> None:
        """VizioInvalidInputError is a VizioInvalidParameterError so callers
        catching invalid_parameter from the device will also catch input
        validation errors before the request goes out."""
        assert issubclass(VizioInvalidInputError, VizioInvalidParameterError)


class TestCatchUniformly:
    """Each subclass can be caught via VizioError."""

    @pytest.mark.parametrize("cls", ALL_ERRORS)
    def test_catch_via_base(self, cls: type[Exception]) -> None:
        with pytest.raises(VizioError):
            raise cls("test message")

    def test_catch_invalid_input_via_invalid_parameter(self) -> None:
        with pytest.raises(VizioInvalidParameterError):
            raise VizioInvalidInputError("HDMI-99 not in [HDMI-1, HDMI-2]")


class TestMessages:
    """Exception messages survive str/repr."""

    @pytest.mark.parametrize("cls", ALL_ERRORS)
    def test_message_in_str(self, cls: type[Exception]) -> None:
        assert "device unavailable" in str(cls("device unavailable"))

    @pytest.mark.parametrize("cls", ALL_ERRORS)
    def test_class_name_in_repr(self, cls: type[Exception]) -> None:
        assert cls.__name__ in repr(cls("x"))

    def test_no_args_constructs_cleanly(self) -> None:
        """Some failure paths raise without a message; that should still
        produce a usable exception."""
        for cls in ALL_ERRORS:
            cls()


class TestDistinctClasses:
    """Sibling exception classes are not confused for each other."""

    def test_auth_is_not_connection(self) -> None:
        assert not issubclass(VizioAuthError, VizioConnectionError)

    def test_not_found_is_not_invalid_parameter(self) -> None:
        assert not issubclass(VizioNotFoundError, VizioInvalidParameterError)

    def test_unsupported_is_not_invalid_input(self) -> None:
        """VizioUnsupportedError ('this device profile doesn't have battery')
        is conceptually distinct from VizioInvalidInputError ('HDMI-99 not a
        valid input on this device')."""
        assert not issubclass(VizioUnsupportedError, VizioInvalidInputError)
