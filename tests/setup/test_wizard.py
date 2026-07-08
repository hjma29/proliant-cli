"""Tests for proliant.setup.wizard — the guided `proliant setup` onboarding flow."""

from __future__ import annotations

import configparser
from unittest.mock import AsyncMock, patch

import pytest

import proliant.setup.wizard as wiz


def _read_cfg(path):
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read(path)
    return cfg


# ---------------------------------------------------------------------------
# _prompt_name
# ---------------------------------------------------------------------------

def test_prompt_name_rejects_empty_and_reserved_and_duplicate_then_accepts():
    answers = iter(["", "defaults", "existing", "srv1"])
    with patch("rich.prompt.Prompt.ask", side_effect=lambda *a, **kw: next(answers)):
        name = wiz._prompt_name({"existing"}, "Server")
    assert name == "srv1"


# ---------------------------------------------------------------------------
# _open_in_editor
# ---------------------------------------------------------------------------

def test_open_in_editor_missing_file_does_nothing(tmp_path):
    missing = tmp_path / "nope.ini"
    with patch("subprocess.run") as run, patch("os.startfile", create=True) as startfile:
        wiz._open_in_editor(missing)
    run.assert_not_called()
    startfile.assert_not_called()


def test_open_in_editor_prefers_editor_env(tmp_path, monkeypatch):
    path = tmp_path / "inv.ini"
    path.write_text("[srv1]\nhost = 10.0.0.5\n")
    monkeypatch.setenv("EDITOR", "myeditor --wait")
    monkeypatch.delenv("VISUAL", raising=False)
    with patch("subprocess.run") as run:
        wiz._open_in_editor(path)
    run.assert_called_once_with(["myeditor", "--wait", str(path)], check=False)


def test_open_in_editor_windows_uses_startfile(tmp_path, monkeypatch):
    path = tmp_path / "inv.ini"
    path.write_text("[srv1]\nhost = 10.0.0.5\n")
    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.delenv("VISUAL", raising=False)
    with patch.object(wiz.sys, "platform", "win32"), patch(
        "os.startfile", create=True
    ) as startfile:
        wiz._open_in_editor(path)
    startfile.assert_called_once_with(str(path))


# ---------------------------------------------------------------------------
# _backup_ini rotation
# ---------------------------------------------------------------------------

def test_backup_ini_noop_when_source_missing(tmp_path):
    dest = tmp_path / "inventory.ini"
    wiz._backup_ini(dest)  # must not raise
    assert not (tmp_path / "inventory.ini.bak1").exists()


def test_backup_ini_creates_bak1_on_first_save(tmp_path):
    dest = tmp_path / "inventory.ini"
    dest.write_text("v1")
    wiz._backup_ini(dest)
    assert (tmp_path / "inventory.ini.bak1").read_text() == "v1"


def test_backup_ini_rotates_and_caps_at_three(tmp_path):
    dest = tmp_path / "inventory.ini"
    # Simulate four successive saves of distinct contents.
    for content in ["v1", "v2", "v3", "v4"]:
        dest.write_text(content)
        wiz._backup_ini(dest)

    # After 4 backups, only the 3 most recent snapshots survive.
    # Each _backup_ini copies the *current* file to .bak1 before the next write.
    assert (tmp_path / "inventory.ini.bak1").read_text() == "v4"
    assert (tmp_path / "inventory.ini.bak2").read_text() == "v3"
    assert (tmp_path / "inventory.ini.bak3").read_text() == "v2"
    # 'v1' has aged out.
    assert not (tmp_path / "inventory.ini.bak4").exists()


def test_save_ini_makes_backup_of_previous_version(tmp_path):
    dest = tmp_path / "inventory.ini"
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.add_section("srv1")
    cfg.set("srv1", "host", "10.0.0.5")
    wiz._save_ini(cfg, dest)  # first save -- nothing to back up yet
    assert not (tmp_path / "inventory.ini.bak1").exists()

    cfg.set("srv1", "host", "10.0.0.6")
    wiz._save_ini(cfg, dest)  # second save -- backs up the first version
    backup = _read_cfg(tmp_path / "inventory.ini.bak1")
    assert backup.get("srv1", "host") == "10.0.0.5"


