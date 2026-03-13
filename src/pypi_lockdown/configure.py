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

def _venv_path() -> Path | None:
    v = os.environ.get("VIRTUAL_ENV")
    return Path(v) if v else None


def _is_windows() -> bool:
    return platform.system() == "Windows"


def _pip_config_venv(venv: Path) -> Path:
    return venv / ("pip.ini" if _is_windows() else "pip.conf")


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
    venv = _venv_path()

    print(f"\nConfiguring index: {index_url}\n")

    # --- pip ---
    if venv and not user_scope:
        print(f"Virtual environment: {venv}\n")
        _write_pip_config(_pip_config_venv(venv), index_url)
    else:
        if venv:
            print("Writing to user directory (--user).\n")
        else:
            print("No virtual environment detected — writing to user directory.\n")
        _write_pip_config(_pip_config_user(), index_url)

    # --- uv (user-level only) ---
    _write_uv_config(_uv_config_user(), index_url)

    # --- poetry ---
    _print_poetry_instructions(index_url)

    print("artifacts-keyring will handle authentication transparently.")
    print()
