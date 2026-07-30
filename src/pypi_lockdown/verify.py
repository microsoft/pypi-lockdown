"""Verify that a configured feed is reachable and authentication works."""

from __future__ import annotations

import subprocess
import sys

# Markers pip emits (at -v) when the index rejects our credentials. These are
# specific enough not to fire on a benign "Found credentials in keyring" debug
# line, so they reliably distinguish an auth failure from a missing package.
_AUTH_MARKERS = ("401", "403", "unauthorized", "could not fetch url")

# pip's message when it reached the index (authenticated) but the probe
# package simply is not published on the feed -- expected for a private feed.
_MISSING_MARKERS = ("no matching distribution", "could not find a version")

_KEYRING_HINT = (
    "    This looks like an authentication problem. pip's keyring subprocess "
    "provider ignores a keyring installed in the active\n"
    "    venv/conda env and uses the next 'keyring' on PATH, so a *global* "
    "keyring tool must expose an Azure Artifacts backend:\n"
    "      uv tool install keyring --with artifacts-keyring-nofuss "
    "--index-url <PUBLIC_FEED_URL>\n"
)


def verify(index_url: str) -> None:
    """Verify feed connectivity and authentication via a dry-run pip install.

    Uses ``--ignore-installed`` so pip must actually query the index (a probe
    package that is already installed would otherwise short-circuit without
    ever authenticating), and ``--no-input`` + a closed stdin so a missing
    credential fails fast instead of blocking on an interactive prompt.
    """
    print(f"\nVerifying feed: {index_url}\n")

    cmd = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--dry-run",
        "--ignore-installed",  # force a real index query (don't short-circuit)
        "--index-url",
        index_url,
        "--no-input",  # never block on an interactive credential prompt
        "-vv",  # surface HTTP status codes so we can tell auth from missing pkg
        "pip",  # harmless probe package
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            # Close stdin so pip can't hang on a prompt even if --no-input is
            # ignored by an older pip; keep a bounded timeout as a backstop.
            stdin=subprocess.DEVNULL,
            timeout=60,
            check=False,
        )
    except subprocess.TimeoutExpired:
        print(
            "  FAIL Verification timed out after 60 seconds "
            "(the feed may be unreachable or slow)."
        )
        raise SystemExit(1) from None
    except FileNotFoundError:
        print("  FAIL Python executable not found -- cannot verify.")
        raise SystemExit(1) from None

    output = f"{result.stdout}\n{result.stderr}"

    if result.returncode == 0:
        print("  OK Feed is reachable and authentication works.")
        print()
        return

    if _looks_like_auth_failure(output):
        print("  FAIL Authentication to the feed failed.\n")
        _print_pip_errors(output)
        print(_KEYRING_HINT)
        raise SystemExit(1)

    if _reached_but_missing_probe(output):
        # The feed answered (so auth worked) but doesn't publish `pip`. That is
        # expected for a private-only feed; report success for auth purposes.
        print(
            "  OK Feed is reachable and authentication works "
            "(the probe package 'pip' is not mirrored on this feed)."
        )
        print()
        return

    print("  FAIL Verification failed.\n")
    _print_pip_errors(output)
    raise SystemExit(1)


def _print_pip_errors(output: str) -> None:
    """Print only the salient error/warning/auth lines from pip's output.

    pip's ``-vv`` output is deliberately noisy; keep just the lines that
    explain the failure (errors, warnings, and any auth-related HTTP status).
    """
    salient = [
        line
        for line in output.splitlines()
        if line.strip().startswith(("ERROR", "WARNING"))
        or any(m in line.lower() for m in _AUTH_MARKERS)
    ]
    # De-duplicate while preserving order (pip retries, so lines repeat).
    unique = list(dict.fromkeys(salient))
    for line in unique or output.splitlines()[-5:]:
        print(f"    {line.strip()}")
    print()


def _looks_like_auth_failure(output: str) -> bool:
    """Return True if pip's output indicates the index rejected credentials."""
    lowered = output.lower()
    return any(marker in lowered for marker in _AUTH_MARKERS)


def _reached_but_missing_probe(output: str) -> bool:
    """Return True if pip reached the index but the probe package is absent."""
    lowered = output.lower()
    return any(marker in lowered for marker in _MISSING_MARKERS)