# ---------------------------------------------------------------------------
# _add_ilo_server
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_add_ilo_server_success_saves_entry(tmp_path):
    dest = tmp_path / "inventory.ini"
    cfg = configparser.ConfigParser(interpolation=None)
    existing: set[str] = set()

    prompt_answers = iter(["srv1", "10.0.0.5", "Administrator"])
    with patch("rich.prompt.Prompt.ask", side_effect=lambda *a, **kw: next(prompt_answers)), \
         patch.object(wiz, "prompt_password_async", AsyncMock(return_value="hunter2")), \
         patch.object(wiz, "_test_ilo", AsyncMock(return_value=(True, "Connected successfully."))):
        added = await wiz._add_ilo_server(cfg, existing, dest)

    assert added is True
    assert "srv1" in existing
    saved = _read_cfg(dest)
    assert saved.get("srv1", "host") == "10.0.0.5"
    assert saved.get("srv1", "password") == "hunter2"


@pytest.mark.asyncio
async def test_add_ilo_server_failed_test_discarded_by_default(tmp_path):
    dest = tmp_path / "inventory.ini"
    cfg = configparser.ConfigParser(interpolation=None)
    existing: set[str] = set()

    prompt_answers = iter(["srv1", "10.0.0.5", "Administrator"])
    with patch("rich.prompt.Prompt.ask", side_effect=lambda *a, **kw: next(prompt_answers)), \
         patch("rich.prompt.Confirm.ask", return_value=False), \
         patch.object(wiz, "prompt_password_async", AsyncMock(return_value="hunter2")), \
         patch.object(wiz, "_test_ilo", AsyncMock(return_value=(False, "Unreachable: simulated"))):
        added = await wiz._add_ilo_server(cfg, existing, dest)

    assert added is False
    assert "srv1" not in existing
    assert not dest.exists()


@pytest.mark.asyncio
async def test_add_ilo_server_failed_test_saved_when_confirmed(tmp_path):
    dest = tmp_path / "inventory.ini"
    cfg = configparser.ConfigParser(interpolation=None)
    existing: set[str] = set()

    prompt_answers = iter(["srv1", "10.0.0.5", "Administrator"])
    with patch("rich.prompt.Prompt.ask", side_effect=lambda *a, **kw: next(prompt_answers)), \
         patch("rich.prompt.Confirm.ask", return_value=True), \
         patch.object(wiz, "prompt_password_async", AsyncMock(return_value="hunter2")), \
         patch.object(wiz, "_test_ilo", AsyncMock(return_value=(False, "Unreachable: simulated"))):
        added = await wiz._add_ilo_server(cfg, existing, dest)

    assert added is True
    saved = _read_cfg(dest)
    assert saved.get("srv1", "host") == "10.0.0.5"


@pytest.mark.asyncio
async def test_add_ilo_server_empty_host_skips_without_saving(tmp_path):
    dest = tmp_path / "inventory.ini"
    cfg = configparser.ConfigParser(interpolation=None)
    existing: set[str] = set()

    prompt_answers = iter(["srv1", ""])
    with patch("rich.prompt.Prompt.ask", side_effect=lambda *a, **kw: next(prompt_answers)):
        added = await wiz._add_ilo_server(cfg, existing, dest)

    assert added is False
    assert not dest.exists()


@pytest.mark.asyncio
async def test_add_ilo_server_blank_password_falls_back_to_defaults_password(tmp_path):
    dest = tmp_path / "inventory.ini"
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.add_section("defaults")
    cfg.set("defaults", "username", "Administrator")
    cfg.set("defaults", "password", "defaultpass")
    existing: set[str] = set()

    prompt_answers = iter(["srv1", "10.0.0.5", "Administrator"])
    with patch("rich.prompt.Prompt.ask", side_effect=lambda *a, **kw: next(prompt_answers)), \
         patch.object(wiz, "prompt_password_async", AsyncMock(return_value="")), \
         patch.object(wiz, "_test_ilo", AsyncMock(return_value=(True, "Connected successfully."))) as fake_test:
        added = await wiz._add_ilo_server(cfg, existing, dest)

    assert added is True
    # blank password prompt -> fall back to [defaults] password for both the
    # saved entry and the connection test that was actually run
    fake_test.assert_awaited_once_with("10.0.0.5", "Administrator", "defaultpass")
    saved = _read_cfg(dest)
    # username matches default -> not written per-section (relies on [defaults])
    assert not saved.has_option("srv1", "username")
    assert not saved.has_option("srv1", "password")


# ---------------------------------------------------------------------------
# _add_oneview
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_add_oneview_success_saves_entry_with_type(tmp_path):
    dest = tmp_path / "inventory.ini"
    cfg = configparser.ConfigParser(interpolation=None)
    existing: set[str] = set()

    prompt_answers = iter(["oneview", "10.0.0.100", "Administrator"])
    with patch("rich.prompt.Prompt.ask", side_effect=lambda *a, **kw: next(prompt_answers)), \
         patch.object(wiz, "prompt_password_async", AsyncMock(return_value="ovpass")), \
         patch.object(wiz, "_test_oneview", AsyncMock(return_value=(True, "Connected successfully."))):
        added = await wiz._add_oneview(cfg, existing, dest)

    assert added is True
    saved = _read_cfg(dest)
    assert saved.get("oneview", "host") == "10.0.0.100"
    assert saved.get("oneview", "type") == "oneview"


