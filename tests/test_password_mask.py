"""Tests for the masked password reader used by `proliant com login`."""

import io
import sys

import pytest

from proliant.com import cli


def _run_windows_reader(monkeypatch, keystrokes):
    """Drive _read_password_masked_windows with a scripted getwch() sequence."""
    it = iter(keystrokes)
    monkeypatch.setattr(cli, "sys", sys)  # ensure same sys
    fake_msvcrt = type("M", (), {"getwch": staticmethod(lambda: next(it))})
    monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)

    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    result = cli._read_password_masked_windows("Password: ")
    return result, out.getvalue()


def test_typed_password_masks_each_char(monkeypatch):
    result, out = _run_windows_reader(monkeypatch, list("secret") + ["\r"])
    assert result == "secret"
    # One asterisk per character typed
    assert out.count("*") == len("secret")
    assert "secret" not in out  # never echo the real password


def test_pasted_password_masks_each_char(monkeypatch):
    # A paste arrives as a burst of characters from getwch(), same as typing
    pasted = list("P@ss w0rd!") + ["\r"]
    result, out = _run_windows_reader(monkeypatch, pasted)
    assert result == "P@ss w0rd!"
    assert out.count("*") == len("P@ss w0rd!")


def test_backspace_removes_char_and_erases_star(monkeypatch):
    # type 'ab', backspace, type 'c' -> 'ac'
    keys = ["a", "b", "\x08", "c", "\r"]
    result, out = _run_windows_reader(monkeypatch, keys)
    assert result == "ac"
    assert "\b \b" in out  # visual erase sequence emitted


def test_ctrl_c_raises(monkeypatch):
    with pytest.raises(KeyboardInterrupt):
        _run_windows_reader(monkeypatch, ["a", "\x03"])


def test_function_key_prefix_is_swallowed(monkeypatch):
    # Arrow keys deliver a \xe0/\x00 prefix + code; both must be ignored
    keys = ["a", "\xe0", "H", "b", "\r"]
    result, out = _run_windows_reader(monkeypatch, keys)
    assert result == "ab"
    assert out.count("*") == 2
