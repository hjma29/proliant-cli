"""Tests for the interactive menu-driven wizard behind bare
`proliant oneview update enclosure` (no NAME given).

The wizard's only job is to interactively collect the same inputs the
--flag form takes, then delegate to the already-tested
`_async_update_enclosure()`. These tests mock that delegate out so they
exercise only the wizard's own step/back/cancel navigation.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from proliant.common.display import OutputMode, set_output_mode
from proliant.oneview import cli


@pytest.fixture(autouse=True)
def reset_output_mode():
    set_output_mode(OutputMode.TABLE)
    yield
    set_output_mode(OutputMode.TABLE)


class _NullStatus:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConsole:
    """Stand-in for rich.Console: feeds canned answers to input(), records
    everything printed so tests can assert on wording."""

    def __init__(self, answers: list[str]):
        self._answers = list(answers)
        self.prints: list[str] = []

    def print(self, *args, **kwargs):
        self.prints.append(" ".join(str(a) for a in args))

    def input(self, prompt: str = "", **kwargs):
        self.prints.append(prompt)
        if not self._answers:
            raise AssertionError(f"wizard asked for more input than the test supplied: {prompt!r}")
        return self._answers.pop(0)

    def status(self, message: str):
        return _NullStatus()


class _FakeCM:
    def __init__(self, client):
        self._client = client

    async def __aenter__(self):
        return self._client

    async def __aexit__(self, *exc):
        return False


_LE1 = {"name": "LE01", "uri": "/rest/logical-enclosures/le1", "status": "OK", "enclosure_uris": []}
_LE2 = {"name": "LE02", "uri": "/rest/logical-enclosures/le2", "status": "OK", "enclosure_uris": []}
_BASELINE_NEW = {"name": "SPP SY-2026.01.02", "version": "SY-2026.01.02", "uri": "/rest/fd/new", "release_date": "2026-03-05"}
_BASELINE_OLD = {"name": "SPP SY-2025.10.01", "version": "SY-2025.10.01", "uri": "/rest/fd/old", "release_date": "2025-09-26"}

_TARGETS = {
    "baselines": [_BASELINE_NEW, _BASELINE_OLD],
    "logical_enclosures": [_LE1, _LE2],
    "server_profiles": [],
    "hardware_enclosure_map": {},
    "appliance_version": "10.00.00-0507518",
}


async def _fake_fetch_apply_targets(_client):
    return _TARGETS


@pytest.fixture
def patched_wizard_io(monkeypatch):
    monkeypatch.setattr(cli, "_load_client", lambda name=None: _FakeCM(object()))
    monkeypatch.setattr(
        "proliant.oneview.ssp_update.fetch_apply_targets", _fake_fetch_apply_targets,
    )
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)


class TestWizardHelpers:
    def test_wizard_choice_blank_picks_default(self):
        console = _FakeConsole([""])
        result = cli._wizard_choice(console, "Pick one", [("a", "Alpha"), ("b", "Bravo")], default_index=1)
        assert result == "b"

    def test_wizard_choice_numeric_selection(self):
        console = _FakeConsole(["1"])
        result = cli._wizard_choice(console, "Pick one", [("a", "Alpha"), ("b", "Bravo")])
        assert result == "a"

    def test_wizard_choice_invalid_then_valid(self):
        console = _FakeConsole(["9", "abc", "2"])
        result = cli._wizard_choice(console, "Pick one", [("a", "Alpha"), ("b", "Bravo")])
        assert result == "b"
        assert any("Enter a number" in p for p in console.prints)

    def test_wizard_choice_back_raises(self):
        console = _FakeConsole(["b"])
        with pytest.raises(cli._WizardBack):
            cli._wizard_choice(console, "Pick one", [("a", "Alpha")], allow_back=True)

    def test_wizard_choice_back_disallowed_is_invalid_input(self):
        console = _FakeConsole(["b", "1"])
        result = cli._wizard_choice(console, "Pick one", [("a", "Alpha")], allow_back=False)
        assert result == "a"

    def test_wizard_choice_cancel_raises(self):
        console = _FakeConsole(["c"])
        with pytest.raises(cli._WizardCancelled):
            cli._wizard_choice(console, "Pick one", [("a", "Alpha")])

    def test_wizard_default_index_prefers_current_value(self):
        options = [("a", "Alpha"), ("b", "Bravo"), ("c", "Charlie")]
        assert cli._wizard_default_index(options, "c", 0) == 2

    def test_wizard_default_index_falls_back_when_no_current(self):
        options = [("a", "Alpha"), ("b", "Bravo")]
        assert cli._wizard_default_index(options, None, 1) == 1

    def test_wizard_default_index_matches_dicts_by_uri(self):
        options = [(_LE1, "LE01"), (_LE2, "LE02")]
        assert cli._wizard_default_index(options, _LE2, None) == 1


@pytest.mark.usefixtures("patched_wizard_io")
class TestWizardFlow:
    def test_full_walkthrough_plan_only(self, monkeypatch):
        # le=2 (LE02), baseline=default (newest), scope=default (shared-infra),
        # activation=default (orchestrated), force=default (No), execute=1 (No -> plan only)
        console = _FakeConsole(["2", "", "", "", "", "1"])
        monkeypatch.setattr(cli, "get_console", lambda: console)

        fake_delegate = AsyncMock()
        with patch.object(cli, "_async_update_enclosure", fake_delegate):
            cli.main(["update", "enclosure"])

        fake_delegate.assert_awaited_once()
        called_args = fake_delegate.await_args.args[0]
        assert called_args.name == "LE02"
        assert called_args.baseline == "SY-2026.01.02"
        assert called_args.scope == "shared-infra"
        assert called_args.install_type is None
        assert called_args.activation_mode == "orchestrated"
        assert called_args.force is False
        assert called_args.execute is False

    def test_shared_infra_and_profiles_install_type_always_skipped(self, monkeypatch):
        # install_type wizard step is always hidden (GUI doesn't expose it either;
        # use --install-type flag for a non-default value).
        # le=1 (LE01), baseline=default, scope=2 (shared-infra-and-profiles),
        # activation=default, force=default, execute=default (No)
        console = _FakeConsole(["1", "", "2", "", "", ""])
        monkeypatch.setattr(cli, "get_console", lambda: console)

        fake_delegate = AsyncMock()
        with patch.object(cli, "_async_update_enclosure", fake_delegate):
            cli.main(["update", "enclosure"])

        fake_delegate.assert_awaited_once()
        called_args = fake_delegate.await_args.args[0]
        assert called_args.scope == "shared-infra-and-profiles"
        assert called_args.install_type is None

    def test_back_navigation_returns_to_previous_step(self, monkeypatch):
        # le=1, baseline default, scope: go back ('b') to baseline, pick baseline=2 (old),
        # then scope default, activation default, force default, execute default(No)
        console = _FakeConsole(["1", "", "b", "2", "", "", "", ""])
        monkeypatch.setattr(cli, "get_console", lambda: console)

        fake_delegate = AsyncMock()
        with patch.object(cli, "_async_update_enclosure", fake_delegate):
            cli.main(["update", "enclosure"])

        fake_delegate.assert_awaited_once()
        called_args = fake_delegate.await_args.args[0]
        assert called_args.baseline == "SY-2025.10.01"

    def test_cancel_mid_flow_does_not_delegate(self, monkeypatch):
        console = _FakeConsole(["1", "c"])
        monkeypatch.setattr(cli, "get_console", lambda: console)

        fake_delegate = AsyncMock()
        with patch.object(cli, "_async_update_enclosure", fake_delegate):
            cli.main(["update", "enclosure"])

        fake_delegate.assert_not_awaited()
        assert any("Cancelled" in p for p in console.prints)

    def test_execute_yes_sets_execute_true(self, monkeypatch):
        console = _FakeConsole(["1", "", "", "", "", "2"])
        monkeypatch.setattr(cli, "get_console", lambda: console)

        fake_delegate = AsyncMock()
        with patch.object(cli, "_async_update_enclosure", fake_delegate):
            cli.main(["update", "enclosure"])

        called_args = fake_delegate.await_args.args[0]
        assert called_args.execute is True


class TestWizardGuards:
    def test_json_mode_declines_wizard(self, monkeypatch, capsys):
        set_output_mode(OutputMode.JSON)
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        fake_delegate = AsyncMock()
        with patch.object(cli, "_async_update_enclosure", fake_delegate):
            cli.main(["update", "enclosure"])
        fake_delegate.assert_not_awaited()
        err = capsys.readouterr().err
        assert "NAME is required" in err

    def test_non_tty_declines_wizard(self, monkeypatch, capsys):
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        fake_delegate = AsyncMock()
        with patch.object(cli, "_async_update_enclosure", fake_delegate):
            cli.main(["update", "enclosure"])
        fake_delegate.assert_not_awaited()
        out = capsys.readouterr().out
        assert "NAME is required" in out
