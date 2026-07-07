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

FAKE_MAC_MAP = [{
    "network": {"name": "VLAN-160", "vlan": 160, "type": "Tagged", "uri": "/rest/ethernet-networks/net1"},
    "mac": "22:00:A3:E0:00:1E",
    "learned_on": "downlink 6:1-2",
    "uplinks": [{
        "uplink_set": "ACI-MAP",
        "li_name": "LE01-LIG-VC100",
        "ports": [{"bay": "3", "port": "Q5:2", "neighbor_switch": "", "neighbor_port": "", "highlight": False}],
        "networks": [{"name": "VLAN-160", "vlan": 160, "native": False}],
        "network_sets": [],
    }],
    "servers": [{
        "profile": "ocp-single-node",
        "server_name": "Enclosure-01, bay 6",
        "bay": 6,
        "connections": [{
            "name": "map-connection-1",
            "port_id": "Mezz 3:1-a",
            "mac": "22:00:A3:E0:00:1E",
            "ic_name": "Enclosure-01, interconnect 3",
            "ic_bay": "3",
            "downlink": 6,
            "highlight": True,
        }],
    }],
}]

FAKE_MAC_LIST_UNRELATED = [{
    "mac": "00:00:0c:07:ac:a0",
    "ic_name": "Enclosure-01, interconnect 6",
    "port": "Q6:1",
    "profile": "",
    "connection": "",
    "last_updated": "",
}]

FAKE_MAC_LIST_RELATED = [{
    "mac": "22:00:A3:E0:00:1E",
    "ic_name": "IC3",
    "port": "p1",
    "profile": "profile1",
    "connection": "conn1",
    "last_updated": "",
}]


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

    def test_parser_servers_firmware_list_parses(self):
        from proliant.oneview.cli import _build_parser, _cmd_firmware_list
        parser = _build_parser()
        args = parser.parse_args(["servers", "firmware", "list", "--server", "Enc1, bay 1"])
        assert args.func is _cmd_firmware_list
        assert args.server == "Enc1, bay 1"

    def test_parser_firmware_bundles_list_parses(self):
        from proliant.oneview.cli import _build_parser, _cmd_firmware_bundles_list
        parser = _build_parser()
        args = parser.parse_args(["firmware", "bundles", "list"])
        assert args.func is _cmd_firmware_bundles_list

    def test_parser_firmware_repository_list_parses(self):
        from proliant.oneview.cli import _build_parser, _cmd_firmware_repository_list
        parser = _build_parser()
        args = parser.parse_args(["firmware", "repository", "list"])
        assert args.func is _cmd_firmware_repository_list

    def test_parser_firmware_compliance_list_parses(self):
        from proliant.oneview.cli import _build_parser, _cmd_firmware_compliance_list
        parser = _build_parser()
        args = parser.parse_args(["firmware", "compliance", "list"])
        assert args.func is _cmd_firmware_compliance_list


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


FAKE_BUNDLES = [
    {"uri": "/rest/firmware-drivers/old", "name": "SPP 2018.12", "version": "2018.12.05.00",
     "bundle_type": "ServicePack", "release_date": "2018-12-05T00:00:00Z",
     "size_bytes": 5_000_000_000, "state": "Created", "locations": {}, "repository_names": "Internal"},
]

FAKE_REPOSITORIES = [
    {"uri": "/rest/repositories/internal", "name": "Internal", "repository_type": "FirmwareInternalRepo",
     "total_gb": 62.0, "available_gb": 60.0, "state": "Normal", "url": "", "bundle_count": 1},
]

FAKE_COMPLIANCE = [
    {"hardware": "Enclosure-01, bay 1", "model": "Synergy 480 Gen10", "logical_resource": "aci-FM-host1",
     "bundle_name": "SPP 2023.05", "bundle_version": "SY-2023.05.01", "managed": True,
     "consistency_state": "Consistent"},
]


class TestOneviewJsonFirmwareBundles:
    def test_firmware_bundles_list_json_is_valid(self, capsys):
        from proliant.oneview import cli

        with patch("proliant.oneview.cli._load_client", return_value=_make_mock_client()), \
             patch("proliant.oneview.firmware.list_bundles",
                   new_callable=AsyncMock, return_value=FAKE_BUNDLES):
            cli.main(["--json", "firmware", "bundles", "list"])

        captured = capsys.readouterr()
        result = json.loads(captured.out)
        assert result == FAKE_BUNDLES


