"""Tests for the OneView server-hardware-types model (hardware_types)."""

from __future__ import annotations

import pytest

from proliant.oneview.hardware_types import (
    build_hardware_type_detail,
    build_hardware_types_list,
    fetch_hardware_type_detail,
    fetch_hardware_types,
    fmt_adapter_location,
    normalize_adapters,
)


# ── live-shaped fixtures (captured from a real Synergy Composer2) ─────────────

HWTYPE_1 = {
    "name": "SY 480 Gen10 1",
    "uri": "/rest/server-hardware-types/hwtype1",
    "model": "Synergy 480 Gen10",
    "formFactor": "HalfHeight",
    "uefiClass": "2",
    "adapters": [
        {"model": "Synergy 6820C 25/50Gb CNA", "location": "Mezz", "slot": 3, "deviceType": "Ethernet"},
    ],
}

HWTYPE_2 = {
    "name": "SY 480 Gen10 2",
    "uri": "/rest/server-hardware-types/hwtype2",
    "model": "Synergy 480 Gen10",
    "formFactor": "HalfHeight",
    "uefiClass": "2",
    "adapters": [
        {"model": "Synergy 4820C 10/20/25Gb CNA", "location": "Mezz", "slot": 3, "deviceType": "Ethernet"},
    ],
}

SERVERS = [
    {"name": "Enclosure-01, bay 3", "uri": "/rest/server-hardware/s3",
     "serverHardwareTypeUri": "/rest/server-hardware-types/hwtype2",
     "status": "OK", "state": "ProfileApplied", "powerState": "On"},
    {"name": "Enclosure-01, bay 7", "uri": "/rest/server-hardware/s7",
     "serverHardwareTypeUri": "/rest/server-hardware-types/hwtype1",
     "status": "OK", "state": "NoProfileApplied", "powerState": "Off"},
    {"name": "Enclosure-01, bay 5", "uri": "/rest/server-hardware/s5",
     "serverHardwareTypeUri": "/rest/server-hardware-types/hwtype2",
     "status": "Warning", "state": "ProfileApplied", "powerState": "On"},
]

PROFILES = [
    {"name": "aci-FM-host1", "serverHardwareTypeUri": "/rest/server-hardware-types/hwtype2",
     "serverHardwareUri": "/rest/server-hardware/s3", "status": "OK", "state": "Normal"},
    {"name": "aci-Mapped-host1", "serverHardwareTypeUri": "/rest/server-hardware-types/hwtype2",
     "serverHardwareUri": "/rest/server-hardware/s5", "status": "Warning", "state": "Normal"},
]

TEMPLATES = [
    {"name": "gen10-4820-ten-connections", "serverHardwareTypeUri": "/rest/server-hardware-types/hwtype2"},
    {"name": "gen10-4820-legacy", "serverHardwareTypeUri": "/rest/server-hardware-types/hwtype2"},
    {"name": "gen10-6820-uplink", "serverHardwareTypeUri": "/rest/server-hardware-types/hwtype1"},
]


# ── formatting helpers ────────────────────────────────────────────────────────

@pytest.mark.parametrize("location,slot,expected", [
    ("Mezz", 3, "Mezzanine 3"),
    ("Mezz", None, "Mezzanine"),
    ("", 1, "Slot 1"),
    (None, None, "—"),
    ("Embedded", 1, "Embedded 1"),
])
def test_fmt_adapter_location(location, slot, expected):
    assert fmt_adapter_location(location, slot) == expected


def test_normalize_adapters():
    adapters = normalize_adapters(HWTYPE_1)
    assert adapters == [{"location": "Mezzanine 3", "model": "Synergy 6820C 25/50Gb CNA", "device_type": "Ethernet"}]


def test_normalize_adapters_empty():
    assert normalize_adapters({}) == []
    assert normalize_adapters({"adapters": []}) == []


# ── build_hardware_types_list ─────────────────────────────────────────────────

