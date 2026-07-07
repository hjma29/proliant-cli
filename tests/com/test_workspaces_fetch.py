"""Tests for proliant.com.workspaces.fetch_workspaces() live-refresh behavior.

Covers the fix for: a workspace created/joined in GreenLake *after* the last
'proliant com login' didn't show up in 'com workspaces list' because the
function only ever consulted the workspace list cached in token.json.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from proliant.com.workspaces import fetch_workspaces

WS_CACHED = {"platform_customer_id": "ws-1", "company_name": "default-ws", "account_status": "ACTIVE"}
WS_NEW    = {"platform_customer_id": "ws-2", "company_name": "hj-tes1",   "account_status": "ACTIVE"}


def _fake_session(user_token=False, workspace_id="ws-1"):
    sess = MagicMock()
    sess._user_token = user_token
    sess._workspace_id = workspace_id
    sess._workspace_name = "default-ws"
    sess.region = "us-west"
    return sess


class TestFetchWorkspacesRefresh:
    @pytest.mark.asyncio
    async def test_default_refresh_picks_up_new_workspace(self, monkeypatch):
        session = _fake_session()

        with patch(
            "proliant.com.login.load_token",
            return_value={"id_token": "idtok", "workspaces": [WS_CACHED]},
        ), patch(
            "proliant.com.login.refresh_workspaces",
            new_callable=AsyncMock,
            return_value=[WS_CACHED, WS_NEW],
        ) as mock_refresh:
            result = await fetch_workspaces(session)

        mock_refresh.assert_awaited_once()
        names = [w.name for w in result]
        assert "hj-tes1" in names

    @pytest.mark.asyncio
    async def test_refresh_failure_falls_back_to_cache(self, monkeypatch):
        session = _fake_session()

        with patch(
            "proliant.com.login.load_token",
            return_value={"id_token": "idtok", "workspaces": [WS_CACHED]},
        ), patch(
            "proliant.com.login.refresh_workspaces",
            new_callable=AsyncMock,
            side_effect=RuntimeError("offline"),
        ):
            result = await fetch_workspaces(session)

        assert [w.name for w in result] == ["default-ws"]

    @pytest.mark.asyncio
    async def test_no_id_token_skips_live_refresh(self, monkeypatch):
        """A pure --api-client/GLP session has no id_token -- must not attempt
        a live refresh call at all, just use the cache."""
        session = _fake_session()

        with patch(
            "proliant.com.login.load_token",
            return_value={"workspaces": [WS_CACHED]},  # no id_token
        ), patch(
            "proliant.com.login.refresh_workspaces",
            new_callable=AsyncMock,
        ) as mock_refresh:
            result = await fetch_workspaces(session)

        mock_refresh.assert_not_awaited()
        assert [w.name for w in result] == ["default-ws"]

    @pytest.mark.asyncio
    async def test_refresh_false_uses_cache_only(self, monkeypatch):
        session = _fake_session()

        with patch(
            "proliant.com.login.load_token",
            return_value={"id_token": "idtok", "workspaces": [WS_CACHED]},
        ), patch(
            "proliant.com.login.refresh_workspaces",
            new_callable=AsyncMock,
        ) as mock_refresh:
            result = await fetch_workspaces(session, refresh=False)

        mock_refresh.assert_not_awaited()
        assert [w.name for w in result] == ["default-ws"]

    @pytest.mark.asyncio
    async def test_no_session_no_cache_raises(self):
        session = _fake_session(user_token=False)

        with patch("proliant.com.login.load_token", return_value=None):
            with pytest.raises(ValueError, match="requires a login session"):
                await fetch_workspaces(session)
