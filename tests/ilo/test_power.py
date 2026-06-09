from __future__ import annotations

import pytest

from pcli.ilo.power import reset_server


class FakeILOClient:
    def __init__(self, system: dict):
        self.system = system
        self.posts: list[tuple[str, dict]] = []

    async def get_system_uri(self) -> str:
        return "/redfish/v1/Systems/1"

    async def get(self, uri: str) -> dict:
        assert uri == "/redfish/v1/Systems/1"
        return self.system

    async def post(self, uri: str, body: dict) -> dict:
        self.posts.append((uri, body))
        return {}


@pytest.mark.asyncio
async def test_reset_server_posts_selected_reset_type():
    client = FakeILOClient({
        "Actions": {
            "#ComputerSystem.Reset": {
                "target": "/redfish/v1/Systems/1/Actions/ComputerSystem.Reset"
            }
        }
    })

    result = await reset_server(client, reset_type="ForceRestart")

    assert result == {
        "status": "accepted",
        "url": "/redfish/v1/Systems/1/Actions/ComputerSystem.Reset",
        "reset_type": "ForceRestart",
    }
    assert client.posts == [
        (
            "/redfish/v1/Systems/1/Actions/ComputerSystem.Reset",
            {"ResetType": "ForceRestart"},
        )
    ]


@pytest.mark.asyncio
async def test_reset_server_dry_run_does_not_post():
    client = FakeILOClient({
        "Actions": {
            "#ComputerSystem.Reset": {
                "target": "/redfish/v1/Systems/1/Actions/ComputerSystem.Reset"
            }
        }
    })

    result = await reset_server(client, dry_run=True)

    assert result == {
        "status": "dry-run",
        "url": "/redfish/v1/Systems/1/Actions/ComputerSystem.Reset",
        "payload": {"ResetType": "GracefulRestart"},
        "reset_type": "GracefulRestart",
    }
    assert client.posts == []


@pytest.mark.asyncio
async def test_reset_server_requires_reset_action():
    client = FakeILOClient({"Actions": {}})

    with pytest.raises(RuntimeError, match="ComputerSystem.Reset action not found"):
        await reset_server(client)
