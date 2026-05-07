"""
SmartCast wire envelope: :class:`Item` and :class:`Response` dataclasses.

Boundary between "raw JSON dict from device" and "typed Python data the
rest of the package operates on." All case-insensitive key handling and
shape validation happens here, once. Downstream code receives normalized
lowercase keys and well-typed fields.

Per protocol-notes.md quirk #1: SmartCast responses come back with keys
in inconsistent casing across firmware versions. ``Response.from_json``
lowercases all keys (including nested ``ITEMS[].VALUE`` dict keys when
that VALUE is itself a dict) so the rest of the codebase can rely on a
consistent shape.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Self

from .errors import VizioNotFoundError, VizioResponseError
from .types import ResponseStatus


@dataclass(frozen=True, slots=True)
class Item:
    """
    A single ITEM record from a SmartCast response.

    Field names are lowercase. ``raw`` retains the original (also
    lowercased) dict so callers can read fields we haven't modeled
    explicitly without a library bump.
    """

    cname: str
    type: str
    name: str
    value: Any
    hashval: int | None = None
    enabled: str | None = None
    elements: tuple[str, ...] = ()
    min: int | None = None
    max: int | None = None
    center: int | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Response:
    """A parsed SmartCast response envelope."""

    status: ResponseStatus
    result_raw: str
    """Original ``STATUS.RESULT`` string. Useful when ``status`` is
    ``ResponseStatus.UNKNOWN`` — caller can inspect what the device
    actually said."""

    detail: str
    items: tuple[Item, ...]
    uri: str = ""
    name: str = ""
    parameters: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Self:
        """
        Validate envelope shape, normalize keys, return a typed Response.

        Raises :class:`VizioResponseError` on malformed input.
        """
        if not isinstance(data, Mapping):
            raise VizioResponseError(
                f"Response must be a dict, got {type(data).__name__}"
            )

        normalized = _lowercase_keys(data)

        status_raw = normalized.get("status")
        if status_raw is None:
            raise VizioResponseError("Response missing STATUS field")
        if not isinstance(status_raw, Mapping):
            raise VizioResponseError(
                f"STATUS must be a dict, got {type(status_raw).__name__}"
            )

        result_value = status_raw.get("result")
        if result_value is None:
            raise VizioResponseError("Response STATUS missing RESULT field")
        result_str = str(result_value)
        status = _parse_status(result_str)
        detail = str(status_raw.get("detail", ""))

        items: list[Item] = []
        if "items" in normalized:
            items_raw = normalized["items"]
            if items_raw is not None and not isinstance(items_raw, list):
                raise VizioResponseError(
                    f"ITEMS must be a list, got {type(items_raw).__name__}"
                )
            for raw_item in items_raw or ():
                items.append(_item_from_dict(raw_item))
        elif "item" in normalized:
            # Pairing endpoints use ITEM (singular). Fold it into the
            # items tuple for uniform downstream handling.
            item_raw = normalized["item"]
            if isinstance(item_raw, Mapping):
                items.append(_item_from_dict(item_raw))

        return cls(
            status=status,
            result_raw=result_str,
            detail=detail,
            items=tuple(items),
            uri=str(normalized.get("uri", "")),
            name=str(normalized.get("name", "")),
            parameters=normalized.get("parameters", {}),
        )

    def find_item(self, cname: str) -> Item | None:
        """Return the first item with matching cname (case-insensitive)."""
        target = cname.lower()
        for item in self.items:
            if item.cname == target:
                return item
        return None

    def require_item(self, cname: str) -> Item:
        """Like :meth:`find_item` but raises :class:`VizioNotFoundError`."""
        item = self.find_item(cname)
        if item is None:
            raise VizioNotFoundError(f"Response has no item with cname={cname!r}")
        return item

    def has_item(self, cname: str) -> bool:
        """Return ``True`` when any item matches ``cname`` (case-insensitive)."""
        return self.find_item(cname) is not None

    def items_by_type(self, type_: str) -> tuple[Item, ...]:
        """Return all items whose ``type`` field matches the given schema kind."""
        return tuple(item for item in self.items if item.type == type_)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_STATUS_VALUES: Final[set[str]] = {s.value for s in ResponseStatus}


def _parse_status(raw: str) -> ResponseStatus:
    """
    Map a STATUS.RESULT string to ``ResponseStatus``, case-insensitive.

    Unknown values resolve to :attr:`ResponseStatus.UNKNOWN`; the original
    string is preserved on :attr:`Response.result_raw`.
    """
    lower = raw.lower()
    if lower in _STATUS_VALUES:
        return ResponseStatus(lower)
    return ResponseStatus.UNKNOWN


def _lowercase_keys(obj: Any) -> Any:
    """
    Recursively lowercase dict keys. Lists/scalars pass through.

    Applied at the wire boundary. Downstream code never sees mixed
    casing.
    """
    if isinstance(obj, Mapping):
        return {str(k).lower(): _lowercase_keys(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_lowercase_keys(v) for v in obj]
    return obj


def _item_from_dict(raw: Mapping[str, Any]) -> Item:
    """Build an :class:`Item` from one (already-lowercased) ITEM dict."""
    raw_cname = raw.get("cname", "")
    cname = str(raw_cname).lower() if raw_cname is not None else ""

    elements_raw = raw.get("elements", ())
    if isinstance(elements_raw, list):
        elements = tuple(str(e) for e in elements_raw)
    else:
        elements = ()

    hashval_raw = raw.get("hashval")
    hashval: int | None
    if hashval_raw is None:
        hashval = None
    else:
        try:
            hashval = int(hashval_raw)
        except (TypeError, ValueError):
            hashval = None

    return Item(
        cname=cname,
        type=str(raw.get("type", "")),
        name=str(raw.get("name", "")),
        value=raw.get("value"),
        hashval=hashval,
        enabled=_optional_str(raw.get("enabled")),
        elements=elements,
        min=_optional_int(raw.get("minimum")),
        max=_optional_int(raw.get("maximum")),
        center=_optional_int(raw.get("center")),
        raw=raw,
    )


def _optional_int(value: Any) -> int | None:
    """Coerce ``value`` to ``int``, returning ``None`` on missing or unparseable."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_str(value: Any) -> str | None:
    """Coerce ``value`` to ``str``; ``None`` only when ``value`` is ``None``."""
    if value is None:
        return None
    return str(value)
