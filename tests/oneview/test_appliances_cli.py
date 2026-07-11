"""CLI-level tests for 'proliant oneview appliances list/use'."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from proliant.common.display import OutputMode, set_output_mode


@pytest.fixture(autouse=True)
def reset_output_mode():
    set_output_mode(OutputMode.TABLE)
    yield
    set_output_mode(OutputMode.TABLE)


_ONE = [{"name": "oneview", "host": "10.0.0.1", "url": "https://10.0.0.1", "username": "Administrator", "password": "pw"}]
_TWO = [
    {"name": "oneview", "host": "10.0.0.1", "url": "https://10.0.0.1", "username": "Administrator", "password": "pw"},
    {"name": "datacenter-b", "host": "10.0.0.2", "url": "https://10.0.0.2", "username": "Administrator", "password": "pw2"},
]


class TestParserWiring:
    def test_parser_appliances_list_parses(self):
        from proliant.oneview.cli import _build_parser, _cmd_appliances_list
        parser = _build_parser()
        args = parser.parse_args(["appliances", "list"])
        assert args.func is _cmd_appliances_list

    def test_parser_appliances_use_parses(self):
        from proliant.oneview.cli import _build_parser, _cmd_appliances_use
        parser = _build_parser()
        args = parser.parse_args(["appliances", "use", "datacenter-b"])
        assert args.func is _cmd_appliances_use
        assert args.name == "datacenter-b"

    def test_parser_appliances_describe_parses(self):
        from proliant.oneview.cli import _build_parser, _cmd_appliances_describe
        parser = _build_parser()
        args = parser.parse_args(["appliances", "describe", "datacenter-b"])
        assert args.func is _cmd_appliances_describe
        assert args.name == "datacenter-b"

    def test_parser_appliances_describe_name_optional(self):
        from proliant.oneview.cli import _build_parser, _cmd_appliances_describe
        parser = _build_parser()
        args = parser.parse_args(["appliances", "describe"])
        assert args.func is _cmd_appliances_describe
        assert args.name is None

    def test_parser_update_enclosure_parses(self):
        from proliant.oneview.cli import _build_parser, _cmd_update_enclosure
        parser = _build_parser()
        args = parser.parse_args([
            "update", "enclosure", "LE01",
            "--baseline", "SY-2026.01.02",
            "--scope", "shared-infra-and-profiles",
            "--install-type", "firmware-and-drivers",
            "--activation-mode", "parallel",
            "--force", "--execute", "--yes",
        ])
        assert args.func is _cmd_update_enclosure
        assert args.name == "LE01"
        assert args.baseline == "SY-2026.01.02"
        assert args.scope == "shared-infra-and-profiles"
        assert args.install_type == "firmware-and-drivers"
        assert args.activation_mode == "parallel"
        assert args.force and args.execute and args.yes

    def test_parser_update_enclosure_defaults(self):
        from proliant.oneview.cli import _build_parser, _cmd_update_enclosure
        parser = _build_parser()
        args = parser.parse_args(["update", "enclosure", "LE01"])
        assert args.func is _cmd_update_enclosure
        assert args.baseline is None
        assert args.scope == "shared-infra"
        assert args.activation_mode == "orchestrated"
        assert args.execute is False  # plan by default

    def test_parser_update_enclosure_name_optional(self):
        """Omitting NAME must parse cleanly (it launches the interactive wizard) --
        it must not be treated as a required positional error."""
        from proliant.oneview.cli import _build_parser, _cmd_update_enclosure
        parser = _build_parser()
        args = parser.parse_args(["update", "enclosure"])
        assert args.func is _cmd_update_enclosure
        assert args.name is None
        # the rest of the flags still get their normal defaults
        assert args.scope == "shared-infra"
        assert args.activation_mode == "orchestrated"
        assert args.force is False
        assert args.execute is False

    def test_parser_update_appliance_run_parses(self):
        from proliant.oneview.cli import _build_parser, _cmd_upgrade_run
        parser = _build_parser()
        args = parser.parse_args(["update", "appliance", "run", "--image", "update.bin", "--execute"])
        assert args.func is _cmd_upgrade_run
        assert args.image == "update.bin"
        assert args.execute is True

    def test_parser_update_appliance_readiness_parses(self):
        from proliant.oneview.cli import _build_parser, _cmd_upgrade_readiness
        parser = _build_parser()
        args = parser.parse_args(["update", "appliance", "readiness"])
        assert args.func is _cmd_upgrade_readiness

    def test_parser_release_parses(self):
        from proliant.oneview.cli import _build_parser, _cmd_release
        parser = _build_parser()
        args = parser.parse_args(["release"])
        assert args.func is _cmd_release


class TestIsAffirmative:
    """`_is_affirmative()` backs the validation-warning "Proceed anyway?"
    prompt. Live testing found two bugs here: Rich's console.input() was
    silently swallowing the "[y/N]" hint as (invalid) markup -- leaving the
    operator with no visible cue of what to type -- and the panel's own
    subtitle text says "...click OK to proceed", so a literal "ok" answer
    (typed against that wording) was being declined instead of accepted."""

    @pytest.mark.parametrize("answer", ["y", "Y", "yes", "YES", "ok", "OK", "okay", "Okay", "  y  "])
    def test_accepts_yes_variants(self, answer):
        from proliant.oneview.cli import _is_affirmative
        assert _is_affirmative(answer) is True

    @pytest.mark.parametrize("answer", ["n", "no", "", "  ", "nope", "cancel"])
    def test_rejects_everything_else(self, answer):
        from proliant.oneview.cli import _is_affirmative
        assert _is_affirmative(answer) is False


class _FakeVersionClient:
    def __init__(self, version):
        self._version = version

    async def get(self, uri):
        return {"softwareVersion": self._version}


class TestReleaseMatrix:
    def test_release_marks_current_appliance(self, capsys):
        from proliant.oneview import cli

        with patch.object(cli, "_load_client",
                           lambda name=None: _FakeCM(_FakeVersionClient("10.00.00-0507518"))):
            cli.main(["release"])

        out = capsys.readouterr().out
        assert "this appliance" in out
        assert "11.3" in out and "2026.04.01" in out

    def test_release_json_output(self, capsys):
        from proliant.oneview import cli

        with patch.object(cli, "_load_client",
                           lambda name=None: _FakeCM(_FakeVersionClient("10.00.00-0507518"))):
            cli.main(["--json", "release"])

        result = json.loads(capsys.readouterr().out)
        assert result["current_track"] == "10.0"
        assert any(r["track"] == "11.3" and r["recommended"] == "2026.04.01"
                   for r in result["releases"])

    def test_release_works_without_a_configured_appliance(self, capsys):
        from proliant.oneview import cli

        def _raise(name=None):
            raise RuntimeError("no OneView appliance configured")

        with patch.object(cli, "_load_client", _raise):
            cli.main(["release"])

        out = capsys.readouterr().out
        assert "11.3" in out
        assert "this appliance" not in out


class TestAppliancesList:
    def test_list_marks_active_appliance(self, capsys):
        from proliant.oneview import cli

        with patch("proliant.oneview.config.list_oneview_appliances", return_value=_TWO), \
             patch("proliant.oneview.config.get_active_appliance_name", return_value="datacenter-b"):
            cli.main(["appliances", "list"])

        out = capsys.readouterr().out
        assert "datacenter-b" in out
        assert "oneview" in out
        assert "* = active appliance" in out

    def test_list_json_output(self, capsys):
        from proliant.oneview import cli

        with patch("proliant.oneview.config.list_oneview_appliances", return_value=_ONE):
            cli.main(["--json", "appliances", "list"])

        result = json.loads(capsys.readouterr().out)
        assert result == _ONE

    def test_list_empty_shows_hint(self, capsys):
        from proliant.oneview import cli

        with patch("proliant.oneview.config.list_oneview_appliances", return_value=[]):
            cli.main(["appliances", "list"])

        out = capsys.readouterr().out
        assert "No OneView appliances configured" in out

    def test_list_single_appliance_omits_switch_hint(self, capsys):
        """No point nudging users to switch when there's nothing to switch to."""
        from proliant.oneview import cli

        with patch("proliant.oneview.config.list_oneview_appliances", return_value=_ONE), \
             patch("proliant.oneview.config.get_active_appliance_name", return_value="oneview"):
            cli.main(["appliances", "list"])

        out = capsys.readouterr().out
        assert "active appliance" not in out


