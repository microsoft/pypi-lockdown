"""Verify that a configured feed is reachable and authentication works."""

from __future__ import annotations

import subprocess
import sys


def verify(index_url: str) -> None:
    """Verify feed connectivity and authentication by running a dry-run pip install."""
    print(f"\nVerifying feed: {index_url}\n")

    cmd = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--dry-run",
        "--index-url",
        index_url,
        "--quiet",
        "pip",  # use pip itself as a harmless probe package
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except subprocess.TimeoutExpired:
        print("  FAIL Verification timed out after 60 seconds.")
        raise SystemExit(1) from None
    except FileNotFoundError:
        print("  FAIL Python executable not found -- cannot verify.")
        raise SystemExit(1) from None

    if result.returncode == 0:
        print("  OK Feed is reachable and authentication works.")
        print()
    else:
        print("  FAIL Verification failed.\n")
        stderr = result.stderr.strip()
        if stderr:
            for line in stderr.splitlines():
                print(f"    {line}")
            print()
        raise SystemExit(1)
