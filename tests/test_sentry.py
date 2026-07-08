"""Tests for Sentry event filtering and scrubbing."""

from __future__ import annotations

import sys

import httpx
import pytest

from proliant.cli import _sentry_scrub


def _event(exc_type: str, value: str) -> dict:
    return {"exception": {"values": [{"type": exc_type, "value": value}]}}


def test_sentry_drops_network_timeout_by_event_type():
    event = _event("ConnectTimeout", "timed out connecting to 10.0.0.10")

    assert _sentry_scrub(event, {}) is None


def test_sentry_drops_wrong_password_messages():
    event = _event("RuntimeError", "POST /redfish/v1/SessionService/Sessions failed - HTTP 401: check username/password")

    assert _sentry_scrub(event, {}) is None


def test_sentry_drops_httpx_4xx_status_from_hint():
    request = httpx.Request("GET", "https://oneview.example/rest/server-hardware")
    response = httpx.Response(403, request=request)
    exc = httpx.HTTPStatusError("forbidden", request=request, response=response)
    event = _event("HTTPStatusError", "forbidden")

    assert _sentry_scrub(event, {"exc_info": (type(exc), exc, None)}) is None


def test_sentry_keeps_and_scrubs_unexpected_bugs():
    event = _event("KeyError", "failed on 10.1.2.3 with password=secret")

    result = _sentry_scrub(event, {})

    assert result is event
    assert event["exception"]["values"][0]["value"] == "failed on <ip> with password=<redacted>"


def test_enable_windows_vt_mode_noop_without_real_console(monkeypatch):
    """Regression test for a real Sentry-reported crash (PROLIANT-CLI-6).

    On a "legacy" Windows console (no virtual-terminal support), Rich writes
    styled table segments straight to stdout via the raw Win32 Console API
    with no size guard, which can raise ``OSError: [Errno 22] Invalid
    argument`` for a large enough table. ``_enable_windows_vt_mode()`` forces
    VT mode on at startup so Rich always takes its chunk-safe ANSI write path
    instead. It must never raise, including in the common case where
    stdout/stderr aren't attached to a real console (piped/redirected output,
    CI, non-interactive shells) and ``GetConsoleMode`` simply fails.
    """
    from proliant.cli import _enable_windows_vt_mode

    # Should be a silent no-op everywhere it can't act, and never raise.
    _enable_windows_vt_mode()


def test_main_can_run_multiple_times_in_one_process_without_io_error(monkeypatch, capsys):
    """Regression test for a real Sentry-reported crash.

    ``main()`` used to reconfigure ``sys.stdout``/``sys.stderr`` by wrapping
    ``.buffer`` in a brand new ``io.TextIOWrapper`` and reassigning it, on
    every call. If ``main()`` ran more than once in the same process (e.g.
    tests invoking ``cli.main()`` repeatedly), the previous wrapper — still
    referencing the same underlying buffer — could get closed (GC or
    otherwise) out from under the new one, raising
    ``ValueError: I/O operation on closed file`` the next time anything wrote
    to stdout/stderr. Calling main() (or just the reconfigure step) twice in
    a row must not raise.
    """
    import proliant.cli as cli

    monkeypatch.setattr(sys, "argv", ["proliant", "-h"])

    for _ in range(3):
        with pytest.raises(SystemExit):
            cli.main(["-h"])
        # Writing again after main() has run must not raise even if
        # sys.stdout/sys.stderr were reconfigured on a prior call.
        print("still writable")
        sys.stderr.write("still writable\n")

    assert "still writable" in capsys.readouterr().out