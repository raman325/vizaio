# Contributing to vizio-smartcast

## Tooling

This project uses Rust-native tooling wherever possible — fast, parallel,
and consistent across machines.

| Tool | Purpose | Rust? |
|------|---------|-------|
| [`uv`](https://github.com/astral-sh/uv) | Package + venv management | yes |
| [`ruff`](https://github.com/astral-sh/ruff) | Lint + format | yes |
| [`taplo`](https://github.com/tamasfe/taplo) | TOML formatter | yes |
| [`prek`](https://github.com/j178/prek) | pre-commit-compatible hook runner | yes |
| `mypy` | Strict static type checking | (not Rust, but fast under prek's parallel scheduling) |
| `pytest` + `pytest-asyncio` | Test runner | |

## Setup

```bash
# Create a venv with Python 3.12 and install the project + dev deps.
uv venv -p 3.12 .venv
uv pip install -e ".[dev,discovery]"

# Install the prek-managed git hook.
prek install
```

`uv pip install` doesn't bootstrap a `pip` binary into the venv by
design — use `uv pip install <pkg>` from the repo root instead of
activating and running `pip`.

## Day-to-day

```bash
# Run tests.
uv run pytest

# Watch mode (re-runs on file change).
uv run pytest --looponfail

# Run all pre-commit hooks against the whole tree.
prek run --all-files

# Run a single hook.
prek run ruff
prek run mypy

# Bump pinned hook versions.
prek autoupdate
```

## Layout

- `src/vizio_smartcast/` — library code
- `tests/` — pytest suite. `_fixtures.py` contains JSON-shape factories
  ported from pyvizio plus casing variations.
- `docs/protocol-notes.md` — every protocol quirk and how we handle it
- `docs/android-app-findings.md` — APK decompile findings (full source
  of truth on protocol behavior)
- `docs/websocket-protocol-notes.md` — WebSocket SCPL findings (v0.2)
- `assets/` — HA migration cheatsheet, error mapping, reference HA
  coordinator (not part of the library; lift into HA core when needed)

## Test-first development

Implementation work follows the failing-test → green-test loop. The full
suite was written **before** any implementation, derived from pyvizio's
test corpus and the APK findings. If you're adding a feature, write the
test first.

```bash
# Confirm everything passes before you start.
uv run pytest

# Run only the test for the area you're working on.
uv run pytest tests/test_wire.py -v

# Check coverage gaps.
uv run pytest --cov=vizio_smartcast --cov-report=term-missing
```

## Commit hooks

`prek install` wires up the hooks defined in `.pre-commit-config.yaml`:

- `ruff` — autofix lint
- `ruff-format` — formatter
- `taplo-format` / `taplo-lint` — TOML formatting
- `mypy --strict` — type check `src/`
- Generic hygiene (trailing whitespace, EOL, large-file guard)

Hooks run on `git commit`. To skip them once, `git commit --no-verify` —
but the CI runs them too, so it's cheaper to fix locally.

## Style

- Async-only. No sync wrappers.
- Exceptions over None returns.
- `from __future__ import annotations` at the top of every module.
- Lines ≤ 88 chars (ruff default).
- Type-annotate everything; mypy strict mode is gospel.
- Frozen dataclasses with `slots=True` for value types.
