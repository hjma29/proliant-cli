"""
Unit tests for hpeilo.firmware
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
These tests use mocked RedfishClient responses to avoid live iLO connections.
Run with: python -m pytest tests/
"""

from unittest.mock import MagicMock, patch

import pytest

from pcli.ilo import firmware


def _make_client(responses: dict) -> MagicMock:
    """Build a minimal mock RedfishClient.

    ``responses`` maps URI strings to (status, obj) tuples:
        {"/redfish/v1/UpdateService/": (200, {...})}
    """
    client = MagicMock()

    def _get(uri):
        resp = MagicMock()
        status, obj = responses.get(uri, (404, {}))
        resp.status = status
        resp.obj = obj
        return resp

    def _post(uri, body=None):
        resp = MagicMock()
        status, obj = responses.get(uri, (200, {}))
        resp.status = status
        resp.obj = obj
        return resp

    def _delete(uri):
        resp = MagicMock()
        resp.status = 204
        resp.obj = {}
        return resp

    client.get.side_effect = _get
    client.post.side_effect = _post
    client.delete.side_effect = _delete
    client.root.obj = {"UpdateService": {"@odata.id": "/redfish/v1/UpdateService/"}}
    return client


# ---------------------------------------------------------------------------
# stage_from_uri
# ---------------------------------------------------------------------------

def test_stage_from_uri_dry_run():
    client = _make_client({
        "/redfish/v1/UpdateService/": (200, {
            "Actions": {
                "Oem": {
                    "Hpe": {
                        "#HpeiLOUpdateServiceExt.AddFromUri": {
                            "target": "/redfish/v1/UpdateService/Actions/Oem/Hpe/HpeiLOUpdateServiceExt.AddFromUri/"
                        }
                    }
                }
            }
        })
    })

    result = firmware.stage_from_uri(
        client,
        "https://example.com/firmware.fwpkg",
        dry_run=True,
    )
    assert result["dry_run"] is True
    assert "AddFromUri" in result["target"]
    assert result["payload"]["ImageURI"] == "https://example.com/firmware.fwpkg"
    assert result["payload"]["UpdateRepository"] is True
    assert result["payload"]["UpdateTarget"] is False


def test_stage_from_uri_missing_action():
    client = _make_client({
        "/redfish/v1/UpdateService/": (200, {"Actions": {"Oem": {"Hpe": {}}}})
    })
    with pytest.raises(RuntimeError, match="AddFromUri action not found"):
        firmware.stage_from_uri(client, "https://example.com/fw.fwpkg")


# ---------------------------------------------------------------------------
# get_task_queue
# ---------------------------------------------------------------------------

def test_get_task_queue_empty():
    client = _make_client({
        "/redfish/v1/UpdateService/UpdateTaskQueue/": (200, {"Members": []})
    })
    assert firmware.get_task_queue(client) == []


def test_get_task_queue_returns_entries():
    # Members already expanded (Gen11 style) — State present, no extra fetch needed
    entries = [{"@odata.id": "/redfish/v1/UpdateService/UpdateTaskQueue/1", "State": "Pending"}]
    client = _make_client({
        "/redfish/v1/UpdateService/UpdateTaskQueue/": (200, {"Members": entries})
    })
    result = firmware.get_task_queue(client)
    assert len(result) == 1
    assert result[0]["State"] == "Pending"


def test_get_task_queue_expands_stubs():
    # Gen12: Members are stubs (only @odata.id) — each must be fetched individually
    stubs = [
        {"@odata.id": "/redfish/v1/UpdateService/UpdateTaskQueue/1"},
        {"@odata.id": "/redfish/v1/UpdateService/UpdateTaskQueue/2"},
    ]
    client = _make_client({
        "/redfish/v1/UpdateService/UpdateTaskQueue/": (200, {"Members": stubs}),
        "/redfish/v1/UpdateService/UpdateTaskQueue/1": (200, {"Filename": "a.fwpkg", "State": "Pending"}),
        "/redfish/v1/UpdateService/UpdateTaskQueue/2": (200, {"Filename": "b.fwpkg", "State": "Complete"}),
    })
    result = firmware.get_task_queue(client)
    assert len(result) == 2
    assert result[0]["State"] == "Pending"
    assert result[1]["Filename"] == "b.fwpkg"


# ---------------------------------------------------------------------------
# add_to_task_queue
# ---------------------------------------------------------------------------

def test_add_to_task_queue_dry_run():
    client = _make_client({})
    result = firmware.add_to_task_queue(
        client, "A66_1.40_01_09_2026.fwpkg", dry_run=True
    )
    assert result["dry_run"] is True
    assert result["payload"]["Filename"] == "A66_1.40_01_09_2026.fwpkg"
    assert result["payload"]["Command"] == "ApplyUpdate"


# ---------------------------------------------------------------------------
# clear_task_queue
# ---------------------------------------------------------------------------

def test_clear_task_queue_dry_run():
    # Gen12 stubs — get_task_queue must expand them before clear_task_queue can read @odata.id
    stubs = [
        {"@odata.id": "/redfish/v1/UpdateService/UpdateTaskQueue/1"},
        {"@odata.id": "/redfish/v1/UpdateService/UpdateTaskQueue/2"},
    ]
    client = _make_client({
        "/redfish/v1/UpdateService/UpdateTaskQueue/": (200, {"Members": stubs}),
        "/redfish/v1/UpdateService/UpdateTaskQueue/1": (200, {"@odata.id": "/redfish/v1/UpdateService/UpdateTaskQueue/1", "State": "Pending"}),
        "/redfish/v1/UpdateService/UpdateTaskQueue/2": (200, {"@odata.id": "/redfish/v1/UpdateService/UpdateTaskQueue/2", "State": "Pending"}),
    })
    uris = firmware.clear_task_queue(client, dry_run=True)
    assert len(uris) == 2
    client.delete.assert_not_called()


def test_clear_task_queue_empty():
    client = _make_client({
        "/redfish/v1/UpdateService/UpdateTaskQueue/": (200, {"Members": []})
    })
    deleted = firmware.clear_task_queue(client)
    assert deleted == []
