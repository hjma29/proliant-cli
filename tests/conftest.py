"""Root test configuration.

Ensure the tests always exercise *this* checkout's ``src/`` rather than any
``proliant`` package that happens to be installed in the active virtualenv
(e.g. an editable install whose ``.pth`` points at a different worktree). This
must run before any test module imports ``proliant``, so it lives in the root
``conftest.py`` which pytest loads first during collection.
"""

import pathlib
import sys

import pytest

_SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
if _SRC.is_dir():
    sys.path.insert(0, str(_SRC))


@pytest.fixture(autouse=True)
def _never_report_test_runs_to_sentry(monkeypatch):
    """Prevent tests from ever activating real Sentry telemetry.

    ``cli.main()`` calls ``_init_sentry()``, which turns on real
    ``sentry_sdk`` reporting if the developer's machine has a
    ``~/.config/proliant-cli/telemetry-enabled`` sentinel (e.g. from earlier
    dogfooding). Without this guard, tests that call ``cli.main()`` on such a
    machine phone home real "production" events for purely local pytest
    artifacts (this is exactly how a stray ``ValueError: I/O operation on
    closed file`` from an earlier draft test made it into Sentry). Tests
    should never depend on - or be affected by - the local machine's
    telemetry opt-in state.
    """
    try:
        from proliant import cli
    except ImportError:
        return
    monkeypatch.setattr(cli, "_init_sentry", lambda: None)
