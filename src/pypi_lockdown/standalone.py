"""Detect shiv zipapp runtime and bootstrap bundled packages into the target env."""

from __future__ import annotations

import json
import os
import platform
import shutil
import sys
import zipfile
from pathlib import Path

# Packages that belong to pypi-lockdown itself and should NOT be copied
_SKIP_PREFIXES = frozenset({"pypi_lockdown", "pypi-lockdown", "shiv", "_shiv"})


def is_standalone() -> bool:
    """Return True if we're running inside a shiv zipapp."""
    try:
        return zipfile.is_zipfile(sys.argv[0])
    except (IndexError, OSError):
        return False


def _shiv_site_packages() -> Path | None:
    """Locate the shiv-extracted site-packages directory."""
    if not is_standalone():
        return None
    try:
        with zipfile.ZipFile(sys.argv[0]) as zf:
            env = json.loads(zf.read("environment.json").decode())
        build_id = env["build_id"]
        root = Path(env.get("root") or os.path.expanduser("~/.shiv"))
        pyz_name = Path(sys.argv[0]).name
        sp = root / f"{pyz_name}_{build_id}" / "site-packages"
        if sp.is_dir():
            return sp
    except (KeyError, OSError, json.JSONDecodeError):
        pass
    return None


def _target_site_packages(env_path: Path) -> Path:
    """Return the site-packages directory for a venv or conda environment."""
    if platform.system() == "Windows":
        return env_path / "Lib" / "site-packages"
    return (
        env_path
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )


def _should_skip(name: str) -> bool:
    """Return True if this item should not be copied to the target env."""
    if name == "__pycache__":
        return True
    lower = name.lower()
    # For .dist-info dirs: "pkg_name-1.2.3.dist-info" → split on first "-" → "pkg_name"
    # Normalized package names use underscores, never hyphens, so first "-" is the version separator.
    if lower.endswith(".dist-info") or lower.endswith(".data"):
        base = lower.split(".dist-info")[0].split(".data")[0]
        pkg = base.split("-", 1)[0]
    else:
        pkg = lower
    return pkg in _SKIP_PREFIXES


def bootstrap_keyring(env_path: Path) -> bool:
    """Copy bundled packages from the shiv site-packages into the target env.

    Returns True if packages were installed, False otherwise.
    """
    src = _shiv_site_packages()
    if src is None:
        return False

    dst = _target_site_packages(env_path)
    if not dst.is_dir():
        print(f"  ⚠ Target site-packages not found: {dst}")
        return False

    copied = []
    for item in sorted(src.iterdir()):
        if _should_skip(item.name):
            continue

        dest = dst / item.name
        if item.is_dir():
            shutil.copytree(item, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(item, dest)
        if item.name.endswith(".dist-info"):
            copied.append(item.name.removesuffix(".dist-info"))

    if copied:
        print("  Installed bundled packages into target environment:")
        for pkg in copied:
            print(f"    ✓ {pkg}")
    return bool(copied)
