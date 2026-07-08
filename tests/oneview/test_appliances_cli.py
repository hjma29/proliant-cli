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

    def test_parser_appliance_alias_works(self):
        from proliant.oneview.cli import _build_parser, _cmd_appliances_list
        parser = _build_parser()
        args = parser.parse_args(["appliance", "list"])
        assert args.func is _cmd_appliances_list


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
