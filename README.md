# pypi-lockdown

Bootstrap a Python environment so that **all** packages are pulled from an
internal, authenticated PyPI feed.  Install this package first, then every
subsequent `pip install` / `uv add` will use the configured feed — with
`artifacts-keyring` handling credentials transparently.

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

## What it does

`pypi-lockdown` writes configuration files that redirect the default package
index:

| Tool    | Scope               | File written                                    |
|---------|---------------------|-------------------------------------------------|
| **pip** | environment (default) | `$VIRTUAL_ENV/pip.conf` or `$CONDA_PREFIX/pip.conf` |
| **pip** | user (fallback)     | `~/.config/pip/pip.conf` (platform-aware)       |
| **uv**  | user                | `~/.config/uv/uv.toml` (platform-aware)        |

Poetry requires per-project configuration — the tool prints the exact
commands and TOML snippet to add.

Works with **venv**, **conda**, and any other environment manager that sets
`VIRTUAL_ENV` or `CONDA_PREFIX`.

## Options

```
python -m pypi_lockdown [configure] INDEX_URL [--user]
python -m pypi_lockdown scaffold NAME INDEX_URL
```

| Command      | Effect |
|--------------|--------|
| `configure`  | Write pip/uv config files (default when omitted). |
| `scaffold`   | Generate a wrapper package that hardcodes a private feed URL. |

| Flag     | Effect |
|----------|--------|
| *(none)* | Writes pip config into the active Python environment. Falls back to user home when no environment is detected. Always writes uv config to user home. |
| `--user` | Forces pip config to the user home directory even when an environment is active. |

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

## Standalone `.pyz` distribution

For environments where you can't (or don't want to) `pip install` first, build
a standalone zipapp using [shiv](https://github.com/linkedin/shiv).  The `.pyz`
bundles pypi-lockdown, `artifacts-keyring-nofuss`, and all transitive
dependencies — including the keyring, which gets auto-installed into the target
environment.

### Build

```bash
pip install 'pypi-lockdown[build]'          # install shiv + tox

# Build for all platforms from a single machine
tox -e standalone                            # → dist/pypi-lockdown-{platform}.pyz

# Or build for the current platform only
tox -e standalone -- native                  # → dist/pypi-lockdown.pyz

# Or build for a specific platform
tox -e standalone -- linux-x86_64            # → dist/pypi-lockdown-linux-x86_64.pyz
```

> **Note:** `.pyz` files are platform-specific because `cryptography` (a
> transitive keyring dependency on Linux) contains native extensions.  Cross-
> building uses `pip download --platform` so all variants can be built from a
> single machine.

### Use

```bash
# Download the .pyz for your platform (from a shared drive, internal server, etc.)
python pypi-lockdown.pyz \
    https://pkgs.dev.azure.com/ORG/PROJECT/_packaging/PRIVATE_FEED/pypi/simple/
```

This writes pip/uv config files **and** installs `artifacts-keyring-nofuss`
plus all its dependencies into the active Python environment — no network
access to any package feed required.

### Wrapper `.pyz` files

Scaffolded wrapper packages include a `tox.ini` and can build their own
standalone `.pyz` files the same way:

```bash
cd ai4s-pypi-lockdown
tox -e standalone       # builds ai4s-pypi-lockdown-{platform}.pyz
```

End users just run the wrapper `.pyz` — the feed URL is hardcoded, zero config
needed.

## User-home config locations

| Tool | Linux                       | macOS                                         | Windows              |
|------|-----------------------------|-----------------------------------------------|----------------------|
| pip  | `~/.config/pip/pip.conf`    | `~/Library/Application Support/pip/pip.conf`  | `%APPDATA%\pip\pip.ini` |
| uv   | `~/.config/uv/uv.toml`     | `~/Library/Application Support/uv/uv.toml`   | `%APPDATA%\uv\uv.toml` |

## Manual Poetry setup

Poetry sources are configured per project.  After running `pypi-lockdown`,
follow the printed instructions in each Poetry project directory:

```bash
poetry source add --priority=primary internal https://pkgs.dev.azure.com/ORG/PROJECT/_packaging/FEED/pypi/simple/
poetry source add --priority=explicit PyPI
```

## Creating a release

Tag the repo and run the release script to build `.pyz` artifacts for all
platforms and publish a GitHub release:

```bash
git tag v1.0.0
./scripts/release.sh v1.0.0
```

The script runs `tox -e standalone` (cross-builds for linux-x86_64,
macOS-universal2, win-amd64) then calls `gh release create` with all `.pyz`
files.

> The build needs `artifacts-keyring-nofuss` resolvable by pip.  If your
> user-level pip config isn't set up, pass feed access via the `PIP_ARGS`
> env variable:
> ```bash
> PIP_ARGS="--extra-index-url https://pkgs.dev.azure.com/.../pypi/simple/" ./scripts/release.sh v1.0.0
> ```

## Security model

- **HTTPS required**: `configure` rejects non-HTTPS index URLs — HTTP would expose
  credentials and package content to network observers.
- **Standalone `.pyz` integrity**: The `.pyz` zipapp is the trust root when
  bootstrapping without network access. Distribute it via a trusted channel (internal
  file share, signed release artifact). Once extracted, packages are verified against
  their bundled `.dist-info` metadata.
- **Build-time zip-slip protection**: Wheel extraction during `.pyz` builds validates
  that no archive entry escapes the staging directory.
- **Narrow config scope**: `pypi-lockdown` only writes `index-url` to pip/uv config
  files. It does not modify global Python settings or install hooks.

## License

MIT
