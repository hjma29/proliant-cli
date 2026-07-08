"""Tests for proliant.oneview.config — config file discovery and error handling."""

from __future__ import annotations

import json
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


def test_list_oneview_appliances_malformed_ini_raises_friendly_valueerror(tmp_path, monkeypatch):
    """A duplicate key must surface as a clear ValueError -- not a raw
    configparser.DuplicateOptionError traceback. Regression test matching
    proliant.ilo.config's handling of the same failure mode."""
    ini = tmp_path / "inventory.ini"
    ini.write_text("[datacenter-a]\nhost = 10.0.0.5\nhost = 10.0.0.6\ntype = oneview\n")
    monkeypatch.setenv("PCLI_CONFIG", str(ini))

    with pytest.raises(ValueError) as excinfo:
        ov_config.list_oneview_appliances()

    message = str(excinfo.value)
    assert "not in the right format" in message
    assert "sample-inventory.ini" in message


def test_main_reports_missing_config_cleanly_instead_of_raw_traceback(capsys):
    """proliant.oneview.cli.main() must not let FileNotFoundError from a
    missing inventory.ini escape as a raw traceback — it should be caught
    and reported the same way ValueError/RuntimeError are."""
    import proliant.oneview.cli as ov_cli

    def _raise(*_a, **_kw):
        raise FileNotFoundError(
            "inventory.ini not found. Expected at: /fake/path/inventory.ini\n"
            "Run 'proliant setup' to add one."
        )

    # _load_client() calls list_oneview_appliances() before load_oneview_config();
    # mock both so this test doesn't depend on whatever inventory.ini (if any)
    # happens to exist on the machine running the suite.
    with patch("proliant.oneview.config.list_oneview_appliances", return_value=[]), \
         patch("proliant.oneview.config.load_oneview_config", side_effect=_raise):
        with pytest.raises(SystemExit) as excinfo:
            ov_cli.main(["servers", "list"])

    assert excinfo.value.code == 1
    out = capsys.readouterr().out
    assert "inventory.ini not found" in out
    assert "Traceback" not in out


# ── multi-appliance support ─────────────────────────────────────────────────

def _write_ini(path: Path, text: str) -> None:
    path.write_text(text)


def test_list_oneview_appliances_empty_when_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr(ov_config, "_find_config_file", lambda: tmp_path / "inventory.ini")
    assert ov_config.list_oneview_appliances() == []


def test_list_oneview_appliances_collects_literal_and_typed_sections(tmp_path, monkeypatch):
    ini = tmp_path / "inventory.ini"
    _write_ini(ini, """
[defaults]
username = Administrator
password = secret

[oneview]
host = 10.0.0.1

[datacenter-b]
host = 10.0.0.2
username = svc
password = pw2
type = oneview

[dl380-gen11]
host = 10.0.0.3
""")
    monkeypatch.setattr(ov_config, "_find_config_file", lambda: ini)

    appliances = ov_config.list_oneview_appliances()
    names = [a["name"] for a in appliances]
    assert names == ["oneview", "datacenter-b"]  # dl380-gen11 (an iLO host) excluded
    assert appliances[0]["host"] == "10.0.0.1"
    assert appliances[0]["username"] == "Administrator"  # falls back to default
    assert appliances[1]["host"] == "10.0.0.2"
    assert appliances[1]["username"] == "svc"


def test_list_oneview_appliances_raises_on_missing_host(tmp_path, monkeypatch):
    ini = tmp_path / "inventory.ini"
    _write_ini(ini, "[oneview]\nusername = Administrator\n")
    monkeypatch.setattr(ov_config, "_find_config_file", lambda: ini)

    with pytest.raises(ValueError, match="missing the 'host' key"):
        ov_config.list_oneview_appliances()


def test_get_active_appliance_name_defaults_to_first_when_unset(tmp_path, monkeypatch):
    monkeypatch.setattr(ov_config, "_state_file", lambda: tmp_path / "oneview_state.json")
    appliances = [{"name": "a"}, {"name": "b"}]
    assert ov_config.get_active_appliance_name(appliances) == "a"


def test_get_active_appliance_name_none_when_no_appliances(tmp_path, monkeypatch):
    monkeypatch.setattr(ov_config, "_state_file", lambda: tmp_path / "oneview_state.json")
    assert ov_config.get_active_appliance_name([]) is None


