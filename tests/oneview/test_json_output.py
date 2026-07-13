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
        args = parser.parse_args(["firmware", "bundles"])
        assert args.func is _cmd_firmware_bundles_list

    def test_parser_mac_describe_accepts_network_filters(self):
        from proliant.oneview.cli import _build_parser, _cmd_mac_describe
        parser = _build_parser()
        args = parser.parse_args([
            "mac", "describe", "00:11:22:33:44:55", "--vlan", "160", "--network-name", "VLAN-160",
        ])
        assert args.func is _cmd_mac_describe
        assert args.vlan == 160
        assert args.network_name == "VLAN-160"

    def test_parser_firmware_repository_list_parses(self):
        from proliant.oneview.cli import _build_parser, _cmd_firmware_repository_list
        parser = _build_parser()
        args = parser.parse_args(["firmware", "repository"])
        assert args.func is _cmd_firmware_repository_list

    def test_parser_compliance_list_parses(self):
        from proliant.oneview.cli import _build_parser, _cmd_compliance_list
        parser = _build_parser()
        args = parser.parse_args(["compliance", "list"])
        assert args.func is _cmd_compliance_list

    def test_parser_compliance_describe_parses(self):
        from proliant.oneview.cli import _build_parser, _cmd_compliance_describe
        parser = _build_parser()
        args = parser.parse_args(["compliance", "describe", "aci-FM-host1", "--baseline", "SY-2026.01.02"])
        assert args.func is _cmd_compliance_describe
        assert args.name == "aci-FM-host1"
        assert args.baseline == "SY-2026.01.02"


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
    {
        "kind": "server-profile",
        "resource_name": "aci-FM-host1",
        "hardware": "Enclosure-01, bay 1",
        "model": "Synergy 480 Gen10",
        "current_baseline": {"uri": "/rest/firmware-drivers/fw1", "name": "SPP 2023.05", "version": "SY-2023.05.01", "label": "SPP 2023.05 SY-2023.05.01"},
        "current_baseline_label": "SPP 2023.05 SY-2023.05.01",
        "target_baseline": {"uri": "/rest/firmware-drivers/fw2", "name": "SPP 2026.01", "version": "SY-2026.01.02", "label": "SPP 2026.01 SY-2026.01.02"},
        "target_baseline_label": "SPP 2026.01 SY-2026.01.02",
        "update_required": True,
        "components_needing_update": 3,
        "components_total": 56,
        "components": [
            {"name": "System ROM", "location": "", "current_version": "U46 v2.52", "target_version": "U46 v2.70", "update_required": True},
            {"name": "iLO", "location": "", "current_version": "3.00", "target_version": "3.00", "update_required": False},
        ],
    },
    {
        "kind": "frame-link-module",
        "resource_name": "Enclosure-01, frame link module 1",
        "hardware": "",
        "model": "HPE Synergy Frame Link Module",
        "current_baseline": {"uri": "/rest/firmware-drivers/fw1", "name": "SPP 2023.05", "version": "SY-2023.05.01", "label": "SPP 2023.05 SY-2023.05.01"},
        "current_baseline_label": "SPP 2023.05 SY-2023.05.01",
        "target_baseline": {"uri": "/rest/firmware-drivers/fw2", "name": "SPP 2026.01", "version": "SY-2026.01.02", "label": "SPP 2026.01 SY-2026.01.02"},
        "target_baseline_label": "SPP 2026.01 SY-2026.01.02",
        "update_required": True,
        "components_needing_update": 1,
        "components_total": 1,
        "components": [
            {"name": "HPE Synergy Frame Link Module", "location": "Bay 1", "current_version": "1.0.0", "target_version": "1.2.3", "update_required": True},
        ],
    },
    {
        "kind": "interconnect",
        "resource_name": "Enclosure-01, interconnect 3",
        "hardware": "",
        "model": "HPE Virtual Connect SE 100Gb F32 Module for Synergy",
        "current_baseline": {"uri": "/rest/firmware-drivers/fw1", "name": "SPP 2023.05", "version": "SY-2023.05.01", "label": "SPP 2023.05 SY-2023.05.01"},
        "current_baseline_label": "SPP 2023.05 SY-2023.05.01",
        "target_baseline": {"uri": "/rest/firmware-drivers/fw2", "name": "SPP 2026.01", "version": "SY-2026.01.02", "label": "SPP 2026.01 SY-2026.01.02"},
        "target_baseline_label": "SPP 2026.01 SY-2026.01.02",
        "update_required": True,
        "components_needing_update": 1,
        "components_total": 1,
        "components": [
            {"name": "HPE Virtual Connect SE 100Gb F32 Module for Synergy", "location": "", "current_version": "2.6.0.1001", "target_version": "2.9.1.1001", "update_required": True},
        ],
    },
]


