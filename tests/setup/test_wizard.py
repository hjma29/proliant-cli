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
# run_setup_wizard — end-to-end flows
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_setup_wizard_creates_new_file(tmp_path):
    dest = tmp_path / "inventory.ini"
    prompt_answers = iter(["srv1", "10.0.0.5", "Administrator"])
    confirm_answers = iter([False, False])  # no more iLO servers, no OneView

    with patch("rich.prompt.Prompt.ask", side_effect=lambda *a, **kw: next(prompt_answers)), \
         patch("rich.prompt.Confirm.ask", side_effect=lambda *a, **kw: next(confirm_answers)), \
         patch.object(wiz, "prompt_password_async", AsyncMock(return_value="hunter2")), \
         patch.object(wiz, "_test_ilo", AsyncMock(return_value=(True, "Connected successfully."))):
        await wiz.run_setup_wizard(dest=dest)

    saved = _read_cfg(dest)
    assert saved.get("srv1", "host") == "10.0.0.5"


@pytest.mark.asyncio
async def test_run_setup_wizard_merges_into_existing_file(tmp_path):
    dest = tmp_path / "inventory.ini"
    dest.write_text("[defaults]\nusername = Administrator\npassword = defaultpass\n\n[existing1]\nhost = 10.0.0.9\n")

    prompt_answers = iter(["srv2", "10.0.0.10", "localadmin"])
    confirm_answers = iter([False, False])

    with patch("rich.prompt.Prompt.ask", side_effect=lambda *a, **kw: next(prompt_answers)), \
         patch("rich.prompt.Confirm.ask", side_effect=lambda *a, **kw: next(confirm_answers)), \
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
        "srv1", "10.0.0.5", "Administrator",       # iLO server
        "oneview", "10.0.0.100", "Administrator",  # OneView appliance
    ])
    confirm_answers = iter([False, True])  # no more iLO servers, yes add OneView

    with patch("rich.prompt.Prompt.ask", side_effect=lambda *a, **kw: next(prompt_answers)), \
         patch("rich.prompt.Confirm.ask", side_effect=lambda *a, **kw: next(confirm_answers)), \
         patch.object(wiz, "prompt_password_async", AsyncMock(return_value="secret")), \
         patch.object(wiz, "_test_ilo", AsyncMock(return_value=(True, "Connected successfully."))), \
         patch.object(wiz, "_test_oneview", AsyncMock(return_value=(True, "Connected successfully."))):
        await wiz.run_setup_wizard(dest=dest)

    saved = _read_cfg(dest)
    assert saved.get("srv1", "host") == "10.0.0.5"
    assert saved.get("oneview", "type") == "oneview"


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
