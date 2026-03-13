# pypi-lockdown

Bootstrap a Python environment so that **all** packages are pulled from an
internal, authenticated PyPI feed.  Install this package first, then every
subsequent `pip install` / `uv add` will use the configured feed — with
`artifacts-keyring` handling credentials transparently.

## Quick start

```bash
# 1. Create & activate a fresh environment
python -m venv .venv && source .venv/bin/activate   # Linux / macOS
python -m venv .venv && .venv\Scripts\activate       # Windows

# 2. Install pypi-lockdown (one-time, explicit index)
pip install pypi-lockdown --index-url https://pkgs.dev.azure.com/ORG/PROJECT/_packaging/FEED/pypi/simple/

# 3. Lock down the environment
python -m pypi_lockdown https://pkgs.dev.azure.com/ORG/PROJECT/_packaging/FEED/pypi/simple/

# 4. Done — all future installs use the internal feed
pip install requests   # resolved from the internal feed
```

## What it does

`pypi-lockdown` writes configuration files that redirect the default package
index:

| Tool    | Scope           | File written                              |
|---------|-----------------|-------------------------------------------|
| **pip** | virtual env     | `$VIRTUAL_ENV/pip.conf` (or `pip.ini`)    |
| **pip** | user (fallback) | `~/.config/pip/pip.conf` (platform-aware) |
| **uv**  | user            | `~/.config/uv/uv.toml` (platform-aware)  |

Poetry requires per-project configuration — the tool prints the exact
commands and TOML snippet to add.

## Options

```
python -m pypi_lockdown INDEX_URL [--user]
```

| Flag     | Effect |
|----------|--------|
| *(none)* | Writes pip config into the active virtual environment. Falls back to user home when no venv is detected. Always writes uv config to user home. |
| `--user` | Forces pip config to the user home directory even when a virtual environment is active. |

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