class TestOneviewJsonFirmwareBundles:
    def test_firmware_bundles_list_json_is_valid(self, capsys):
        from proliant.oneview import cli

        with patch("proliant.oneview.cli._load_client", return_value=_make_mock_client()), \
             patch("proliant.oneview.firmware.list_bundles",
                   new_callable=AsyncMock, return_value=FAKE_BUNDLES):
            cli.main(["--json", "firmware", "bundles"])

        captured = capsys.readouterr()
        result = json.loads(captured.out)
        assert result == FAKE_BUNDLES


class TestOneviewJsonFirmwareRepository:
    def test_firmware_repository_list_json_is_valid(self, capsys):
        from proliant.oneview import cli

        with patch("proliant.oneview.cli._load_client", return_value=_make_mock_client()), \
             patch("proliant.oneview.firmware.list_repositories",
                   new_callable=AsyncMock, return_value=FAKE_REPOSITORIES):
            cli.main(["--json", "firmware", "repository"])

        captured = capsys.readouterr()
        result = json.loads(captured.out)
        assert result == FAKE_REPOSITORIES


class TestOneviewJsonFirmwareCompliance:
    def test_firmware_compliance_list_json_is_valid(self, capsys):
        from proliant.oneview import cli

        with patch("proliant.oneview.cli._load_client", return_value=_make_mock_client()), \
             patch("proliant.oneview.firmware.list_compliance",
                   new_callable=AsyncMock, return_value=FAKE_COMPLIANCE):
            cli.main(["--json", "compliance", "list"])

        captured = capsys.readouterr()
        result = json.loads(captured.out)
        assert result == FAKE_COMPLIANCE

    def test_firmware_compliance_list_table_shows_status(self, capsys):
        from proliant.oneview import cli
        from proliant.oneview.cli import _short_baseline_label

        with patch("proliant.oneview.cli._load_client", return_value=_make_mock_client()), \
             patch("proliant.oneview.firmware.list_compliance",
                   new_callable=AsyncMock, return_value=FAKE_COMPLIANCE):
            cli.main(["compliance", "list"])

        captured = capsys.readouterr()
        assert "aci-FM-host1" in captured.out
        assert "BIOS:" in captured.out
        assert "Enclosure-01, FLM 1" in captured.out
        assert "Enclosure-01, IC 3" in captured.out
        assert _short_baseline_label("SPP SY-2023.05.01 SY-2023.05.01") == "2023.05.01"
        assert "SY-2023.05.01" not in captured.out
        assert "Type" not in captured.out
        assert "Components" not in captured.out
        assert "System ROM" not in captured.out

    def test_known_firmware_version_unifies_embedded_date_formats(self):
        from proliant.oneview.cli import _known_firmware_version

        assert _known_firmware_version("I42 v2.78 (03/16/2023)") == "I42 v2.78 (2023.03.16)"
        assert _known_firmware_version("2.81 Mar 07 2023") == "2.81 2023.03.07"
        assert _known_firmware_version("v3.60 (08/06/2025)") == "v3.60 (2025.08.06)"
        assert _known_firmware_version("5.03.00") == "5.03.00"
        assert _known_firmware_version("unknown") == ""

    def test_firmware_version_style_greens_only_a_newer_target(self):
        from proliant.oneview.cli import _firmware_version_style

        # Target newer than current -> green only for the target column.
        assert _firmware_version_style("I42 v2.78 (2023.03.16)", "v3.60 (2025.08.06)", is_target=True) == "green"
        assert _firmware_version_style("I42 v2.78 (2023.03.16)", "v3.60 (2025.08.06)", is_target=False) == "white"
        assert _firmware_version_style("2.81 2023.03.07", "3.17", is_target=True) == "green"

        # Equal versions (already compliant) -> white, never green.
        assert _firmware_version_style("5.03.00", "5.03.00", is_target=True) == "white"
        assert _firmware_version_style("5.03.00", "5.03.00", is_target=False) == "white"

        # Target older/equal or unparsable -> white.
        assert _firmware_version_style("3.60", "2.78", is_target=True) == "white"
        assert _firmware_version_style("unknown", "unknown", is_target=True) == "white"

    def test_single_component_version_summary_wraps_nothing_but_colors_newer_target(self):
        from proliant.oneview.cli import _single_component_version_summary

        components = [{"current_version": "2.8.0.1001", "target_version": "2.9.2.1001", "update_required": True}]
        assert "[white]2.8.0.1001[/]" == _single_component_version_summary(components, target=False)
        assert "[green]2.9.2.1001[/]" == _single_component_version_summary(components, target=True)

    def test_server_version_summary_prefers_real_bios_and_ilo_firmware(self):
        from proliant.oneview.cli import _firmware_version_summary

        row = {
            "kind": "server-profile",
            "components": [
                {
                    "name": "Redundant System ROM",
                    "current_version": "I42 v2.68 (07/14/2022)",
                    "target_version": "unknown",
                    "update_required": False,
                },
                {
                    "name": "ilo-driver",
                    "current_version": "700.10.7.5.2-1OEM.700.1.0.15843807",
                    "target_version": "unknown",
                    "update_required": False,
                },
                {
                    "name": "System ROM",
                    "current_version": "I42 v2.78 (03/16/2023)",
                    "target_version": "v3.60 (08/06/2025)",
                    "update_required": True,
                },
                {
                    "name": "iLO 5",
                    "current_version": "2.81 Mar 07 2023",
                    "target_version": "3.17",
                    "update_required": True,
                },
            ],
        }

        current = _firmware_version_summary(row, target=False)
        target = _firmware_version_summary(row, target=True)

        assert "BIOS:I42 v2.78 (2023.03.16)" in current
        assert "iLO:(2.81 2023.03.07)" in current
        assert "BIOS:v3.60 (2025.08.06)" in target
        assert "iLO:(3.17)" in target
        assert "[white]BIOS:I42 v2.78 (2023.03.16)[/]" in current
        assert "[white]iLO:(2.81 2023.03.07)[/]" in current
        assert "[green]BIOS:v3.60 (2025.08.06)[/]" in target
        assert "[green]iLO:(3.17)[/]" in target
        assert "I42 v2.68" not in current
        assert "700.10" not in current
        assert "unknown" not in target

    def test_compliance_describe_table_shows_component_details(self, capsys):
        from proliant.oneview import cli

        with patch("proliant.oneview.cli._load_client", return_value=_make_mock_client()), \
             patch("proliant.oneview.firmware.list_compliance",
                   new_callable=AsyncMock, return_value=FAKE_COMPLIANCE):
            cli.main(["compliance", "describe", "aci-FM-host1"])

        captured = capsys.readouterr()
        assert "aci-FM-host1" in captured.out
        assert "Component Firmware" in captured.out
        assert "System ROM" in captured.out
        assert "U46 v2.70" in captured.out
        assert "iLO" in captured.out
        assert "3.00" in captured.out

    def test_compliance_describe_json_returns_one_resource(self, capsys):
        from proliant.oneview import cli

        with patch("proliant.oneview.cli._load_client", return_value=_make_mock_client()), \
             patch("proliant.oneview.firmware.list_compliance",
                   new_callable=AsyncMock, return_value=FAKE_COMPLIANCE):
            cli.main(["--json", "compliance", "describe", "aci-FM-host1"])

        captured = capsys.readouterr()
        result = json.loads(captured.out)
        assert result["resource_name"] == "aci-FM-host1"
        assert len(result["components"]) == 2


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

    def test_mac_describe_filters_by_vlan_in_json_output(self, capsys):
        from proliant.oneview import cli

        mac_maps = [
            {
                **FAKE_MAC_MAP[0],
                "network": {**FAKE_MAC_MAP[0]["network"], "name": "VLAN-160", "vlan": 160},
            },
            {
                **FAKE_MAC_MAP[0],
                "network": {**FAKE_MAC_MAP[0]["network"], "name": "VLAN-170", "vlan": 170},
            },
        ]
        with patch("proliant.oneview.cli._load_client", return_value=_make_mock_client()), \
             patch("proliant.oneview.topology.trace_mac",
                   new_callable=AsyncMock, return_value=mac_maps):
            cli.main(["--json", "mac", "describe", "22:00:A3:E0:00:1E", "--vlan", "170"])

        captured = capsys.readouterr()
        result = json.loads(captured.out)
        assert len(result) == 1
        assert result[0]["network"]["vlan"] == 170
        assert result[0]["network"]["name"] == "VLAN-170"

    def test_mac_describe_filters_by_network_name_in_json_output(self, capsys):
        from proliant.oneview import cli

        mac_maps = [
            {
                **FAKE_MAC_MAP[0],
                "network": {**FAKE_MAC_MAP[0]["network"], "name": "VLAN-160", "vlan": 160},
            },
            {
                **FAKE_MAC_MAP[0],
                "network": {**FAKE_MAC_MAP[0]["network"], "name": "ACI-Tunnel-Net", "vlan": 4094},
            },
        ]
        with patch("proliant.oneview.cli._load_client", return_value=_make_mock_client()), \
             patch("proliant.oneview.topology.trace_mac",
                   new_callable=AsyncMock, return_value=mac_maps):
            cli.main(["--json", "mac", "describe", "22:00:A3:E0:00:1E", "--network-name", "tunnel"])

        captured = capsys.readouterr()
        result = json.loads(captured.out)
        assert len(result) == 1
        assert result[0]["network"]["name"] == "ACI-Tunnel-Net"


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
