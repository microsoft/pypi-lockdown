# Copilot Instructions

## Architecture

pypi-lockdown is a CLI tool that bootstraps Python environments to use internal, authenticated PyPI feeds. It has two commands:

- **`configure`** (default) — writes `pip.conf`/`pip.ini` and `uv.toml` config files pointing at an internal feed URL. Targets the active venv/conda environment by default, falls back to user-home config. Poetry instructions are printed (not written) since Poetry requires per-project config.
- **`scaffold`** — generates a standalone wrapper package that hardcodes a specific feed URL and depends on `pypi-lockdown`, so teams can distribute a single `pip install` command.

Entry point: `src/pypi_lockdown/__main__.py` parses args and treats a bare URL as shorthand for `configure URL`.

## Project layout

- `src/pypi_lockdown/configure.py` — path resolution (platform-aware for Linux/macOS/Windows) and config file writers for pip, uv, and poetry
- `src/pypi_lockdown/scaffold.py` — generates a complete package directory from string templates (`_PYPROJECT_TOML`, `_MAIN_PY`)

## Build

Uses setuptools with setuptools-scm for versioning (version derived from git tags). Install in dev mode:

```sh
pip install -e .
```

## Conventions

- `from __future__ import annotations` at the top of every module.
- Private helpers prefixed with `_` (e.g. `_env_path`, `_write_pip_config`).
- A `_MARKER` comment is written at the top of generated config files to indicate they are managed by pypi-lockdown.
- No test suite exists yet. There is no CI configuration.
