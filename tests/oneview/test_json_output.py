"""
CLI-level JSON output tests for proliant oneview --json.
"""

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from proliant.common.display import OutputMode, set_output_mode


@pytest.fixture(autouse=True)
def reset_output_mode():
    set_output_mode(OutputMode.TABLE)
    yield
    set_output_mode(OutputMode.TABLE)


FAKE_SERVERS = [
    {"name": "Enc1, bay 1", "model": "ProLiant BL460c Gen10", "serial": "XXXXX1",
     "ilo_model": "iLO 5", "ilo_version": "2.78", "ilo_ip": "192.168.1.10",
     "power": "On", "state": "ProfileApplied", "profile": "ov-profile-1"},
]

FAKE_NETWORKS = [
    {"name": "prod-vlan100", "vlan": 100, "type": "Ethernet", "purpose": "General",
     "status": "OK", "state": "Active", "smart_link": True},
]


def _make_mock_client():
    """Return an async context manager yielding a mock OneView client."""
    mock_client = MagicMock()
    mock_client.api_version = 3000

    @asynccontextmanager
    async def _ctx():
        yield mock_client

    return _ctx()


class TestOneviewParserJson:
    def test_parser_accepts_json_flag(self):
        from proliant.oneview.cli import _build_parser
        parser = _build_parser()
        args = parser.parse_args(["--json", "servers", "list"])
        assert args.json_output is True

    def test_parser_json_default_false(self):
        from proliant.oneview.cli import _build_parser
        parser = _build_parser()
        args = parser.parse_args(["servers", "list"])
        assert args.json_output is False

    def test_parser_json_on_list_networks(self):
        from proliant.oneview.cli import _build_parser
        parser = _build_parser()
        args = parser.parse_args(["--json", "networks", "list"])
        assert args.json_output is True


class TestOneviewJsonServers:
    def test_list_servers_json_is_valid(self, capsys):
        from proliant.oneview import cli

        with patch("proliant.oneview.cli._load_client", return_value=_make_mock_client()), \
             patch("proliant.oneview.servers.list_servers_with_profiles",
                   new_callable=AsyncMock, return_value=FAKE_SERVERS):
            cli.main(["--json", "servers", "list"])

        captured = capsys.readouterr()
        result = json.loads(captured.out)
        assert isinstance(result, list)
        assert result[0]["name"] == "Enc1, bay 1"

    def test_list_servers_json_no_rich_markup(self, capsys):
        from proliant.oneview import cli

        with patch("proliant.oneview.cli._load_client", return_value=_make_mock_client()), \
             patch("proliant.oneview.servers.list_servers_with_profiles",
                   new_callable=AsyncMock, return_value=FAKE_SERVERS):
            cli.main(["--json", "servers", "list"])

        captured = capsys.readouterr()
        assert "[bold" not in captured.out
        assert "[green" not in captured.out
        assert "\x1b[" not in captured.out


class TestOneviewJsonNetworks:
    def test_list_networks_json_is_valid(self, capsys):
        from proliant.oneview import cli

        with patch("proliant.oneview.cli._load_client", return_value=_make_mock_client()), \
             patch("proliant.oneview.network.list_networks",
                   new_callable=AsyncMock, return_value=FAKE_NETWORKS):
            cli.main(["--json", "networks", "list"])

        captured = capsys.readouterr()
        result = json.loads(captured.out)
        assert isinstance(result, list)
        assert result[0]["name"] == "prod-vlan100"
        assert result[0]["vlan"] == 100