def test_build_hardware_types_list_counts_usage():
    result = build_hardware_types_list([HWTYPE_1, HWTYPE_2], SERVERS, PROFILES)
    by_name = {t["name"]: t for t in result}
    assert by_name["SY 480 Gen10 1"]["server_count"] == 1
    assert by_name["SY 480 Gen10 1"]["profile_count"] == 0
    assert by_name["SY 480 Gen10 2"]["server_count"] == 2
    assert by_name["SY 480 Gen10 2"]["profile_count"] == 2


def test_build_hardware_types_list_sorted_by_name():
    result = build_hardware_types_list([HWTYPE_2, HWTYPE_1])
    assert [t["name"] for t in result] == ["SY 480 Gen10 1", "SY 480 Gen10 2"]


def test_build_hardware_types_list_no_usage_data():
    result = build_hardware_types_list([HWTYPE_1])
    assert result[0]["server_count"] == 0
    assert result[0]["profile_count"] == 0


# ── build_hardware_type_detail ────────────────────────────────────────────────

def test_build_hardware_type_detail_cross_references_servers_and_profiles():
    info = build_hardware_type_detail(HWTYPE_2, SERVERS, PROFILES, TEMPLATES)
    assert info["name"] == "SY 480 Gen10 2"
    assert [s["name"] for s in info["servers"]] == ["Enclosure-01, bay 3", "Enclosure-01, bay 5"]
    assert info["servers"][0]["profile"] == "aci-FM-host1"
    assert [p["name"] for p in info["profiles"]] == ["aci-FM-host1", "aci-Mapped-host1"]
    assert info["profiles"][0]["server"] == "Enclosure-01, bay 3"
    assert info["templates"] == ["gen10-4820-legacy", "gen10-4820-ten-connections"]


def test_build_hardware_type_detail_no_profile_assigned():
    info = build_hardware_type_detail(HWTYPE_1, SERVERS, PROFILES, TEMPLATES)
    assert len(info["servers"]) == 1
    assert info["servers"][0]["profile"] == ""
    assert info["profiles"] == []
    assert info["templates"] == ["gen10-6820-uplink"]


def test_build_hardware_type_detail_empty_inputs():
    info = build_hardware_type_detail(HWTYPE_1)
    assert info["servers"] == []
    assert info["profiles"] == []
    assert info["templates"] == []
    assert info["adapters"] == normalize_adapters(HWTYPE_1)


# ── fetch (fake client) ───────────────────────────────────────────────────────

class _FakeClient:
    async def get_all(self, uri):
        if uri == "/rest/server-hardware-types":
            return [HWTYPE_1, HWTYPE_2]
        if uri == "/rest/server-hardware":
            return SERVERS
        if uri == "/rest/server-profiles":
            return PROFILES
        if uri == "/rest/server-profile-templates":
            return TEMPLATES
        return []


@pytest.mark.asyncio
async def test_fetch_hardware_types_assembles_list():
    result = await fetch_hardware_types(_FakeClient())
    by_name = {t["name"]: t for t in result}
    assert by_name["SY 480 Gen10 2"]["server_count"] == 2
    assert by_name["SY 480 Gen10 1"]["profile_count"] == 0


@pytest.mark.asyncio
async def test_fetch_hardware_type_detail_by_name():
    info = await fetch_hardware_type_detail(_FakeClient(), "SY 480 Gen10 2")
    assert len(info["servers"]) == 2
    assert len(info["profiles"]) == 2
    assert info["templates"] == ["gen10-4820-legacy", "gen10-4820-ten-connections"]


@pytest.mark.asyncio
async def test_fetch_hardware_type_detail_is_case_insensitive():
    info = await fetch_hardware_type_detail(_FakeClient(), "sy 480 gen10 1")
    assert info["name"] == "SY 480 Gen10 1"


@pytest.mark.asyncio
async def test_fetch_hardware_type_detail_not_found_raises():
    with pytest.raises(ValueError, match="not found"):
        await fetch_hardware_type_detail(_FakeClient(), "SY 999 Gen99")
