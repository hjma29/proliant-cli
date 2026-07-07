"""Tests for `_run_update()`'s Windows install-confirmation prompt.

`_run_update()` is the download/install engine used internally by `proliant
version`'s upgrade prompt. On Windows it launches an elevated GUI installer
with /SILENT (no wizard pages) -- so without an explicit confirmation step,
an upgrade would silently install into the current location with no
on-screen indication of where files go or how to undo it. These tests cover
`_confirm_windows_update()`, the pure-ish helper (only side effects:
print/input) that surfaces that information and gates the actual
download/install on the user's answer.
"""

from __future__ import annotations

import os

import pytest

from proliant import cli


def test_confirm_skips_prompt_entirely_on_non_windows(monkeypatch, capsys):
    monkeypatch.setattr(cli.sys, "platform", "linux")

    def _unexpected_input(prompt=""):
        raise AssertionError("input() should not be called on non-Windows")

    monkeypatch.setattr("builtins.input", _unexpected_input)

    assert cli._confirm_windows_update("1.2.3", auto_confirm=False) is True
    assert capsys.readouterr().out == ""


def test_confirm_shows_install_dir_and_uninstall_info(monkeypatch, capsys):
    monkeypatch.setattr(cli.sys, "platform", "win32")
    monkeypatch.setattr(cli, "_win_install_dir_hint", lambda: r"C:\Program Files\proliant-cli")
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")

    assert cli._confirm_windows_update("1.2.3", auto_confirm=False) is True

    out = capsys.readouterr().out
    assert "1.2.3" in out
    assert r"C:\Program Files\proliant-cli" in out
    assert "no separate extraction step" in out
    assert "Uninstall" in out


@pytest.mark.parametrize("answer", ["", "y", "Y", "yes", "YES"])
def test_confirm_proceeds_on_affirmative_answers(monkeypatch, answer):
    monkeypatch.setattr(cli.sys, "platform", "win32")
    monkeypatch.setattr(cli, "_win_install_dir_hint", lambda: r"C:\Program Files\proliant-cli")
    monkeypatch.setattr("builtins.input", lambda prompt="": answer)

    assert cli._confirm_windows_update("1.2.3", auto_confirm=False) is True


@pytest.mark.parametrize("answer", ["n", "N", "no", "nope", "cancel"])
def test_confirm_cancels_on_negative_or_unrecognized_answers(monkeypatch, capsys, answer):
    monkeypatch.setattr(cli.sys, "platform", "win32")
    monkeypatch.setattr(cli, "_win_install_dir_hint", lambda: r"C:\Program Files\proliant-cli")
    monkeypatch.setattr("builtins.input", lambda prompt="": answer)

    assert cli._confirm_windows_update("1.2.3", auto_confirm=False) is False
    assert "Update cancelled." in capsys.readouterr().out


def test_confirm_auto_confirm_skips_prompt_but_still_shows_info(monkeypatch, capsys):
    monkeypatch.setattr(cli.sys, "platform", "win32")
    monkeypatch.setattr(cli, "_win_install_dir_hint", lambda: r"C:\Program Files\proliant-cli")

    def _unexpected_input(prompt=""):
        raise AssertionError("input() should not be called when auto_confirm=True")

    monkeypatch.setattr("builtins.input", _unexpected_input)

    assert cli._confirm_windows_update("1.2.3", auto_confirm=True) is True
    assert r"C:\Program Files\proliant-cli" in capsys.readouterr().out


def test_win_install_dir_hint_uses_installed_exe_path_when_frozen(monkeypatch):
    monkeypatch.setattr(cli, "is_frozen", lambda: True)
    monkeypatch.setattr(
        cli, "_resolve_installed_exe_path", lambda: r"C:\Program Files\proliant-cli\proliant.exe"
    )
    assert cli._win_install_dir_hint() == r"C:\Program Files\proliant-cli"


def test_win_install_dir_hint_falls_back_when_not_frozen(monkeypatch):
    monkeypatch.setattr(cli, "is_frozen", lambda: False)
    assert cli._win_install_dir_hint() == os.path.expandvars(r"%ProgramFiles%\proliant-cli")


