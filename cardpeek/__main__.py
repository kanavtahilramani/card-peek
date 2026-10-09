"""Start Card Peek: the menu bar app on macOS, the tray app on Windows, the Tk window elsewhere.

    python -m cardpeek               # run it
    python -m cardpeek --self-test   # check OCR end to end on a made-up board, then exit
"""
from __future__ import annotations

import argparse
import os
import sys

from . import __version__


def attach_console():
    """The packaged Windows app has no console of its own. Run from one with --self-test
    or --version, print to that one (a GUI app run from a pipe already has its output)."""
    if os.name != "nt" or sys.stdout is not None:
        return
    import ctypes
    if ctypes.windll.kernel32.AttachConsole(-1):  # ATTACH_PARENT_PROCESS
        sys.stdout = sys.stderr = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)


def main():
    if len(sys.argv) > 1:
        attach_console()
    ap = argparse.ArgumentParser(prog="cardpeek", description="Hover over Magic cards on a video stream "
                                 "to see the full card. Everything else is set from the app itself.")
    ap.add_argument("--self-test", action="store_true",
                    help="read a made-up board with OCR, print what was found, and exit (0 if all is well)")
    ap.add_argument("--version", action="version", version=f"Card Peek {__version__}")
    # macOS passes -psn_... to apps opened from Finder on some versions; ignore extras.
    args, _ = ap.parse_known_args()

    from .core import IS_MAC, IS_WINDOWS, setup_logging
    setup_logging()
    if args.self_test:
        from .selftest import run
        sys.exit(run())
    if IS_MAC and os.environ.get("CARDPEEK_UI") != "tk":
        from .mac import run
    elif IS_WINDOWS and os.environ.get("CARDPEEK_UI") != "tk":
        from .win import run
    else:
        from .tk_ui import run
    run()


if __name__ == "__main__":
    main()