# ---------------------------------------------------------------------------
# _select_entry
# ---------------------------------------------------------------------------

def test_select_entry_valid_number_returns_name():
    with patch("rich.prompt.Prompt.ask", return_value="2"):
        name = wiz._select_entry(["srv1", "srv2"], "edit")
    assert name == "srv2"


def test_select_entry_blank_cancels():
    with patch("rich.prompt.Prompt.ask", return_value=""):
        name = wiz._select_entry(["srv1", "srv2"], "edit")
    assert name is None


def test_select_entry_non_numeric_cancels():
    with patch("rich.prompt.Prompt.ask", return_value="abc"):
        name = wiz._select_entry(["srv1", "srv2"], "edit")
    assert name is None


def test_select_entry_out_of_range_cancels():
    with patch("rich.prompt.Prompt.ask", return_value="9"):
        name = wiz._select_entry(["srv1", "srv2"], "edit")
    assert name is None


def test_select_entry_prints_compact_numbered_list(capsys):
    with patch("rich.prompt.Prompt.ask", return_value="2"):
        wiz._select_entry(["srv1", "srv2"], "delete")
    out = capsys.readouterr().out
    assert "1. srv1" in out
    assert "2. srv2" in out


# ---------------------------------------------------------------------------
# _edit_ilo_server / _edit_oneview
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_edit_ilo_server_updates_host_and_username(tmp_path):
    dest = tmp_path / "inventory.ini"
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.add_section("defaults")
    cfg.set("defaults", "username", "Administrator")
    cfg.set("defaults", "password", "defaultpass")
    cfg.add_section("srv1")
    cfg.set("srv1", "host", "10.0.0.5")

    prompt_answers = iter(["srv1", "10.0.0.6", "localadmin"])
    with patch("rich.prompt.Prompt.ask", side_effect=lambda *a, **kw: next(prompt_answers)), \
         patch.object(wiz, "prompt_password_async", AsyncMock(return_value="")), \
         patch.object(wiz, "_test_ilo", AsyncMock(return_value=(True, "Connected successfully."))) as fake_test:
        await wiz._edit_ilo_server(cfg, "srv1", dest)

    # blank password -> keeps falling back to [defaults] password for the test
    fake_test.assert_awaited_once_with("10.0.0.6", "localadmin", "defaultpass")
    saved = _read_cfg(dest)
    assert saved.get("srv1", "host") == "10.0.0.6"
    assert saved.get("srv1", "username") == "localadmin"
    assert not saved.has_option("srv1", "password")


@pytest.mark.asyncio
async def test_edit_ilo_server_blank_password_keeps_existing_override(tmp_path):
    dest = tmp_path / "inventory.ini"
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.add_section("defaults")
    cfg.set("defaults", "username", "Administrator")
    cfg.set("defaults", "password", "defaultpass")
    cfg.add_section("srv1")
    cfg.set("srv1", "host", "10.0.0.5")
    cfg.set("srv1", "password", "customsecret")

    prompt_answers = iter(["srv1", "10.0.0.5", "Administrator"])
    with patch("rich.prompt.Prompt.ask", side_effect=lambda *a, **kw: next(prompt_answers)), \
         patch.object(wiz, "prompt_password_async", AsyncMock(return_value="")), \
         patch.object(wiz, "_test_ilo", AsyncMock(return_value=(True, "Connected successfully."))):
        await wiz._edit_ilo_server(cfg, "srv1", dest)

    saved = _read_cfg(dest)
    # left blank -> existing per-section password override is untouched
    assert saved.get("srv1", "password") == "customsecret"


@pytest.mark.asyncio
async def test_edit_ilo_server_failed_test_discarded_by_default(tmp_path):
    dest = tmp_path / "inventory.ini"
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.add_section("srv1")
    cfg.set("srv1", "host", "10.0.0.5")
    wiz._save_ini(cfg, dest)

    prompt_answers = iter(["srv1", "10.0.0.9", "Administrator"])
    with patch("rich.prompt.Prompt.ask", side_effect=lambda *a, **kw: next(prompt_answers)), \
         patch("rich.prompt.Confirm.ask", return_value=False), \
         patch.object(wiz, "prompt_password_async", AsyncMock(return_value="x")), \
         patch.object(wiz, "_test_ilo", AsyncMock(return_value=(False, "Unreachable: simulated"))):
        await wiz._edit_ilo_server(cfg, "srv1", dest)

    saved = _read_cfg(dest)
    # discarded -- original host preserved on disk
    assert saved.get("srv1", "host") == "10.0.0.5"


