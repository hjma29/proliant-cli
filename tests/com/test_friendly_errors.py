"""Tests for friendly httpx error surfacing in 'proliant com'.

Covers:
  - proliant.com.client.friendly_http_error(): 403 gets GUI-matching wording
    with no raw MDN URL, other statuses get a clean generic message, non-HTTP
    exceptions fall back to str(e).
  - CLI handlers print the friendly message (not httpx's default text) and
    exit(1) instead of leaking a traceback, including the three handlers
    (_cmd_report_gpu, _cmd_report_memory, _cmd_describe_server) that
    previously had no error handling at all.
"""
from __future__ import annotations

import argparse
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from proliant.com.client import friendly_http_error


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://example.invalid/v1/servers")
    response = httpx.Response(status, request=request)
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        return e
    raise AssertionError("expected raise_for_status to raise")


class TestFriendlyHttpError:
    def test_403_matches_greenlake_gui_wording_no_url(self):
        msg = friendly_http_error(_http_error(403))
        assert "role assigned for Compute Ops Management" in msg
        assert "403" in msg
        assert "developer.mozilla.org" not in msg

    def test_other_status_is_clean_generic_message(self):
        msg = friendly_http_error(_http_error(500))
        assert "500" in msg
        assert "developer.mozilla.org" not in msg

    def test_non_http_exception_falls_back_to_str(self):
        msg = friendly_http_error(ValueError("boom"))
        assert msg == "boom"


class TestCliHandlersSurfaceFriendlyMessage:
    """Handlers must print the friendly message and exit(1), not raise."""

    @pytest.mark.asyncio
    async def test_show_devices_403_is_friendly(self, capsys):
        from proliant.com import cli

        args = argparse.Namespace(
            command="servers", fields=None, sort_by=None, filter_text=None,
            filter_model=None,
        )
        fake_session = MagicMock()
        with patch("proliant.com.cli._ensure_session", new_callable=AsyncMock,
                   return_value=fake_session), \
             patch.object(cli._servers_mod, "fetch_servers", new_callable=AsyncMock,
                   side_effect=_http_error(403)):
            with pytest.raises(SystemExit) as exc_info:
                await cli._cmd_show_devices(args)

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "role assigned for Compute Ops Management" in captured.out
        assert "developer.mozilla.org" not in captured.out

    @pytest.mark.asyncio
    async def test_report_gpu_403_no_longer_raises_uncaught(self, capsys):
        """Previously unguarded — a raw HTTPStatusError would propagate."""
        from proliant.com import cli

        args = argparse.Namespace()
        fake_session = MagicMock()
        with patch("proliant.com.cli._ensure_session", new_callable=AsyncMock,
                   return_value=fake_session), \
             patch("proliant.com.cli._run_report_gpu", new_callable=AsyncMock,
                   side_effect=_http_error(403)):
            with pytest.raises(SystemExit) as exc_info:
                await cli._cmd_report_gpu(args)

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "role assigned for Compute Ops Management" in captured.out

    @pytest.mark.asyncio
    async def test_report_memory_403_no_longer_raises_uncaught(self, capsys):
        from proliant.com import cli

        args = argparse.Namespace()
        fake_session = MagicMock()
        with patch("proliant.com.cli._ensure_session", new_callable=AsyncMock,
                   return_value=fake_session), \
             patch("proliant.com.cli._run_report_memory", new_callable=AsyncMock,
                   side_effect=_http_error(403)):
            with pytest.raises(SystemExit) as exc_info:
                await cli._cmd_report_memory(args)

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "role assigned for Compute Ops Management" in captured.out

    @pytest.mark.asyncio
    async def test_describe_server_403_no_longer_raises_uncaught(self, capsys):
        from proliant.com import cli

        args = argparse.Namespace(server="dl380-gen11")
        fake_session = MagicMock()
        with patch("proliant.com.cli._ensure_session", new_callable=AsyncMock,
                   return_value=fake_session), \
             patch("proliant.com.cli._run_describe", new_callable=AsyncMock,
                   side_effect=_http_error(403)):
            with pytest.raises(SystemExit) as exc_info:
                await cli._cmd_describe_server(args)

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "role assigned for Compute Ops Management" in captured.out
