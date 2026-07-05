"""Tests for `proliant update`'s Windows install-confirmation prompt.

On Windows, `proliant update` launches an elevated GUI installer with
/SILENT (no wizard pages) -- so without an explicit confirmation step, a
plain `proliant update` would silently install into the current location
with no on-screen indication of where files go or how to undo it. These
tests cover `_confirm_windows_update()`, the pure-ish helper (only side
effects: print/input) that surfaces that information and gates the actual
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


def test_update_args_help_flag_detected():
    assert cli._update_args_request_help(["--help"]) is True
    assert cli._update_args_request_help(["-h"]) is True
    assert cli._update_args_request_help([]) is False
    assert cli._update_args_request_help(["-y"]) is False


def test_update_args_yes_flag_detected():
    assert cli._update_args_want_auto_confirm(["-y"]) is True
    assert cli._update_args_want_auto_confirm(["--yes"]) is True
    assert cli._update_args_want_auto_confirm([]) is False


def test_update_usage_mentions_yes_flag():
    assert "-y, --yes" in cli._UPDATE_USAGE