@pytest.mark.asyncio
async def test_edit_oneview_updates_fields(tmp_path):
    dest = tmp_path / "inventory.ini"
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.add_section("oneview")
    cfg.set("oneview", "host", "10.0.0.100")
    cfg.set("oneview", "username", "Administrator")
    cfg.set("oneview", "type", "oneview")

    prompt_answers = iter(["oneview", "10.0.0.101", "ovadmin"])
    with patch("rich.prompt.Prompt.ask", side_effect=lambda *a, **kw: next(prompt_answers)), \
         patch.object(wiz, "prompt_password_async", AsyncMock(return_value="newpass")), \
         patch.object(wiz, "_test_oneview", AsyncMock(return_value=(True, "Connected successfully."))):
        await wiz._edit_oneview(cfg, "oneview", dest)

    saved = _read_cfg(dest)
    assert saved.get("oneview", "host") == "10.0.0.101"
    assert saved.get("oneview", "username") == "ovadmin"
    assert saved.get("oneview", "password") == "newpass"


@pytest.mark.asyncio
async def test_edit_ilo_server_renames_section_and_updates_existing_and_statuses(tmp_path):
    dest = tmp_path / "inventory.ini"
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.add_section("srv1")
    cfg.set("srv1", "host", "10.0.0.5")
    cfg.add_section("srv2")
    cfg.set("srv2", "host", "10.0.0.6")
    existing = {"srv1", "srv2"}
    statuses = {"srv1": "Reachable", "srv2": "Reachable"}

    prompt_answers = iter(["dl380-gen11", "10.0.0.5", "Administrator"])
    with patch("rich.prompt.Prompt.ask", side_effect=lambda *a, **kw: next(prompt_answers)), \
         patch.object(wiz, "prompt_password_async", AsyncMock(return_value="")), \
         patch.object(wiz, "_test_ilo", AsyncMock(return_value=(True, "Connected successfully."))):
        new_name = await wiz._edit_ilo_server(cfg, "srv1", dest, statuses, existing)

    assert new_name == "dl380-gen11"
    saved = _read_cfg(dest)
    assert not saved.has_section("srv1")
    assert saved.get("dl380-gen11", "host") == "10.0.0.5"
    # section order preserved -- renamed entry stays in its original position
    assert saved.sections() == ["dl380-gen11", "srv2"]
    assert existing == {"dl380-gen11", "srv2"}
    assert statuses == {"dl380-gen11": "Reachable", "srv2": "Reachable"}


@pytest.mark.asyncio
async def test_edit_ilo_server_rename_to_existing_name_reprompts(tmp_path):
    dest = tmp_path / "inventory.ini"
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.add_section("srv1")
    cfg.set("srv1", "host", "10.0.0.5")
    cfg.add_section("srv2")
    cfg.set("srv2", "host", "10.0.0.6")
    existing = {"srv1", "srv2"}

    # first attempt collides with the other entry's name -- must re-prompt
    prompt_answers = iter(["srv2", "srv1", "10.0.0.5", "Administrator"])
    with patch("rich.prompt.Prompt.ask", side_effect=lambda *a, **kw: next(prompt_answers)), \
         patch.object(wiz, "prompt_password_async", AsyncMock(return_value="")), \
         patch.object(wiz, "_test_ilo", AsyncMock(return_value=(True, "Connected successfully."))):
        new_name = await wiz._edit_ilo_server(cfg, "srv1", dest, existing=existing)

    assert new_name == "srv1"


@pytest.mark.asyncio
async def test_edit_ilo_server_rename_discarded_on_failed_test_keeps_old_name(tmp_path):
    dest = tmp_path / "inventory.ini"
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.add_section("srv1")
    cfg.set("srv1", "host", "10.0.0.5")
    existing = {"srv1"}
    statuses = {"srv1": "Reachable"}

    prompt_answers = iter(["dl380-gen11", "10.0.0.9", "Administrator"])
    with patch("rich.prompt.Prompt.ask", side_effect=lambda *a, **kw: next(prompt_answers)), \
         patch("rich.prompt.Confirm.ask", return_value=False), \
         patch.object(wiz, "prompt_password_async", AsyncMock(return_value="")), \
         patch.object(wiz, "_test_ilo", AsyncMock(return_value=(False, "Unreachable: simulated"))):
        new_name = await wiz._edit_ilo_server(cfg, "srv1", dest, statuses, existing)

    # discarded -- neither the section rename nor the existing/statuses bookkeeping happened
    assert new_name == "srv1"
    assert cfg.has_section("srv1")
    assert existing == {"srv1"}
    assert statuses == {"srv1": "Reachable"}