class TestAppliancesUse:
    def test_use_switches_and_reports_resolved_name(self, capsys):
        from proliant.oneview import cli

        with patch("proliant.oneview.config.set_active_appliance", return_value="Datacenter-B") as mock_set:
            cli.main(["appliances", "use", "datacenter-b"])

        mock_set.assert_called_once_with("datacenter-b")
        out = capsys.readouterr().out
        assert "Switched to OneView appliance" in out
        assert "Datacenter-B" in out

    def test_use_unknown_name_reports_error_and_exits(self, capsys):
        from proliant.oneview import cli

        with patch("proliant.oneview.config.set_active_appliance",
                   side_effect=ValueError("OneView appliance 'ghost' not found. Known appliances: oneview")):
            with pytest.raises(SystemExit) as excinfo:
                cli.main(["appliances", "use", "ghost"])

        assert excinfo.value.code == 1
        out = capsys.readouterr().out
        assert "not found" in out


class _FakeCM:
    """Async context manager yielding a dummy client for _load_client patches."""

    def __init__(self, client):
        self._client = client

    async def __aenter__(self):
        return self._client

    async def __aexit__(self, *exc):
        return False


def _sample_info():
    from proliant.oneview.appliance_info import build_appliance_info
    from tests.oneview.test_appliance_info import HA_NODES, STATUS, TASKS, VERSION
    return build_appliance_info(HA_NODES, STATUS, VERSION, TASKS)


