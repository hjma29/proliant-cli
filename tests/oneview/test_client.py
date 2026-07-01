"""Tests for OneView client error handling."""

from __future__ import annotations

import httpx
import pytest

from proliant.oneview.client import OneViewClient, OneViewError


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