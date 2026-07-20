"""Write pip / uv / poetry configuration pointing at an internal PyPI feed."""

from __future__ import annotations

import configparser
import os
import platform
from pathlib import Path

_MARKER = (
    "# Managed by pypi-lockdown -- safe to edit, will be overwritten on next run\n"
)


# ---------------------------------------------------------------------------
# Auto-detect
# ---------------------------------------------------------------------------


def _detect_from_hatch(hatch: object) -> str | None:
    """Extract feed URL from hatch env-vars config."""
    if not isinstance(hatch, dict):
        return None
    hatch_envs = hatch.get("envs", {})
    if not isinstance(hatch_envs, dict):
        return None
    hatch_default = hatch_envs.get("default", {})
    if not isinstance(hatch_default, dict):
        return None
    hatch_env_vars = hatch_default.get("env-vars", {})
    if not isinstance(hatch_env_vars, dict):
        return None
    for key in ("PIP_INDEX_URL", "UV_DEFAULT_INDEX", "UV_INDEX_URL"):
        url = hatch_env_vars.get(key)
        if url:
            return _strip_userinfo(str(url))
    return None


def _detect_from_tool(tool: dict[str, object]) -> str | None:
    """Search tool tables for a feed URL (uv → poetry → hatch)."""
    from typing import Any  # noqa: PLC0415

    _tool: dict[str, Any] = tool

    # Try uv indexes first
    uv = _tool.get("uv", {})
    if isinstance(uv, dict):
        for idx in uv.get("index", []):
            if isinstance(idx, dict) and idx.get("default"):
                url = idx.get("url")
                if url:
                    return _strip_userinfo(str(url))

    # Fall back to poetry sources
    poetry = _tool.get("poetry", {})
    if isinstance(poetry, dict):
        for src in poetry.get("source", []):
            if isinstance(src, dict) and src.get("priority") == "primary":
                url = src.get("url")
                if url:
                    return _strip_userinfo(str(url))

    # Fall back to hatch default env-vars
    return _detect_from_hatch(_tool.get("hatch"))

    return None


def detect_index_url() -> str | None:
    """Try to read the default index URL from ``pyproject.toml`` in the cwd.

    Checks ``[[tool.uv.index]]`` entries for one marked ``default = true``,
    then falls back to ``[[tool.poetry.source]]`` with
    ``priority = "primary"``, then to
    ``[tool.hatch.envs.default.env-vars]`` for ``PIP_INDEX_URL`` or
    ``UV_DEFAULT_INDEX``.
    Returns the URL with any ``__token__@`` userinfo stripped, or *None* if
    no feed URL could be found.
    """
    pyproject = Path.cwd() / "pyproject.toml"
    if not pyproject.exists():
        return None

    import tomlkit  # noqa: PLC0415

    try:
        doc = tomlkit.parse(pyproject.read_text())
    except (OSError, tomlkit.exceptions.TOMLKitError):
        return None
    tool = doc.get("tool", {})
    if not isinstance(tool, dict):
        return None

    return _detect_from_tool(tool)


def _strip_userinfo(url: str) -> str:
    """Remove userinfo (e.g. ``__token__@``) from a URL."""
    from urllib.parse import urlparse, urlunparse  # noqa: PLC0415

    parsed = urlparse(url)
    if "@" not in parsed.netloc:
        return url
    netloc = parsed.netloc.rsplit("@", 1)[1]
    return urlunparse(parsed._replace(netloc=netloc))


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


def _write_pip_config(
    path: Path,
    index_url: str,
    *,
    keyring_subprocess: bool = False,
) -> None:
    cfg = configparser.ConfigParser()
    if path.exists():
        cfg.read(path)
    if not cfg.has_section("global"):
        cfg.add_section("global")
    cfg.set("global", "index-url", index_url)
    if keyring_subprocess:
        # Global scope: pip has no importable backend in arbitrary
        # interpreters, so authenticate via the `keyring` subprocess.
        cfg.set("global", "keyring-provider", "subprocess")
    elif cfg.has_option("global", "keyring-provider"):
        # Env scope uses the import model; drop a subprocess provider left
        # over from a previous global run so it doesn't linger unexpectedly.
        cfg.remove_option("global", "keyring-provider")

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        fh.write(_MARKER)
        cfg.write(fh)
    print(f"  OK {path}")


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
    print(f"  OK {path}")


