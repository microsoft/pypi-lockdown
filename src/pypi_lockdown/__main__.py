from __future__ import annotations

import argparse
import sys

from .configure import configure, detect_index_url
from .scaffold import scaffold
from .verify import verify


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="pypi-lockdown",
        description=(
            "Lock down pip, uv, and poetry to pull packages from an internal PyPI feed."
        ),
    )
    sub = parser.add_subparsers(dest="command")

    # --- configure (default when no subcommand) ---
    p_configure = sub.add_parser(
        "configure",
        help="Write pip/uv config files pointing at an internal feed",
    )
    p_configure.add_argument(
        "index_url",
        nargs="?",
        default=None,
        help=(
            "Internal feed URL. If omitted, auto-detected from"
            " pyproject.toml ([[tool.uv.index]] or [[tool.poetry.source]])."
        ),
    )
    p_configure.add_argument(
        "--user",
        action="store_true",
        help="Write pip config to user home instead of the active Python environment",
    )
    p_configure.add_argument(
        "--ci",
        action="store_true",
        help=(
            "Non-interactive CI mode: skip pyproject.toml modification"
            " and poetry instructions"
        ),
    )
    p_configure.add_argument(
        "--verify",
        action="store_true",
        help="After configuring, verify the feed is reachable and authentication works",
    )

    # --- verify ---
    p_verify = sub.add_parser(
        "verify",
        help="Test that the configured feed is reachable and authentication works",
    )
    p_verify.add_argument(
        "index_url",
        help="Feed URL to verify",
    )

    # --- scaffold ---
    p_scaffold = sub.add_parser(
        "scaffold",
        help="Generate a wrapper package that hardcodes a private feed URL",
    )
    p_scaffold.add_argument(
        "name",
        help="Package name (e.g. ai4s-pypi-lockdown)",
    )
    p_scaffold.add_argument(
        "index_url",
        help="Internal feed URL to hardcode",
    )

    # Allow bare `python -m pypi_lockdown URL` as shorthand for `configure URL`
    _commands = {"configure", "scaffold", "verify", "-h", "--help"}
    argv = sys.argv[1:]
    if argv and argv[0] not in _commands:
        argv = ["configure", *argv]

    args = parser.parse_args(argv)

    if args.command == "configure" or args.command is None:
        index_url = getattr(args, "index_url", None)
        if index_url is None:
            index_url = detect_index_url()
            if index_url is None:
                parser.error(
                    "INDEX_URL is required (no pyproject.toml with a configured"
                    " feed was found in the current directory)"
                )
            print(f"Auto-detected feed URL from pyproject.toml: {index_url}\n")
        configure(
            index_url,
            user_scope=getattr(args, "user", False),
            ci=getattr(args, "ci", False),
        )
        if getattr(args, "verify", False):
            verify(index_url)
    elif args.command == "verify":
        verify(args.index_url)
    elif args.command == "scaffold":
        scaffold(args.name, args.index_url)


if __name__ == "__main__":
    main()
