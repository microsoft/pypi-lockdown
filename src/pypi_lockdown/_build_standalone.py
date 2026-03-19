"""Build standalone .pyz zipapps for all target platforms using shiv.

Usage:
    python -m pypi_lockdown._build_standalone        # build all platforms
    python -m pypi_lockdown._build_standalone native  # build for current platform only
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import TypedDict


class _PlatformCfg(TypedDict):
    platform: list[str]
    python_version: str


PLATFORMS: dict[str, _PlatformCfg] = {
    "linux-x86_64": {
        "platform": ["manylinux2014_x86_64"],
        "python_version": "310",
    },
    "macos-universal2": {
        "platform": ["macosx_11_0_universal2"],
        "python_version": "310",
    },
    "win-amd64": {
        "platform": ["win_amd64"],
        "python_version": "310",
    },
}


def _find_repo_root() -> Path:
    """Locate the repo root by searching for pyproject.toml.

    When running from a source checkout ``parents[2]`` works, but when the
    package is installed into a venv (e.g. via tox) the file lives under
    site-packages and that heuristic breaks.  Walk upward from the file
    first, then fall back to cwd.
    """
    for anchor in (Path(__file__).resolve(), Path.cwd()):
        cur = anchor if anchor.is_dir() else anchor.parent
        while True:
            if (cur / "pyproject.toml").is_file():
                return cur
            parent = cur.parent
            if parent == cur:
                break
            cur = parent
    msg = (
        "Cannot locate repo root (no pyproject.toml found). "
        "Run this script from inside the pypi-lockdown source tree."
    )
    raise FileNotFoundError(msg)


ROOT = _find_repo_root()


def _run(cmd: list[str], **kw: object) -> None:
    print(f"  $ {' '.join(cmd)}")
    subprocess.check_call(cmd, **kw)  # type: ignore[arg-type]


def _extract_wheels(wheel_dir: Path, staging: Path) -> None:
    """Unzip all .whl files into a flat site-packages layout."""
    staging.mkdir(parents=True, exist_ok=True)
    for whl in sorted(wheel_dir.glob("*.whl")):
        with zipfile.ZipFile(whl) as zf:
            zf.extractall(staging)


def build_native(dist_dir: Path, pip_args: list[str] | None = None) -> Path:
    """Build a .pyz for the current platform using shiv directly."""
    dist_dir.mkdir(parents=True, exist_ok=True)
    output = dist_dir / "pypi-lockdown.pyz"
    cmd = [
        sys.executable,
        "-m",
        "shiv",
        "-e",
        "pypi_lockdown.__main__:main",
        "-o",
        str(output),
        "--compressed",
        str(ROOT),
    ]
    if pip_args:
        cmd.extend(pip_args)
    _run(cmd)
    print(f"\n  ✓ {output} ({output.stat().st_size // 1024} KB)")
    return output


def _resolve_deps(package_path: Path) -> list[str]:
    """Resolve the dependency tree natively and return pinned requirements.

    By resolving once on the current platform we get exact versions instantly.
    Cross-platform downloads can then use ``--no-deps`` to skip the slow
    resolver entirely.
    """
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        report_path = f.name
    try:
        _run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--dry-run",
                "--ignore-installed",
                "--report",
                report_path,
                "--quiet",
                str(package_path),
            ]
        )
        report = json.loads(Path(report_path).read_text())
    finally:
        Path(report_path).unlink(missing_ok=True)

    pkg_name = "pypi_lockdown"
    reqs = []
    for item in report["install"]:
        meta = item["metadata"]
        name = meta["name"]
        if name.replace("-", "_").lower() == pkg_name:
            continue
        reqs.append(f"{name}=={meta['version']}")
    return reqs


def build_cross(target: str, dist_dir: Path, pip_args: list[str] | None = None) -> Path:
    """Build a .pyz for a different platform via cross-download.

    Uses a two-phase strategy to avoid pip's slow cross-platform resolver:
    1. Resolve the full dependency tree natively (fast).
    2. Download pinned wheels per-package with ``--no-deps`` for the target
       platform (no backtracking).
    """
    cfg = PLATFORMS[target]
    dist_dir.mkdir(parents=True, exist_ok=True)
    output = dist_dir / f"pypi-lockdown-{target}.pyz"

    with tempfile.TemporaryDirectory() as tmp:
        wheel_dir = Path(tmp) / "wheels"
        staging = Path(tmp) / "site-packages"
        wheel_dir.mkdir()

        # Phase 1: build local package wheel
        _run(
            [
                sys.executable,
                "-m",
                "pip",
                "wheel",
                "--no-deps",
                "-w",
                str(wheel_dir),
                str(ROOT),
            ]
        )

        # Phase 2: resolve deps natively, then download each for the target
        deps = _resolve_deps(ROOT)
        print(f"  resolved {len(deps)} dependencies: {', '.join(deps)}")

        platform_args: list[str] = []
        for plat in cfg["platform"]:
            platform_args += ["--platform", plat]
        platform_args += ["--platform", "any"]

        for dep in deps:
            cmd = [
                sys.executable,
                "-m",
                "pip",
                "download",
                "--no-deps",
                "--only-binary",
                ":all:",
                "--python-version",
                cfg["python_version"],
                "-d",
                str(wheel_dir),
                *platform_args,
            ]
            if pip_args:
                cmd.extend(pip_args)
            cmd.append(dep)
            try:
                _run(cmd)
            except subprocess.CalledProcessError:
                print(f"  ⚠ Skipping {dep} (no wheel for {target})")

        # Extract wheels into a site-packages layout
        _extract_wheels(wheel_dir, staging)

        # Build the zipapp with shiv
        _run(
            [
                sys.executable,
                "-m",
                "shiv",
                "--site-packages",
                str(staging),
                "-e",
                "pypi_lockdown.__main__:main",
                "-o",
                str(output),
                "--compressed",
            ]
        )

    print(f"\n  ✓ {output} ({output.stat().st_size // 1024} KB)")
    return output


def main() -> None:
    dist_dir = ROOT / "dist"

    # Separate our args from extra pip args (after --)
    argv = sys.argv[1:]
    if "--" in argv:
        sep = argv.index("--")
        our_args = argv[:sep]
        pip_args = argv[sep + 1 :]
    else:
        our_args = argv
        pip_args = []

    mode = our_args[0] if our_args else "all"

    if mode == "native":
        print("\n=== Building for current platform ===\n")
        build_native(dist_dir, pip_args)
    elif mode == "all":
        for target in PLATFORMS:
            print(f"\n=== Building for {target} ===\n")
            build_cross(target, dist_dir, pip_args)
    elif mode in PLATFORMS:
        print(f"\n=== Building for {mode} ===\n")
        build_cross(mode, dist_dir, pip_args)
    else:
        targets = ", ".join(["native", "all", *list(PLATFORMS)])
        print(
            f"Usage: python -m pypi_lockdown._build_standalone"
            f" [{targets}] [-- PIP_ARGS]",
        )
        raise SystemExit(1)


if __name__ == "__main__":
    main()
