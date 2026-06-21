"""``vizaio.__version__`` tracks the installed distribution metadata.

Regression guard: the version used to be a hardcoded string in
``__init__.py`` that the release workflow never updated, so e.g. the 0.2.0
wheel shipped ``__version__ == "0.1.0"``. Deriving it from the installed
metadata keeps it in lockstep with the real release version (the workflow
stamps the dist version from the git tag at build time).
"""

from __future__ import annotations

from importlib.metadata import version

import vizaio


def test_version_matches_distribution_metadata() -> None:
    assert vizaio.__version__ == version("vizaio")
    assert vizaio.__version__  # non-empty
