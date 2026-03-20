"""Detect shiv zipapp runtime and bootstrap bundled packages into the target env."""

from __future__ import annotations

import json
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
        root = Path(env.get("root") or Path("~/.shiv").expanduser())
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
    # For .dist-info dirs: "pkg_name-1.2.3.dist-info" → split on first "-"
    # Normalized package names use underscores, never hyphens,
    # so first "-" is the version separator.
    if lower.endswith((".dist-info", ".data")):
        base = lower.split(".dist-info")[0].split(".data")[0]
        pkg = base.split("-", 1)[0]
    else:
        pkg = lower
    return pkg in _SKIP_PREFIXES


def _parse_dist_info(name: str) -> tuple[str, str] | None:
    """Extract ``(normalised_name, version)`` from a ``.dist-info`` dir name."""
    base = name.removesuffix(".dist-info")
    parts = base.split("-", 1)
    if len(parts) != 2:  # name-version
        return None
    return parts[0].lower().replace("-", "_"), parts[1]


def _installed_packages(site_packages: Path) -> dict[str, str]:
    """Return ``{normalised_name: version}`` for every package in *site_packages*."""
    installed: dict[str, str] = {}
    for di in site_packages.glob("*.dist-info"):
        parsed = _parse_dist_info(di.name)
        if parsed:
            installed[parsed[0]] = parsed[1]
    return installed


def _pkg_name_for(item_name: str) -> str:
    """Return the normalised package name that *item_name* belongs to."""
    lower = item_name.lower()
    if lower.endswith((".dist-info", ".data")):
        base = lower.split(".dist-info")[0].split(".data")[0]
        return base.split("-", 1)[0].replace("-", "_")
    return lower.replace("-", "_")


def _classify_packages(
    bundled: dict[str, str],
    existing: dict[str, str],
) -> tuple[set[str], list[str], list[tuple[str, str, str]]]:
    """Compare bundled vs existing and return (skip_set, same, conflicts)."""
    skip_pkgs: set[str] = set()
    skipped_same: list[str] = []
    skipped_conflict: list[tuple[str, str, str]] = []
    for name, bundled_ver in bundled.items():
        installed_ver = existing.get(name)
        if installed_ver is not None:
            if installed_ver == bundled_ver:
                skipped_same.append(f"{name}-{bundled_ver}")
            else:
                skipped_conflict.append((name, installed_ver, bundled_ver))
            skip_pkgs.add(name)
    return skip_pkgs, skipped_same, skipped_conflict


def _report_bootstrap(
    copied: list[str],
    skipped_same: list[str],
    skipped_conflict: list[tuple[str, str, str]],
) -> None:
    """Print a human-readable summary of the bootstrap result."""
    if copied:
        print("  Installed bundled packages:")
        for pkg in copied:
            print(f"    ✓ {pkg}")
    if skipped_same:
        print("  Already installed (same version):")
        for pkg in skipped_same:
            print(f"    · {pkg}")
    if skipped_conflict:
        print("  ⚠ Skipped (different version already installed):")
        for name, inst, bund in skipped_conflict:
            print(f"    · {name}: installed {inst}, bundled {bund}")


def bootstrap_keyring(env_path: Path) -> bool:
    """Copy bundled packages from the shiv site-packages into the target env.

    Skips packages that are already installed.  Warns (and skips) when the
    installed version differs from the bundled version so that carefully-pinned
    environments are not silently corrupted.

    Returns True if any packages were installed, False otherwise.
    """
    src = _shiv_site_packages()
    if src is None:
        return False

    dst = _target_site_packages(env_path)
    if not dst.is_dir():
        print(f"  ⚠ Target site-packages not found: {dst}")
        return False

    existing = _installed_packages(dst)

    # Determine which bundled packages are present in src.
    bundled: dict[str, str] = {}
    for di in sorted(src.glob("*.dist-info")):
        parsed = _parse_dist_info(di.name)
        if parsed:
            bundled[parsed[0]] = parsed[1]

    skip_pkgs, skipped_same, skipped_conflict = _classify_packages(bundled, existing)

    copied: list[str] = []
    for item in sorted(src.iterdir()):
        if _should_skip(item.name):
            continue
        if _pkg_name_for(item.name) in skip_pkgs:
            continue

        dest = dst / item.name
        if item.is_dir():
            shutil.copytree(item, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(item, dest)
        if item.name.endswith(".dist-info"):
            copied.append(item.name.removesuffix(".dist-info"))

    _report_bootstrap(copied, skipped_same, skipped_conflict)
    return bool(copied)