# ---------------------------------------------------------------------------
# _delete_entry
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_delete_entry_confirmed_removes_section(tmp_path):
    dest = tmp_path / "inventory.ini"
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.add_section("srv1")
    cfg.set("srv1", "host", "10.0.0.5")
    existing = {"srv1"}

    with patch("rich.prompt.Prompt.ask", return_value="1"), \
         patch("rich.prompt.Confirm.ask", return_value=True):
        await wiz._delete_entry(cfg, ["srv1"], existing, dest)

    assert "srv1" not in existing
    saved = _read_cfg(dest)
    assert not saved.has_section("srv1")


@pytest.mark.asyncio
async def test_delete_entry_not_confirmed_keeps_section(tmp_path):
    dest = tmp_path / "inventory.ini"
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.add_section("srv1")
    cfg.set("srv1", "host", "10.0.0.5")
    existing = {"srv1"}

    with patch("rich.prompt.Prompt.ask", return_value="1"), \
         patch("rich.prompt.Confirm.ask", return_value=False):
        await wiz._delete_entry(cfg, ["srv1"], existing, dest)

    assert "srv1" in existing
    assert not dest.exists()  # never saved -- deletion was cancelled


# ---------------------------------------------------------------------------
# _prompt_menu
# ---------------------------------------------------------------------------

def test_prompt_menu_returns_key_for_valid_number():
    with patch("rich.prompt.Prompt.ask", return_value="2"):
        key = wiz._prompt_menu([("add", "Add"), ("done", "Done")])
    assert key == "done"


def test_prompt_menu_blank_uses_default():
    # Simulates Rich's real behavior: blank input -> returns the `default` kwarg passed to it.
    def fake_ask(prompt, default=None, **kw):
        return default

    with patch("rich.prompt.Prompt.ask", side_effect=fake_ask):
        key = wiz._prompt_menu([("ilo", "iLO server"), ("oneview", "OneView appliance")], default="ilo")
    assert key == "ilo"


def test_prompt_menu_reprompts_on_invalid_then_out_of_range_then_accepts():
    answers = iter(["abc", "9", "1"])
    with patch("rich.prompt.Prompt.ask", side_effect=lambda *a, **kw: next(answers)):
        key = wiz._prompt_menu([("add", "Add"), ("done", "Done")])
    assert key == "add"


# ---------------------------------------------------------------------------
# run_setup_wizard — end-to-end flows
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_setup_wizard_creates_new_file(tmp_path):
    dest = tmp_path / "inventory.ini"
    # menu: Add(1)/Done(2) -> "1"; kind: iLO(1)/OneView(2) -> "1"; then fields;
    # loop again with 1 entry -> Add(1)/Edit(2)/Delete(3)/Open(4)/Done(5) -> "5"
    prompt_answers = iter(["1", "1", "srv1", "10.0.0.5", "Administrator", "5"])

    with patch("rich.prompt.Prompt.ask", side_effect=lambda *a, **kw: next(prompt_answers)), \
         patch.object(wiz, "prompt_password_async", AsyncMock(return_value="hunter2")), \
         patch.object(wiz, "_test_ilo", AsyncMock(return_value=(True, "Connected successfully."))):
        await wiz.run_setup_wizard(dest=dest)

    saved = _read_cfg(dest)
    assert saved.get("srv1", "host") == "10.0.0.5"


@pytest.mark.asyncio
async def test_run_setup_wizard_merges_into_existing_file(tmp_path):
    dest = tmp_path / "inventory.ini"
    dest.write_text("[defaults]\nusername = Administrator\npassword = defaultpass\n\n[existing1]\nhost = 10.0.0.9\n")

    # starts with 1 entry -> Add(1)/Edit(2)/Delete(3)/Open(4)/Done(5) -> "1"; kind iLO(1)/OneView(2) -> "1";
    # then fields; loop again with 2 entries -> Done is "5"
    prompt_answers = iter(["1", "1", "srv2", "10.0.0.10", "localadmin", "5"])

    with patch("rich.prompt.Prompt.ask", side_effect=lambda *a, **kw: next(prompt_answers)), \
         patch.object(wiz, "prompt_password_async", AsyncMock(return_value="p@ss%word")), \
         patch.object(wiz, "_test_ilo", AsyncMock(return_value=(True, "Connected successfully."))):
        await wiz.run_setup_wizard(dest=dest)

    saved = _read_cfg(dest)
    # original entry untouched
    assert saved.get("existing1", "host") == "10.0.0.9"
    # new entry merged in, including a password containing a literal '%'
    assert saved.get("srv2", "host") == "10.0.0.10"
    assert saved.get("srv2", "password") == "p@ss%word"


