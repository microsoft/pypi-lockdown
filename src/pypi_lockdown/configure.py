"""Write pip / uv / poetry configuration pointing at an internal PyPI feed."""

from __future__ import annotations

import configparser
import os
import platform
import shutil
import subprocess
from pathlib import Path

_MARKER = (
    "# Managed by pypi-lockdown -- safe to edit, will be overwritten on next run\n"
)
_MARKER_SUBSTR = "Managed by pypi-lockdown"


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


def _is_windows() -> bool:
    return platform.system() == "Windows"


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
    # uv/pip authenticate via the `keyring` subprocess provider, so a global
    # `keyring` CLI (not an importable module) supplies the credentials.
    cfg.set("global", "keyring-provider", "subprocess")

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


def _configure_pyproject(index_url: str) -> None:
    """Write project-level uv/poetry/hatch config into ``./pyproject.toml``.

    Opt-in via ``--project``. Global pip/uv config already covers pip, uv, and
    Hatch; the main reason to write project config is Poetry, which ignores
    pip/uv config and needs its own ``[[tool.poetry.source]]``.
    """
    pyproject = Path.cwd() / "pyproject.toml"
    if not pyproject.exists():
        print(
            "\n  --project given, but no pyproject.toml in the current"
            " directory; nothing to write."
        )
        _print_poetry_instructions(index_url)
        return

    print(f"\n  Writing project config to {pyproject}")
    _write_pyproject_uv(pyproject, index_url)
    _write_pyproject_poetry(pyproject, index_url)
    _write_pyproject_hatch(pyproject, index_url)


