"""PyInstaller entry point: starts the menu bar app (or runs a bundle check)."""

import sys

if "--selftest" in sys.argv:
    from deceive.core import selftest

    sys.exit(selftest())

if "--version" in sys.argv:
    print("Deceive 1.0.0")
    sys.exit(0)

if "-h" in sys.argv or "--help" in sys.argv:
    from deceive.core import main as cli

    sys.exit(cli())  # prints the CLI usage and exits, instead of launching a game

from deceive.app import run  # noqa: E402

run()
