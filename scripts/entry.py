"""PyInstaller entry point: starts the menu bar app (or runs a bundle check)."""

import sys

if "--selftest" in sys.argv:
    from deceive.core import selftest

    sys.exit(selftest())

if "--version" in sys.argv:
    from Foundation import NSBundle

    version = NSBundle.mainBundle().objectForInfoDictionaryKey_("CFBundleShortVersionString")
    print("Deceive", version or "unknown")
    sys.exit(0)

if "-h" in sys.argv or "--help" in sys.argv:
    from deceive.core import main as cli

    sys.exit(cli())  # prints the CLI usage and exits, instead of launching a game

from deceive.app import run  # noqa: E402

run()
