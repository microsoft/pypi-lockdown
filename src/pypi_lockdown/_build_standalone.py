"""Build standalone .pyz zipapps for all target platforms using shiv.

Usage:
    python -m pypi_lockdown._build_standalone        # build all platforms
    python -m pypi_lockdown._build_standalone native  # build for current platform only
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

PLATFORMS = {
    "linux-x86_64": {
        "platform": ["manylinux2014_x86_64"],
        "python_version": "39",
    },
    "macos-universal2": {
        "platform": ["macosx_10_9_universal2"],
        "python_version": "39",
    },
    "win-amd64": {
        "platform": ["win_amd64"],
        "python_version": "39",
    },
}

ROOT = Path(__file__).resolve().parents[2]  # repo root


def _run(cmd: list[str], **kw) -> None:
    print(f"  $ {' '.join(cmd)}")
    subprocess.check_call(cmd, **kw)


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
        sys.executable, "-m", "shiv",
        "-e", "pypi_lockdown.__main__:main",
        "-o", str(output),
        "--compressed",
        str(ROOT),
    ]
    if pip_args:
        cmd.extend(pip_args)
    _run(cmd)
    print(f"\n  ✓ {output} ({output.stat().st_size // 1024} KB)")
    return output


def build_cross(target: str, dist_dir: Path, pip_args: list[str] | None = None) -> Path:
    """Build a .pyz for a different platform via cross-download."""
    cfg = PLATFORMS[target]
    dist_dir.mkdir(parents=True, exist_ok=True)
    output = dist_dir / f"pypi-lockdown-{target}.pyz"

    with tempfile.TemporaryDirectory() as tmp:
        wheel_dir = Path(tmp) / "wheels"
        staging = Path(tmp) / "site-packages"
        wheel_dir.mkdir()

        # Download wheels for the target platform
        cmd = [
            sys.executable, "-m", "pip", "download",
            "--only-binary", ":all:",
            "--python-version", cfg["python_version"],
            "-d", str(wheel_dir),
        ]
        for plat in cfg["platform"]:
            cmd += ["--platform", plat]
        # Also accept pure-Python wheels
        cmd += ["--platform", "any"]
        if pip_args:
            cmd.extend(pip_args)
        cmd.append(str(ROOT))
        _run(cmd)

        # Extract wheels into a site-packages layout
        _extract_wheels(wheel_dir, staging)

        # Build the zipapp with shiv
        _run([
            sys.executable, "-m", "shiv",
            "--site-packages", str(staging),
            "-e", "pypi_lockdown.__main__:main",
            "-o", str(output),
            "--compressed",
        ])

    print(f"\n  ✓ {output} ({output.stat().st_size // 1024} KB)")
    return output


def main() -> None:
    dist_dir = ROOT / "dist"

    # Separate our args from extra pip args (after --)
    argv = sys.argv[1:]
    if "--" in argv:
        sep = argv.index("--")
        our_args = argv[:sep]
        pip_args = argv[sep + 1:]
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
        targets = ", ".join(["native", "all"] + list(PLATFORMS))
        print(f"Usage: python -m pypi_lockdown._build_standalone [{targets}] [-- PIP_ARGS]")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
