"""Generate a minimal wrapper package that hardcodes a private feed URL."""

from __future__ import annotations

import re
from pathlib import Path


def _to_module_name(package_name: str) -> str:
    return re.sub(r"[-.]", "_", package_name)


_PYPROJECT_TOML = """\
[build-system]
requires = ["setuptools>=64", "setuptools-scm>=8"]
build-backend = "setuptools.build_meta"

[project]
name = "{name}"
dynamic = ["version"]
description = "Lock down pip/uv/poetry to use the {name} internal feed"
requires-python = ">=3.9"
license = "MIT"
dependencies = [
    "pypi-lockdown",
]

[tool.setuptools-scm]

[tool.setuptools.packages.find]
where = ["src"]
"""

_MAIN_PY = """\
from pypi_lockdown.configure import configure

INDEX_URL = "{index_url}"


def main():
    import argparse

    parser = argparse.ArgumentParser(
        prog="{name}",
        description="Lock down pip/uv/poetry to use the {name} internal feed.",
    )
    parser.add_argument(
        "--user",
        action="store_true",
        help="Write pip config to user home instead of the active Python environment",
    )
    args = parser.parse_args()
    configure(INDEX_URL, user_scope=args.user)


if __name__ == "__main__":
    main()
"""


def scaffold(name: str, index_url: str, output_dir: Path | None = None) -> Path:
    """Create a wrapper package directory.

    Returns the path to the created package directory.
    """
    module = _to_module_name(name)
    root = (output_dir or Path.cwd()) / name
    pkg = root / "src" / module

    if root.exists():
        raise SystemExit(f"Error: {root} already exists")

    pkg.mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        _PYPROJECT_TOML.format(name=name)
    )
    (pkg / "__init__.py").write_text("")
    (pkg / "__main__.py").write_text(
        _MAIN_PY.format(name=name, index_url=index_url)
    )

    print(f"\nScaffolded {name} at {root}\n")
    print(f"  {root}/")
    print(f"  ├── pyproject.toml")
    print(f"  └── src/{module}/")
    print(f"      ├── __init__.py")
    print(f"      └── __main__.py")
    print()
    print(f"  Index URL: {index_url}")
    print()
    print("  Next steps:")
    print(f"    cd {name}")
    print(f"    git init && git add -A && git tag v0.1.0 && git commit -m 'Initial commit'")
    print(f"    pip install -e .")
    print(f"    python -m {module}          # test it")
    print(f"    python -m build             # build for publishing")
    print()

    return root
