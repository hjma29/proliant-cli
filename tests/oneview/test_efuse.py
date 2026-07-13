from __future__ import annotations

import pytest

from proliant.oneview.efuse import run_efuse_action


ENC_URI = "/rest/enclosures/enc1"
SERVER_URI = "/rest/server-hardware/sh1"
PROFILE_URI = "/rest/server-profiles/sp1"
IC_URI = "/rest/interconnects/ic6"


class FakeClient:
    def __init__(self, collections: dict[str, list[dict]], singles: dict[str, dict] | None = None):
        self._collections = collections
        self._singles = singles or {}
        self.patches: list[tuple[str, list[dict]]] = []

    async def get_all(self, uri: str) -> list[dict]:
        return self._collections.get(uri, [])

    async def get(self, uri: str, params=None) -> dict:
        return self._singles.get(uri, {})

    async def patch(self, uri: str, body: list[dict], **kwargs) -> dict:
        self.patches.append((uri, body))
        return {"uri": "/rest/tasks/efuse-task"}


def _server(**overrides) -> dict:
    data = {
        "name": "Enclosure-01, bay 6",
        "uri": SERVER_URI,
        "locationUri": ENC_URI,
        "position": 6,
        "powerState": "On",
    }
    data.update(overrides)
    return data


def _enclosure(**overrides) -> dict:
    data = {
        "name": "Enclosure-01",
        "uri": ENC_URI,
        "managerBays": [{"bayNumber": 1}, {"bayNumber": 2}],
    }
    data.update(overrides)
    return data


@pytest.mark.asyncio
async def test_run_efuse_action_server_by_location():
    client = FakeClient(
        collections={
            "/rest/enclosures": [_enclosure()],
            "/rest/server-hardware": [_server()],
        },
        singles={ENC_URI: _enclosure()},
    )

    result = await run_efuse_action(client, "server", enclosure="Enclosure-01", bay=6)

    assert client.patches == [
        (ENC_URI, [{"op": "replace", "path": "/deviceBays/6/bayPowerState", "value": "E-Fuse"}])
    ]
    assert result["method"] == "enclosure eFuse"
    assert result["target_type"] == "server"
    assert result["component"] == "Device"


@pytest.mark.asyncio
async def test_run_efuse_action_profile_resolves_assigned_server():
    client = FakeClient(
        collections={
            "/rest/server-profiles": [{
                "name": "ocp-host-1",
                "uri": PROFILE_URI,
                "serverHardwareUri": SERVER_URI,
            }],
        },
        singles={SERVER_URI: _server(), ENC_URI: _enclosure()},
    )

    result = await run_efuse_action(client, "profile", name="ocp-host-1", dry_run=True)

    assert client.patches == []
    assert result["status"] == "dry-run"
    assert result["target_type"] == "profile"
    assert result["target"] == "ocp-host-1"


@pytest.mark.asyncio
async def test_run_efuse_action_interconnect():
    client = FakeClient(
        collections={
            "/rest/interconnects": [{
                "name": "Enclosure-01, interconnect 6",
                "uri": IC_URI,
                "interconnectLocation": {
                    "locationEntries": [
                        {"type": "Enclosure", "value": ENC_URI},
                        {"type": "Bay", "value": "6"},
                    ],
                },
            }],
        },
        singles={ENC_URI: _enclosure()},
    )

    result = await run_efuse_action(client, "interconnect", name="Enclosure-01, interconnect 6")

    assert client.patches == [
        (ENC_URI, [{"op": "replace", "path": "/interconnectBays/6/bayPowerState", "value": "E-Fuse"}])
    ]
    assert result["component"] == "ICM"


@pytest.mark.asyncio
async def test_run_efuse_action_flm():
    client = FakeClient(collections={"/rest/enclosures": [_enclosure()]})

    result = await run_efuse_action(client, "flm", enclosure="Enclosure-01", bay=1)

    assert client.patches == [
        (ENC_URI, [{"op": "replace", "path": "/managerBays/1/bayPowerState", "value": "E-Fuse"}])
    ]
    assert result["target"] == "Enclosure-01, frame link module 1"
    assert result["component"] == "FLM"


@pytest.mark.asyncio
async def test_run_efuse_action_flm_requires_bay_and_enclosure():
    client = FakeClient(collections={})

    with pytest.raises(ValueError, match="FLM cycle requires ENCLOSURE and BAY"):
        await run_efuse_action(client, "flm", enclosure="Enclosure-01")


@pytest.mark.asyncio
async def test_run_efuse_action_unsupported_target():
    client = FakeClient(collections={})

    with pytest.raises(ValueError, match="Unsupported eFuse target"):
        await run_efuse_action(client, "bogus", name="x")


def test_parser_efuse_server_by_location_parses():
    from proliant.oneview.cli import _build_parser, _cmd_efuse

    parser = _build_parser()
    args = parser.parse_args(["efuse", "server", "--enclosure", "Enclosure-01", "--bay", "6", "--yes"])

    assert args.func is _cmd_efuse
    assert args.power_target_type == "server"
    assert args.enclosure == "Enclosure-01"
    assert args.bay == 6
    assert args.yes is True


def test_parser_efuse_flm_parses():
    from proliant.oneview.cli import _build_parser, _cmd_efuse

    parser = _build_parser()
    args = parser.parse_args(["efuse", "flm", "Enclosure-01", "1", "--dry-run"])

    assert args.func is _cmd_efuse
    assert args.power_target_type == "flm"
    assert args.enclosure == "Enclosure-01"
    assert args.bay == 1
    assert args.dry_run is True


def test_parser_efuse_profile_parses():
    from proliant.oneview.cli import _build_parser, _cmd_efuse

    parser = _build_parser()
    args = parser.parse_args(["efuse", "profile", "ocp-host-1", "--yes"])

    assert args.func is _cmd_efuse
    assert args.power_target_type == "profile"
    assert args.name == "ocp-host-1"