def test_win_install_dir_hint_falls_back_on_resolve_error(monkeypatch):
    monkeypatch.setattr(cli, "is_frozen", lambda: True)

    def _boom():
        raise OSError("no parent pid")

    monkeypatch.setattr(cli, "_resolve_installed_exe_path", _boom)
    assert cli._win_install_dir_hint() == os.path.expandvars(r"%ProgramFiles%\proliant-cli")


# ---------------------------------------------------------------------------
# telemetry ping (counts update events by OS via the Cloudflare Worker)
# ---------------------------------------------------------------------------

def test_ping_telemetry_disabled_by_env(monkeypatch):
    monkeypatch.setenv("PROLIANT_NO_TELEMETRY", "1")
    started = []
    monkeypatch.setattr(
        "threading.Thread", lambda *a, **kw: started.append((a, kw)) or _FakeThread()
    )
    cli._ping_telemetry("/update/unix")
    assert started == []  # opt-out short-circuits before any thread is created


class _FakeThread:
    def __init__(self, *a, **kw):
        self.started = False

    def start(self):
        self.started = True


def test_ping_telemetry_starts_daemon_thread_with_expected_url(monkeypatch):
    monkeypatch.delenv("PROLIANT_NO_TELEMETRY", raising=False)
    captured = {}

    def _fake_thread(target=None, args=(), daemon=None):
        captured["target"] = target
        captured["args"] = args
        captured["daemon"] = daemon
        return _FakeThread()

    monkeypatch.setattr("threading.Thread", _fake_thread)
    cli._ping_telemetry("/update/windows")

    assert captured["target"] is cli._telemetry_send
    assert captured["args"] == (f"{cli._TELEMETRY_BASE}/update/windows",)
    assert captured["daemon"] is True


def test_ping_telemetry_never_raises_if_thread_fails(monkeypatch):
    monkeypatch.delenv("PROLIANT_NO_TELEMETRY", raising=False)

    def _boom(*a, **kw):
        raise RuntimeError("can't start thread")

    monkeypatch.setattr("threading.Thread", _boom)
    cli._ping_telemetry("/update/unix")  # must not raise


def test_telemetry_send_calls_urlopen_with_url(monkeypatch):
    import urllib.request

    seen = {}

    class _Resp:
        def close(self):
            seen["closed"] = True

    def _fake_urlopen(req, *a, **kw):
        seen["url"] = req.full_url
        return _Resp()

    monkeypatch.setattr(cli, "_ssl_context", lambda: None)
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    cli._telemetry_send("https://example.test/update/unix")
    assert seen["url"] == "https://example.test/update/unix"
    assert seen["closed"] is True


def test_telemetry_send_swallows_network_errors(monkeypatch):
    import urllib.request

    def _boom(*a, **kw):
        raise OSError("network down")

    monkeypatch.setattr(cli, "_ssl_context", lambda: None)
    monkeypatch.setattr(urllib.request, "urlopen", _boom)

    cli._telemetry_send("https://example.test/update/unix")  # must not raise


def test_run_update_fires_update_ping_before_download(monkeypatch):
    import json

    monkeypatch.setattr(cli.sys, "platform", "linux")
    monkeypatch.setattr(cli, "_ssl_context", lambda: None)
    monkeypatch.setattr(cli, "_get_current_version", lambda: "1.0.0")
    monkeypatch.setattr(cli, "_confirm_windows_update", lambda ver, auto: True)

    class _Resp:
        def __init__(self, data):
            self._d = data

        def read(self):
            return self._d

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    payload = json.dumps({"tag_name": "v9.9.9", "assets": []}).encode()
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **kw: _Resp(payload))

    pings = []
    monkeypatch.setattr(cli, "_ping_telemetry", lambda path: pings.append(path))

    # No matching asset in the (empty) release -> _run_update exits(1) after the
    # ping has already fired, which is exactly what we want to assert.
    with pytest.raises(SystemExit):
        cli._run_update(auto_confirm=True)

    assert pings == ["/update/unix"]