@pytest.mark.asyncio
async def test_run_setup_wizard_handles_keyboard_interrupt_gracefully(tmp_path, capsys):
    dest = tmp_path / "inventory.ini"

    with patch("rich.prompt.Prompt.ask", side_effect=KeyboardInterrupt):
        await wiz.run_setup_wizard(dest=dest)

    # must not raise -- interrupted cleanly, nothing saved
    assert not dest.exists()


@pytest.mark.asyncio
async def test_run_setup_wizard_adds_oneview_when_confirmed(tmp_path):
    dest = tmp_path / "inventory.ini"

    prompt_answers = iter([
        "1", "1", "srv1", "10.0.0.5", "Administrator",              # menu:add, kind:ilo -- add iLO server
        "1", "2", "oneview", "10.0.0.100", "Administrator",         # menu:add, kind:oneview -- add OneView appliance
        "5",                                                         # menu:done (2 entries now, done is #5)
    ])

    with patch("rich.prompt.Prompt.ask", side_effect=lambda *a, **kw: next(prompt_answers)), \
         patch.object(wiz, "prompt_password_async", AsyncMock(return_value="secret")), \
         patch.object(wiz, "_test_ilo", AsyncMock(return_value=(True, "Connected successfully."))), \
         patch.object(wiz, "_test_oneview", AsyncMock(return_value=(True, "Connected successfully."))):
        await wiz.run_setup_wizard(dest=dest)

    saved = _read_cfg(dest)
    assert saved.get("srv1", "host") == "10.0.0.5"
    assert saved.get("oneview", "type") == "oneview"


@pytest.mark.asyncio
async def test_run_setup_wizard_edit_then_delete(tmp_path):
    dest = tmp_path / "inventory.ini"
    dest.write_text("[srv1]\nhost = 10.0.0.5\nusername = Administrator\n")

    # edit srv1's host, then delete it, then done
    prompt_answers = iter([
        "2", "1", "srv1", "10.0.0.6", "Administrator",  # menu:edit -- select #1, keep name, new host, same username
        "3", "1",                                # menu:delete -- select #1
        "3",                                     # menu:done (0 entries now, Add(1)/Open(2)/Done(3))
    ])
    confirm_answers = iter([True])  # confirm the deletion

    with patch("rich.prompt.Prompt.ask", side_effect=lambda *a, **kw: next(prompt_answers)), \
         patch("rich.prompt.Confirm.ask", side_effect=lambda *a, **kw: next(confirm_answers)), \
         patch.object(wiz, "prompt_password_async", AsyncMock(return_value="")), \
         patch.object(wiz, "_test_ilo", AsyncMock(return_value=(True, "Connected successfully."))):
        await wiz.run_setup_wizard(dest=dest)

    saved = _read_cfg(dest)
    assert not saved.has_section("srv1")


# ---------------------------------------------------------------------------
# _test_ilo / _test_oneview error classification
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_test_ilo_reports_unreachable_host():
    import httpx

    with patch("proliant.ilo.client.ilo_session", side_effect=httpx.ConnectError("boom")):
        ok, message = await wiz._test_ilo("10.0.0.5", "Administrator", "pass")

    assert ok is False
    assert "Unreachable" in message


@pytest.mark.asyncio
async def test_test_oneview_reports_oneview_error():
    from proliant.oneview.client import OneViewError

    with patch("proliant.oneview.client.OneViewClient", side_effect=OneViewError("login failed")):
        ok, message = await wiz._test_oneview("10.0.0.100", "Administrator", "pass")

    assert ok is False
    assert "login failed" in message


@pytest.mark.asyncio
async def test_test_ilo_reports_timeout():
    import httpx

    with patch("proliant.ilo.client.ilo_session", side_effect=httpx.ConnectTimeout("timed out")):
        ok, message = await wiz._test_ilo("10.0.0.5", "Administrator", "pass")

    assert ok is False
    assert message.startswith("Timeout")


@pytest.mark.asyncio
async def test_test_ilo_reports_auth_failed():
    with patch(
        "proliant.ilo.client.ilo_session",
        side_effect=RuntimeError("POST /redfish/v1/SessionService/Sessions failed — HTTP 401: check username/password"),
    ):
        ok, message = await wiz._test_ilo("10.0.0.5", "Administrator", "wrongpass")

    assert ok is False
    assert message == "Auth failed: check username/password"
    # the raw method/URI/HTTP-status detail must not leak into the user-facing message
    assert "POST" not in message
    assert "HTTP" not in message