class TestOneviewJsonFirmwareRepository:
    def test_firmware_repository_list_json_is_valid(self, capsys):
        from proliant.oneview import cli

        with patch("proliant.oneview.cli._load_client", return_value=_make_mock_client()), \
             patch("proliant.oneview.firmware.list_repositories",
                   new_callable=AsyncMock, return_value=FAKE_REPOSITORIES):
            cli.main(["--json", "firmware", "repository", "list"])

        captured = capsys.readouterr()
        result = json.loads(captured.out)
        assert result == FAKE_REPOSITORIES


class TestOneviewJsonFirmwareCompliance:
    def test_firmware_compliance_list_json_is_valid(self, capsys):
        from proliant.oneview import cli

        with patch("proliant.oneview.cli._load_client", return_value=_make_mock_client()), \
             patch("proliant.oneview.firmware.list_compliance",
                   new_callable=AsyncMock, return_value=FAKE_COMPLIANCE):
            cli.main(["--json", "firmware", "compliance", "list"])

        captured = capsys.readouterr()
        result = json.loads(captured.out)
        assert result == FAKE_COMPLIANCE

    def test_firmware_compliance_list_table_shows_status(self, capsys):
        from proliant.oneview import cli

        with patch("proliant.oneview.cli._load_client", return_value=_make_mock_client()), \
             patch("proliant.oneview.firmware.list_compliance",
                   new_callable=AsyncMock, return_value=FAKE_COMPLIANCE):
            cli.main(["firmware", "compliance", "list"])

        captured = capsys.readouterr()
        assert "aci-FM-host1" in captured.out
        assert "Consistent" in captured.out


class TestOneviewMacDescribe:
    def test_mac_describe_uses_diagram_output_by_default(self, capsys):
        from proliant.oneview import cli

        with patch("proliant.oneview.cli._load_client", return_value=_make_mock_client()), \
             patch("proliant.oneview.topology.trace_mac",
                   new_callable=AsyncMock, return_value=FAKE_MAC_MAP):
            cli.main(["mac", "describe", "22:00:A3:E0:00:1E"])

        captured = capsys.readouterr()
        assert "uplinkset: ACI-MAP" in captured.out
        assert "ocp-single-node" in captured.out
        assert "┌" in captured.out
        assert "├── ▲ Upstream uplinks" not in captured.out
        assert "└── ▼ Downlink servers" not in captured.out


class TestOneviewMacList:
    def test_mac_list_hides_profile_columns_when_unrelated(self, capsys):
        from proliant.oneview import cli

        with patch("proliant.oneview.cli._load_client", return_value=_make_mock_client()), \
             patch("proliant.oneview.interconnects.get_mac_table",
                   new_callable=AsyncMock, return_value=FAKE_MAC_LIST_UNRELATED):
            cli.main(["mac", "list", "--address", "00:00:0c:07:ac:a0"])

        captured = capsys.readouterr()
        assert "MAC Address" in captured.out
        assert "Q6:1" in captured.out
        assert "Server Profile" not in captured.out
        assert "Connection" not in captured.out

    def test_mac_list_shows_profile_columns_when_related(self, capsys):
        from proliant.oneview import cli

        with patch("proliant.oneview.cli._load_client", return_value=_make_mock_client()), \
             patch("proliant.oneview.interconnects.get_mac_table",
                   new_callable=AsyncMock, return_value=FAKE_MAC_LIST_RELATED):
            cli.main(["mac", "list", "--address", "22:00:A3:E0:00:1E"])

        captured = capsys.readouterr()
        assert "Server Profile" in captured.out
        assert "Connection" in captured.out
        assert "profile1" in captured.out
        assert "conn1" in captured.out

    def test_mac_list_accepts_json_after_subcommand_args(self, capsys):
        from proliant.oneview import cli

        with patch("proliant.oneview.cli._load_client", return_value=_make_mock_client()), \
             patch("proliant.oneview.interconnects.get_mac_table",
                   new_callable=AsyncMock, return_value=FAKE_MAC_LIST_RELATED):
            cli.main(["mac", "list", "--address", "22:00:A3:E0:00:1E", "--json"])

        captured = capsys.readouterr()
        result = json.loads(captured.out)
        assert result == FAKE_MAC_LIST_RELATED
