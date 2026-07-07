"""Tests for proliant.com.login workspace-refresh helpers.

Covers the fix for: a workspace created/joined in GreenLake *after* the last
'proliant com login' didn't show up in 'com workspaces list' or
'com workspace use <name>' because both relied solely on the workspace list
cached in token.json at login time.
"""
from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from proliant.com import login as login_mod
from proliant.com.auth import CredentialsError


WS_CACHED = {"platform_customer_id": "ws-1", "company_name": "default-ws", "account_status": "ACTIVE"}
WS_NEW    = {"platform_customer_id": "ws-2", "company_name": "hj-tes1",   "account_status": "ACTIVE"}


def _write_token(tmp_path, monkeypatch, workspaces=None, id_token="idtok", access_token="acc"):
    token_path = tmp_path / "token.json"
    monkeypatch.setattr(login_mod, "TOKEN_CACHE", token_path)
    payload = {
        "access_token": access_token,
        "refresh_token": "reftok",
        "id_token": id_token,
        "expires_at": time.time() + 3600,
        "region": "us-west",
        "workspace_id": "ws-1",
        "workspace_name": "default-ws",
        "ccs_session": "cookie",
        "workspaces": workspaces if workspaces is not None else [WS_CACHED],
    }
    token_path.write_text(json.dumps(payload))
    return token_path


class TestMatchWorkspace:
    def test_exact_name_match(self):
        assert login_mod._match_workspace([WS_CACHED, WS_NEW], "hj-tes1") == WS_NEW

    def test_case_insensitive_match(self):
        assert login_mod._match_workspace([WS_CACHED, WS_NEW], "HJ-TES1") == WS_NEW

    def test_id_match(self):
        assert login_mod._match_workspace([WS_CACHED, WS_NEW], "ws-2") == WS_NEW

    def test_no_match_returns_none(self):
        assert login_mod._match_workspace([WS_CACHED], "does-not-exist") is None


class TestRefreshWorkspaces:
    @pytest.mark.asyncio
    async def test_refresh_updates_cached_workspace_list(self, tmp_path, monkeypatch):
        _write_token(tmp_path, monkeypatch, workspaces=[WS_CACHED])

        with respx.mock:
            respx.post(f"{login_mod.USER_API_BASE}/authn/v1/session").mock(
                return_value=httpx.Response(200, json={"accounts": [WS_CACHED, WS_NEW]})
            )
            result = await login_mod.refresh_workspaces()

        names = [w["company_name"] for w in result]
        assert "hj-tes1" in names

        saved = json.loads(login_mod.TOKEN_CACHE.read_text())
        saved_names = [w["company_name"] for w in saved["workspaces"]]
        assert "hj-tes1" in saved_names
        # active workspace untouched by a plain refresh
        assert saved["workspace_id"] == "ws-1"

    @pytest.mark.asyncio
    async def test_refresh_requires_user_login_session(self, tmp_path, monkeypatch):
        """A pure --api-client / GLP client-credentials session has no id_token
        and cannot be refreshed live -- must raise a clear error."""
        _write_token(tmp_path, monkeypatch, id_token="")

        with pytest.raises(CredentialsError, match="user login session"):
            await login_mod.refresh_workspaces()

    @pytest.mark.asyncio
    async def test_refresh_requires_login(self, tmp_path, monkeypatch):
        monkeypatch.setattr(login_mod, "TOKEN_CACHE", tmp_path / "missing.json")

        with pytest.raises(CredentialsError, match="Not logged in"):
            await login_mod.refresh_workspaces()


class TestSwitchWorkspace:
    @pytest.mark.asyncio
    async def test_switch_to_cached_workspace_no_refresh_needed(self, tmp_path, monkeypatch):
        _write_token(tmp_path, monkeypatch, workspaces=[WS_CACHED, WS_NEW])

        with respx.mock:
            respx.get(f"{login_mod.USER_API_BASE}/authn/v1/session/load-account/ws-2").mock(
                return_value=httpx.Response(200)
            )
            name = await login_mod.switch_workspace("hj-tes1")

        assert name == "hj-tes1"
        saved = json.loads(login_mod.TOKEN_CACHE.read_text())
        assert saved["workspace_id"] == "ws-2"

    @pytest.mark.asyncio
    async def test_switch_to_new_workspace_triggers_live_refresh(self, tmp_path, monkeypatch):
        """Regression test: a workspace created after the last login isn't in
        the cached list -- switch_workspace() must refresh live and retry
        instead of immediately failing."""
        _write_token(tmp_path, monkeypatch, workspaces=[WS_CACHED])  # hj-tes1 NOT cached yet

        with respx.mock:
            respx.post(f"{login_mod.USER_API_BASE}/authn/v1/session").mock(
                return_value=httpx.Response(200, json={"accounts": [WS_CACHED, WS_NEW]})
            )
            respx.get(f"{login_mod.USER_API_BASE}/authn/v1/session/load-account/ws-2").mock(
                return_value=httpx.Response(200)
            )
            name = await login_mod.switch_workspace("hj-tes1")

        assert name == "hj-tes1"
        saved = json.loads(login_mod.TOKEN_CACHE.read_text())
        assert saved["workspace_id"] == "ws-2"
        # the refreshed workspace list must survive the final write-back
        assert any(w["company_name"] == "hj-tes1" for w in saved["workspaces"])

    @pytest.mark.asyncio
    async def test_switch_still_not_found_after_refresh_raises_with_fresh_names(self, tmp_path, monkeypatch):
        _write_token(tmp_path, monkeypatch, workspaces=[WS_CACHED])

        with respx.mock:
            respx.post(f"{login_mod.USER_API_BASE}/authn/v1/session").mock(
                return_value=httpx.Response(200, json={"accounts": [WS_CACHED, WS_NEW]})
            )
            with pytest.raises(ValueError, match="not found"):
                await login_mod.switch_workspace("totally-unknown-workspace")

    @pytest.mark.asyncio
    async def test_switch_no_cached_workspaces_raises(self, tmp_path, monkeypatch):
        _write_token(tmp_path, monkeypatch, workspaces=[])

        with pytest.raises(CredentialsError, match="No workspace list cached"):
            await login_mod.switch_workspace("anything")
