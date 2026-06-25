"""Platform/runtime helpers shared across proliant modules."""

import sys


def is_frozen() -> bool:
    """Return True when running as a packaged binary (PyInstaller or Nuitka).

    PyInstaller sets ``sys.frozen``; Nuitka does NOT — it injects a module-level
    ``__compiled__`` marker into every compiled module instead.  Checking both
    makes frozen-detection work for the Nuitka onefile build shipped to users.
    """
    return bool(getattr(sys, "frozen", False)) or "__compiled__" in globals()
