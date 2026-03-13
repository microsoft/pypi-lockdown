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
python -m pypi_lockdown INDEX_URL [--user]
```

| Flag     | Effect |
|----------|--------|
| *(none)* | Writes pip config into the active Python environment. Falls back to user home when no environment is detected. Always writes uv config to user home. |
| `--user` | Forces pip config to the user home directory even when an environment is active. |

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

## License

MIT
