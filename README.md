# pypi-lockdown

[![CI](https://github.com/microsoft/pypi-lockdown/actions/workflows/ci.yml/badge.svg)](https://github.com/microsoft/pypi-lockdown/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/pypi-lockdown)](https://pypi.org/project/pypi-lockdown/)
[![Python](https://img.shields.io/pypi/pyversions/pypi-lockdown)](https://pypi.org/project/pypi-lockdown/)
[![License](https://img.shields.io/github/license/microsoft/pypi-lockdown)](LICENSE)

Bootstrap Python tooling so that **all** packages are pulled from an internal,
authenticated PyPI feed.  `pypi-lockdown` writes **user-global** pip/uv config
pointing at your feed, and a `keyring` backend supplies credentials
transparently for every environment.

> **You need a keyring backend.** uv and pip authenticate by invoking a global
> `keyring` command (the `subprocess` provider), so install it once as a tool
> with an Azure Artifacts backend — from your **public bootstrap feed**:
> ```bash
> uv tool install keyring --with artifacts-keyring-nofuss \
>     --index-url https://pkgs.dev.azure.com/ORG/PROJECT/_packaging/PUBLIC_FEED/pypi/simple/
> ```
> Use `artifacts-keyring-nofuss` (pure-Python fork, no .NET) or the upstream
> `artifacts-keyring` (needs .NET) — install either from the same public feed.
> `configure` prints the exact command if no backend is found.

📖 **[Full setup guide](docs/securing-python-packaging.md)** — covers uv, pip, conda, CI pipelines, Docker, GitHub Actions, and devcontainers.

## Quick start

```bash
# 1. Install a global keyring backend once, from your public bootstrap feed
uv tool install keyring --with artifacts-keyring-nofuss \
    --index-url https://pkgs.dev.azure.com/ORG/PROJECT/_packaging/PUBLIC_FEED/pypi/simple/

# 2. Install pypi-lockdown from the public feed
pip install pypi-lockdown \
    --index-url https://pkgs.dev.azure.com/ORG/PROJECT/_packaging/PUBLIC_FEED/pypi/simple/

# 3. Write user-global config pointing at your authenticated feed
pypi-lockdown \
    https://pkgs.dev.azure.com/ORG/PROJECT/_packaging/PRIVATE_FEED/pypi/simple/

# 4. Done — every environment now installs from the authenticated feed
pip install requests   # resolved from PRIVATE_FEED, authenticated via keyring
```

> **Scope to a single environment instead?** Activate the venv/conda env and
> run `pypi-lockdown --env <FEED>`.  This writes config into that environment
> only and copies a backend into it, so install
> `pip install "pypi-lockdown[nofuss]"` (or `[official]`) beforehand.

### Standalone `.pyz` (build locally)

For environments where you can't `pip install` first, you can build a
standalone `.pyz` zipapp that bundles all dependencies:

```bash
pip install tox shiv
tox -e standalone -- linux-x86_64    # or macos-universal2, win-amd64
python dist/pypi-lockdown-linux-x86_64.pyz \
    https://pkgs.dev.azure.com/ORG/PROJECT/_packaging/PRIVATE_FEED/pypi/simple/
```

This writes pip/uv config files **and** installs the official `artifacts-keyring`
plus all its dependencies into the active environment — no network access to
any package feed required.

> `.pyz` files are platform-specific (Linux, macOS, Windows) because
> `cryptography` contains native extensions.

## What it does

`pypi-lockdown` writes configuration files that redirect the default package
index:

| Tool       | Scope                 | File written                                    |
|------------|----------------------|-------------------------------------------------|
| **pip**    | user (default)        | `~/.config/pip/pip.conf` (platform-aware), with `keyring-provider = subprocess` |
| **pip**    | environment (`--env`) | `$VIRTUAL_ENV/pip.conf` or `$CONDA_PREFIX/pip.conf` |
| **uv**     | user                 | `~/.config/uv/uv.toml` (platform-aware)        |
| **uv**     | project (prompted)   | `./pyproject.toml` `[tool.uv]` section          |
| **Poetry** | project (prompted)   | `./pyproject.toml` `[[tool.poetry.source]]`     |
| **Hatch**  | project (if `[tool.hatch]` exists) | `./pyproject.toml` `[tool.hatch.envs.default.env-vars]` |

When run inside a project directory (containing `pyproject.toml`), the tool
offers to configure uv, Poetry, and Hatch settings directly in the project
file — including `keyring-provider` and index URLs with the `__token__@` prefix
that uv requires for keyring authentication. Hatch configuration is only written
when an existing `[tool.hatch]` section is detected.

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
python -m pypi_lockdown [configure] [INDEX_URL] [--env] [--ci] [--verify]
python -m pypi_lockdown verify INDEX_URL
python -m pypi_lockdown scaffold NAME INDEX_URL
```

| Command      | Effect |
|--------------|--------|
| `configure`  | Write pip and uv config files, and optionally update project `pyproject.toml` for Poetry/Hatch (default when omitted). |
| `verify`     | Test that the configured feed is reachable and authentication works. |
| `scaffold`   | Generate a wrapper package that hardcodes a private feed URL. |

| Flag       | Effect |
|------------|--------|
| *(none)*   | Write user-global config for all environments; prompt to update `pyproject.toml` if present. |
| `--env`    | Scope the lockdown to the active venv/conda environment and copy a backend into it. |
| `--ci`     | Non-interactive CI mode: skip `pyproject.toml` modification and poetry instructions. |
| `--verify` | After configuring, verify the feed is reachable and authentication works. |

### Auto-detect feed URL

When `INDEX_URL` is omitted, `pypi-lockdown` reads the current directory's
`pyproject.toml` and looks for a configured feed:

1. `[[tool.uv.index]]` entry with `default = true`
2. `[[tool.poetry.source]]` entry with `priority = "primary"`
3. `[tool.hatch.envs.default.env-vars]` for `PIP_INDEX_URL` or `UV_DEFAULT_INDEX`

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
- **Narrow config scope**: `pypi-lockdown` only writes `index-url` to pip/uv/hatch config
  files. It does not modify global Python settings or install hooks.

## License

MIT
