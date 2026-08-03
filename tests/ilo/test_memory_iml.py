"""Tests for memory-report status field and IML memory-event history."""

from __future__ import annotations

import pytest

from proliant.ilo import inventory


class FakeClient:
    def __init__(self, responses: dict[str, object]):
        self.responses = responses

    async def get_system_uri(self) -> str:
        return "/redfish/v1/Systems/1"

    async def get(self, uri: str) -> dict:
        value = self.responses.get(uri, {})
        if isinstance(value, dict):
            return value
        raise AssertionError(f"Unexpected GET response for {uri!r}: {value!r}")


def _dimm(locator, cap_mib, status, part="P00000-000", vendor_part="ABC123"):
    return {
        "DeviceLocator": locator,
        "CapacityMiB": cap_mib,
        "BaseModuleType": "RDIMM",
        "PartNumber": vendor_part,
        "Manufacturer": "SK Hynix",
        "Oem": {"Hpe": {
            "DIMMStatus": status,
            "PartNumber": part,
            "VendorName": "SK Hynix",
            "MaxOperatingSpeedMTs": 2933,
        }},
    }


class TestFetchMemoryReportDataStatus:
    @pytest.mark.asyncio
    async def test_includes_status_field(self):
        client = FakeClient({
            "/redfish/v1/Systems/1": {"Memory": {"@odata.id": "/redfish/v1/Systems/1/Memory"}},
            "/redfish/v1/Systems/1/Memory": {
                "Members": [{"@odata.id": "/redfish/v1/Systems/1/Memory/1"}]
            },
            "/redfish/v1/Systems/1/Memory/1": _dimm("proc1dimm1", 16384, "GoodInUse"),
        })

        result = await inventory.fetch_memory_report_data(client)

        assert len(result) == 1
        assert result[0]["status"] == "GoodInUse"

    @pytest.mark.asyncio
    async def test_degraded_status_preserved(self):
        client = FakeClient({
            "/redfish/v1/Systems/1": {"Memory": {"@odata.id": "/redfish/v1/Systems/1/Memory"}},
            "/redfish/v1/Systems/1/Memory": {
                "Members": [{"@odata.id": "/redfish/v1/Systems/1/Memory/1"}]
            },
            "/redfish/v1/Systems/1/Memory/1": _dimm("proc1dimm1", 16384, "Degraded"),
        })

        result = await inventory.fetch_memory_report_data(client)

        assert result[0]["status"] == "Degraded"


