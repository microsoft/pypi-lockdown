# Copilot Instructions

## Architecture

pypi-lockdown is a CLI tool that bootstraps Python environments to use internal, authenticated PyPI feeds. It has two commands:

- **`configure`** (default) — writes user-global `pip.conf`/`pip.ini` and `uv.toml` config files pointing at an internal feed URL. Global-only by default; pass `--project` to also write uv, Poetry, and Hatch project-level config into `./pyproject.toml`.
- **`scaffold`** — generates a standalone wrapper package that hardcodes a specific feed URL and depends on `pypi-lockdown`, so teams can distribute a single `pip install` command.

Entry point: `src/pypi_lockdown/__main__.py` parses args and treats a bare URL as shorthand for `configure URL`.

## Project layout

- `src/pypi_lockdown/configure.py` — path resolution (platform-aware for Linux/macOS/Windows) and config file writers for pip, uv, poetry, and hatch
- `src/pypi_lockdown/scaffold.py` — generates a complete package directory from string templates (`_PYPROJECT_TOML`, `_MAIN_PY`)

## Build & Lint

```bash
pip install -e ".[dev]"        # editable install with dev tools
pre-commit install             # one-time git hook setup
pre-commit run --all-files     # run all hooks manually
ruff check src/                # lint only
ruff format src/               # format only
mypy src/                      # type-check (strict mode)
```

Versioning is dynamic via `setuptools-scm` from git tags.

## Conventions

- `from __future__ import annotations` at the top of every module.
- Private helpers prefixed with `_` (e.g. `_pip_config_user`, `_write_pip_config`).
- A `_MARKER` comment is written at the top of generated config files to indicate they are managed by pypi-lockdown.
