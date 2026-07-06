"""Tests for proliant.ilo.client — timeouts, exception handling, connect hint.

Covers the fixes for iLO/OneView "getting stuck" on a wrong/unreachable
host: a fast connect timeout, a ServerDownOrUnreachableError alias broad
enough to catch connect timeouts (not just refused connections), the
request()-timeout-forwarding bug (an explicit ``timeout=None`` used to
disable timeouts entirely instead of falling back to the client default),
and the opt-in "Connecting to..." status hint on ilo_session().
"""
from __future__ import annotations

import httpx
import pytest

from proliant.ilo.client import (
    ILOClient,
    ServerDownOrUnreachableError,
    _CONNECT_TIMEOUT,
    ilo_session,
)


def test_server_down_error_is_transport_error_alias():
    """Must alias httpx.TransportError, not the narrower httpx.ConnectError."""
    assert ServerDownOrUnreachableError is httpx.TransportError


def test_server_down_error_catches_connect_timeout():
    """ConnectTimeout is a sibling of ConnectError, not a subclass -- both
    must be caught since either can fire against a wrong/unreachable host."""
    request = httpx.Request("GET", "https://ilo.example/redfish/v1/")
    exc = httpx.ConnectTimeout("timed out", request=request)
    assert isinstance(exc, ServerDownOrUnreachableError)


def test_server_down_error_catches_connect_error():
    request = httpx.Request("GET", "https://ilo.example/redfish/v1/")
    exc = httpx.ConnectError("refused", request=request)
    assert isinstance(exc, ServerDownOrUnreachableError)


def test_server_down_error_does_not_catch_http_status_error():
    """HTTP 4xx/5xx means the server WAS reached -- must not be swallowed
    as a transport/unreachable-host failure."""
    request = httpx.Request("GET", "https://ilo.example/redfish/v1/")
    response = httpx.Response(500, request=request)
    exc = httpx.HTTPStatusError("boom", request=request, response=response)
    assert not isinstance(exc, ServerDownOrUnreachableError)


class _RecordingHttp:
    """Fake httpx.AsyncClient standing in for ILOClient._http in tests."""

    def __init__(self, response: httpx.Response):
        self.calls: list[dict] = []
        self._response = response

    async def request(self, method, uri, **kwargs):
        self.calls.append(kwargs)
        return self._response


def _make_ok_response() -> httpx.Response:
    request = httpx.Request("GET", "https://ilo.example/redfish/v1/Systems/1")
    return httpx.Response(200, request=request, json={"ok": True})


@pytest.mark.asyncio
async def test_request_omits_timeout_kwarg_when_none():
    """timeout=None must NOT be forwarded to httpx -- an explicit None means
    "disable timeout entirely" in httpx, not "use the client default"."""
    client = ILOClient("https://ilo.example", "user", "pass")
    fake_http = _RecordingHttp(_make_ok_response())
    client._http = fake_http  # type: ignore[assignment]
    client._token = "tok"

    await client.request("GET", "/redfish/v1/Systems/1")

    assert "timeout" not in fake_http.calls[0]


@pytest.mark.asyncio
async def test_request_forwards_explicit_timeout_override():
    client = ILOClient("https://ilo.example", "user", "pass")
    fake_http = _RecordingHttp(_make_ok_response())
    client._http = fake_http  # type: ignore[assignment]
    client._token = "tok"

    await client.request("GET", "/redfish/v1/Systems/1", timeout=_CONNECT_TIMEOUT)

    assert fake_http.calls[0]["timeout"] is _CONNECT_TIMEOUT


@pytest.mark.asyncio
async def test_get_forwards_timeout_through_to_request():
    client = ILOClient("https://ilo.example", "user", "pass")
    fake_http = _RecordingHttp(_make_ok_response())
    client._http = fake_http  # type: ignore[assignment]
    client._token = "tok"

    await client.get("/redfish/v1/Systems/1", timeout=_CONNECT_TIMEOUT)

    assert fake_http.calls[0]["timeout"] is _CONNECT_TIMEOUT


class _FakeStatus:
    """Rich Console.status() stand-in: records the message, no-op context manager."""

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


def _host(name="srv1", url="https://10.0.0.5"):
    return {"name": name, "url": url, "username": "user", "password": "pass"}


@pytest.mark.asyncio
async def test_ilo_session_show_hint_true_displays_connecting_status(monkeypatch):
    entered = {"count": 0}

    async def fake_aenter(self):
        entered["count"] += 1
        return self

    async def fake_aexit(self, *exc):
        return None

    monkeypatch.setattr(ILOClient, "__aenter__", fake_aenter)
    monkeypatch.setattr(ILOClient, "__aexit__", fake_aexit)

    statuses: list[str] = []
    monkeypatch.setattr(
        "proliant.common.display.get_console", lambda: _FakeConsole(statuses)
    )

    async with ilo_session(_host(), show_hint=True) as c:
        assert entered["count"] == 1

    assert any("Connecting to srv1" in s for s in statuses)


@pytest.mark.asyncio
async def test_ilo_session_show_hint_false_stays_silent_by_default(monkeypatch):
    async def fake_aenter(self):
        return self

    async def fake_aexit(self, *exc):
        return None

    monkeypatch.setattr(ILOClient, "__aenter__", fake_aenter)
    monkeypatch.setattr(ILOClient, "__aexit__", fake_aexit)

    def _boom():
        raise AssertionError("get_console() must not be called when show_hint=False")

    monkeypatch.setattr("proliant.common.display.get_console", _boom)

    async with ilo_session(_host()) as c:
        pass  # no assertion needed -- reaching here means get_console() was never called