class TestFetchMemoryIMLEvents:
    def _iml_entry(self, id_, created, message, class_desc, severity="OK"):
        return {
            "@odata.id": f"/redfish/v1/Systems/1/LogServices/IML/Entries/{id_}/",
            "Id": id_,
            "Created": created,
            "Message": message,
            "Severity": severity,
            "Oem": {"Hpe": {"ClassDescription": class_desc, "Severity": severity, "Count": 1}},
        }

    @pytest.mark.asyncio
    async def test_filters_and_sorts_memory_events_newest_first(self):
        client = FakeClient({
            "/redfish/v1/Systems/1": {"LogServices": {"@odata.id": "/redfish/v1/Systems/1/LogServices/"}},
            "/redfish/v1/Systems/1/LogServices/": {
                "Members": [
                    {"@odata.id": "/redfish/v1/Systems/1/LogServices/IML/"},
                    {"@odata.id": "/redfish/v1/Systems/1/LogServices/SL/"},
                ]
            },
            "/redfish/v1/Systems/1/LogServices/IML/": {
                "Entries": {"@odata.id": "/redfish/v1/Systems/1/LogServices/IML/Entries/"}
            },
            "/redfish/v1/Systems/1/LogServices/IML/Entries/": {
                "Members": [
                    self._iml_entry("1", "2026-01-01T00:00:00Z", "IML Cleared", "Maintenance"),
                    self._iml_entry(
                        "2", "2026-03-01T00:00:00Z",
                        "Corrected Memory Error (Processor 1, DIMM 3)", "Memory", severity="Critical",
                    ),
                    self._iml_entry(
                        "3", "2026-02-01T00:00:00Z",
                        "Uncorrectable Memory Error (Processor 1, DIMM 5)", "Memory", severity="Critical",
                    ),
                    self._iml_entry("4", "2026-04-01T00:00:00Z", "Fan installed", "Hardware"),
                ]
            },
        })

        events = await inventory.fetch_memory_iml_events(client)

        assert [e["created"] for e in events] == ["2026-03-01T00:00:00Z", "2026-02-01T00:00:00Z"]
        assert all("Memory" in e["message"] or "DIMM" in e["message"] for e in events)
        assert events[0]["severity"] == "Critical"

    @pytest.mark.asyncio
    async def test_keyword_fallback_catches_dimm_mentions_outside_memory_class(self):
        client = FakeClient({
            "/redfish/v1/Systems/1": {"LogServices": {"@odata.id": "/redfish/v1/Systems/1/LogServices/"}},
            "/redfish/v1/Systems/1/LogServices/": {
                "Members": [{"@odata.id": "/redfish/v1/Systems/1/LogServices/IML/"}]
            },
            "/redfish/v1/Systems/1/LogServices/IML/": {
                "Entries": {"@odata.id": "/redfish/v1/Systems/1/LogServices/IML/Entries/"}
            },
            "/redfish/v1/Systems/1/LogServices/IML/Entries/": {
                "Members": [
                    self._iml_entry("1", "2026-01-01T00:00:00Z", "DIMM N1 removed", "Hardware"),
                ]
            },
        })

        events = await inventory.fetch_memory_iml_events(client)

        assert len(events) == 1
        assert "DIMM" in events[0]["message"]

    @pytest.mark.asyncio
    async def test_respects_limit(self):
        members = [
            self._iml_entry(str(i), f"2026-01-{i:02d}T00:00:00Z", "Memory error", "Memory")
            for i in range(1, 6)
        ]
        client = FakeClient({
            "/redfish/v1/Systems/1": {"LogServices": {"@odata.id": "/redfish/v1/Systems/1/LogServices/"}},
            "/redfish/v1/Systems/1/LogServices/": {
                "Members": [{"@odata.id": "/redfish/v1/Systems/1/LogServices/IML/"}]
            },
            "/redfish/v1/Systems/1/LogServices/IML/": {
                "Entries": {"@odata.id": "/redfish/v1/Systems/1/LogServices/IML/Entries/"}
            },
            "/redfish/v1/Systems/1/LogServices/IML/Entries/": {"Members": members},
        })

        events = await inventory.fetch_memory_iml_events(client, limit=2)

        assert len(events) == 2
        assert events[0]["created"] == "2026-01-05T00:00:00Z"

    @pytest.mark.asyncio
    async def test_no_memory_events_returns_empty_list(self):
        client = FakeClient({
            "/redfish/v1/Systems/1": {"LogServices": {"@odata.id": "/redfish/v1/Systems/1/LogServices/"}},
            "/redfish/v1/Systems/1/LogServices/": {
                "Members": [{"@odata.id": "/redfish/v1/Systems/1/LogServices/IML/"}]
            },
            "/redfish/v1/Systems/1/LogServices/IML/": {
                "Entries": {"@odata.id": "/redfish/v1/Systems/1/LogServices/IML/Entries/"}
            },
            "/redfish/v1/Systems/1/LogServices/IML/Entries/": {
                "Members": [
                    self._iml_entry("1", "2026-01-01T00:00:00Z", "IML Cleared", "Maintenance"),
                ]
            },
        })

        events = await inventory.fetch_memory_iml_events(client)

        assert events == []

    @pytest.mark.asyncio
    async def test_no_log_services_link_returns_empty_list(self):
        client = FakeClient({"/redfish/v1/Systems/1": {}})

        events = await inventory.fetch_memory_iml_events(client)

        assert events == []

    @pytest.mark.asyncio
    async def test_no_iml_member_returns_empty_list(self):
        client = FakeClient({
            "/redfish/v1/Systems/1": {"LogServices": {"@odata.id": "/redfish/v1/Systems/1/LogServices/"}},
            "/redfish/v1/Systems/1/LogServices/": {
                "Members": [{"@odata.id": "/redfish/v1/Systems/1/LogServices/SL/"}]
            },
        })

        events = await inventory.fetch_memory_iml_events(client)

        assert events == []
