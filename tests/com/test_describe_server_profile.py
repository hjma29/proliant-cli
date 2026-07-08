"""Tests for proliant.com.describe._render_server_profile -- the OneView
"Server Profile" panel shown for OneView-managed servers on `com servers
describe`.

This connects directly to the local OneView appliance (via inventory.ini,
not through COM) to fetch profile status/virtual-identity/connections that
COM itself doesn't expose. Matched by hardware serial number (COM's
`oneview.name` field is actually the hardware's OneView name/bay label, not
the profile name, so it can't be used to look up the profile directly).
All network calls are mocked.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

from proliant.com.describe import _render_server_profile


@asynccontextmanager
async def _fake_client_ctx(client):
    yield client


class TestRenderServerProfileSkips:
    """Best-effort: silently skip (no crash, no output) whenever the
    OneView side of the lookup can't be completed."""

    @pytest.mark.asyncio
    async def test_skips_when_serial_missing(self, capsys):
        await _render_server_profile({}, {}, "")
        assert capsys.readouterr().out == ""

    @pytest.mark.asyncio
    async def test_skips_when_serial_is_placeholder(self, capsys):
        await _render_server_profile({}, {}, "—")
        assert capsys.readouterr().out == ""

    @pytest.mark.asyncio
    async def test_skips_when_no_local_appliance_configured(self, capsys):
        with patch("proliant.oneview.config.list_oneview_appliances", return_value=[]):
            await _render_server_profile({}, {}, "3M1D0P12KD")
        assert capsys.readouterr().out == ""

    @pytest.mark.asyncio
    async def test_skips_when_appliance_unreachable(self, capsys):
        appliances = [{"name": "oneview", "host": "10.1.1.1",
                       "username": "Administrator", "password": "pw"}]
        with patch("proliant.oneview.config.list_oneview_appliances", return_value=appliances), \
             patch("proliant.oneview.client.OneViewClient", side_effect=ConnectionError("unreachable")):
            await _render_server_profile({}, {}, "3M1D0P12KD")
        assert capsys.readouterr().out == ""

    @pytest.mark.asyncio
    async def test_skips_when_no_hardware_matches_serial(self, capsys):
        appliances = [{"name": "oneview", "host": "10.1.1.1",
                       "username": "Administrator", "password": "pw"}]
        with patch("proliant.oneview.config.list_oneview_appliances", return_value=appliances), \
             patch("proliant.oneview.client.OneViewClient", return_value=_fake_client_ctx(object())), \
             patch("proliant.oneview.profiles.describe_profile_by_serial",
                   AsyncMock(side_effect=ValueError("No OneView server hardware found with serial '3M1D0P12KD'"))):
            await _render_server_profile({}, {}, "3M1D0P12KD")
        assert capsys.readouterr().out == ""


class TestRenderServerProfileRenders:
    @pytest.mark.asyncio
    async def test_renders_status_identity_and_connections(self, capsys):
        appliances = [{"name": "oneview", "host": "10.1.1.1",
                       "username": "Administrator", "password": "pw"}]
        profile = {
            "status": "OK",
            "name": "HyperV-04",
            "state": "Normal",
            "serial_number": "VCGXK9400Q",
            "uuid": "201667e5-3c4b-49fd-b2de-85fd99414921",
            "connections": [
                {"id": 1, "name": "ETH0", "function_type": "Ethernet",
                 "mac": "92:9F:9F:60:00:58", "wwnn": "", "wwpn": ""},
                {"id": 3, "name": "SAN-A", "function_type": "FibreChannel",
                 "mac": "", "wwnn": "20:00:9F:9F:60:00:7D", "wwpn": "92:9F:9F:60:00:7D"},
            ],
        }
        with patch("proliant.oneview.config.list_oneview_appliances", return_value=appliances), \
             patch("proliant.oneview.client.OneViewClient", return_value=_fake_client_ctx(object())), \
             patch("proliant.oneview.profiles.describe_profile_by_serial", AsyncMock(return_value=profile)):
            await _render_server_profile({}, {}, "3M1D0P12KD")

        out = capsys.readouterr().out
        assert "Server Profile" in out
        assert "HyperV-04" in out
        assert "Normal" in out
        assert "VCGXK9400Q" in out
        assert "201667e5-3c4b-49fd-b2de-85fd99414921" in out
        assert "Connections" in out
        assert "ETH0" in out
        assert "92:9F:9F:60:00:58" in out
        assert "SAN-A" in out
        assert "FibreChannel" in out
        assert "92:9F:9F:60:00:7D" in out  # falls back to wwpn when mac is empty

    @pytest.mark.asyncio
    async def test_picks_matching_appliance_by_hostname_when_multiple_configured(self, capsys):
        appliances = [
            {"name": "dc-a", "host": "oneview-a.example.com",
             "username": "Administrator", "password": "pw"},
            {"name": "dc-b", "host": "oneview-b.example.com",
             "username": "Administrator", "password": "pw"},
        ]
        profile = {"status": "OK", "name": "HyperV-04", "state": "Normal",
                   "serial_number": "—", "uuid": "—", "connections": []}
        appliance_map = {"appl-2": "oneview-b.example.com"}
        server_appliance = {"applianceId": "appl-2"}

        captured_hosts = []

        class _RecordingOneViewClient:
            def __init__(self, host, username, password):
                captured_hosts.append(host)

            async def __aenter__(self):
                return object()

            async def __aexit__(self, *exc):
                return False

        with patch("proliant.oneview.config.list_oneview_appliances", return_value=appliances), \
             patch("proliant.oneview.client.OneViewClient", _RecordingOneViewClient), \
             patch("proliant.oneview.profiles.describe_profile_by_serial", AsyncMock(return_value=profile)):
            await _render_server_profile(appliance_map, server_appliance, "3M1D0P12KD")

        assert captured_hosts == ["oneview-b.example.com"]
