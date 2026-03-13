"""Write pip / uv / poetry configuration pointing at an internal PyPI feed."""

from __future__ import annotations

import configparser
import os
import platform
import sys
from pathlib import Path

_MARKER = "# Managed by pypi-lockdown — safe to edit, will be overwritten on next run\n"


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _env_path() -> Path | None:
    """Return the active Python environment root (venv or conda), if any."""
    for var in ("VIRTUAL_ENV", "CONDA_PREFIX"):
        v = os.environ.get(var)
        if v:
            return Path(v)
    return None


def _is_windows() -> bool:
    return platform.system() == "Windows"


def _pip_config_env(env: Path) -> Path:
    """pip config inside a venv or conda environment."""
    return env / ("pip.ini" if _is_windows() else "pip.conf")


def _pip_config_user() -> Path:
    system = platform.system()
    if system == "Windows":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return base / "pip" / "pip.ini"
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "pip" / "pip.conf"
    return Path.home() / ".config" / "pip" / "pip.conf"


def _uv_config_user() -> Path:
    system = platform.system()
    if system == "Windows":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return base / "uv" / "uv.toml"
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "uv" / "uv.toml"
    return Path.home() / ".config" / "uv" / "uv.toml"


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------

def _write_pip_config(path: Path, index_url: str) -> None:
    cfg = configparser.ConfigParser()
    if path.exists():
        cfg.read(path)
    if not cfg.has_section("global"):
        cfg.add_section("global")
    cfg.set("global", "index-url", index_url)

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as fh:
        fh.write(_MARKER)
        cfg.write(fh)
    print(f"  \u2713 {path}")


def _write_uv_config(path: Path, index_url: str) -> None:
    content = (
        _MARKER
        + "\n"
        + "[[index]]\n"
        + f'url = "{index_url}"\n'
        + "default = true\n"
        + "\n"
        + "[pip]\n"
        + f'index-url = "{index_url}"\n'
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    print(f"  \u2713 {path}")


def _print_poetry_instructions(index_url: str) -> None:
    print(
        "\n"
        "  Poetry (per-project — run in each Poetry project directory):\n"
        "\n"
        f"    poetry source add --priority=primary internal {index_url}\n"
        "    poetry source add --priority=explicit PyPI\n"
        "\n"
        "  Or add to pyproject.toml manually:\n"
        "\n"
        '    [[tool.poetry.source]]\n'
        '    name = "internal"\n'
        f'    url = "{index_url}"\n'
        '    priority = "primary"\n'
        "\n"
        '    [[tool.poetry.source]]\n'
        '    name = "PyPI"\n'
        '    priority = "explicit"\n'
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def configure(index_url: str, *, user_scope: bool = False) -> None:
    env = _env_path()

    print(f"\nConfiguring index: {index_url}\n")

    # --- pip ---
    if env and not user_scope:
        print(f"Python environment: {env}\n")
        _write_pip_config(_pip_config_env(env), index_url)
    else:
        if env:
            print("Writing to user directory (--user).\n")
        else:
            print("No Python environment detected — writing to user directory.\n")
        _write_pip_config(_pip_config_user(), index_url)

    # --- uv (user-level only) ---
    _write_uv_config(_uv_config_user(), index_url)

    # --- poetry ---
    _print_poetry_instructions(index_url)

    print("artifacts-keyring will handle authentication transparently.")
    print()
