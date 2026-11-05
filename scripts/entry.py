"""PyInstaller entry point: starts the menu bar app (or runs a bundle check)."""

import sys

if "--selftest" in sys.argv:
    from deceive.core import selftest

    sys.exit(selftest())

if "--version" in sys.argv:
    print("Deceive 1.0.0")
    sys.exit(0)

from deceive.app import run  # noqa: E402

run()
