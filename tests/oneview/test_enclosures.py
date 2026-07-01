"""Tests for OneView enclosure layout aggregation."""

from __future__ import annotations

import pytest

from proliant.oneview.cli import _appliance_bay_lines, _bay_rows, _front_bay_columns, _front_bay_height, _front_frame_ratios, _power_supply_lines, _rear_bay_columns, _rear_frame_ratios, _server_bay_lines
from proliant.oneview.enclosures import describe_enclosure


ENCL_URI = "/rest/enclosures/encl1"
LE_URI = "/rest/logical-enclosures/le1"
EG_URI = "/rest/enclosure-groups/eg1"
LI_URI = "/rest/logical-interconnects/li1"
PROFILE_URI = "/rest/server-profiles/profile1"
SERVER_URI = "/rest/server-hardware/server1"
IC_URI = "/rest/interconnects/ic3"


class FakeClient:
    def __init__(self, collections: dict[str, list[dict]]):
        self._collections = collections

    async def get_all(self, uri: str) -> list[dict]:
        return self._collections.get(uri, [])


def _collections() -> dict[str, list[dict]]:
    return {
        "/rest/enclosures": [
            {
                "uri": ENCL_URI,
                "name": "Enclosure-01",
                "enclosureModel": "Synergy 12000 Frame",
                "serialNumber": "MXQ71305CL",
                "state": "Configured",
                "status": "OK",
                "deviceBays": [
                    {
                        "bayNumber": 1,
                        "devicePresence": "Present",
                        "deviceUri": SERVER_URI,
                        "profileUri": PROFILE_URI,
                        "powerAllocationWatts": 308,
                    },
                    {
                        "bayNumber": 2,
                        "devicePresence": "Absent",
                        "deviceUri": None,
                        "profileUri": None,
                        "powerAllocationWatts": None,
                    },
                ],
                "applianceBays": [
                    {
                        "bayNumber": 1,
                        "model": "Synergy Composer2",
                        "serialNumber": "CNX23304MG",
                        "partNumber": "872957-B21",
                        "sparePartNumber": "879540-001",
                        "devicePresence": "Present",
                        "status": "OK",
                        "poweredOn": True,
                    },
                    {
                        "bayNumber": 2,
                        "model": "Synergy Composer2",
                        "serialNumber": "CNX23304PC",
                        "partNumber": "872957-B21",
                        "sparePartNumber": "879540-001",
                        "devicePresence": "Present",
                        "status": "OK",
                        "poweredOn": True,
                    },
                ],
                "powerSupplyBays": [
                    {
                        "bayNumber": 1,
                        "label": "1",
                        "devicePresence": "Present",
                        "status": "OK",
                        "model": "2650W AC Titanium Hot Plug Power Supply",
                        "serialNumber": "9C4612003S",
                        "partNumber": "798095-B21",
                        "sparePartNumber": "813829-001",
                        "outputCapacityWatts": 2650,
                    },
                    {
                        "bayNumber": 2,
                        "label": "2",
                        "devicePresence": "Present",
                        "status": "Warning",
                        "model": "2650W AC Titanium Hot Plug Power Supply",
                        "serialNumber": "9C4612002Q",
                        "partNumber": "798095-B21",
                        "sparePartNumber": "813829-001",
                        "outputCapacityWatts": 2650,
                    },
                ],
                "fanBays": [
                    {
                        "bayNumber": 1,
                        "devicePresence": "Present",
                        "deviceRequired": True,
                        "status": "OK",
                        "model": "Synergy Fan Module",
                        "serialNumber": "7C66162631",
                        "partNumber": "809097-001",
                        "sparePartNumber": "807967-001",
                    },
                ],
                "managerBays": [
                    {
                        "bayNumber": 1,
                        "role": "Standby",
                        "fwVersion": "4.01.00",
                        "devicePresence": "Present",
                        "status": "OK",
                        "model": "Synergy Frame Link Module",
                        "serialNumber": "CN7643V08S",
                        "partNumber": "802341-B21",
                        "sparePartNumber": "807963-001",
                        "ipAddress": "fe80::1602:ecff:fe44:bd50",
                    },
                ],
                "interconnectBays": [
                    {
                        "bayNumber": 3,
                        "interconnectUri": IC_URI,
                        "interconnectModel": "Virtual Connect SE 100Gb F32 Module for Synergy",
                        "serialNumber": "IC123",
                        "partNumber": "867796-B21",
                        "powerAllocationWatts": 270,
                    },
                ],
            },
        ],
        "/rest/server-hardware": [
            {
                "uri": SERVER_URI,
                "name": "Enclosure-01, bay 1",
                "serverGroupUri": EG_URI,
                "locationUri": ENCL_URI,
                "position": 1,
                "model": "SY 480 Gen10",
                "serverName": "aci-vc-tunnel-host1.hst.enablement.local",
                "serialNumber": "SCR096BA54F2",
                "partNumber": "P12345-B21",
                "serverProfileUri": PROFILE_URI,
                "powerState": "On",
                "state": "ProfileApplied",
                "status": "Warning",
                "mpModel": "iLO5",
                "mpFirmwareVersion": "2.81",
                "romVersion": "I42 v2.78 (03/16/2023)",
                "mpIpAddresses": [{"address": "192.0.2.10"}],
            },
        ],
        "/rest/server-profiles": [
            {"uri": PROFILE_URI, "name": "aci-vc-tunnel-host1", "status": "Critical", "state": "Normal"},
        ],
        "/rest/interconnects": [
            {
                "uri": IC_URI,
                "name": "Enclosure-01, interconnect 3",
                "model": "Virtual Connect SE 100Gb F32 Module for Synergy",
                "serialNumber": "IC123",
                "partNumber": "867796-B21",
                "firmwareVersion": "2.6.0.1001",
                "powerState": "On",
                "logicalInterconnectUri": LI_URI,
                "state": "Configured",
                "status": "OK",
                "interconnectLocation": {
                    "locationEntries": [
                        {"type": "Enclosure", "value": ENCL_URI},
                        {"type": "Bay", "value": "3"},
                    ]
                },
            },
        ],
        "/rest/logical-interconnects": [
            {"uri": LI_URI, "name": "LE01-LIG-VC100"},
        ],
        "/rest/logical-enclosures": [
            {
                "uri": LE_URI,
                "name": "LE01",
                "enclosureUris": [ENCL_URI],
                "enclosureGroupUri": EG_URI,
            },
        ],
        "/rest/enclosure-groups": [
            {"uri": EG_URI, "name": "EG-Synergy"},
        ],
    }


