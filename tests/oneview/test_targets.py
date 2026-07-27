"""Tests for shared OneView name-lookup helpers in proliant.oneview.targets."""

from __future__ import annotations

import pytest

from proliant.oneview.targets import get_interconnect, get_server, normalize_name


@pytest.mark.parametrize(
    "a, b",
    [
        ("Enclosure-01,bay 7", "Enclosure-01, bay 7"),
        ("enclosure-01, BAY 7", "Enclosure-01, bay 7"),
        ("Enclosure-01,  bay   7", "Enclosure-01, bay 7"),
        ("  Enclosure-01, bay 7  ", "Enclosure-01, bay 7"),
    ],
)
def test_normalize_name_matches_despite_comma_spacing_case_and_whitespace(a, b):
    assert normalize_name(a) == normalize_name(b)


def test_normalize_name_does_not_falsely_match_different_bays():
    assert normalize_name("Enclosure-01,bay 7") != normalize_name("Enclosure-01, bay 2")


class _FakeClient:
    def __init__(self, items: list[dict]):
        self._items = items

    async def get_all(self, _path):
        return self._items


@pytest.mark.asyncio
async def test_get_server_matches_name_missing_space_after_comma():
    client = _FakeClient([
        {"name": "Enclosure-01, bay 7", "uri": "/rest/server-hardware/7"},
        {"name": "Enclosure-01, bay 2", "uri": "/rest/server-hardware/2"},
    ])
    server = await get_server(client, "Enclosure-01,bay 7")
    assert server["uri"] == "/rest/server-hardware/7"


@pytest.mark.asyncio
async def test_get_interconnect_matches_name_missing_space_after_comma():
    client = _FakeClient([
        {"name": "Enclosure-01, Interconnect 3", "uri": "/rest/interconnects/3"},
    ])
    ic = await get_interconnect(client, "Enclosure-01,Interconnect 3")
    assert ic["uri"] == "/rest/interconnects/3"