def _print_poetry_instructions(index_url: str) -> None:
    print(
        "\n"
        "  Poetry (per-project -- run in each Poetry project directory):\n"
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

    # Upsert [[tool.uv.index]] -- find an existing default or matching URL
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
    print(f"  OK {path} ([tool.uv])")


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
    print(f"  OK {path} ([[tool.poetry.source]])")


def _write_pyproject_hatch(path: Path, index_url: str) -> None:
    """Upsert ``[tool.hatch.envs.default.env-vars]`` in an existing ``pyproject.toml``.

    Only writes if a ``[tool.hatch]`` section already exists, to avoid adding
    Hatch configuration to non-Hatch projects.  Sets both ``PIP_INDEX_URL``
    (for pip-backed envs) and ``UV_DEFAULT_INDEX`` (for uv-backed envs).
    """
    import tomlkit  # noqa: PLC0415

    doc = tomlkit.parse(path.read_text())

    tool = doc.get("tool", {})
    if "hatch" not in tool:
        return

    hatch = tool["hatch"]
    envs = hatch.setdefault("envs", {})
    default = envs.setdefault("default", {})
    env_vars = default.setdefault("env-vars", {})

    uv_url = _ensure_userinfo(index_url)
    env_vars["PIP_INDEX_URL"] = index_url
    env_vars["UV_DEFAULT_INDEX"] = uv_url

    path.write_text(tomlkit.dumps(doc))
    print(f"  OK {path} ([tool.hatch.envs.default.env-vars])")


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
    if not _prompt_yes_no("Write uv/poetry/hatch config to pyproject.toml?"):
        return

    print()
    _write_pyproject_uv(pyproject, index_url)
    _write_pyproject_poetry(pyproject, index_url)
    _write_pyproject_hatch(pyproject, index_url)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def configure(index_url: str, *, env_scope: bool = False, ci: bool = False) -> None:
    if not index_url.startswith("https://"):
        print(
            f"\n  ERROR: Refusing to configure non-HTTPS index URL: {index_url}\n"
            "    HTTPS is required to protect credentials and package integrity.\n"
        )
        raise SystemExit(1)

    env = _env_path()

    print(f"\nConfiguring index: {index_url}\n")

    # env scope requires an active environment; fall back to global otherwise
    if env_scope and env is None:
        print(
            "  Note: --env given but no active venv/conda environment detected;"
            " configuring user-global instead.\n"
        )
        env_scope = False

    # --- pip ---
    if env_scope:
        assert env is not None  # noqa: S101  (narrowed by fallback above)
        print(f"Python environment: {env}\n")
        _write_pip_config(_pip_config_env(env), index_url)
    else:
        print(
            "Configuring user-global (all environments). Use --env to scope"
            " to the active environment only.\n"
        )
        _write_pip_config(_pip_config_user(), index_url, keyring_subprocess=True)

    # --- uv (user-level only) ---
    _write_uv_config(_uv_config_user(), index_url)

    # --- bootstrap keyring into target env (env scope only) ---
    if env_scope:
        from .standalone import bootstrap_keyring  # noqa: PLC0415

        assert env is not None  # noqa: S101
        print(f"Bootstrapping keyring packages into {env} ...")
        if not bootstrap_keyring(env):
            print("  Already up to date.")
        print()

    # --- project-level pyproject.toml (uv + poetry) ---
    if not ci:
        _configure_pyproject(index_url)

        # --- poetry fallback instructions ---
        pyproject = Path.cwd() / "pyproject.toml"
        if not pyproject.exists():
            _print_poetry_instructions(index_url)

    # --- verify a keyring backend is present ---
    from .standalone import artifacts_backend_installed  # noqa: PLC0415

    backend_env = env if env_scope else None
    if artifacts_backend_installed(backend_env):
        print("The keyring backend will handle authentication transparently.")
    elif env_scope:
        print(
            "  WARNING: No artifacts-keyring backend is installed in this "
            "environment, so the feed cannot authenticate yet.\n"
            "    Install one from your public bootstrap feed (the feed you"
            " installed pypi-lockdown from):\n"
            "      pip install artifacts-keyring-nofuss"
            " --index-url <PUBLIC_FEED_URL>   # pure-Python fork\n"
            "      pip install artifacts-keyring"
            " --index-url <PUBLIC_FEED_URL>          # upstream (requires .NET)"
        )
    else:
        # User/global scope: uv/pip authenticate via the `keyring` subprocess,
        # so a global `keyring` CLI must expose the backend. Recommend a tool
        # install rather than adding the backend to some interpreter. Both
        # backends live on the public bootstrap feed, so target it.
        print(
            "  WARNING: No global 'keyring' backend found on PATH, so the "
            "feed cannot authenticate yet.\n"
            "    Install keyring as a tool with an artifacts backend, from your"
            " public bootstrap feed (the feed you installed pypi-lockdown"
            " from):\n"
            "      uv tool install keyring --with artifacts-keyring-nofuss"
            " --index-url <PUBLIC_FEED_URL>   # pure-Python fork\n"
            "      uv tool install keyring --with artifacts-keyring"
            " --index-url <PUBLIC_FEED_URL>          # upstream (requires .NET)\n"
            "      # pipx equivalent: pipx install keyring"
            " --index-url <PUBLIC_FEED_URL>"
            " && pipx inject keyring <backend> --index-url <PUBLIC_FEED_URL>"
        )
    print()
