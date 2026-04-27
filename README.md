# pypi-lockdown

[![CI](https://github.com/microsoft/pypi-lockdown/actions/workflows/ci.yml/badge.svg)](https://github.com/microsoft/pypi-lockdown/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/pypi-lockdown)](https://pypi.org/project/pypi-lockdown/)
[![Python](https://img.shields.io/pypi/pyversions/pypi-lockdown)](https://pypi.org/project/pypi-lockdown/)
[![License](https://img.shields.io/github/license/microsoft/pypi-lockdown)](LICENSE)

Bootstrap a Python environment so that **all** packages are pulled from an
internal, authenticated PyPI feed.  Install this package first, then every
subsequent `pip install` / `uv add` will use the configured feed — with
`artifacts-keyring` handling credentials transparently.

📖 **[Full setup guide](docs/securing-python-packaging.md)** — covers uv, pip, conda, CI pipelines, Docker, GitHub Actions, and devcontainers.

## Quick start

```bash
# 1. Create & activate a fresh environment
python -m venv .venv && source .venv/bin/activate   # venv (Linux / macOS)
python -m venv .venv && .venv\Scripts\activate       # venv (Windows)
conda create -n myenv python && conda activate myenv # conda

# 2. Install pypi-lockdown from the public feed
pip install pypi-lockdown \
    --index-url https://pkgs.dev.azure.com/ORG/PROJECT/_packaging/PUBLIC_FEED/pypi/simple/

# 3. Lock down the environment to use the authenticated feed
python -m pypi_lockdown \
    https://pkgs.dev.azure.com/ORG/PROJECT/_packaging/PRIVATE_FEED/pypi/simple/

# 4. Done — all future installs use the authenticated feed
pip install requests   # resolved from PRIVATE_FEED, authenticated via artifacts-keyring
```

### Standalone `.pyz` (build locally)

For environments where you can't `pip install` first, you can build a
standalone `.pyz` zipapp that bundles all dependencies:

```bash
pip install tox shiv
tox -e standalone -- linux-x86_64    # or macos-universal2, win-amd64
python dist/pypi-lockdown-linux-x86_64.pyz \
    https://pkgs.dev.azure.com/ORG/PROJECT/_packaging/PRIVATE_FEED/pypi/simple/
```

This writes pip/uv config files **and** installs `artifacts-keyring-nofuss`
plus all its dependencies into the active environment — no network access to
any package feed required.

> `.pyz` files are platform-specific (Linux, macOS, Windows) because
> `cryptography` contains native extensions.

## What it does

`pypi-lockdown` writes configuration files that redirect the default package
index:

| Tool       | Scope                 | File written                                    |
|------------|----------------------|-------------------------------------------------|
| **pip**    | environment (default) | `$VIRTUAL_ENV/pip.conf` or `$CONDA_PREFIX/pip.conf` |
| **pip**    | user (fallback)      | `~/.config/pip/pip.conf` (platform-aware)       |
| **uv**     | user                 | `~/.config/uv/uv.toml` (platform-aware)        |
| **uv**     | project (prompted)   | `./pyproject.toml` `[tool.uv]` section          |
| **Poetry** | project (prompted)   | `./pyproject.toml` `[[tool.poetry.source]]`     |

When run inside a project directory (containing `pyproject.toml`), the tool
offers to configure uv and Poetry settings directly in the project file —
including `keyring-provider` and index URLs with the `__token__@` prefix that
uv requires for keyring authentication.

Works with **venv**, **conda**, and any other environment manager that sets
`VIRTUAL_ENV` or `CONDA_PREFIX`.

### Platform-specific config paths

| Tool | Linux                       | macOS                                         | Windows              |
|------|-----------------------------|-----------------------------------------------|----------------------|
| pip  | `~/.config/pip/pip.conf`    | `~/Library/Application Support/pip/pip.conf`  | `%APPDATA%\pip\pip.ini` |
| uv   | `~/.config/uv/uv.toml`     | `~/Library/Application Support/uv/uv.toml`   | `%APPDATA%\uv\uv.toml` |

### Manual Poetry setup

If you run `pypi-lockdown` outside a project directory (no `pyproject.toml`),
or decline the prompt, you can configure Poetry manually:

```bash
poetry source add --priority=primary internal https://pkgs.dev.azure.com/ORG/PROJECT/_packaging/FEED/pypi/simple/
poetry source add --priority=explicit PyPI
```

## CLI reference

```
python -m pypi_lockdown [configure] [INDEX_URL] [--user] [--ci] [--verify]
python -m pypi_lockdown verify INDEX_URL
python -m pypi_lockdown scaffold NAME INDEX_URL
```

| Command      | Effect |
|--------------|--------|
| `configure`  | Write pip/uv config files (default when omitted). |
| `verify`     | Test that the configured feed is reachable and authentication works. |
| `scaffold`   | Generate a wrapper package that hardcodes a private feed URL. |

| Flag       | Effect |
|------------|--------|
| *(none)*   | Target the active environment; prompt to update `pyproject.toml` if present. |
| `--user`   | Write pip config to user home instead of the active environment. |
| `--ci`     | Non-interactive CI mode: skip `pyproject.toml` modification and poetry instructions. |
| `--verify` | After configuring, verify the feed is reachable and authentication works. |

### Auto-detect feed URL

When `INDEX_URL` is omitted, `pypi-lockdown` reads the current directory's
`pyproject.toml` and looks for a configured feed:

1. `[[tool.uv.index]]` entry with `default = true`
2. `[[tool.poetry.source]]` entry with `priority = "primary"`

This means after initial setup, team members can simply run:

```bash
python -m pypi_lockdown
```

## Creating team-specific wrapper packages

Use `scaffold` to generate a small package that hardcodes your team's feed
URL and depends on `pypi-lockdown`:

```bash
python -m pypi_lockdown scaffold ai4s-pypi-lockdown \
    https://pkgs.dev.azure.com/ai4s/ai4s/_packaging/ai4s-pypi/pypi/simple/
```

This creates a ready-to-publish package:

```
ai4s-pypi-lockdown/
├── pyproject.toml
├── tox.ini
└── src/ai4s_pypi_lockdown/
    ├── __init__.py
    └── __main__.py
```

Users of that wrapper only need:

```bash
pip install ai4s-pypi-lockdown --index-url https://pkgs.dev.azure.com/.../PUBLIC_FEED/pypi/simple/
python -m ai4s_pypi_lockdown
```

Scaffolded packages can also build their own standalone `.pyz` files:

```bash
cd ai4s-pypi-lockdown
tox -e standalone       # builds ai4s-pypi-lockdown-{platform}.pyz
```

## Creating a release

Create a GitHub release — the CI workflow builds a wheel and sdist, attaches
them to the release, and publishes to the ADO PyPI feed:

```bash
gh release create v1.0.0 --generate-notes
```

To build a standalone `.pyz` locally (e.g. for air-gapped environments):

```bash
pip install tox shiv
tox -e standalone -- linux-x86_64    # or macos-universal2, win-amd64
```

## Security model

- **HTTPS required**: `configure` rejects non-HTTPS index URLs — HTTP would expose
  credentials and package content to network observers.
- **Build provenance**: Wheel and sdist releases are built in CI with
  [signed build provenance](https://docs.github.com/en/actions/security-for-github-actions/using-artifact-attestations)
  — verify with `gh attestation verify <file> --owner microsoft`.
- **Standalone `.pyz` integrity**: When building `.pyz` locally for air-gapped
  use, the build includes zip-slip protection that validates no archive entry
  escapes the staging directory.
- **Narrow config scope**: `pypi-lockdown` only writes `index-url` to pip/uv config
  files. It does not modify global Python settings or install hooks.

## License

MIT
