"""Tests for OneView client error handling."""

from __future__ import annotations

import httpx
import pytest

from proliant.oneview.client import OneViewClient, OneViewError, _CONNECT_TIMEOUT


class TimeoutHttp:
    async def get(self, *args, **kwargs):
        request = httpx.Request("GET", "https://oneview.example/rest/server-hardware")
        raise httpx.ConnectTimeout("timed out", request=request)


@pytest.mark.asyncio
async def test_get_wraps_request_timeout_as_oneview_error():
    client = OneViewClient("oneview.example", "user", "password")
    client._http = TimeoutHttp()  # type: ignore[assignment]

    with pytest.raises(OneViewError, match="Cannot reach OneView appliance"):
        await client.get("/rest/server-hardware")


class _RecordingHttp:
    """Fake httpx.AsyncClient standing in for OneViewClient._http in tests."""

    def __init__(self, version_response: httpx.Response, login_response: httpx.Response):
        self.get_calls: list[dict] = []
        self.post_calls: list[dict] = []
        self._version_response = version_response
        self._login_response = login_response

    async def get(self, uri, **kwargs):
        self.get_calls.append(kwargs)
        return self._version_response

    async def post(self, uri, **kwargs):
        self.post_calls.append(kwargs)
        return self._login_response

    async def aclose(self):
        pass


def _version_ok_response() -> httpx.Response:
    request = httpx.Request("GET", "https://oneview.example/rest/version")
    return httpx.Response(200, request=request, json={"currentVersion": 4400})


def _login_ok_response() -> httpx.Response:
    request = httpx.Request("POST", "https://oneview.example/rest/login-sessions")
    return httpx.Response(200, request=request, json={"sessionID": "tok-123"})


@pytest.mark.asyncio
async def test_aenter_uses_connect_timeout_for_version_check(monkeypatch):
    fake_http = _RecordingHttp(_version_ok_response(), _login_ok_response())
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: fake_http)

    client = OneViewClient("oneview.example", "user", "password")
    await client.__aenter__()

    assert fake_http.get_calls[0]["timeout"] is _CONNECT_TIMEOUT


@pytest.mark.asyncio
async def test_aenter_uses_connect_timeout_for_login(monkeypatch):
    fake_http = _RecordingHttp(_version_ok_response(), _login_ok_response())
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: fake_http)

    client = OneViewClient("oneview.example", "user", "password")
    await client.__aenter__()

    assert fake_http.post_calls[0]["timeout"] is _CONNECT_TIMEOUT


class _AsyncOpHttp:
    """Fake httpx client returning a canned response for patch/put."""

    def __init__(self, response: httpx.Response):
        self._response = response

    async def patch(self, uri, **kwargs):
        return self._response

    async def put(self, uri, **kwargs):
        return self._response


@pytest.mark.asyncio
async def test_patch_promotes_location_task_uri_when_body_has_none():
    # OneView replies 202 with the monitoring task in the Location header and
    # an empty body — the client must surface that task URI so callers can poll.
    request = httpx.Request("PATCH", "https://oneview.example/rest/logical-enclosures/le-1")
    resp = httpx.Response(
        202, request=request, content=b"",
        headers={"Location": "https://oneview.example/rest/tasks/abc-123"},
    )
    client = OneViewClient("oneview.example", "user", "password")
    client._http = _AsyncOpHttp(resp)  # type: ignore[assignment]

    body = await client.patch("/rest/logical-enclosures/le-1", [], headers={"If-Match": "*"})
    assert body["uri"] == "/rest/tasks/abc-123"


@pytest.mark.asyncio
async def test_put_prefers_body_task_uri_over_location():
    # If the body already carries a task URI, keep it (don't clobber with Location).
    request = httpx.Request("PUT", "https://oneview.example/rest/server-profiles/sp-1")
    resp = httpx.Response(
        202, request=request, json={"uri": "/rest/tasks/body-task", "taskState": "Running"},
        headers={"Location": "https://oneview.example/rest/tasks/header-task"},
    )
    client = OneViewClient("oneview.example", "user", "password")
    client._http = _AsyncOpHttp(resp)  # type: ignore[assignment]

    body = await client.put("/rest/server-profiles/sp-1", {})
    assert body["uri"] == "/rest/tasks/body-task"


@pytest.mark.asyncio
async def test_put_leaves_body_untouched_when_no_task_location():
    # Synchronous 200 with a non-task Location (or none) must not be rewritten.
    request = httpx.Request("PUT", "https://oneview.example/rest/server-profiles/sp-1")
    resp = httpx.Response(
        200, request=request, json={"uri": "/rest/server-profiles/sp-1", "name": "sp1"},
    )
    client = OneViewClient("oneview.example", "user", "password")
    client._http = _AsyncOpHttp(resp)  # type: ignore[assignment]

    body = await client.put("/rest/server-profiles/sp-1", {})
    assert body["uri"] == "/rest/server-profiles/sp-1"