def test_get_active_appliance_name_falls_back_when_stale(tmp_path, monkeypatch):
    """A previously-active appliance that was since removed from inventory.ini
    shouldn't leave the CLI stuck erroring -- fall back to the first one."""
    state_file = tmp_path / "oneview_state.json"
    state_file.write_text(json.dumps({"active_appliance": "removed-appliance"}))
    monkeypatch.setattr(ov_config, "_state_file", lambda: state_file)

    appliances = [{"name": "a"}, {"name": "b"}]
    assert ov_config.get_active_appliance_name(appliances) == "a"


def test_set_active_appliance_persists_and_is_case_insensitive(tmp_path, monkeypatch):
    ini = tmp_path / "inventory.ini"
    _write_ini(ini, "[Datacenter-B]\nhost = 10.0.0.2\ntype = oneview\n")
    state_file = tmp_path / "oneview_state.json"
    monkeypatch.setattr(ov_config, "_find_config_file", lambda: ini)
    monkeypatch.setattr(ov_config, "_state_file", lambda: state_file)

    resolved = ov_config.set_active_appliance("datacenter-b")

    assert resolved == "Datacenter-B"  # returns the canonical section-name casing
    assert json.loads(state_file.read_text())["active_appliance"] == "Datacenter-B"


def test_set_active_appliance_unknown_name_raises_with_known_list(tmp_path, monkeypatch):
    ini = tmp_path / "inventory.ini"
    _write_ini(ini, "[oneview]\nhost = 10.0.0.1\n")
    monkeypatch.setattr(ov_config, "_find_config_file", lambda: ini)
    monkeypatch.setattr(ov_config, "_state_file", lambda: tmp_path / "oneview_state.json")

    with pytest.raises(ValueError, match="Known appliances: oneview"):
        ov_config.set_active_appliance("nope")


def test_load_oneview_config_single_appliance_ignores_state(tmp_path, monkeypatch):
    """With only one appliance configured, always return it -- zero friction,
    identical behaviour to before multi-appliance support existed."""
    ini = tmp_path / "inventory.ini"
    _write_ini(ini, "[oneview]\nhost = 10.0.0.1\nusername = Administrator\npassword = pw\n")
    monkeypatch.setattr(ov_config, "_find_config_file", lambda: ini)
    monkeypatch.setattr(ov_config, "_state_file", lambda: tmp_path / "oneview_state.json")

    cfg = ov_config.load_oneview_config()
    assert cfg == {
        "name": "oneview", "host": "10.0.0.1", "url": "https://10.0.0.1",
        "username": "Administrator", "password": "pw",
    }


def test_load_oneview_config_multi_appliance_uses_active_selection(tmp_path, monkeypatch):
    ini = tmp_path / "inventory.ini"
    _write_ini(ini, """
[oneview]
host = 10.0.0.1
type = oneview

[datacenter-b]
host = 10.0.0.2
type = oneview
""")
    state_file = tmp_path / "oneview_state.json"
    state_file.write_text(json.dumps({"active_appliance": "datacenter-b"}))
    monkeypatch.setattr(ov_config, "_find_config_file", lambda: ini)
    monkeypatch.setattr(ov_config, "_state_file", lambda: state_file)

    cfg = ov_config.load_oneview_config()
    assert cfg["name"] == "datacenter-b"
    assert cfg["host"] == "10.0.0.2"


def test_load_oneview_config_explicit_name_overrides_active(tmp_path, monkeypatch):
    ini = tmp_path / "inventory.ini"
    _write_ini(ini, """
[oneview]
host = 10.0.0.1
type = oneview

[datacenter-b]
host = 10.0.0.2
type = oneview
""")
    state_file = tmp_path / "oneview_state.json"
    state_file.write_text(json.dumps({"active_appliance": "datacenter-b"}))
    monkeypatch.setattr(ov_config, "_find_config_file", lambda: ini)
    monkeypatch.setattr(ov_config, "_state_file", lambda: state_file)

    cfg = ov_config.load_oneview_config(name="oneview")
    assert cfg["name"] == "oneview"
    assert cfg["host"] == "10.0.0.1"


def test_load_oneview_config_unknown_explicit_name_raises(tmp_path, monkeypatch):
    ini = tmp_path / "inventory.ini"
    _write_ini(ini, "[oneview]\nhost = 10.0.0.1\n")
    monkeypatch.setattr(ov_config, "_find_config_file", lambda: ini)
    monkeypatch.setattr(ov_config, "_state_file", lambda: tmp_path / "oneview_state.json")

    with pytest.raises(ValueError, match="not found"):
        ov_config.load_oneview_config(name="ghost")
