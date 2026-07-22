"""Tests for the OneView server-hardware describe model (server_detail)."""

from __future__ import annotations

import pytest

from proliant.oneview.server_detail import (
    build_server_detail,
    fetch_server_detail,
    fmt_cpu,
    fmt_ip_addresses,
    fmt_memory_gb,
    fmt_state,
    fmt_temperature_f,
    normalize_device_inventory,
    normalize_utilization,
)


# ── live-shaped fixtures (captured from a real Synergy 480 Gen10, bay 3) ──────

RAW_SERVER = {
    "name": "Enclosure-01, bay 3",
    "serverName": "aci-FM-host1.hst.enablement.local",
    "status": "OK",
    "state": "ProfileApplied",
    "powerState": "On",
    "model": "Synergy 480 Gen10",
    "operatingSystem": "",
    "processorCount": 2,
    "processorType": "Intel(R) Xeon(R) Silver 4214R CPU @ 2.40GHz",
    "processorSpeedMhz": 2400,
    "processorCoreCount": 12,
    "memoryMb": 262144,
    "serialNumber": "CN77090BD1",
    "romVersion": "I42 v2.42 (03/22/2024)",
    "serverProfileUri": "/rest/server-profiles/profile3",
    "serverHardwareTypeUri": "/rest/server-hardware-types/hwtype1",
    "mpModel": "iLO5",
    "mpFirmwareVersion": "3.17 Dec 02 2025",
    "mpHostInfo": {
        "mpHostName": "ILOCN77090BD1.hst.enablement.local",
        "mpIpAddresses": [
            {"address": "fe80:0:0:32e1:71ff:fe57:d736", "type": "LinkLocal"},
            {"address": "10.16.41.10", "type": "DHCP"},
        ],
    },
    "uri": "/rest/server-hardware/server3",
}

FIRMWARE = {
    "components": [
        {"componentName": "Embedded Video Controller", "componentVersion": "2.5",
         "componentLocation": "Embedded Device", "componentKey": "k1"},
        {"componentName": "HPE Smart Array P204i-c SR Gen10", "componentVersion": "1.34",
         "componentLocation": "Embedded RAID", "componentKey": "k2"},
        {"componentName": "Synergy 6820C 25/50Gb CNA", "componentVersion": "08.45.20",
         "componentLocation": "Mezzanine Slot 3", "componentKey": "k3"},
        {"componentName": "Redundant System ROM", "componentVersion": "1.42",
         "componentLocation": "System Board", "componentKey": "k4"},
        {"componentName": "Broadcom-ntg3", "componentVersion": "1.2.3",
         "componentLocation": " ", "componentKey": "k5"},
    ],
}

UTILIZATION = {
    "metricList": [
        {"metricName": "AmbientTemperature", "metricSamples": [[1784682600000, 17], [1784682000000, 16]]},
        {"metricName": "AveragePower", "metricSamples": [[1784682600000, 67]]},
        {"metricName": "CpuUtilization", "metricSamples": [[1784682600000, 3]]},
    ],
}


# ── formatting helpers ────────────────────────────────────────────────────────

@pytest.mark.parametrize("state,expected", [
    ("ProfileApplied", "Profile Applied"),
    ("NoProfileApplied", "No Profile Applied"),
    ("Unmanaged", "Unmanaged"),
    ("", "—"),
    (None, "—"),
])
def test_fmt_state(state, expected):
    assert fmt_state(state) == expected


@pytest.mark.parametrize("mb,expected", [
    (262144, "256 GB"),
    (8192, "8 GB"),
    (12800, "12.5 GB"),
    (0, "—"),
    (None, "—"),
])
def test_fmt_memory_gb(mb, expected):
    assert fmt_memory_gb(mb) == expected


def test_fmt_cpu_matches_gui_format():
    assert fmt_cpu(2, "Intel(R) Xeon(R) Silver 4214R CPU @ 2.40GHz", 2400, 12) == (
        "2 processors, Intel(R) Xeon(R) Silver 4214R CPU (2.40 GHz / 12-core)"
    )


def test_fmt_cpu_singular_and_missing():
    assert fmt_cpu(1, "Intel(R) Xeon(R) Gold 5117 CPU @ 2.00GHz", 2000, 14) == (
        "1 processor, Intel(R) Xeon(R) Gold 5117 CPU (2.00 GHz / 14-core)"
    )
    assert fmt_cpu(None, None, None, None) == "—"


def test_fmt_ip_addresses_orders_routable_before_link_local():
    mp_host_info = RAW_SERVER["mpHostInfo"]
    assert fmt_ip_addresses(mp_host_info) == ["10.16.41.10", "fe80:0:0:32e1:71ff:fe57:d736"]


def test_fmt_ip_addresses_empty():
    assert fmt_ip_addresses(None) == []
    assert fmt_ip_addresses({}) == []


def test_fmt_temperature_f_converts_celsius():
    assert fmt_temperature_f(17) == 63
    assert fmt_temperature_f(None) is None


# ── normalization ─────────────────────────────────────────────────────────────