@pytest.mark.asyncio
async def test_describe_enclosure_builds_front_and_rear_layout():
    result = await describe_enclosure(FakeClient(_collections()), "Enclosure-01")

    assert result["name"] == "Enclosure-01"
    assert result["logical_enclosure"] == "LE01"
    assert result["enclosure_group"] == "EG-Synergy"
    assert result["front_bay_count"] == 12
    assert result["rear_bay_count"] == 6
    assert result["appliances"] == [
        {
            "bay": 1,
            "model": "Synergy Composer2",
            "serial": "CNX23304MG",
            "part_number": "872957-B21",
            "spare_part_number": "879540-001",
            "presence": "Present",
            "power": "On",
            "status": "OK",
        },
        {
            "bay": 2,
            "model": "Synergy Composer2",
            "serial": "CNX23304PC",
            "part_number": "872957-B21",
            "spare_part_number": "879540-001",
            "presence": "Present",
            "power": "On",
            "status": "OK",
        },
    ]
    assert result["power_supplies"] == [
        {
            "bay": 1,
            "label": "1",
            "model": "2650W AC Titanium Hot Plug Power Supply",
            "serial": "9C4612003S",
            "part_number": "798095-B21",
            "spare_part_number": "813829-001",
            "presence": "Present",
            "capacity_watts": 2650,
            "status": "OK",
        },
        {
            "bay": 2,
            "label": "2",
            "model": "2650W AC Titanium Hot Plug Power Supply",
            "serial": "9C4612002Q",
            "part_number": "798095-B21",
            "spare_part_number": "813829-001",
            "presence": "Present",
            "capacity_watts": 2650,
            "status": "Warning",
        },
    ]
    assert result["fans"] == [
        {
            "bay": 1,
            "model": "Synergy Fan Module",
            "serial": "7C66162631",
            "part_number": "809097-001",
            "spare_part_number": "807967-001",
            "presence": "Present",
            "required": True,
            "status": "OK",
        }
    ]
    assert result["frame_link_modules"] == [
        {
            "bay": 1,
            "model": "Synergy Frame Link Module",
            "serial": "CN7643V08S",
            "part_number": "802341-B21",
            "spare_part_number": "807963-001",
            "role": "Standby",
            "fw_version": "4.01.00",
            "ip_address": "fe80::1602:ecff:fe44:bd50",
            "presence": "Present",
            "status": "OK",
        }
    ]

    assert result["servers"] == [
        {
            "bay": 1,
            "name": "Enclosure-01, bay 1",
            "server_name": "aci-vc-tunnel-host1.hst.enablement.local",
            "model": "SY 480 Gen10",
            "serial": "SCR096BA54F2",
            "part_number": "P12345-B21",
            "profile": "aci-vc-tunnel-host1",
            "profile_status": "Critical",
            "profile_state": "Normal",
            "power": "On",
            "power_allocation_watts": 308,
            "state": "ProfileApplied",
            "status": "Warning",
            "mp_model": "iLO5",
            "mp_firmware_version": "2.81",
            "rom_version": "I42 v2.78 (03/16/2023)",
            "ilo_ip": "192.0.2.10",
            "uri": SERVER_URI,
        }
    ]
    assert result["interconnects"] == [
        {
            "bay": 3,
            "name": "Enclosure-01, interconnect 3",
            "model": "Virtual Connect SE 100Gb F32 Module for Synergy",
            "serial": "IC123",
            "part_number": "867796-B21",
            "firmware_version": "2.6.0.1001",
            "power": "On",
            "power_allocation_watts": 270,
            "logical_interconnect": "LE01-LIG-VC100",
            "state": "Configured",
            "status": "OK",
            "uri": IC_URI,
        }
    ]
    assert result["devices"] == [
        {
            "bay": 1,
            "hardware": "Enclosure-01, bay 1",
            "server_name": "aci-vc-tunnel-host1.hst.enablement.local",
            "model": "SY 480 Gen10",
            "serial": "SCR096BA54F2",
            "profile": "aci-vc-tunnel-host1",
            "status": "Warning",
            "profile_status": "Critical",
            "presence": "Present",
            "power_allocation_watts": 308,
        },
        {
            "bay": 2,
            "hardware": "empty",
            "server_name": "not set",
            "model": "",
            "serial": "",
            "profile": "",
            "status": "",
            "profile_status": "",
            "presence": "Absent",
            "power_allocation_watts": 0,
        },
    ]
    assert result["firmware"] == [
        {
            "name": "Enclosure-01, frame link module 1",
            "component": "Frame link module",
            "installed": "4.01.00",
        },
        {
            "name": "Enclosure-01, bay 1",
            "component": "aci-vc-tunnel-host1",
            "installed": "",
        },
        {"name": "", "component": "iLO5", "installed": "2.81"},
        {"name": "", "component": "ROM", "installed": "I42 v2.78 (03/16/2023)"},
        {
            "name": "Enclosure-01, interconnect 3",
            "component": "Virtual Connect SE 100Gb F32 Module for Synergy",
            "installed": "2.6.0.1001",
        },
    ]


