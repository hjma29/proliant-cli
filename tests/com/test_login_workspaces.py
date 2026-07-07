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


def _write_token(tmp_path, monkeypatch, workspaces=None, id_token="idtok", access_token="acc",
                  ccs_session="cookie"):
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
        "ccs_session": ccs_session,
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


class TestPickWorkspace:
    """_pick_workspace() interactive numbered/multi-column picker.

    Replaces the old questionary.select arrow-key picker — verifies the
    numbered-choice + partial-name-match flow works without any arrow-key
    navigation dependency.
    """

    @pytest.mark.asyncio
    async def test_single_workspace_auto_selected_no_prompt(self):
        result = await login_mod._pick_workspace([WS_CACHED])
        assert result == WS_CACHED

    @pytest.mark.asyncio
    async def test_explicit_workspace_name_bypasses_prompt(self):
        result = await login_mod._pick_workspace([WS_CACHED, WS_NEW], workspace_name="hj-tes1")
        assert result == WS_NEW

    @pytest.mark.asyncio
    async def test_explicit_unknown_name_raises(self):
        with pytest.raises(login_mod.AuthFlowError):
            await login_mod._pick_workspace([WS_CACHED, WS_NEW], workspace_name="does-not-exist")

    @pytest.mark.asyncio
    async def test_numbered_selection(self):
        with patch("proliant.com.login.Prompt.ask", return_value="2"):
            result = await login_mod._pick_workspace([WS_CACHED, WS_NEW])
        assert result == WS_NEW

    @pytest.mark.asyncio
    async def test_partial_name_selection(self):
        with patch("proliant.com.login.Prompt.ask", return_value="hj-tes1"):
            result = await login_mod._pick_workspace([WS_CACHED, WS_NEW])
        assert result == WS_NEW

    @pytest.mark.asyncio
    async def test_out_of_range_number_reprompts(self):
        with patch("proliant.com.login.Prompt.ask", side_effect=["9", "1"]):
            result = await login_mod._pick_workspace([WS_CACHED, WS_NEW])
        assert result == WS_CACHED

    @pytest.mark.asyncio
    async def test_ambiguous_name_reprompts(self):
        ws_a = {"platform_customer_id": "ws-a", "company_name": "acme-labs", "account_status": "ACTIVE"}
        ws_b = {"platform_customer_id": "ws-b", "company_name": "acme-prod", "account_status": "ACTIVE"}
        with patch("proliant.com.login.Prompt.ask", side_effect=["acme", "acme-labs"]):
            result = await login_mod._pick_workspace([ws_a, ws_b])
        assert result == ws_a

    @pytest.mark.asyncio
    async def test_ctrl_c_falls_back_to_first_workspace(self):
        with patch("proliant.com.login.Prompt.ask", side_effect=KeyboardInterrupt):
            result = await login_mod._pick_workspace([WS_CACHED, WS_NEW])
        assert result == WS_CACHED


class TestMergeWorkspaces:
    def test_dedups_by_platform_customer_id(self):
        merged = login_mod._merge_workspaces([WS_CACHED], [WS_CACHED, WS_NEW])
        ids = [w["platform_customer_id"] for w in merged]
        assert ids == ["ws-1", "ws-2"]

    def test_primary_wins_on_conflict(self):
        stale = {**WS_CACHED, "company_name": "stale-name"}
        fresh = {**WS_CACHED, "company_name": "fresh-name"}
        merged = login_mod._merge_workspaces([stale], [fresh])
        assert merged == [stale]

    def test_empty_extra_returns_primary_unchanged(self):
        assert login_mod._merge_workspaces([WS_CACHED], []) == [WS_CACHED]


class TestFetchListAccounts:
    """Covers the fix for: self-service (IAMv2) workspaces a user creates
    themselves in the GreenLake console (e.g. via 'Create workspace') never
    appear in /authn/v1/session's 'accounts' list -- they only show up via
    GET /accounts/ui/v1/customer/list-accounts. Verified live against the
    real HPE GreenLake API."""

    @pytest.mark.asyncio
    async def test_returns_customers_list(self):
        with respx.mock:
            respx.get(f"{login_mod.USER_API_BASE}/accounts/ui/v1/customer/list-accounts").mock(
                return_value=httpx.Response(200, json={"customers": [WS_NEW]})
            )
            result = await login_mod._fetch_list_accounts("acc", "cookie")

        assert result == [WS_NEW]

    @pytest.mark.asyncio
    async def test_no_ccs_session_returns_empty_without_calling_api(self):
        with respx.mock:
            route = respx.get(f"{login_mod.USER_API_BASE}/accounts/ui/v1/customer/list-accounts")
            result = await login_mod._fetch_list_accounts("acc", "")

        assert result == []
        assert route.call_count == 0

    @pytest.mark.asyncio
    async def test_non_200_returns_empty(self):
        with respx.mock:
            respx.get(f"{login_mod.USER_API_BASE}/accounts/ui/v1/customer/list-accounts").mock(
                return_value=httpx.Response(401, json={"message": "Session not found"})
            )
            result = await login_mod._fetch_list_accounts("acc", "cookie")

        assert result == []

    @pytest.mark.asyncio
    async def test_request_exception_returns_empty(self):
        with respx.mock:
            respx.get(f"{login_mod.USER_API_BASE}/accounts/ui/v1/customer/list-accounts").mock(
                side_effect=httpx.ConnectError("boom")
            )
            result = await login_mod._fetch_list_accounts("acc", "cookie")

        assert result == []