class TestAppliancesDescribe:
    def test_describe_renders_general_page(self, capsys):
        from proliant.oneview import cli

        async def _fake_fetch(_client):
            return _sample_info()

        with patch.object(cli, "_load_client", lambda name=None: _FakeCM(object())), \
             patch("proliant.oneview.appliance_info.fetch_appliance_info", _fake_fetch):
            cli.main(["appliances", "describe"])

        out = capsys.readouterr().out
        assert "General" in out
        assert "Synergy Composer2" in out
        assert "64 GB" in out
        assert "10.00.00-0507518" in out
        assert "Apr 21, 2025" in out
        assert "Connected" in out
        assert "Composable Infrastructure Appliances" in out
        assert "appliance bay 2" in out

    def test_describe_json_output(self, capsys):
        from proliant.oneview import cli

        async def _fake_fetch(_client):
            return _sample_info()

        with patch.object(cli, "_load_client", lambda name=None: _FakeCM(object())), \
             patch("proliant.oneview.appliance_info.fetch_appliance_info", _fake_fetch):
            cli.main(["--json", "appliances", "describe"])

        result = json.loads(capsys.readouterr().out)
        assert result["model"] == "Synergy Composer2"
        assert result["connected"] is True
        assert result["firmware"]["version"] == "10.00.00-0507518"

    def test_describe_passes_name_to_load_client(self, capsys):
        from proliant.oneview import cli

        seen = {}

        def _fake_load(name=None):
            seen["name"] = name
            return _FakeCM(object())

        async def _fake_fetch(_client):
            return _sample_info()

        with patch.object(cli, "_load_client", _fake_load), \
             patch("proliant.oneview.appliance_info.fetch_appliance_info", _fake_fetch):
            cli.main(["appliances", "describe", "datacenter-b"])

        assert seen["name"] == "datacenter-b"
