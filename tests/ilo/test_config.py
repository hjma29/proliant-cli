"""Tests for proliant.ilo.config — inventory.ini loading, esp. username fallback."""

from __future__ import annotations

from pathlib import Path

import pytest

import proliant.ilo.config as ilo_config


def _write_ini(path: Path, text: str) -> None:
    path.write_text(text)


def test_load_hosts_defaults_missing_username_to_administrator(tmp_path, monkeypatch):
    """Regression test: 'proliant setup' never writes a [defaults] section at
    all (see wizard.py — nothing ever calls cfg.set("defaults", ...)), and
    deliberately skips writing 'username' on an entry when it equals the
    wizard's own assumed default ("Administrator") to keep inventory.ini
    terse. A fresh inventory.ini with an all-"Administrator" fleet therefore
    has NO [defaults] section and NO per-entry 'username' key anywhere.

    load_hosts() previously fell back to "" in that case, silently sending an
    empty username on every real login (guaranteed 401 / "check
    authentication?") even though 'proliant setup's own live connection test
    reported "Reachable" moments earlier — it uses a different fallback
    (wizard._effective_username(), which already defaults to "Administrator").
    This must match that behavior (and proliant.oneview.config's own
    hardcoded "Administrator" fallback) so a freshly set-up fleet actually
    authenticates.
    """
    ini = tmp_path / "inventory.ini"
    _write_ini(ini, """
[server-1]
host = 10.16.41.31
password = secret1

[server-2]
host = 10.16.41.32
password = secret2
""")
    monkeypatch.setattr(ilo_config, "HOSTS_FILE", ini)

    hosts = ilo_config.load_hosts()

    assert [h["username"] for h in hosts] == ["Administrator", "Administrator"]
    assert [h["password"] for h in hosts] == ["secret1", "secret2"]


def test_load_hosts_defaults_section_username_still_takes_priority(tmp_path, monkeypatch):
    ini = tmp_path / "inventory.ini"
    _write_ini(ini, """
[defaults]
username = svc-account
password = shared-pw

[server-1]
host = 10.0.0.1
""")
    monkeypatch.setattr(ilo_config, "HOSTS_FILE", ini)

    hosts = ilo_config.load_hosts()

    assert hosts[0]["username"] == "svc-account"
    assert hosts[0]["password"] == "shared-pw"


def test_load_hosts_per_entry_username_overrides_default(tmp_path, monkeypatch):
    ini = tmp_path / "inventory.ini"
    _write_ini(ini, """
[defaults]
username = Administrator

[server-1]
host = 10.0.0.1
username = localadmin
password = pw
""")
    monkeypatch.setattr(ilo_config, "HOSTS_FILE", ini)

    hosts = ilo_config.load_hosts()

    assert hosts[0]["username"] == "localadmin"


def test_load_hosts_skips_oneview_sections(tmp_path, monkeypatch):
    ini = tmp_path / "inventory.ini"
    _write_ini(ini, """
[server-1]
host = 10.0.0.1

[oneview]
host = 10.0.0.2
type = oneview
""")
    monkeypatch.setattr(ilo_config, "HOSTS_FILE", ini)

    hosts = ilo_config.load_hosts()

    assert [h["name"] for h in hosts] == ["server-1"]


def test_load_hosts_missing_file_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(ilo_config, "HOSTS_FILE", tmp_path / "does-not-exist.ini")

    with pytest.raises(FileNotFoundError):
        ilo_config.load_hosts()


def test_load_hosts_malformed_ini_raises_friendly_valueerror_not_traceback(tmp_path, monkeypatch):
    """A duplicate key (e.g. hand-edited inventory.ini) must surface as a
    clear, actionable ValueError -- not a raw configparser.DuplicateOptionError
    traceback. Regression test for a real crash seen with 'proliant setup'."""
    ini = tmp_path / "inventory.ini"
    _write_ini(ini, "[dl380-gen11]\nhost = 10.0.0.5\nhost = 10.0.0.6\n")
    monkeypatch.setattr(ilo_config, "HOSTS_FILE", ini)

    with pytest.raises(ValueError) as excinfo:
        ilo_config.load_hosts()

    message = str(excinfo.value)
    assert "not in the right format" in message
    assert "sample-inventory.ini" in message
