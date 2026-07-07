"""Tests for `proliant version` -- prints the installed version and, if a
newer GitHub release exists, offers to install it (replaces the old
standalone `proliant update` command / `-V, --version` flag).
"""

from __future__ import annotations

import pytest

from proliant import cli


def test_version_args_help_flag_detected():
    assert cli._version_args_request_help(["--help"]) is True
    assert cli._version_args_request_help(["-h"]) is True
    assert cli._version_args_request_help([]) is False
    assert cli._version_args_request_help(["-y"]) is False


def test_version_args_yes_flag_detected():
    assert cli._version_args_want_auto_confirm(["-y"]) is True
    assert cli._version_args_want_auto_confirm(["--yes"]) is True
    assert cli._version_args_want_auto_confirm([]) is False


def test_version_usage_mentions_yes_flag():
    assert "-y, --yes" in cli._VERSION_USAGE


def test_cmd_version_skips_network_check_for_dev_version(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_get_current_version", lambda: "dev")

    def _unexpected_fetch(*a, **kw):
        raise AssertionError("_fetch_latest_release should not be called for dev installs")

    monkeypatch.setattr(cli, "_fetch_latest_release", _unexpected_fetch)

    cli._cmd_version()

    out = capsys.readouterr().out
    assert "proliant dev" in out


def test_cmd_version_reports_up_to_date(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_get_current_version", lambda: "1.2.3")
    monkeypatch.setattr(cli, "_fetch_latest_release", lambda *a, **kw: {"tag_name": "v1.2.3"})

    def _unexpected_input(prompt=""):
        raise AssertionError("input() should not be called when already up to date")

    monkeypatch.setattr("builtins.input", _unexpected_input)

    cli._cmd_version()

    out = capsys.readouterr().out
    assert "proliant 1.2.3" in out
    assert "Already up to date." in out


def test_cmd_version_handles_unreachable_github(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_get_current_version", lambda: "1.2.3")
    monkeypatch.setattr(cli, "_fetch_latest_release", lambda *a, **kw: None)

    cli._cmd_version()

    err = capsys.readouterr().err
    assert "Could not reach GitHub" in err


def test_cmd_version_prompts_and_declines_upgrade(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_get_current_version", lambda: "1.0.0")
    monkeypatch.setattr(cli, "_fetch_latest_release", lambda *a, **kw: {"tag_name": "v2.0.0"})
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")

    def _unexpected_run_update(*a, **kw):
        raise AssertionError("_run_update should not be called when the user declines")

    monkeypatch.setattr(cli, "_run_update", _unexpected_run_update)

    cli._cmd_version()

    out = capsys.readouterr().out
    assert "2.0.0" in out
    assert "proliant version -y" in out


def test_cmd_version_prompts_and_accepts_upgrade(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_get_current_version", lambda: "1.0.0")
    release = {"tag_name": "v2.0.0"}
    monkeypatch.setattr(cli, "_fetch_latest_release", lambda *a, **kw: release)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")

    calls = []
    monkeypatch.setattr(
        cli, "_run_update", lambda auto_confirm=False, release=None: calls.append((auto_confirm, release))
    )

    cli._cmd_version()

    assert calls == [(True, release)]


def test_cmd_version_auto_confirm_skips_prompt(monkeypatch):
    monkeypatch.setattr(cli, "_get_current_version", lambda: "1.0.0")
    release = {"tag_name": "v2.0.0"}
    monkeypatch.setattr(cli, "_fetch_latest_release", lambda *a, **kw: release)

    def _unexpected_input(prompt=""):
        raise AssertionError("input() should not be called when auto_confirm=True")

    monkeypatch.setattr("builtins.input", _unexpected_input)

    calls = []
    monkeypatch.setattr(
        cli, "_run_update", lambda auto_confirm=False, release=None: calls.append((auto_confirm, release))
    )

    cli._cmd_version(auto_confirm=True)

    assert calls == [(True, release)]


def test_run_update_uses_prefetched_release_without_extra_fetch(monkeypatch, capsys):
    monkeypatch.setattr(cli.sys, "platform", "linux")
    monkeypatch.setattr(cli, "_get_current_version", lambda: "1.2.3")

    def _unexpected_fetch(*a, **kw):
        raise AssertionError("_fetch_latest_release should not be called when release is pre-supplied")

    monkeypatch.setattr(cli, "_fetch_latest_release", _unexpected_fetch)

    with pytest.raises(SystemExit):
        cli._run_update(auto_confirm=True, release={"tag_name": "v1.2.3", "assets": []})

    out = capsys.readouterr().out
    assert "Checking for updates..." not in out
    assert "Already up to date." in out
