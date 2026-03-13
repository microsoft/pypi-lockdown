import argparse
import sys

from .configure import configure


def main():
    parser = argparse.ArgumentParser(
        prog="pypi-lockdown",
        description=(
            "Lock down pip, uv, and poetry to pull packages from an "
            "internal PyPI feed. Run once after creating a new environment."
        ),
    )
    parser.add_argument(
        "index_url",
        help="Internal feed URL (e.g. https://pkgs.dev.azure.com/ORG/PROJECT/_packaging/FEED/pypi/simple/)",
    )
    parser.add_argument(
        "--user",
        action="store_true",
        help="Write pip config to user home instead of the active Python environment",
    )
    args = parser.parse_args()
    configure(args.index_url, user_scope=args.user)


if __name__ == "__main__":
    main()
