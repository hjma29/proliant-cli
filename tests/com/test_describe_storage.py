"""Tests for proliant.com.describe._render_storage -- the embedded "Storage"
section shown on `proliant com servers describe`, mirroring the same
RAID-vs-Direct-Attached breakdown `ilo`/`oneview servers describe` show.

Best-effort: fetched via a fresh COMClient session and silently skipped (no
crash, no output) if COM hasn't collected storage inventory for this server
yet, or the request otherwise fails.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

from proliant.com.describe import _render_storage


@asynccontextmanager
async def _fake_client_ctx(client):
    yield client


class TestRenderStorageSkips:
    @pytest.mark.asyncio
    async def test_skips_when_server_id_missing(self, capsys):
        await _render_storage(object(), "")
        assert capsys.readouterr().out == ""

    @pytest.mark.asyncio
    async def test_skips_when_client_construction_fails(self, capsys):
        with patch("proliant.com.describe.COMClient", side_effect=ConnectionError("unreachable")):
            await _render_storage(object(), "server-1")
        assert capsys.readouterr().out == ""

    @pytest.mark.asyncio
    async def test_skips_when_fetch_raises(self, capsys):
        with patch("proliant.com.describe.COMClient", return_value=_fake_client_ctx(object())), \
             patch("proliant.com.storage.fetch_storage_report_data",
                   AsyncMock(side_effect=RuntimeError("inventory not collected yet"))):
            await _render_storage(object(), "server-1")
        assert capsys.readouterr().out == ""

    @pytest.mark.asyncio
    async def test_skips_when_report_is_empty(self, capsys):
        with patch("proliant.com.describe.COMClient", return_value=_fake_client_ctx(object())), \
             patch("proliant.com.storage.fetch_storage_report_data", AsyncMock(return_value=[])):
            await _render_storage(object(), "server-1")
        assert capsys.readouterr().out == ""


class TestRenderStorageRenders:
    @pytest.mark.asyncio
    async def test_renders_raid_and_direct_attached_disks(self, capsys):
        report = [
            {
                "controller": "HPE MR408i-o Gen11",
                "firmware": "52.36.3-6584",
                "attach_mode": "RAID Controller",
                "drives": [
                    {"id": "0", "name": "N/A", "bay": "Slot=14:Port=1:Box=3:Bay=4",
                     "capacity_gib": 2981, "capacity_gb": 3200, "media_type": "SSD",
                     "protocol": "NVMe", "type": "NVMe", "model": "MO0032…",
                     "serial": "SN1", "part_number": "PN1", "firmware": "GPK8",
                     "health": "OK"},
                ],
            },
        ]
        with patch("proliant.com.describe.COMClient", return_value=_fake_client_ctx(object())), \
             patch("proliant.com.storage.fetch_storage_report_data", AsyncMock(return_value=report)):
            await _render_storage(object(), "server-1")
        out = capsys.readouterr().out
        assert "Storage" in out
        assert "HPE MR408i-o Gen11" in out
        assert "RAID Controller" in out
