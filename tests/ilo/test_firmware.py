"""Unit tests for async proliant.ilo.firmware."""

from __future__ import annotations

import pytest

from proliant.ilo import firmware


class FakeClient:
    def __init__(self, responses: dict[str, object]):
        self.responses = responses
        self.deleted: list[str] = []

    async def get_update_service_uri(self) -> str:
        return "/redfish/v1/UpdateService/"

    async def get(self, uri: str) -> dict:
        value = self.responses.get(uri, {})
        if isinstance(value, dict):
            return value
        raise AssertionError(f"Unexpected GET response for {uri!r}: {value!r}")

    async def post(self, uri: str, body: dict) -> dict:
        value = self.responses.get(("POST", uri), self.responses.get(uri, {}))
        if isinstance(value, dict):
            return value
        raise AssertionError(f"Unexpected POST response for {uri!r}: {value!r}")

    async def delete(self, uri: str) -> int:
        self.deleted.append(uri)
        value = self.responses.get(("DELETE", uri), 204)
        if isinstance(value, int):
            return value
        raise AssertionError(f"Unexpected DELETE response for {uri!r}: {value!r}")


@pytest.mark.asyncio
async def test_stage_from_uri_dry_run():
    client = FakeClient({
        "/redfish/v1/UpdateService/": {
            "Actions": {
                "Oem": {
                    "Hpe": {
                        "#HpeiLOUpdateServiceExt.AddFromUri": {
                            "target": "/redfish/v1/UpdateService/Actions/Oem/Hpe/HpeiLOUpdateServiceExt.AddFromUri/"
                        }
                    }
                }
            }
        }
    })
    result = await firmware.stage_from_uri(client, "https://example.com/firmware.fwpkg", dry_run=True)
    assert result["dry_run"] is True
    assert "AddFromUri" in result["target"]
    assert result["payload"]["ImageURI"] == "https://example.com/firmware.fwpkg"
    assert result["payload"]["UpdateRepository"] is True
    assert result["payload"]["UpdateTarget"] is False


@pytest.mark.asyncio
async def test_stage_from_uri_missing_action():
    client = FakeClient({"/redfish/v1/UpdateService/": {"Actions": {"Oem": {"Hpe": {}}}}})
    with pytest.raises(RuntimeError, match="AddFromUri action not found"):
        await firmware.stage_from_uri(client, "https://example.com/fw.fwpkg")


@pytest.mark.asyncio
async def test_get_task_queue_empty():
    client = FakeClient({"/redfish/v1/UpdateService/UpdateTaskQueue/": {"Members": []}})
    assert await firmware.get_task_queue(client) == []


@pytest.mark.asyncio
async def test_get_task_queue_returns_entries():
    entries = [{"@odata.id": "/redfish/v1/UpdateService/UpdateTaskQueue/1", "State": "Pending"}]
    client = FakeClient({"/redfish/v1/UpdateService/UpdateTaskQueue/": {"Members": entries}})
    result = await firmware.get_task_queue(client)
    assert len(result) == 1
    assert result[0]["State"] == "Pending"


@pytest.mark.asyncio
async def test_get_task_queue_expands_stubs():
    stubs = [
        {"@odata.id": "/redfish/v1/UpdateService/UpdateTaskQueue/1"},
        {"@odata.id": "/redfish/v1/UpdateService/UpdateTaskQueue/2"},
    ]
    client = FakeClient({
        "/redfish/v1/UpdateService/UpdateTaskQueue/": {"Members": stubs},
        "/redfish/v1/UpdateService/UpdateTaskQueue/1": {"Filename": "a.fwpkg", "State": "Pending"},
        "/redfish/v1/UpdateService/UpdateTaskQueue/2": {"Filename": "b.fwpkg", "State": "Complete"},
    })
    result = await firmware.get_task_queue(client)
    assert len(result) == 2
    assert result[0]["State"] == "Pending"
    assert result[1]["Filename"] == "b.fwpkg"


@pytest.mark.asyncio
async def test_add_to_task_queue_dry_run():
    client = FakeClient({})
    result = await firmware.add_to_task_queue(client, "A66_1.40_01_09_2026.fwpkg", dry_run=True)
    assert result["dry_run"] is True
    assert result["payload"]["Filename"] == "A66_1.40_01_09_2026.fwpkg"
    assert result["payload"]["Command"] == "ApplyUpdate"


@pytest.mark.asyncio
async def test_clear_task_queue_dry_run():
    stubs = [
        {"@odata.id": "/redfish/v1/UpdateService/UpdateTaskQueue/1"},
        {"@odata.id": "/redfish/v1/UpdateService/UpdateTaskQueue/2"},
    ]
    client = FakeClient({
        "/redfish/v1/UpdateService/UpdateTaskQueue/": {"Members": stubs},
        "/redfish/v1/UpdateService/UpdateTaskQueue/1": {"@odata.id": "/redfish/v1/UpdateService/UpdateTaskQueue/1", "State": "Pending"},
        "/redfish/v1/UpdateService/UpdateTaskQueue/2": {"@odata.id": "/redfish/v1/UpdateService/UpdateTaskQueue/2", "State": "Pending"},
    })
    uris = await firmware.clear_task_queue(client, dry_run=True)
    assert len(uris) == 2
    assert client.deleted == []


@pytest.mark.asyncio
async def test_clear_task_queue_empty():
    client = FakeClient({"/redfish/v1/UpdateService/UpdateTaskQueue/": {"Members": []}})
    deleted = await firmware.clear_task_queue(client)
    assert deleted == []