@pytest.mark.asyncio
async def test_describe_enclosure_unknown_name_raises():
    with pytest.raises(ValueError, match="Enclosure 'missing' not found"):
        await describe_enclosure(FakeClient(_collections()), "missing")


def test_enclosure_renderer_uses_physical_bay_rows():
    assert _front_bay_columns(12) == 6
    assert _front_frame_ratios(12) == [1, 1, 1, 1, 1, 1, 1]
    assert _front_bay_height(14) == 10
    assert _front_bay_height(21) == 14
    assert _bay_rows(12, 6) == [
        [1, 2, 3, 4, 5, 6],
        [7, 8, 9, 10, 11, 12],
    ]
    assert _rear_bay_columns(6) == 3
    assert _rear_frame_ratios(6) == [1, 1, 1, 1, 1, 1, 1]
    assert _bay_rows(6, 3) == [
        [1, 2, 3],
        [4, 5, 6],
    ]


def test_front_bay_text_omits_redundant_state_and_bay_label():
    lines = _server_bay_lines(
        1,
        {
            "name": "Enclosure-01, bay 1",
            "model": "SY 480 Gen10",
            "serial": "SCR096BA54F2",
            "profile": "aci-vc-tunnel-host1",
            "profile_status": "Critical",
            "power": "On",
            "state": "ProfileApplied",
        },
    )

    rendered = "\n".join(lines)
    assert "aci-vc-tunnel-host1" in rendered
    assert "◆" in rendered
    assert "bay 1" not in rendered
    assert "ProfileApplied" not in rendered
    assert "On" not in rendered


def test_appliance_bay_text_uses_composer_model():
    lines = _appliance_bay_lines(
        1,
        {
            "bay": 1,
            "model": "Synergy Composer2",
            "serial": "CNX23304MG",
            "status": "OK",
        },
    )

    rendered = "\n".join(lines)
    assert "C1" in rendered
    assert "Synergy Composer2" in rendered
    assert "CNX23304MG" in rendered
    assert "Appliance\nbay" not in rendered


def test_power_supply_text_shows_model_and_alert_status():
    ok_lines = _power_supply_lines(
        1,
        {
            "bay": 1,
            "model": "2650W AC Titanium Hot Plug Power Supply",
            "serial": "9C4612003S",
            "capacity_watts": 2650,
            "status": "OK",
        },
    )
    warning_lines = _power_supply_lines(
        2,
        {
            "bay": 2,
            "model": "2650W AC Titanium Hot Plug Power Supply",
            "serial": "9C4612002Q",
            "capacity_watts": 2650,
            "status": "Warning",
        },
    )

    assert ok_lines == [
        "Power Supply 1",
        "2650W AC Titanium",
        "",
    ]
    assert warning_lines == [
        "Power Supply 2",
        "2650W AC Titanium",
        "Warning",
    ]
