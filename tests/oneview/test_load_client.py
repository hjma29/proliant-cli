"""Tests for proliant.oneview.cli._load_client()'s "Connecting..." hint.

A wrong/unreachable OneView appliance host previously left the terminal
looking frozen with zero feedback until the login handshake finally timed
out. _load_client() now wraps the connect/login in a status hint that
disappears the moment we get a real response (success or failure).
"""
from __future__ import annotations

import pytest

import proliant.oneview.cli as oneview_cli


class _FakeStatus:
    def __init__(self, message: str, sink: list[str]):
        sink.append(message)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConsole:
    def __init__(self, sink: list[str]):
        self._sink = sink

    def status(self, message: str):
        return _FakeStatus(message, self._sink)


class _FakeOneViewClient:
    """Stand-in for OneViewClient that records __aenter__/__aexit__ calls."""

    instances: list["_FakeOneViewClient"] = []

    def __init__(self, host, username, password):
        self.host = host
        self.entered = False
        self.exited = False
        type(self).instances.append(self)

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *exc):
        self.exited = True


@pytest.fixture(autouse=True)
def _reset_fake_client_instances():
    _FakeOneViewClient.instances.clear()
    yield
    _FakeOneViewClient.instances.clear()


@pytest.fixture
def patched_load_client(monkeypatch):
    """Patch the config + client lookups _load_client() does internally."""
    monkeypatch.setattr(
        "proliant.oneview.config.load_oneview_config",
        lambda: {"host": "10.0.0.99", "url": "https://10.0.0.99", "username": "u", "password": "p"},
    )
    monkeypatch.setattr("proliant.oneview.client.OneViewClient", _FakeOneViewClient)


@pytest.mark.asyncio
async def test_load_client_shows_connecting_hint(monkeypatch, patched_load_client):
    statuses: list[str] = []
    monkeypatch.setattr(oneview_cli, "get_console", lambda: _FakeConsole(statuses))

    async with oneview_cli._load_client() as client:
        assert client.entered is True

    assert _FakeOneViewClient.instances[0].exited is True
    assert any("Connecting to OneView at 10.0.0.99" in s for s in statuses)


@pytest.mark.asyncio
async def test_load_client_closes_client_even_on_error(monkeypatch, patched_load_client):
    monkeypatch.setattr(oneview_cli, "get_console", lambda: _FakeConsole([]))

    with pytest.raises(RuntimeError):
        async with oneview_cli._load_client() as client:
            raise RuntimeError("boom")

    assert _FakeOneViewClient.instances[0].exited is True
