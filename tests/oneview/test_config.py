"""Tests for proliant.oneview.config — config file discovery and error handling."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

import proliant.oneview.config as ov_config


def test_find_config_file_falls_back_to_config_dir_when_nothing_found(tmp_path, monkeypatch):
    """When no inventory.ini exists anywhere, fall back to the canonical
    ~/.config/proliant-cli/ location — same as ilo/config.py — not the
    current working directory (which is often ~/Documents on Windows and
    would produce a misleading 'expected at' path)."""
    fake_home = tmp_path / "home"
    fake_cwd = tmp_path / "somewhere-else"
    fake_home.mkdir()
    fake_cwd.mkdir()
    monkeypatch.delenv("PCLI_CONFIG", raising=False)

    with patch.object(Path, "home", return_value=fake_home), \
         patch.object(Path, "cwd", return_value=fake_cwd), \
         patch.object(ov_config, "is_frozen", return_value=True), \
         patch("sys.executable", str(fake_cwd / "proliant.exe")):
        resolved = ov_config._find_config_file()

    assert resolved == fake_home / ".config" / "proliant-cli" / "inventory.ini"


def test_load_oneview_config_missing_file_error_points_at_config_dir(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_cwd = tmp_path / "somewhere-else"
    fake_home.mkdir()
    fake_cwd.mkdir()
    monkeypatch.delenv("PCLI_CONFIG", raising=False)

    with patch.object(Path, "home", return_value=fake_home), \
         patch.object(Path, "cwd", return_value=fake_cwd), \
         patch.object(ov_config, "is_frozen", return_value=True), \
         patch("sys.executable", str(fake_cwd / "proliant.exe")):
        with pytest.raises(FileNotFoundError) as excinfo:
            ov_config.load_oneview_config()

    assert str(fake_home / ".config" / "proliant-cli" / "inventory.ini") in str(excinfo.value)


def test_main_reports_missing_config_cleanly_instead_of_raw_traceback(capsys):
    """proliant.oneview.cli.main() must not let FileNotFoundError from a
    missing inventory.ini escape as a raw traceback — it should be caught
    and reported the same way ValueError/RuntimeError are."""
    import proliant.oneview.cli as ov_cli

    def _raise(*_a, **_kw):
        raise FileNotFoundError(
            "inventory.ini not found. Expected at: /fake/path/inventory.ini\n"
            "Run 'proliant ilo init' to create a starter config."
        )

    with patch("proliant.oneview.config.load_oneview_config", side_effect=_raise):
        with pytest.raises(SystemExit) as excinfo:
            ov_cli.main(["servers", "list"])

    assert excinfo.value.code == 1
    out = capsys.readouterr().out
    assert "inventory.ini not found" in out
    assert "Traceback" not in out
