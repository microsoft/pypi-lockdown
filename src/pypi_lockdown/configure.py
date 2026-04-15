"""Write pip / uv / poetry configuration pointing at an internal PyPI feed."""

from __future__ import annotations

import configparser
import os
import platform
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
    with path.open("w") as fh:
        fh.write(_MARKER)
        cfg.write(fh)
    print(f"  \u2713 {path}")


def _ensure_userinfo(url: str) -> str:
    """Inject ``__token__@`` into the URL if no userinfo is present.

    uv requires a username in the URL to trigger keyring lookup.
    """
    from urllib.parse import urlparse, urlunparse  # noqa: PLC0415

    parsed = urlparse(url)
    if parsed.username:
        return url
    netloc = f"__token__@{parsed.hostname}"
    if parsed.port:
        netloc += f":{parsed.port}"
    return urlunparse(parsed._replace(netloc=netloc))


def _write_uv_config(path: Path, index_url: str) -> None:
    uv_url = _ensure_userinfo(index_url)
    content = (
        _MARKER
        + "\n"
        + 'keyring-provider = "subprocess"\n'
        + "\n"
        + "[[index]]\n"
        + f'url = "{uv_url}"\n'
        + "default = true\n"
        + "\n"
        + "[pip]\n"
        + f'index-url = "{uv_url}"\n'
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
    )


# ---------------------------------------------------------------------------
# pyproject.toml writers (uv + poetry)
# ---------------------------------------------------------------------------


def _write_pyproject_uv(path: Path, index_url: str) -> None:
    """Upsert ``[tool.uv]`` settings in an existing ``pyproject.toml``."""
    import tomlkit  # noqa: PLC0415

    uv_url = _ensure_userinfo(index_url)
    doc = tomlkit.parse(path.read_text())

    tool = doc.setdefault("tool", {})
    uv = tool.setdefault("uv", {})

    uv["keyring-provider"] = "subprocess"

    # Upsert [[tool.uv.index]] — find an existing default or matching URL
    indexes = uv.setdefault("index", tomlkit.aot())
    found = False
    for idx in indexes:
        if idx.get("default") or idx.get("url") == uv_url:
            idx["url"] = uv_url
            idx["default"] = True
            found = True
            break
    if not found:
        entry = tomlkit.table()
        entry.add("url", uv_url)
        entry.add("default", True)  # noqa: FBT003
        indexes.append(entry)

    path.write_text(tomlkit.dumps(doc))
    print(f"  ✓ {path} ([tool.uv])")


def _write_pyproject_poetry(path: Path, index_url: str) -> None:
    """Upsert ``[[tool.poetry.source]]`` entries in an existing ``pyproject.toml``."""
    import tomlkit  # noqa: PLC0415

    doc = tomlkit.parse(path.read_text())

    tool = doc.setdefault("tool", {})
    poetry = tool.setdefault("poetry", {})
    sources = poetry.setdefault("source", tomlkit.aot())

    # Upsert internal source
    internal_found = False
    for src in sources:
        if src.get("name") == "internal" or src.get("priority") == "primary":
            src["name"] = "internal"
            src["url"] = index_url
            src["priority"] = "primary"
            internal_found = True
            break
    if not internal_found:
        entry = tomlkit.table()
        entry.add("name", "internal")
        entry.add("url", index_url)
        entry.add("priority", "primary")
        sources.append(entry)

    # Ensure PyPI explicit source exists
    pypi_found = any(src.get("name") == "PyPI" for src in sources)
    if not pypi_found:
        entry = tomlkit.table()
        entry.add("name", "PyPI")
        entry.add("priority", "explicit")
        sources.append(entry)

    path.write_text(tomlkit.dumps(doc))
    print(f"  ✓ {path} ([[tool.poetry.source]])")


def _prompt_yes_no(prompt: str) -> bool:
    """Prompt the user for yes/no confirmation. Returns True for yes."""
    import sys  # noqa: PLC0415

    if not sys.stdin.isatty():
        return False
    try:
        answer = input(f"  {prompt} [Y/n] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer in ("", "y", "yes")


def _configure_pyproject(index_url: str) -> None:
    """Detect pyproject.toml in cwd and offer to configure uv + poetry."""
    pyproject = Path.cwd() / "pyproject.toml"
    if not pyproject.exists():
        return

    print(f"\n  Found {pyproject}")
    if not _prompt_yes_no("Write uv/poetry config to pyproject.toml?"):
        return

    print()
    _write_pyproject_uv(pyproject, index_url)
    _write_pyproject_poetry(pyproject, index_url)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def configure(index_url: str, *, user_scope: bool = False) -> None:
    if not index_url.startswith("https://"):
        print(
            f"\n  ✗ Refusing to configure non-HTTPS index URL: {index_url}\n"
            "    HTTPS is required to protect credentials and package integrity.\n"
        )
        raise SystemExit(1)

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

    # --- standalone: bootstrap keyring into target env ---
    if env:
        from .standalone import bootstrap_keyring, is_standalone  # noqa: PLC0415

        if is_standalone():
            print()
            bootstrap_keyring(env)

    # --- project-level pyproject.toml (uv + poetry) ---
    _configure_pyproject(index_url)

    # --- poetry fallback instructions ---
    pyproject = Path.cwd() / "pyproject.toml"
    if not pyproject.exists():
        _print_poetry_instructions(index_url)

    print("artifacts-keyring will handle authentication transparently.")
    print()
