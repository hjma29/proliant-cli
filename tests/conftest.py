"""Root test configuration.

Ensure the tests always exercise *this* checkout's ``src/`` rather than any
``proliant`` package that happens to be installed in the active virtualenv
(e.g. an editable install whose ``.pth`` points at a different worktree). This
must run before any test module imports ``proliant``, so it lives in the root
``conftest.py`` which pytest loads first during collection.
"""

import pathlib
import sys

_SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
if _SRC.is_dir():
    sys.path.insert(0, str(_SRC))
