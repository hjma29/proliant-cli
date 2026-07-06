"""Tests for Windows packaged-EXE first-run setup (installer model).

In the installer-based distribution the GUI installer owns file placement and
the machine PATH, so the only first-run job left to proliant.exe is configuring
PowerShell tab completion once, guarded by a sentinel in the user config dir.
"""

from __future__ import annotations

from proliant import cli


def _force_frozen_win32(monkeypatch, config_root, version="1.0.14"):
    """Make _windows_first_run_check run its body on any host OS."""
    monkeypatch.setattr(cli.sys, "platform", "win32")
    monkeypatch.setattr(cli, "is_frozen", lambda: True)
    monkeypatch.setattr(cli, "_get_current_version", lambda: version)
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
    _force_frozen_win32(monkeypatch, tmp_path, version="1.0.14")

    cli._windows_first_run_check()

    assert calls == {"completion": 1, "policy": 1}
    sentinel = tmp_path / ".win-completion-done"
    assert sentinel.exists()
    assert sentinel.read_text(encoding="utf-8").strip() == "1.0.14"


def test_first_run_banner_offers_same_window_remedy(monkeypatch, tmp_path, capsys):
    """The one-time banner must tell the user how to get completion working
    in THIS window (not just "open a new one") -- a wrong/unreachable install
    is often tested in the same terminal the installer just ran in, so
    '. $PROFILE' is the only remedy that doesn't require starting over."""
    monkeypatch.setattr(cli, "_win_add_powershell_completion", lambda: None)
    monkeypatch.setattr(cli, "_win_check_execution_policy", lambda: None)
    _force_frozen_win32(monkeypatch, tmp_path, version="1.0.14")

    cli._windows_first_run_check()

    out = capsys.readouterr().out
    assert "enabled" in out
    assert ". $PROFILE" in out
    assert "new PowerShell window" in out


def test_first_run_is_idempotent_when_sentinel_matches_current_version(monkeypatch, tmp_path):
    (tmp_path / ".win-completion-done").write_text("1.0.14", encoding="utf-8")
    calls = {"completion": 0}
    monkeypatch.setattr(
        cli, "_win_add_powershell_completion", lambda: calls.__setitem__("completion", calls["completion"] + 1)
    )
    monkeypatch.setattr(cli, "_win_check_execution_policy", lambda: None)
    _force_frozen_win32(monkeypatch, tmp_path, version="1.0.14")

    cli._windows_first_run_check()

    assert calls["completion"] == 0  # sentinel matches installed version → no re-run


def test_first_run_reruns_completion_setup_after_version_upgrade(monkeypatch, tmp_path, capsys):
    # Sentinel from an earlier install: completion was set up for 1.0.14, but
    # proliant has since been updated to 1.0.15. The (idempotent) profile
    # rewrite must run again so improvements to the completion block reach
    # already-set-up users -- without re-printing the one-time "enabled"
    # banner or re-checking the execution policy.
    (tmp_path / ".win-completion-done").write_text("1.0.14", encoding="utf-8")
    calls = {"completion": 0, "policy": 0}
    monkeypatch.setattr(
        cli, "_win_add_powershell_completion", lambda: calls.__setitem__("completion", calls["completion"] + 1)
    )
    monkeypatch.setattr(
        cli, "_win_check_execution_policy", lambda: calls.__setitem__("policy", calls["policy"] + 1)
    )
    _force_frozen_win32(monkeypatch, tmp_path, version="1.0.15")

    cli._windows_first_run_check()

    assert calls == {"completion": 1, "policy": 0}
    sentinel = tmp_path / ".win-completion-done"
    assert sentinel.read_text(encoding="utf-8").strip() == "1.0.15"
    assert "enabled" not in capsys.readouterr().out


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


def test_merge_returns_none_when_block_already_up_to_date():
    existing = "Write-Host 'hi'\n\n" + cli._POWERSHELL_COMPLETION_BLOCK
    assert cli._merge_powershell_completion_block(existing) is None


def test_merge_appends_block_to_untouched_profile():
    result = cli._merge_powershell_completion_block("Write-Host 'hi'\n")
    assert result is not None
    assert result.startswith("Write-Host 'hi'\n")
    assert result.count("Register-ArgumentCompleter") == 1
    assert cli._POWERSHELL_COMPLETION_BLOCK in result


def test_merge_replaces_stale_marker_based_block_without_duplicating():
    # A previous run of the (already-updated) marker-based format, with
    # different scriptblock contents than the current constant.
    stale_block = (
        "# >>> proliant tab completion >>>\n"
        "Register-ArgumentCompleter -Native -CommandName proliant -ScriptBlock {\n"
        "    param($wordToComplete, $commandAst, $cursorPosition)\n"
        "    Write-Host 'old logic'\n"
        "}\n\n"
        "# Show completion menu instead of cycling (added by proliant)\n"
        "if (-not (Get-PSReadLineKeyHandler | Where-Object { $_.Key -eq 'Tab' -and $_.Function -eq 'MenuComplete' })) {\n"
        "    Set-PSReadLineKeyHandler -Key Tab -Function MenuComplete\n"
        "}\n"
        "# <<< proliant tab completion <<<\n"
    )
    existing = "Write-Host 'hi'\n\n" + stale_block
    result = cli._merge_powershell_completion_block(existing)
    assert result is not None
    assert result.count("Register-ArgumentCompleter") == 1
    assert result.count("MenuComplete") == 2  # one Where-Object ref + one Set-PSReadLineKeyHandler call
    assert "old logic" not in result
    assert cli._POWERSHELL_COMPLETION_BLOCK in result


def test_merge_migrates_legacy_duplicated_profile_without_duplicating_further():
    # This exact shape (comment+if-block, then comment+RAC, then a second
    # comment+if-block with no RAC in between) was found in a real user
    # profile: the pre-fix stripping regex only matched through the FIRST
    # top-level closing brace, leaving the trailing "Show completion menu"
    # if-block behind on every reinstall and causing it to double up.
    legacy_duplicated = (
        "# Show completion menu instead of cycling (added by proliant)\n"
        "if (-not (Get-PSReadLineKeyHandler | Where-Object { $_.Key -eq 'Tab' -and $_.Function -eq 'MenuComplete' })) {\n"
        "    Set-PSReadLineKeyHandler -Key Tab -Function MenuComplete\n"
        "}\n\n"
        "# proliant tab completion (added by proliant)\n"
        "Register-ArgumentCompleter -Native -CommandName proliant -ScriptBlock {\n"
        "    param($commandName, $wordToComplete, $cursorPosition)\n"
        "    $env:COMP_LINE = $wordToComplete\n"
        "    proliant 2>&1 | Out-Null\n"
        "}\n\n"
        "# Show completion menu instead of cycling (added by proliant)\n"
        "if (-not (Get-PSReadLineKeyHandler | Where-Object { $_.Key -eq 'Tab' -and $_.Function -eq 'MenuComplete' })) {\n"
        "    Set-PSReadLineKeyHandler -Key Tab -Function MenuComplete\n"
        "}\n"
    )
    result = cli._merge_powershell_completion_block(legacy_duplicated)
    assert result is not None
    # Migrating must converge to exactly one copy of each piece, not two.
    assert result.count("Register-ArgumentCompleter") == 1
    assert result.count("Set-PSReadLineKeyHandler -Key Tab -Function MenuComplete") == 1
    assert cli._POWERSHELL_COMPLETION_BLOCK in result
    # Re-running the merge again on the now-clean result must be a no-op.
    assert cli._merge_powershell_completion_block(result) is None