class TestInitWorkspaceSessionMergesListAccounts:
    @pytest.mark.asyncio
    async def test_self_service_workspace_merged_in(self):
        """Regression test: /authn/v1/session alone only returned the 'HPE
        TechEnablement Labs' invited-org account; a self-service workspace
        the user created themselves ('hj-tes1') was missing entirely from
        both the login workspace picker and 'com workspaces list'."""
        with respx.mock:
            respx.post(f"{login_mod.USER_API_BASE}/authn/v1/session").mock(
                return_value=httpx.Response(
                    200,
                    json={"accounts": [WS_CACHED]},
                    headers={"set-cookie": "ccs-session=cookie123; Path=/"},
                )
            )
            respx.get(f"{login_mod.USER_API_BASE}/accounts/ui/v1/customer/list-accounts").mock(
                return_value=httpx.Response(200, json={"customers": [WS_NEW]})
            )
            workspaces, ccs_session = await login_mod._init_workspace_session("acc", "idtok")

        names = [w["company_name"] for w in workspaces]
        assert names == ["default-ws", "hj-tes1"]
        assert ccs_session == "cookie123"

    @pytest.mark.asyncio
    async def test_list_accounts_failure_does_not_break_login(self):
        """list-accounts is best-effort -- if it fails, login must still
        succeed with just the /authn/v1/session accounts."""
        with respx.mock:
            respx.post(f"{login_mod.USER_API_BASE}/authn/v1/session").mock(
                return_value=httpx.Response(
                    200,
                    json={"accounts": [WS_CACHED]},
                    headers={"set-cookie": "ccs-session=cookie123; Path=/"},
                )
            )
            respx.get(f"{login_mod.USER_API_BASE}/accounts/ui/v1/customer/list-accounts").mock(
                return_value=httpx.Response(500)
            )
            workspaces, _ = await login_mod._init_workspace_session("acc", "idtok")

        assert [w["company_name"] for w in workspaces] == ["default-ws"]