def test_normalize_device_inventory_excludes_system_board_and_blank_location():
    devices = normalize_device_inventory(FIRMWARE)
    names = [d["name"] for d in devices]
    assert "Redundant System ROM" not in names
    assert "Broadcom-ntg3" not in names
    assert names == [
        "Embedded Video Controller",
        "HPE Smart Array P204i-c SR Gen10",
        "Synergy 6820C 25/50Gb CNA",
    ]
    assert devices[0]["location"] == "Embedded Device"
    assert devices[0]["version"] == "2.5"


def test_normalize_device_inventory_empty():
    assert normalize_device_inventory(None) == []
    assert normalize_device_inventory({"components": []}) == []


def test_normalize_utilization_uses_newest_sample_and_converts_temperature():
    util = normalize_utilization(UTILIZATION)
    assert util["cpu_percent"] == 3
    assert util["power_w"] == 67
    assert util["temperature_f"] == 63  # 17C -> 63F, matches live GUI observation


def test_normalize_utilization_missing_metrics():
    assert normalize_utilization(None) == {
        "cpu_percent": None, "power_w": None, "temperature_f": None,
    }


# ── build_server_detail ───────────────────────────────────────────────────────

def test_build_server_detail_assembles_model():
    info = build_server_detail(RAW_SERVER, "aci-FM-host1", "SY 480 Gen10 1", FIRMWARE, UTILIZATION)
    assert info["name"] == "Enclosure-01, bay 3"
    assert info["state_display"] == "Profile Applied"
    assert info["profile"] == "aci-FM-host1"
    assert info["hardware_type"] == "SY 480 Gen10 1"
    assert info["cpu"] == "2 processors, Intel(R) Xeon(R) Silver 4214R CPU (2.40 GHz / 12-core)"
    assert info["memory"] == "256 GB"
    assert info["mp_ip_addresses"] == ["10.16.41.10", "fe80:0:0:32e1:71ff:fe57:d736"]
    assert len(info["device_inventory"]) == 3
    assert info["utilization"]["cpu_cores_total"] == 24
    assert info["utilization"]["temperature_f"] == 63


def test_build_server_detail_handles_unmanaged_server_with_no_profile():
    raw = {**RAW_SERVER, "serverProfileUri": None, "serverHardwareTypeUri": None,
           "state": "NoProfileApplied", "operatingSystem": "Unknown", "powerState": "Off"}
    info = build_server_detail(raw, None, None, None, None)
    assert info["profile"] is None
    assert info["hardware_type"] is None
    assert info["state_display"] == "No Profile Applied"
    assert info["device_inventory"] == []
    assert info["utilization"] == {"cpu_percent": None, "power_w": None, "temperature_f": None}


# ── fetch (fake client) ───────────────────────────────────────────────────────

class _FakeClient:
    def __init__(self, *, firmware_raises=False, utilization_raises=False):
        self.firmware_raises = firmware_raises
        self.utilization_raises = utilization_raises
        self.calls: list[str] = []

    async def get_all(self, uri):
        self.calls.append(uri)
        if uri == "/rest/server-hardware":
            return [RAW_SERVER]
        return []

    async def get(self, uri, params=None):
        self.calls.append(uri)
        if uri == "/rest/server-profiles/profile3":
            return {"name": "aci-FM-host1"}
        if uri == "/rest/server-hardware-types/hwtype1":
            return {"name": "SY 480 Gen10 1"}
        if uri == "/rest/server-hardware/server3/firmware":
            if self.firmware_raises:
                raise RuntimeError("firmware endpoint unavailable")
            return FIRMWARE
        if uri == "/rest/server-hardware/server3/utilization":
            if self.utilization_raises:
                raise RuntimeError("utilization endpoint unavailable")
            return UTILIZATION
        return {}


@pytest.mark.asyncio
async def test_fetch_server_detail_assembles_model():
    info = await fetch_server_detail(_FakeClient(), "Enclosure-01, bay 3")
    assert info["profile"] == "aci-FM-host1"
    assert info["hardware_type"] == "SY 480 Gen10 1"
    assert len(info["device_inventory"]) == 3
    assert info["utilization"]["power_w"] == 67


@pytest.mark.asyncio
async def test_fetch_server_detail_is_case_insensitive():
    info = await fetch_server_detail(_FakeClient(), "enclosure-01, bay 3")
    assert info["name"] == "Enclosure-01, bay 3"


@pytest.mark.asyncio
async def test_fetch_server_detail_not_found_raises():
    with pytest.raises(ValueError, match="not found"):
        await fetch_server_detail(_FakeClient(), "Enclosure-01, bay 99")


@pytest.mark.asyncio
async def test_fetch_server_detail_tolerates_optional_endpoint_failures():
    info = await fetch_server_detail(
        _FakeClient(firmware_raises=True, utilization_raises=True), "Enclosure-01, bay 3"
    )
    assert info["device_inventory"] == []
    assert info["utilization"] == {"cpu_percent": None, "power_w": None, "temperature_f": None}
    # core hardware info still present even without the optional device/utilization data
    assert info["profile"] == "aci-FM-host1"