@pytest.mark.asyncio
async def test_test_ilo_reports_auth_failed_forbidden():
    with patch(
        "proliant.ilo.client.ilo_session",
        side_effect=RuntimeError("POST /redfish/v1/... failed — HTTP 403: account lacks permission for this operation"),
    ):
        ok, message = await wiz._test_ilo("10.0.0.5", "Administrator", "pass")

    assert ok is False
    assert message == "Auth failed: account lacks permission for this operation"
    assert "HTTP" not in message


@pytest.mark.asyncio
async def test_test_oneview_reports_auth_failed():
    from proliant.oneview.client import OneViewError

    with patch(
        "proliant.oneview.client.OneViewClient",
        side_effect=OneViewError("OneView login failed (HTTP 401): invalid credentials"),
    ):
        ok, message = await wiz._test_oneview("10.0.0.100", "Administrator", "wrongpass")

    assert ok is False
    assert message == "Auth failed: check username/password"
    assert "HTTP" not in message


@pytest.mark.asyncio
async def test_test_oneview_reports_timeout_via_chained_cause():
    import httpx

    from proliant.oneview.client import OneViewError

    # side_effect with a bare exception instance never goes through 'raise ... from',
    # so __cause__ would be None -- construct it via a real raise to populate __cause__.
    try:
        try:
            raise httpx.ConnectTimeout("timed out")
        except httpx.ConnectTimeout as exc:
            raise OneViewError("Cannot reach OneView appliance at https://10.0.0.100: timed out") from exc
    except OneViewError as chained:
        wrapped = chained

    with patch("proliant.oneview.client.OneViewClient", side_effect=wrapped):
        ok, message = await wiz._test_oneview("10.0.0.100", "Administrator", "pass")

    assert ok is False
    assert message.startswith("Timeout")


# ---------------------------------------------------------------------------
# _status_label
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "ok, message, expected",
    [
        (True, "Connected successfully.", "Reachable"),
        (False, "Timeout: timed out", "Timeout"),
        (False, "Unreachable: connection refused", "Unreachable"),
        (False, "Auth failed: HTTP 401", "Auth failed"),
        (False, "some unclassified error", "Error"),
    ],
)
def test_status_label_classifies_known_prefixes(ok, message, expected):
    assert wiz._status_label(ok, message) == expected


# ---------------------------------------------------------------------------
# _check_entry_status / _check_all_statuses
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_check_entry_status_no_host_returns_no_host():
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.add_section("srv1")

    status = await wiz._check_entry_status(cfg, "srv1")

    assert status == "No host"


@pytest.mark.asyncio
async def test_check_entry_status_dispatches_to_test_ilo():
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.add_section("srv1")
    cfg.set("srv1", "host", "10.0.0.5")

    with patch.object(wiz, "_test_ilo", AsyncMock(return_value=(True, "Connected successfully."))) as fake:
        status = await wiz._check_entry_status(cfg, "srv1")

    fake.assert_awaited_once()
    assert status == "Reachable"


@pytest.mark.asyncio
async def test_check_entry_status_dispatches_to_test_oneview():
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.add_section("oneview")
    cfg.set("oneview", "host", "10.0.0.100")
    cfg.set("oneview", "type", "oneview")

    with patch.object(wiz, "_test_oneview", AsyncMock(return_value=(False, "Unreachable: boom"))) as fake:
        status = await wiz._check_entry_status(cfg, "oneview")

    fake.assert_awaited_once()
    assert status == "Unreachable"


@pytest.mark.asyncio
async def test_check_all_statuses_runs_concurrently_for_every_entry():
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.add_section("srv1")
    cfg.set("srv1", "host", "10.0.0.5")
    cfg.add_section("srv2")
    cfg.set("srv2", "host", "10.0.0.6")

    async def fake_test_ilo(host, username, password):
        if host == "10.0.0.5":
            return True, "Connected successfully."
        return False, "Timeout: timed out"

    with patch.object(wiz, "_test_ilo", AsyncMock(side_effect=fake_test_ilo)):
        statuses = await wiz._check_all_statuses(cfg, ["srv1", "srv2"])

    assert statuses == {"srv1": "Reachable", "srv2": "Timeout"}


@pytest.mark.asyncio
async def test_check_all_statuses_empty_entries_returns_empty_dict():
    cfg = configparser.ConfigParser(interpolation=None)

    statuses = await wiz._check_all_statuses(cfg, [])

    assert statuses == {}