class TestRefreshWorkspaces:
    @pytest.mark.asyncio
    async def test_refresh_updates_cached_workspace_list(self, tmp_path, monkeypatch):
        _write_token(tmp_path, monkeypatch, workspaces=[WS_CACHED])

        with respx.mock:
            respx.get(f"{login_mod.USER_API_BASE}/accounts/ui/v1/customer/list-accounts").mock(
                return_value=httpx.Response(200, json={"customers": [WS_NEW]})
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
        """A pure --api-client / GLP client-credentials session has no
        ccs_session and cannot be refreshed live -- must raise a clear error."""
        _write_token(tmp_path, monkeypatch, ccs_session="")

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
    async def test_switch_regenerates_glp_credential_for_new_workspace(self, tmp_path, monkeypatch):
        """Regression test: compute-ops-mgmt calls (com devices list, etc.) use
        a workspace-scoped GLP client-credentials token. Without regenerating
        it on switch, 'com devices list' silently kept returning the OLD
        workspace's devices even after 'com workspace use' reported success."""
        token_path = _write_token(tmp_path, monkeypatch, workspaces=[WS_CACHED, WS_NEW])
        payload = json.loads(token_path.read_text())
        payload["glp_client_id"] = "old-client-id"
        payload["glp_client_secret"] = "old-secret"
        payload["glp_credential_name"] = "GLP-proliant-com-temp-old"
        payload["glp_access_token"] = "old-glp-access-token"
        payload["glp_token_expires_at"] = time.time() + 3600
        token_path.write_text(json.dumps(payload))

        with respx.mock:
            respx.get(f"{login_mod.USER_API_BASE}/authn/v1/session/load-account/ws-2").mock(
                return_value=httpx.Response(200)
            )
            with patch(
                "proliant.com.login.create_glp_api_credential",
                new_callable=AsyncMock,
                return_value={"client_id": "new-client-id", "client_secret": "new-secret",
                              "name": "GLP-proliant-com-temp-new"},
            ) as mock_create:
                await login_mod.switch_workspace("hj-tes1")

        mock_create.assert_awaited_once_with("acc", "cookie", "ws-2")
        saved = json.loads(login_mod.TOKEN_CACHE.read_text())
        assert saved["glp_client_id"] == "new-client-id"
        assert saved["glp_client_secret"] == "new-secret"
        assert saved["glp_credential_name"] == "GLP-proliant-com-temp-new"
        # stale token for the OLD workspace must not survive the switch
        assert saved["glp_access_token"] == ""
        assert saved["glp_token_expires_at"] == 0

    @pytest.mark.asyncio
    async def test_switch_clears_glp_token_even_if_credential_creation_fails(self, tmp_path, monkeypatch):
        """If regenerating the GLP credential fails, the stale wrong-workspace
        token must still be cleared so callers fail loudly instead of silently
        hitting the old workspace's compute-ops-mgmt data."""
        token_path = _write_token(tmp_path, monkeypatch, workspaces=[WS_CACHED, WS_NEW])
        payload = json.loads(token_path.read_text())
        payload["glp_access_token"] = "old-glp-access-token"
        payload["glp_token_expires_at"] = time.time() + 3600
        token_path.write_text(json.dumps(payload))

        with respx.mock:
            respx.get(f"{login_mod.USER_API_BASE}/authn/v1/session/load-account/ws-2").mock(
                return_value=httpx.Response(200)
            )
            with patch(
                "proliant.com.login.create_glp_api_credential",
                new_callable=AsyncMock,
                side_effect=RuntimeError("network error"),
            ):
                await login_mod.switch_workspace("hj-tes1")

        saved = json.loads(login_mod.TOKEN_CACHE.read_text())
        assert saved["glp_access_token"] == ""
        assert saved["glp_client_id"] == ""

    @pytest.mark.asyncio
    async def test_switch_retries_after_401_with_forced_token_refresh(self, tmp_path, monkeypatch):
        """Regression test: the ccs-session cookie can expire independently of
        (and well before) the OAuth access token -- observed live with an
        access token still valid for ~75 more minutes. A 401
        'HPE_GL_V1_SESSION_NOT_FOUND' from load-account must trigger a forced
        token refresh (which re-establishes ccs-session) and a single retry,
        instead of failing the switch immediately."""
        _write_token(tmp_path, monkeypatch, workspaces=[WS_CACHED, WS_NEW])

        refreshed_data = {
            "access_token": "new-acc", "ccs_session": "new-cookie",
            "workspace_id": "ws-1", "workspace_name": "default-ws",
            "workspaces": [WS_CACHED, WS_NEW],
        }

        with respx.mock:
            respx.get(f"{login_mod.USER_API_BASE}/authn/v1/session/load-account/ws-2").mock(
                side_effect=[
                    httpx.Response(401, json={"errorCode": "HPE_GL_V1_SESSION_NOT_FOUND"}),
                    httpx.Response(200),
                ]
            )
            with patch("proliant.com.login.refresh_token_if_needed", new_callable=AsyncMock,
                       return_value=refreshed_data) as mock_refresh:
                name = await login_mod.switch_workspace("hj-tes1")

        mock_refresh.assert_awaited_once_with(force=True)
        assert name == "hj-tes1"
        saved = json.loads(login_mod.TOKEN_CACHE.read_text())
        assert saved["workspace_id"] == "ws-2"

    @pytest.mark.asyncio
    async def test_switch_401_after_failed_refresh_raises_clean_error(self, tmp_path, monkeypatch):
        """If the forced refresh itself fails (e.g. the refresh_token has also
        expired), surface a clean re-login hint instead of the raw HPE JSON
        error body."""
        _write_token(tmp_path, monkeypatch, workspaces=[WS_CACHED, WS_NEW])

        with respx.mock:
            respx.get(f"{login_mod.USER_API_BASE}/authn/v1/session/load-account/ws-2").mock(
                return_value=httpx.Response(401, json={"errorCode": "HPE_GL_V1_SESSION_NOT_FOUND"})
            )
            with patch("proliant.com.login.refresh_token_if_needed", new_callable=AsyncMock,
                       return_value=None):
                with pytest.raises(login_mod.AuthFlowError, match="login session has expired"):
                    await login_mod.switch_workspace("hj-tes1")

    @pytest.mark.asyncio
    async def test_switch_to_new_workspace_triggers_live_refresh(self, tmp_path, monkeypatch):
        """Regression test: a workspace created after the last login isn't in
        the cached list -- switch_workspace() must refresh live and retry
        instead of immediately failing."""
        _write_token(tmp_path, monkeypatch, workspaces=[WS_CACHED])  # hj-tes1 NOT cached yet

        with respx.mock:
            respx.get(f"{login_mod.USER_API_BASE}/accounts/ui/v1/customer/list-accounts").mock(
                return_value=httpx.Response(200, json={"customers": [WS_NEW]})
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
            respx.get(f"{login_mod.USER_API_BASE}/accounts/ui/v1/customer/list-accounts").mock(
                return_value=httpx.Response(200, json={"customers": [WS_NEW]})
            )
            with pytest.raises(ValueError, match="not found"):
                await login_mod.switch_workspace("totally-unknown-workspace")

    @pytest.mark.asyncio
    async def test_switch_no_cached_workspaces_raises(self, tmp_path, monkeypatch):
        _write_token(tmp_path, monkeypatch, workspaces=[])

        with pytest.raises(CredentialsError, match="No workspace list cached"):
            await login_mod.switch_workspace("anything")
