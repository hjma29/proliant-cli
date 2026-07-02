"""Tests for Windows packaged-EXE first-run setup (installer model).

In the installer-based distribution the GUI installer owns file placement and
the machine PATH, so the only first-run job left to proliant.exe is configuring
PowerShell tab completion once, guarded by a sentinel in the user config dir.
"""

from __future__ import annotations

from proliant import cli


def _force_frozen_win32(monkeypatch, config_root):
    """Make _windows_first_run_check run its body on any host OS."""
    monkeypatch.setattr(cli.sys, "platform", "win32")
    monkeypatch.setattr(cli, "is_frozen", lambda: True)
    monkeypatch.setattr(
        "proliant.common.config_dir", lambda: config_root, raising=True
    )


def test_first_run_configures_completion_and_writes_sentinel(monkeypatch, tmp_path):
    calls = {"completion": 0, "policy": 0}
    monkeypatch.setattr(
        cli, "_win_add_powershell_completion", lambda: calls.__setitem__("completion", calls["completion"] + 1)
    )
    monkeypatch.setattr(
        cli, "_win_check_execution_policy", lambda: calls.__setitem__("policy", calls["policy"] + 1)
    )
    _force_frozen_win32(monkeypatch, tmp_path)

    cli._windows_first_run_check()

    assert calls == {"completion": 1, "policy": 1}
    assert (tmp_path / ".win-completion-done").exists()


def test_first_run_is_idempotent_when_sentinel_exists(monkeypatch, tmp_path):
    (tmp_path / ".win-completion-done").write_text("", encoding="utf-8")
    calls = {"completion": 0}
    monkeypatch.setattr(
        cli, "_win_add_powershell_completion", lambda: calls.__setitem__("completion", calls["completion"] + 1)
    )
    monkeypatch.setattr(cli, "_win_check_execution_policy", lambda: None)
    _force_frozen_win32(monkeypatch, tmp_path)

    cli._windows_first_run_check()

    assert calls["completion"] == 0  # sentinel present → no re-run


def test_first_run_noop_when_not_frozen(monkeypatch, tmp_path):
    called = {"completion": False}
    monkeypatch.setattr(cli.sys, "platform", "win32")
    monkeypatch.setattr(cli, "is_frozen", lambda: False)
    monkeypatch.setattr(
        cli, "_win_add_powershell_completion", lambda: called.__setitem__("completion", True)
    )

    cli._windows_first_run_check()

    assert called["completion"] is False
    assert not (tmp_path / ".win-completion-done").exists()