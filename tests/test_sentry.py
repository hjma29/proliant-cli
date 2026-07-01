"""Tests for Sentry event filtering and scrubbing."""

from __future__ import annotations

import httpx

from proliant.cli import _sentry_scrub


def _event(exc_type: str, value: str) -> dict:
    return {"exception": {"values": [{"type": exc_type, "value": value}]}}


def test_sentry_drops_network_timeout_by_event_type():
    event = _event("ConnectTimeout", "timed out connecting to 10.0.0.10")

    assert _sentry_scrub(event, {}) is None


def test_sentry_drops_wrong_password_messages():
    event = _event("RuntimeError", "POST /redfish/v1/SessionService/Sessions failed - HTTP 401: check username/password")

    assert _sentry_scrub(event, {}) is None


def test_sentry_drops_httpx_4xx_status_from_hint():
    request = httpx.Request("GET", "https://oneview.example/rest/server-hardware")
    response = httpx.Response(403, request=request)
    exc = httpx.HTTPStatusError("forbidden", request=request, response=response)
    event = _event("HTTPStatusError", "forbidden")

    assert _sentry_scrub(event, {"exc_info": (type(exc), exc, None)}) is None


def test_sentry_keeps_and_scrubs_unexpected_bugs():
    event = _event("KeyError", "failed on 10.1.2.3 with password=secret")

    result = _sentry_scrub(event, {})

    assert result is event
    assert event["exception"]["values"][0]["value"] == "failed on <ip> with password=<redacted>"