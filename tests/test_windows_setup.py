"""Tests for Windows packaged-EXE first-run setup helpers."""

from __future__ import annotations

from proliant import cli


def test_win_path_contains_checks_current_process_path(monkeypatch):
    monkeypatch.setenv("PATH", r"C:\Windows;C:\Users\user\bin")
    monkeypatch.setattr(cli, "_win_user_path", lambda: "")

    assert cli._win_path_contains(r"C:\Users\user\bin")


def test_win_path_contains_checks_persisted_user_path(monkeypatch):
    monkeypatch.setenv("PATH", r"C:\Windows")
    monkeypatch.setattr(cli, "_win_user_path", lambda: r"C:\Users\user\bin;C:\Tools")

    assert cli._win_path_contains(r"C:\Users\user\bin")


def test_win_path_contains_strips_trailing_backslash(monkeypatch):
    monkeypatch.setenv("PATH", r"C:\Windows")
    monkeypatch.setattr(cli, "_win_user_path", lambda: "C:\\Users\\user\\bin\\")

    assert cli._win_path_contains(r"C:\Users\user\bin")