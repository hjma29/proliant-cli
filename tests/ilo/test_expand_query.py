"""Tests for the Redfish ``$expand=.`` optimisation in ``_member_resources``.

Behaviour verified live against a DL380 Gen11 (iLO 6, Redfish 1.20.0) and a
DL345 Gen12 (iLO 7, Redfish 1.22.1). Both advertise::

    ProtocolFeaturesSupported.ExpandQuery =
        {"ExpandAll": False, "Levels": True, "Links": False,
         "MaxLevels": 1, "NoLinks": True}

so only the ``$expand=.`` (NoLinks) form is usable -- ``$expand=*`` and
``$levels > 1`` return HTTP 400 and must never be sent.

Measured on that hardware, using $expand cut ``fetch_all_firmware`` from 30
GETs to 2. Concurrency is deliberately NOT used as the fallback: iLO speaks
only HTTP/1.1, and firing the member GETs in parallel measured the same or
slower (0.62-0.95x at 8-way) and could drop the connection.
"""

from __future__ import annotations

import pytest

from proliant.ilo import inventory


COLLECTION = "/redfish/v1/Chassis/1/Widgets"


class ExpandClient:
    """Client stub that serves an expanded collection in one GET."""

    def __init__(self, *, supports: bool, expanded: bool = True):
        self._supports = supports
        self._expanded = expanded
        self.uris: list[str] = []

    async def supports_expand(self) -> bool:
        return self._supports

    async def get(self, uri: str) -> dict:
        self.uris.append(uri)
        if "$expand" in uri:
            if self._expanded:
                return {"Members": [
                    {"@odata.id": f"{COLLECTION}/1", "Name": "A", "Version": "1"},
                    {"@odata.id": f"{COLLECTION}/2", "Name": "B", "Version": "2"},
                ]}
            # $expand advertised but silently ignored -- bare links only
            return {"Members": [
                {"@odata.id": f"{COLLECTION}/1"},
                {"@odata.id": f"{COLLECTION}/2"},
            ]}
        if uri == COLLECTION:
            return {"Members": [
                {"@odata.id": f"{COLLECTION}/1"},
                {"@odata.id": f"{COLLECTION}/2"},
            ]}
        idx = uri.rsplit("/", 1)[-1]
        return {"@odata.id": uri, "Name": "AB"[int(idx) - 1], "Version": idx}


class NoProbeClient(ExpandClient):
    """Client-like object that predates the capability probe entirely."""

    supports_expand = None  # type: ignore[assignment]


@pytest.mark.asyncio
async def test_expand_collapses_collection_to_single_request():
    client = ExpandClient(supports=True)
    rows = await inventory._member_resources(client, COLLECTION)

    assert [r["Name"] for r in rows] == ["A", "B"]
    # One request total -- no collection GET, no per-member GETs
    assert client.uris == [f"{COLLECTION}?$expand=."]


@pytest.mark.asyncio
async def test_sequential_walk_when_expand_unsupported():
    client = ExpandClient(supports=False)
    rows = await inventory._member_resources(client, COLLECTION)

    assert [r["Name"] for r in rows] == ["A", "B"]
    assert not any("$expand" in u for u in client.uris)
    assert client.uris == [COLLECTION, f"{COLLECTION}/1", f"{COLLECTION}/2"]


@pytest.mark.asyncio
async def test_ignored_expand_reuses_member_list_without_refetch():
    """A service that advertises $expand but ignores it must not cost extra.

    The unexpanded response is still a valid member list, so the walk reuses
    it rather than re-fetching the collection.
    """
    client = ExpandClient(supports=True, expanded=False)
    rows = await inventory._member_resources(client, COLLECTION)

    assert [r["Name"] for r in rows] == ["A", "B"]
    assert client.uris == [
        f"{COLLECTION}?$expand=.",
        f"{COLLECTION}/1",
        f"{COLLECTION}/2",
    ]
    assert client.uris.count(COLLECTION) == 0  # no wasted re-fetch


@pytest.mark.asyncio
async def test_expand_failure_falls_back_to_sequential_walk():
    class Boom(ExpandClient):
        async def get(self, uri: str) -> dict:
            if "$expand" in uri:
                self.uris.append(uri)
                raise RuntimeError("HTTP 400 — query not supported")
            return await super().get(uri)

    client = Boom(supports=True)
    rows = await inventory._member_resources(client, COLLECTION)

    assert [r["Name"] for r in rows] == ["A", "B"]
    assert client.uris[0] == f"{COLLECTION}?$expand=."
    assert COLLECTION in client.uris  # fell back to the normal walk


@pytest.mark.asyncio
async def test_client_without_capability_probe_still_works():
    """supports_expand is an optional hint, never a required interface."""
    client = NoProbeClient(supports=False)
    rows = await inventory._member_resources(client, COLLECTION)

    assert [r["Name"] for r in rows] == ["A", "B"]
    assert not any("$expand" in u for u in client.uris)


@pytest.mark.asyncio
async def test_expand_appended_with_ampersand_when_uri_has_query():
    client = ExpandClient(supports=True)
    await inventory._member_resources(client, f"{COLLECTION}?foo=bar")

    assert client.uris == [f"{COLLECTION}?foo=bar&$expand=."]


class RootClient:
    """Minimal stub exercising ILOClient.supports_expand against a root doc."""

    def __init__(self, root: dict):
        self._root = root
        self.gets = 0

    async def get(self, uri: str) -> dict:
        self.gets += 1
        return self._root


@pytest.mark.parametrize(
    ("expand_query", "expected"),
    [
        ({"NoLinks": True, "ExpandAll": False, "MaxLevels": 1}, True),   # iLO 6/7 live
        ({"NoLinks": False, "ExpandAll": False}, False),
        ({}, False),
    ],
)
@pytest.mark.asyncio
async def test_supports_expand_reads_protocol_features(expand_query, expected):
    from proliant.ilo.client import ILOClient

    client = ILOClient("https://ilo.example", "u", "p")
    client._root = {"ProtocolFeaturesSupported": {"ExpandQuery": expand_query}}

    assert await client.supports_expand() is expected


@pytest.mark.asyncio
async def test_supports_expand_false_when_root_lacks_the_field():
    from proliant.ilo.client import ILOClient

    client = ILOClient("https://ilo.example", "u", "p")
    client._root = {"RedfishVersion": "1.20.0"}

    assert await client.supports_expand() is False


@pytest.mark.asyncio
async def test_supports_expand_uses_cached_root_without_extra_request():
    """The root doc is already cached, so the probe must cost no round trip."""
    from proliant.ilo.client import ILOClient

    client = ILOClient("https://ilo.example", "u", "p")
    client._root = {"ProtocolFeaturesSupported": {"ExpandQuery": {"NoLinks": True}}}

    async def fail_get(*_a, **_kw):  # pragma: no cover - must never run
        raise AssertionError("supports_expand must not issue a request")

    client.get = fail_get  # type: ignore[method-assign]
    assert await client.supports_expand() is True
