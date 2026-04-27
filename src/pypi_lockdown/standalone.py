"""Bootstrap keyring packages into a target Python environment.

Supports two source modes:
- **shiv zipapp**: copy from the shiv-extracted site-packages (original path)
- **installed** (pipx / uv tool): copy from the current process's site-packages

Only broadly compatible packages are copied: pure-Python
(``py3-none-any``) distributions and ``abi3`` wheels, to reduce ABI issues
when the source and target Python versions differ.
"""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

# Packages that belong to pypi-lockdown itself and should NOT be copied
_SKIP_PREFIXES = frozenset({"pypi_lockdown", "pypi-lockdown", "shiv", "_shiv"})

# Root packages to bootstrap -- their transitive deps are resolved at runtime
_BOOTSTRAP_ROOTS = ("artifacts-keyring-nofuss", "keyring")


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


def _target_site_packages(env_path: Path) -> Path | None:
    """Return the site-packages directory for a venv or conda environment.

    Tries to ask the target env's own Python first; falls back to
    platform-specific heuristics.
    """
    python = _target_python(env_path)
    if python and python.is_file():
        try:
            _cmd = [
                str(python),
                "-c",
                "import sysconfig; print(sysconfig.get_path('purelib'))",
            ]
            result = subprocess.run(
                _cmd,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if result.returncode == 0:
                sp = Path(result.stdout.strip())
                if sp.is_dir():
                    return sp
        except (OSError, subprocess.TimeoutExpired):
            pass

    # Fallback: guess from env layout
    if platform.system() == "Windows":
        sp = env_path / "Lib" / "site-packages"
    else:
        candidates = sorted(
            (env_path / "lib").glob("python*/site-packages"),
            key=lambda p: tuple(
                int(x) for x in p.parts[-2].removeprefix("python").split(".")
            ),
        )
        major, minor = sys.version_info.major, sys.version_info.minor
        sp = (
            candidates[-1]
            if candidates
            else (env_path / "lib" / f"python{major}.{minor}" / "site-packages")
        )
    return sp if sp.is_dir() else None


def _target_python(env_path: Path) -> Path | None:
    """Return the Python executable inside a venv/conda environment."""
    if platform.system() == "Windows":
        p = env_path / "Scripts" / "python.exe"
    else:
        p = env_path / "bin" / "python"
    return p if p.is_file() else None


def _target_python_version(env_path: Path) -> tuple[int, int] | None:
    """Return ``(major, minor)`` of the target environment's Python."""
    python = _target_python(env_path)
    if python is None:
        return None
    try:
        result = subprocess.run(
            [str(python), "-c", "import sys; print(sys.version_info[:2])"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0:
            parts = result.stdout.strip().strip("()").split(",")
            return int(parts[0]), int(parts[1])
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def _process_site_packages() -> Path | None:
    """Locate site-packages of the current running process.

    Uses ``importlib.metadata`` to find where ``keyring`` is installed,
    then derives the site-packages root from the dist-info location.
    """
    import importlib.metadata as md  # noqa: PLC0415

    for probe in _BOOTSTRAP_ROOTS:
        try:
            dist = md.distribution(probe)
        except md.PackageNotFoundError:
            continue
        # Use public locate_file API to resolve site-packages root.
        # locate_file("") returns the base directory containing the dist-info.
        sp = Path(str(dist.locate_file(""))).resolve()
        if sp.is_dir():
            return sp
    return None


def _resolve_bootstrap_allowlist(
    site_packages: Path,
    *,
    native_ok: bool = False,
) -> set[str]:
    """Build the set of normalised package names to bootstrap.

    Starts from ``_BOOTSTRAP_ROOTS`` and recursively adds their
    ``Requires-Dist`` dependencies (non-extra only).  Only includes
    packages that are actually installed in *site_packages*.

    When *native_ok* is False (the default), only pure-Python
    ``py3-none-any`` distributions and ``abi3`` wheels are included.
    When True, C-extension packages are included too (safe when the
    source and target Python share the same version and platform).
    """
    installed = _installed_packages(site_packages)
    allowlist: set[str] = set()
    queue = list(_BOOTSTRAP_ROOTS)

    while queue:
        raw_name = queue.pop()
        name = _normalise_name(raw_name)
        if name in allowlist or name not in installed:
            continue
        # Check purelib -- skip C extensions unless native_ok
        if not native_ok and not _is_pure_python(site_packages, name):
            continue
        allowlist.add(name)
        # Enqueue runtime deps (skip extras, skip markers we can't evaluate)
        queue.extend(_runtime_deps(site_packages, name))

    # Remove packages we never want to copy
    allowlist -= {_normalise_name(p) for p in _SKIP_PREFIXES}
    return allowlist


def _is_pure_python(site_packages: Path, normalised_name: str) -> bool:
    """Return True if the package has a ``py3-none-any`` (or similar pure) wheel tag."""
    for di in site_packages.glob("*.dist-info"):
        parsed = _parse_dist_info(di.name)
        if parsed and parsed[0] == normalised_name:
            wheel_file = di / "WHEEL"
            if wheel_file.exists():
                for line in wheel_file.read_text(encoding="utf-8").splitlines():
                    if line.startswith("Tag:"):
                        tag = line.split(":", 1)[1].strip()
                        if "none-any" in tag:
                            return True
                        # abi3 is stable ABI -- compatible across versions
                        if "abi3" in tag:
                            return True
                return False
    # No WHEEL file found -- can't confirm pure Python, skip to be safe.
    return False


def _bare_pkg_name(spec: str) -> str:
    """Extract the bare package name from a Requires-Dist specifier."""
    import re  # noqa: PLC0415

    # Take everything before markers (;) and strip version specifiers
    name_part = spec.split(";", maxsplit=1)[0].strip().split()[0]
    return re.split(r"[><=!\[~]", name_part, maxsplit=1)[0]


def _runtime_deps(site_packages: Path, normalised_name: str) -> list[str]:
    """Return non-extra Requires-Dist names for a package."""
    for di in site_packages.glob("*.dist-info"):
        parsed = _parse_dist_info(di.name)
        if parsed and parsed[0] == normalised_name:
            metadata_file = di / "METADATA"
            if not metadata_file.exists():
                return []
            deps: list[str] = []
            for line in metadata_file.read_text(encoding="utf-8").splitlines():
                if not line.startswith("Requires-Dist:"):
                    continue
                spec = line.split(":", 1)[1].strip()
                # Skip extras: "foo ; extra == ..."
                if "extra ==" in spec or "extra==" in spec:
                    continue
                # Include deps with environment markers unconditionally --
                # they may be needed on the target platform, and excluding
                # them risks missing packages.  The allowlist already
                # filters to actually-installed packages.
                deps.append(_bare_pkg_name(spec))
            return deps
    return []


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


def _normalise_name(name: str) -> str:
    """Return the dist-info / wheel-style normalised package name."""
    return name.lower().replace("-", "_").replace(".", "_")


def _parse_dist_info(name: str) -> tuple[str, str] | None:
    """Extract ``(normalised_name, version)`` from a ``.dist-info`` dir name."""
    base = name.removesuffix(".dist-info")
    parts = base.split("-", 1)
    if len(parts) != 2:  # name-version
        return None
    return _normalise_name(parts[0]), parts[1]


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
            print(f"    + {pkg}")
    if skipped_same:
        print("  Already installed (same version):")
        for pkg in skipped_same:
            print(f"    - {pkg}")
    if skipped_conflict:
        print("  WARNING: Skipped (different version already installed):")
        for name, inst, bund in skipped_conflict:
            print(f"    - {name}: installed {inst}, bundled {bund}")


def _find_source() -> tuple[Path | None, bool]:
    """Find the source site-packages and whether to use an allowlist."""
    src = _shiv_site_packages()
    if src is not None:
        return src, False  # shiv: copy all non-skip packages
    return _process_site_packages(), True  # pipx/uv: use allowlist


def _collect_bundled(
    src: Path,
    allowed: set[str] | None,
) -> dict[str, str]:
    """Collect package names and versions from source site-packages."""
    bundled: dict[str, str] = {}
    for di in sorted(src.glob("*.dist-info")):
        parsed = _parse_dist_info(di.name)
        if not parsed:
            continue
        if allowed is not None and parsed[0] not in allowed:
            continue
        bundled[parsed[0]] = parsed[1]
    return bundled


def _expand_extension_stems(src: Path, stems: set[str]) -> set[str]:
    """Find files in *src* whose stem (before the first dot) matches *stems*.

    C extensions have platform-specific suffixes
    (e.g. ``_cffi_backend.cpython-312-x86_64-linux-gnu.so``)
    that are not listed in ``top_level.txt``.
    """
    extra: set[str] = set()
    for item in src.iterdir():
        if item.is_file() and item.name.split(".", 1)[0] in stems:
            extra.add(item.name)
    return extra


def _toplevel_from_dist(di: Path, src: Path) -> set[str]:
    """Return top-level file/dir names owned by one distribution.

    Reads ``top_level.txt`` first; falls back to ``RECORD``.
    """
    owned: set[str] = set()

    top_level = di / "top_level.txt"
    if top_level.exists():
        stems: set[str] = set()
        for line in top_level.read_text(encoding="utf-8").splitlines():
            name = line.strip()
            if name:
                owned.add(name)
                stems.add(name)
        owned |= _expand_extension_stems(src, stems)
        return owned

    record = di / "RECORD"
    if record.exists():
        for line in record.read_text(encoding="utf-8").splitlines():
            entry = line.split(",")[0]
            if not entry or entry.startswith(di.name):
                continue
            top = entry.split("/")[0]
            if top and top != "__pycache__":
                owned.add(top)
    return owned


def _owned_toplevel_dirs(
    src: Path,
    allowed: set[str],
) -> set[str]:
    """Return top-level file/dir names owned by allowlisted distributions.

    Uses ``top_level.txt`` and ``RECORD`` from dist-info to determine
    which filesystem entries to copy.  This correctly handles namespace
    packages (e.g. ``jaraco/`` owned by ``jaraco.classes``).
    """
    owned: set[str] = set()
    for di in src.glob("*.dist-info"):
        parsed = _parse_dist_info(di.name)
        if not parsed or parsed[0] not in allowed:
            continue
        owned.add(di.name)
        owned |= _toplevel_from_dist(di, src)
    return owned


def _copy_packages(
    src: Path,
    dst: Path,
    allowed: set[str] | None,
    skip_pkgs: set[str],
) -> list[str]:
    """Copy eligible packages from *src* to *dst*. Returns list of copied."""
    eligible = _eligible_items(src, allowed, skip_pkgs)

    copied: list[str] = []
    for item in sorted(src.iterdir()):
        if item.name not in eligible:
            continue
        dest = dst / item.name
        if item.is_dir():
            shutil.copytree(item, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(item, dest)
        if item.name.endswith(".dist-info"):
            copied.append(item.name.removesuffix(".dist-info"))
    return copied


def _eligible_items(
    src: Path,
    allowed: set[str] | None,
    skip_pkgs: set[str],
) -> set[str]:
    """Return filesystem entry names eligible for copying."""
    if allowed is not None:
        effective = allowed - skip_pkgs
        owned = _owned_toplevel_dirs(src, effective)
        # Remove dist-info dirs for skipped packages
        for di in src.glob("*.dist-info"):
            parsed = _parse_dist_info(di.name)
            if parsed and parsed[0] in skip_pkgs:
                owned.discard(di.name)
        return owned

    # Shiv mode: all items except skip-prefixed and skipped packages
    eligible: set[str] = set()
    for item in src.iterdir():
        if _should_skip(item.name):
            continue
        if _pkg_name_for(item.name) in skip_pkgs:
            continue
        eligible.add(item.name)
    return eligible


def bootstrap_keyring(env_path: Path) -> bool:
    """Copy keyring packages into the target env's site-packages.

    Tries shiv site-packages first (zipapp mode), then falls back to
    the current process's site-packages (pipx / uv tool mode).

    In shiv mode, copies all non-skip packages (backward compat).
    In process mode, copies only an allowlisted set of keyring-related
    packages and their transitive pure-Python dependencies.

    Skips packages that are already installed.  Warns (and skips) when the
    installed version differs from the bundled version so that carefully-pinned
    environments are not silently corrupted.

    Returns True if any packages were installed, False otherwise.
    """
    src, use_allowlist = _find_source()
    if src is None:
        return False

    dst = _target_site_packages(env_path)
    if dst is None:
        print(f"  WARNING: Target site-packages not found for: {env_path}")
        return False

    # Same env -- packages are already available
    if src.resolve() == dst.resolve():
        return False

    allowed = (
        _resolve_bootstrap_allowlist(
            src,
            native_ok=_target_python_version(env_path)
            == (sys.version_info.major, sys.version_info.minor),
        )
        if use_allowlist
        else None
    )
    bundled = _collect_bundled(src, allowed)
    if not bundled:
        return False

    existing = _installed_packages(dst)
    skip_pkgs, skipped_same, skipped_conflict = _classify_packages(
        bundled,
        existing,
    )
    copied = _copy_packages(src, dst, allowed, skip_pkgs)
    _report_bootstrap(copied, skipped_same, skipped_conflict)
    return bool(copied)