def _hint_project_config() -> None:
    """When ``--project`` was not given, note that project config is opt-in."""
    if (Path.cwd() / "pyproject.toml").exists():
        print(
            "  Detected pyproject.toml. Global config already covers pip, uv,"
            " and Hatch. To also pin the index in this project -- or to"
            " configure Poetry, which ignores global pip/uv config -- re-run"
            " with --project.\n"
        )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def _keyring_backend_available() -> bool:
    """Return True if a ``keyring`` CLI on PATH exposes an artifacts backend.

    uv (and pip with ``--keyring-provider subprocess``) authenticate by
    invoking ``keyring`` as a subprocess, so the backend must be reachable
    through that executable. Plain ``keyring`` alone cannot authenticate an
    Azure Artifacts feed.
    """
    exe = shutil.which("keyring")
    if exe is None:
        return False
    try:
        result = subprocess.run(
            [exe, "--list-backends"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and "ArtifactsKeyring" in result.stdout


def configure(
    index_url: str,
    *,
    project_scope: bool = False,
    ci: bool = False,
) -> None:
    if not index_url.startswith("https://"):
        print(
            f"\n  ERROR: Refusing to configure non-HTTPS index URL: {index_url}\n"
            "    HTTPS is required to protect credentials and package integrity.\n"
        )
        raise SystemExit(1)

    print(f"\nConfiguring index: {index_url}\n")

    # --- pip (user-global) ---
    print("Configuring user-global config (all environments).\n")
    _write_pip_config(_pip_config_user(), index_url)

    # --- uv (user-level only) ---
    _write_uv_config(_uv_config_user(), index_url)

    # --- project-level pyproject.toml (opt-in via --project) ---
    if project_scope:
        _configure_pyproject(index_url)
    elif not ci:
        _hint_project_config()

    # --- verify a keyring backend is present ---
    if _keyring_backend_available():
        print("The keyring backend will handle authentication transparently.")
    else:
        # uv/pip authenticate via the `keyring` subprocess, so a global
        # `keyring` CLI must expose the backend. Recommend a tool install
        # rather than adding the backend to some interpreter. Both backends
        # live on the public bootstrap feed, so target it.
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


# ---------------------------------------------------------------------------
# status / undo
# ---------------------------------------------------------------------------


def _is_managed(path: Path) -> bool:
    """Return True if *path* starts with the pypi-lockdown marker comment."""
    try:
        with path.open() as fh:
            first = fh.readline()
    except OSError:
        return False
    return _MARKER_SUBSTR in first


def _read_pip_index(path: Path) -> str | None:
    """Read ``[global] index-url`` from a pip config file, userinfo stripped."""
    cfg = configparser.ConfigParser()
    try:
        cfg.read(path)
    except configparser.Error:
        return None
    if cfg.has_option("global", "index-url"):
        return _strip_userinfo(cfg.get("global", "index-url"))
    return None


def _read_uv_index(path: Path) -> str | None:
    """Read the default index URL from a uv config file, userinfo stripped."""
    import tomlkit  # noqa: PLC0415

    try:
        doc = tomlkit.parse(path.read_text())
    except (OSError, tomlkit.exceptions.TOMLKitError):
        return None
    for idx in doc.get("index", []):
        if isinstance(idx, dict) and idx.get("default"):
            url = idx.get("url")
            if url:
                return _strip_userinfo(str(url))
    pip = doc.get("pip", {})
    if isinstance(pip, dict) and pip.get("index-url"):
        return _strip_userinfo(str(pip["index-url"]))
    return None


def _print_status_row(
    label: str, path: Path, url: str | None, *, managed: bool
) -> None:
    if url is None:
        print(f"  {label:<11} (not configured)   {path}")
        return
    tag = "[managed]" if managed else "[not managed by pypi-lockdown]"
    print(f"  {label:<11} {tag}\n{'':14}-> {url}\n{'':14}   {path}")


def status() -> None:
    """Report the current index configuration for pip, uv, and the project."""
    print("\npypi-lockdown status\n")

    pip_user = _pip_config_user()
    _print_status_row(
        "pip (user)",
        pip_user,
        _read_pip_index(pip_user) if pip_user.exists() else None,
        managed=_is_managed(pip_user),
    )

    uv_user = _uv_config_user()
    _print_status_row(
        "uv (user)",
        uv_user,
        _read_uv_index(uv_user) if uv_user.exists() else None,
        managed=_is_managed(uv_user),
    )

    pyproject = Path.cwd() / "pyproject.toml"
    if pyproject.exists():
        proj_url = detect_index_url()
        if proj_url:
            print(
                f"  {'project':<11} [uv/poetry/hatch]\n{'':14}-> {proj_url}"
                f"\n{'':14}   {pyproject}"
            )
        else:
            print(f"  {'project':<11} (no pypi-lockdown config)   {pyproject}")
    print()


def _undo_pip(path: Path) -> bool:
    """Remove managed pip settings from *path*. Returns True if it was managed."""
    if not path.exists() or not _is_managed(path):
        return False

    cfg = configparser.ConfigParser()
    cfg.read(path)
    if cfg.has_section("global"):
        for opt in ("index-url", "keyring-provider"):
            if cfg.has_option("global", opt):
                cfg.remove_option("global", opt)
        if not cfg.options("global"):
            cfg.remove_section("global")

    if not cfg.sections():
        path.unlink()
        print(f"  removed {path}")
    else:
        # Other settings remain: rewrite without the marker (no longer managed).
        with path.open("w") as fh:
            cfg.write(fh)
        print(f"  cleaned {path} (kept other settings)")
    return True


def _undo_uv(path: Path) -> bool:
    """Delete the fully-generated uv config if managed. Returns True if removed."""
    if not path.exists() or not _is_managed(path):
        return False
    path.unlink()
    print(f"  removed {path}")
    return True


def _undo_pyproject_uv(tool: dict[str, object]) -> bool:
    """Strip pypi-lockdown ``[tool.uv]`` settings. Returns True if changed."""
    import tomlkit  # noqa: PLC0415

    uv = tool.get("uv")
    if not isinstance(uv, dict):
        return False
    changed = False
    if "keyring-provider" in uv:
        del uv["keyring-provider"]
        changed = True
    indexes = uv.get("index")
    if indexes is not None:
        kept = tomlkit.aot()
        for idx in indexes:
            if idx.get("default"):
                changed = True
            else:
                kept.append(idx)
        if len(kept) == 0:
            del uv["index"]
        else:
            uv["index"] = kept
    if not uv:
        del tool["uv"]
    return changed


def _undo_pyproject_poetry(tool: dict[str, object]) -> bool:
    """Strip pypi-lockdown ``[[tool.poetry.source]]`` entries. True if changed."""
    import tomlkit  # noqa: PLC0415

    poetry = tool.get("poetry")
    if not isinstance(poetry, dict):
        return False
    changed = False
    sources = poetry.get("source")
    if sources is not None:
        kept = tomlkit.aot()
        for src in sources:
            if src.get("name") in ("internal", "PyPI") or src.get("priority") == (
                "primary"
            ):
                changed = True
            else:
                kept.append(src)
        if len(kept) == 0:
            del poetry["source"]
        else:
            poetry["source"] = kept
    if not poetry:
        del tool["poetry"]
    return changed


def _undo_pyproject_hatch(tool: dict[str, object]) -> bool:
    """Strip pypi-lockdown Hatch env-vars. Returns True if changed."""
    hatch = tool.get("hatch")
    if not isinstance(hatch, dict):
        return False
    envs = hatch.get("envs")
    if not isinstance(envs, dict):
        return False
    default = envs.get("default")
    if not isinstance(default, dict):
        return False
    env_vars = default.get("env-vars")
    if not isinstance(env_vars, dict):
        return False
    changed = False
    for key in ("PIP_INDEX_URL", "UV_DEFAULT_INDEX"):
        if key in env_vars:
            del env_vars[key]
            changed = True
    # Drop containers we created if they are now empty (leave [tool.hatch]).
    if not env_vars:
        del default["env-vars"]
    if not default:
        del envs["default"]
    if not envs:
        del hatch["envs"]
    return changed


def _undo_pyproject(path: Path) -> bool:
    """Strip pypi-lockdown config from ``pyproject.toml``. Returns True if changed."""
    import tomlkit  # noqa: PLC0415

    doc = tomlkit.parse(path.read_text())
    tool = doc.get("tool")
    if not isinstance(tool, dict):
        return False

    changed = _undo_pyproject_uv(tool)
    changed = _undo_pyproject_poetry(tool) or changed
    changed = _undo_pyproject_hatch(tool) or changed

    if changed:
        path.write_text(tomlkit.dumps(doc))
        print(f"  cleaned {path}")
    return changed


def undo(*, project_scope: bool = False) -> None:
    """Remove pypi-lockdown-managed configuration files/sections."""
    print("\nRemoving pypi-lockdown configuration...\n")
    removed = False

    if _undo_pip(_pip_config_user()):
        removed = True
    if _undo_uv(_uv_config_user()):
        removed = True

    pyproject = Path.cwd() / "pyproject.toml"
    if project_scope:
        if pyproject.exists() and _undo_pyproject(pyproject):
            removed = True
    elif pyproject.exists() and detect_index_url():
        print(
            "  Note: ./pyproject.toml contains pypi-lockdown config;"
            " re-run 'undo --project' to remove it."
        )

    if not removed:
        print("  Nothing to remove -- no managed configuration found.")
    print()