# ---------------------------------------------------------------------------
# statuses dict threading through add/edit/delete
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_add_ilo_server_records_status_when_statuses_dict_provided(tmp_path):
    dest = tmp_path / "inventory.ini"
    cfg = configparser.ConfigParser(interpolation=None)
    existing: set[str] = set()
    statuses: dict[str, str] = {}

    prompt_answers = iter(["srv1", "10.0.0.5", "Administrator"])
    with patch("rich.prompt.Prompt.ask", side_effect=lambda *a, **kw: next(prompt_answers)), \
         patch.object(wiz, "prompt_password_async", AsyncMock(return_value="hunter2")), \
         patch.object(wiz, "_test_ilo", AsyncMock(return_value=(True, "Connected successfully."))):
        await wiz._add_ilo_server(cfg, existing, dest, statuses)

    assert statuses == {"srv1": "Reachable"}


@pytest.mark.asyncio
async def test_edit_ilo_server_updates_status_when_statuses_dict_provided(tmp_path):
    dest = tmp_path / "inventory.ini"
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.add_section("srv1")
    cfg.set("srv1", "host", "10.0.0.5")
    statuses = {"srv1": "Reachable"}

    prompt_answers = iter(["srv1", "10.0.0.9", "Administrator"])
    with patch("rich.prompt.Prompt.ask", side_effect=lambda *a, **kw: next(prompt_answers)), \
         patch("rich.prompt.Confirm.ask", return_value=True), \
         patch.object(wiz, "prompt_password_async", AsyncMock(return_value="")), \
         patch.object(wiz, "_test_ilo", AsyncMock(return_value=(False, "Timeout: timed out"))):
        await wiz._edit_ilo_server(cfg, "srv1", dest, statuses)

    assert statuses == {"srv1": "Timeout"}


@pytest.mark.asyncio
async def test_delete_entry_removes_status_when_statuses_dict_provided(tmp_path):
    dest = tmp_path / "inventory.ini"
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.add_section("srv1")
    cfg.set("srv1", "host", "10.0.0.5")
    existing = {"srv1"}
    statuses = {"srv1": "Reachable"}

    with patch("rich.prompt.Prompt.ask", return_value="1"), \
         patch("rich.prompt.Confirm.ask", return_value=True):
        await wiz._delete_entry(cfg, ["srv1"], existing, dest, statuses)

    assert "srv1" not in statuses


# ---------------------------------------------------------------------------
# _print_entries with statuses
# ---------------------------------------------------------------------------

def test_print_entries_renders_status_column(capsys):
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.add_section("srv1")
    cfg.set("srv1", "host", "10.0.0.5")
    cfg.add_section("srv2")
    cfg.set("srv2", "host", "10.0.0.6")

    wiz._print_entries(cfg, ["srv1", "srv2"], {"srv1": "Reachable", "srv2": "Timeout"})

    out = capsys.readouterr().out
    assert "Reachable" in out
    assert "Timeout" in out


def test_print_entries_shows_placeholder_when_no_statuses_known(capsys):
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.add_section("srv1")
    cfg.set("srv1", "host", "10.0.0.5")

    wiz._print_entries(cfg, ["srv1"])

    out = capsys.readouterr().out
    assert "?" in out


# ---------------------------------------------------------------------------
# run_setup_wizard -- initial parallel status check
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_setup_wizard_checks_statuses_at_start_for_existing_entries(tmp_path):
    dest = tmp_path / "inventory.ini"
    dest.write_text("[srv1]\nhost = 10.0.0.5\nusername = Administrator\n")

    prompt_answers = iter(["5"])  # menu:done immediately (1 entry -> Done is #5)

    with patch("rich.prompt.Prompt.ask", side_effect=lambda *a, **kw: next(prompt_answers)), \
         patch.object(wiz, "_test_ilo", AsyncMock(return_value=(True, "Connected successfully."))) as fake_test, \
         patch.object(wiz, "_print_entries") as fake_print:
        await wiz.run_setup_wizard(dest=dest)

    # the pre-loop parallel check ran exactly once for the one existing entry
    fake_test.assert_awaited_once()
    # and the resulting status was threaded into the table render
    _, args, _ = fake_print.mock_calls[0]
    rendered_statuses = args[2]
    assert rendered_statuses == {"srv1": "Reachable"}


@pytest.mark.asyncio
async def test_run_setup_wizard_skips_status_check_when_no_entries(tmp_path):
    dest = tmp_path / "inventory.ini"

    prompt_answers = iter(["3"])  # menu:done immediately (0 entries -> Add(1)/Open(2)/Done(3))

    with patch("rich.prompt.Prompt.ask", side_effect=lambda *a, **kw: next(prompt_answers)), \
         patch.object(wiz, "_test_ilo", AsyncMock(return_value=(True, "Connected successfully."))) as fake_test:
        await wiz.run_setup_wizard(dest=dest)

    fake_test.assert_not_awaited()
