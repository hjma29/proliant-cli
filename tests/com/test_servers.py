"""Tests for proliant.com.servers -- Server.from_api() derivation logic and
device_to_server_row() adapter.

These are pure unit tests against fixed JSON fixtures (no network calls) --
the field-source mappings below were discovered via live, read-only API
probing against a real HPE GreenLake/COM workspace and are documented in
notes.md / AGENTS instructions.
"""
from __future__ import annotations

from proliant.com.servers import Server, device_to_server_row


def _base_server(**overrides) -> dict:
    s = {
        "id": "srv-1",
        "name": "dl380-20",
        "serverGeneration": "GEN_11",
        "connectionType": "DIRECT",
        "maintenanceMode": False,
        "autoIloFwUpdate": True,
        "processorVendor": "Intel(R) Xeon(R) Gold 5416S",
        "firmwareBundleUri": "",
        "hardware": {
            "serialNumber": "2M240400JK",
            "model": "ProLiant DL380a Gen11",
            "productId": "P54903-B21",
            "manufacturer": "HPE",
            "uuid": "39343550-3130-XX",
            "powerState": "ON",
            "health": {"summary": "OK"},
            "bmc": {
                "hostname": "ILO2M240400JK.ai.hpe.local",
                "ip": "192.168.109.110",
                "version": "iLO 6 v1.77",
                "license": "iLO Advanced",
            },
        },
        "state": {
            "connected": True,
            "managed": True,
            "subscriptionState": "ACTIVE",
            "subscriptionTier": "Standard",
            "subscriptionKey": "K051040577",
        },
        "host": {"osName": "Microsoft Windows Server 2025"},
        "oneview": {},
        "appliance": {},
    }
    s.update(overrides)
    return s


def test_from_api_connected_server_maps_all_fields():
    s = _base_server()
    server = Server.from_api(s, group_by_serial={}, baseline_by_id={}, appliance_by_id={})

    assert server.serial_number == "2M240400JK"
    assert server.state_label == "Connected"
    assert server.connected is True
    assert server.connection_type == "Direct"
    assert server.health == "OK"
    assert server.operating_system == "Microsoft Windows Server 2025"
    assert server.cpu == "Intel(R) Xeon(R) Gold 5416S"
    assert server.ilo_license == "iLO Advanced"
    assert server.group == "—"
    assert server.baseline == "—"
    assert server.appliance_name == "—"


def test_from_api_not_connected_and_subscription_required_is_not_activated():
    s = _base_server(state={"connected": False, "subscriptionState": "REQUIRED"})
    server = Server.from_api(s, group_by_serial={}, baseline_by_id={}, appliance_by_id={})
    assert server.state_label == "Not activated"
    assert server.connected is False


def test_from_api_not_connected_other_subscription_is_not_connected():
    s = _base_server(state={"connected": False, "subscriptionState": "ACTIVE"})
    server = Server.from_api(s, group_by_serial={}, baseline_by_id={}, appliance_by_id={})
    assert server.state_label == "Not connected"


def test_from_api_missing_health_defaults_to_unknown():
    s = _base_server()
    s["hardware"]["health"] = {}
    server = Server.from_api(s, group_by_serial={}, baseline_by_id={}, appliance_by_id={})
    assert server.health == "UNKNOWN"


def test_from_api_oneview_connection_resolves_appliance_and_oneview_fields():
    s = _base_server(
        connectionType="ONEVIEW",
        oneview={"name": "HSTESX04-ILO", "state": "Monitored"},
        appliance={"applianceId": "appl-1"},
    )
    server = Server.from_api(
        s, group_by_serial={}, baseline_by_id={},
        appliance_by_id={"appl-1": "hstov01.hst.enablement.local"},
    )
    assert server.connection_type == "OneView managed"
    assert server.appliance_name == "hstov01.hst.enablement.local"
    assert server.oneview_name == "HSTESX04-ILO"
    assert server.oneview_state == "Monitored"


def test_from_api_group_lookup_by_serial():
    s = _base_server()
    server = Server.from_api(
        s, group_by_serial={"2M240400JK": "Lab-SQL_Server_Perf"},
        baseline_by_id={}, appliance_by_id={},
    )
    assert server.group == "Lab-SQL_Server_Perf"


def test_from_api_baseline_resolves_from_bundle_uri():
    s = _base_server(firmwareBundleUri="/v1/firmware-bundles/bundle-123")
    server = Server.from_api(
        s, group_by_serial={}, baseline_by_id={"bundle-123": "2024.04.00.02"},
        appliance_by_id={},
    )
    assert server.baseline == "2024.04.00.02"


def test_from_api_baseline_falls_back_to_custom_when_bundle_id_unknown():
    s = _base_server(firmwareBundleUri="/v1/settings/custom-ilo-baseline")
    server = Server.from_api(s, group_by_serial={}, baseline_by_id={}, appliance_by_id={})
    assert server.baseline == "Custom"


class _FakeDevice:
    """Minimal stand-in for proliant.com.devices.Device."""
    def __init__(self, **kwargs):
        self.id = kwargs.get("id", "dev-1")
        self.serial_number = kwargs.get("serial_number", "AF-301872")
        self.display_name = kwargs.get("display_name", "")
        self.model = kwargs.get("model", "NS 6010 AF DC CTO BASE")
        self.product_id = kwargs.get("product_id", "")
        self.device_type = kwargs.get("device_type", "STORAGE")
        self.raw = kwargs.get("raw", {})


def test_device_to_server_row_fills_dashes_for_com_only_fields():
    d = _FakeDevice()
    row = device_to_server_row(d)

    assert row.serial_number == "AF-301872"
    assert row.device_type == "STORAGE"
    assert row.health == "—"
    assert row.group == "—"
    assert row.baseline == "—"
    assert row.connected is False


def test_device_to_server_row_uses_raw_name_when_display_name_missing():
    d = _FakeDevice(display_name="", raw={"name": "TW26KM003T-switch"})
    row = device_to_server_row(d)
    assert row.name == "TW26KM003T-switch"
    assert row.ilo_hostname == "TW26KM003T-switch"


def test_device_to_server_row_falls_back_to_serial_when_no_name_available():
    d = _FakeDevice(display_name="", raw={})
    row = device_to_server_row(d)
    assert row.name == "AF-301872"
