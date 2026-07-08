"""Tests for the COM region-switching feature.

A single GreenLake workspace can have Compute Ops Management provisioned
independently in more than one region (e.g. 'us-west' AND 'eu-central'),
each an independent service instance with its own device/server inventory
-- mirrors the region switcher in the GreenLake GUI. Covers:

  - login.fetch_com_regions() -- raw provisions endpoint wrapper
  - login._resolve_login_region() -- silent-default-or-hint auto-detect
  - login.switch_region() -- 'proliant com regions use <region>'
  - login.switch_workspace() -- sticky/auto-detect region restore on switch
  - login.save_token() -- workspace_regions map survives a fresh login
  - auth.COMSession.from_user_token(region_override=...) -- --region flag
  - regions.fetch_regions() -- Region dataclass / active marking
  - cli.py -- 'regions'/'region' subcommand parsing and dispatch
"""
from __future__ import annotations

import argparse
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx

from proliant.com import login as login_mod
from proliant.com.auth import COMSession, CredentialsError


WS_CACHED = {"platform_customer_id": "ws-1", "company_name": "default-ws", "account_status": "ACTIVE"}
WS_NEW    = {"platform_customer_id": "ws-2", "company_name": "hj-tes1",   "account_status": "ACTIVE"}

PROVISIONS_URL = f"{login_mod.USER_API_BASE}/ui-doorway/ui/v1/applications/provisions"


def _provision(region: str, status: str = "PROVISIONED", name: str = "Compute Ops Management") -> dict:
    return {
        "name": name,
        "region": region,
        "provision_status": status,
        "application_instance_id": f"instance-{region}",
        "location": region.upper(),
    }


def _write_token(tmp_path, monkeypatch, workspaces=None, workspace_regions=None,
                  workspace_id="ws-1", region="us-west", ccs_session="cookie"):
    token_path = tmp_path / "token.json"
    monkeypatch.setattr(login_mod, "TOKEN_CACHE", token_path)
    payload = {
        "access_token": "acc",
        "refresh_token": "reftok",
        "id_token": "idtok",
        "expires_at": time.time() + 3600,
        "region": region,
        "workspace_id": workspace_id,
        "workspace_name": "default-ws",
        "ccs_session": ccs_session,
        "workspaces": workspaces if workspaces is not None else [WS_CACHED],
        "workspace_regions": workspace_regions or {},
    }
    token_path.write_text(json.dumps(payload))
    return token_path


class TestFetchComRegions:
    @pytest.mark.asyncio
    async def test_filters_to_com_and_returns_raw_entries(self):
        provisions = [
            _provision("us-west"),
            _provision("eu-central", status=""),
            {"name": "Something Else", "region": "us-west", "provision_status": "PROVISIONED"},
        ]
        with respx.mock:
            respx.get(PROVISIONS_URL).mock(
                return_value=httpx.Response(200, json={"provisions": provisions})
            )
            result = await login_mod.fetch_com_regions("acc", "cookie")

        assert len(result) == 2
        assert {p["region"] for p in result} == {"us-west", "eu-central"}

    @pytest.mark.asyncio
    async def test_no_ccs_session_returns_empty_without_calling_api(self):
        with respx.mock:
            route = respx.get(PROVISIONS_URL)
            result = await login_mod.fetch_com_regions("acc", "")

        assert result == []
        assert route.call_count == 0

    @pytest.mark.asyncio
    async def test_non_200_returns_empty(self):
        with respx.mock:
            respx.get(PROVISIONS_URL).mock(return_value=httpx.Response(401))
            result = await login_mod.fetch_com_regions("acc", "cookie")

        assert result == []

    @pytest.mark.asyncio
    async def test_request_exception_returns_empty(self):
        with respx.mock:
            respx.get(PROVISIONS_URL).mock(side_effect=httpx.ConnectError("boom"))
            result = await login_mod.fetch_com_regions("acc", "cookie")

        assert result == []


class TestResolveLoginRegion:
    @pytest.mark.asyncio
    async def test_zero_provisioned_returns_fallback(self):
        with respx.mock:
            respx.get(PROVISIONS_URL).mock(
                return_value=httpx.Response(200, json={"provisions": []})
            )
            region = await login_mod._resolve_login_region("acc", "cookie", fallback="us-west")

        assert region == "us-west"

    @pytest.mark.asyncio
    async def test_single_provisioned_silently_selected(self):
        with respx.mock:
            respx.get(PROVISIONS_URL).mock(
                return_value=httpx.Response(200, json={"provisions": [_provision("eu-central")]})
            )
            region = await login_mod._resolve_login_region("acc", "cookie")

        assert region == "eu-central"

    @pytest.mark.asyncio
    async def test_multiple_provisioned_prefers_us_west_and_prints_hint(self, capsys):
        provisions = [_provision("us-west"), _provision("eu-central")]
        with respx.mock:
            respx.get(PROVISIONS_URL).mock(
                return_value=httpx.Response(200, json={"provisions": provisions})
            )
            region = await login_mod._resolve_login_region("acc", "cookie")

        assert region == "us-west"
        out = capsys.readouterr().out
        assert "Multiple COM regions available" in out
        assert "regions use" in out

    @pytest.mark.asyncio
    async def test_multiple_provisioned_without_us_west_picks_first_alphabetical(self):
        provisions = [_provision("eu-central"), _provision("ap-northeast")]
        with respx.mock:
            respx.get(PROVISIONS_URL).mock(
                return_value=httpx.Response(200, json={"provisions": provisions})
            )
            region = await login_mod._resolve_login_region("acc", "cookie")

        assert region == "ap-northeast"  # alphabetically first

    @pytest.mark.asyncio
    async def test_fetch_failure_returns_fallback(self):
        with respx.mock:
            respx.get(PROVISIONS_URL).mock(side_effect=httpx.ConnectError("boom"))
            region = await login_mod._resolve_login_region("acc", "cookie", fallback="us-west")

        assert region == "us-west"


class TestSaveTokenPreservesWorkspaceRegions:
    def test_merges_new_workspace_without_wiping_others(self, tmp_path, monkeypatch):
        token_path = tmp_path / "token.json"
        monkeypatch.setattr(login_mod, "TOKEN_CACHE", token_path)

        # Simulate a prior session that already remembered a region for ws-1
        token_path.write_text(json.dumps({"workspace_regions": {"ws-1": "eu-central"}}))

        login_mod.save_token(
            {"access_token": "a", "refresh_token": "r", "id_token": "i", "expires_in": 3600},
            region="us-west", workspace_id="ws-2", workspace_name="new-ws",
        )

        saved = json.loads(token_path.read_text())
        assert saved["workspace_regions"] == {"ws-1": "eu-central", "ws-2": "us-west"}

    def test_no_prior_file_starts_empty_map(self, tmp_path, monkeypatch):
        token_path = tmp_path / "token.json"
        monkeypatch.setattr(login_mod, "TOKEN_CACHE", token_path)

        login_mod.save_token(
            {"access_token": "a", "refresh_token": "r", "id_token": "i", "expires_in": 3600},
            region="us-west", workspace_id="ws-1", workspace_name="default-ws",
        )

        saved = json.loads(token_path.read_text())
        assert saved["workspace_regions"] == {"ws-1": "us-west"}


class TestSwitchRegion:
    @pytest.mark.asyncio
    async def test_switch_to_provisioned_region_succeeds(self, tmp_path, monkeypatch):
        _write_token(tmp_path, monkeypatch, workspace_regions={"ws-1": "us-west"})

        provisions = [_provision("us-west"), _provision("eu-central")]
        with respx.mock:
            respx.get(PROVISIONS_URL).mock(
                return_value=httpx.Response(200, json={"provisions": provisions})
            )
            resolved = await login_mod.switch_region("eu-central")

        assert resolved == "eu-central"
        saved = json.loads(login_mod.TOKEN_CACHE.read_text())
        assert saved["region"] == "eu-central"
        assert saved["workspace_regions"]["ws-1"] == "eu-central"

    @pytest.mark.asyncio
    async def test_switch_is_case_insensitive(self, tmp_path, monkeypatch):
        _write_token(tmp_path, monkeypatch)

        with respx.mock:
            respx.get(PROVISIONS_URL).mock(
                return_value=httpx.Response(200, json={"provisions": [_provision("eu-central")]})
            )
            resolved = await login_mod.switch_region("EU-CENTRAL")

        assert resolved == "eu-central"

    @pytest.mark.asyncio
    async def test_switch_to_unprovisioned_region_raises_with_suggestion(self, tmp_path, monkeypatch):
        _write_token(tmp_path, monkeypatch)

        with respx.mock:
            respx.get(PROVISIONS_URL).mock(
                return_value=httpx.Response(200, json={"provisions": [_provision("us-west")]})
            )
            with pytest.raises(ValueError, match="not provisioned"):
                await login_mod.switch_region("ap-northeast")

    @pytest.mark.asyncio
    async def test_not_logged_in_raises_credentials_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(login_mod, "TOKEN_CACHE", tmp_path / "missing.json")

        with pytest.raises(CredentialsError, match="Not logged in"):
            await login_mod.switch_region("us-west")

    @pytest.mark.asyncio
    async def test_api_client_session_raises_credentials_error(self, tmp_path, monkeypatch):
        _write_token(tmp_path, monkeypatch, ccs_session="")

        with pytest.raises(CredentialsError, match="user login session"):
            await login_mod.switch_region("us-west")


class TestSwitchWorkspaceRegionRestore:
    """switch_workspace() must resolve/persist the active COM region for
    whichever workspace becomes active -- sticky preference first, then
    auto-detect (silent if unambiguous, hint if 2+ options)."""

    @pytest.mark.asyncio
    async def test_sticky_preference_restored_silently(self, tmp_path, monkeypatch, capsys):
        _write_token(tmp_path, monkeypatch, workspaces=[WS_CACHED, WS_NEW],
                     workspace_regions={"ws-2": "eu-central"})

        with respx.mock:
            respx.get(f"{login_mod.USER_API_BASE}/authn/v1/session/load-account/ws-2").mock(
                return_value=httpx.Response(200)
            )
            respx.get(PROVISIONS_URL).mock(
                return_value=httpx.Response(200, json={"provisions": [
                    _provision("us-west"), _provision("eu-central"),
                ]})
            )
            await login_mod.switch_workspace("hj-tes1")

        saved = json.loads(login_mod.TOKEN_CACHE.read_text())
        assert saved["region"] == "eu-central"
        out = capsys.readouterr().out
        assert "Multiple COM regions" not in out  # sticky match -> no hint

    @pytest.mark.asyncio
    async def test_single_provisioned_region_auto_selected(self, tmp_path, monkeypatch):
        _write_token(tmp_path, monkeypatch, workspaces=[WS_CACHED, WS_NEW])

        with respx.mock:
            respx.get(f"{login_mod.USER_API_BASE}/authn/v1/session/load-account/ws-2").mock(
                return_value=httpx.Response(200)
            )
            respx.get(PROVISIONS_URL).mock(
                return_value=httpx.Response(200, json={"provisions": [_provision("eu-central")]})
            )
            await login_mod.switch_workspace("hj-tes1")

        saved = json.loads(login_mod.TOKEN_CACHE.read_text())
        assert saved["region"] == "eu-central"
        assert saved["workspace_regions"]["ws-2"] == "eu-central"

    @pytest.mark.asyncio
    async def test_multiple_provisioned_no_sticky_defaults_and_hints(self, tmp_path, monkeypatch, capsys):
        _write_token(tmp_path, monkeypatch, workspaces=[WS_CACHED, WS_NEW])

        with respx.mock:
            respx.get(f"{login_mod.USER_API_BASE}/authn/v1/session/load-account/ws-2").mock(
                return_value=httpx.Response(200)
            )
            respx.get(PROVISIONS_URL).mock(
                return_value=httpx.Response(200, json={"provisions": [
                    _provision("us-west"), _provision("eu-central"),
                ]})
            )
            await login_mod.switch_workspace("hj-tes1")

        saved = json.loads(login_mod.TOKEN_CACHE.read_text())
        assert saved["region"] == "us-west"
        out = capsys.readouterr().out
        assert "Multiple COM regions available" in out

    @pytest.mark.asyncio
    async def test_nothing_provisioned_leaves_region_unchanged(self, tmp_path, monkeypatch):
        _write_token(tmp_path, monkeypatch, workspaces=[WS_CACHED, WS_NEW], region="us-west")

        with respx.mock:
            respx.get(f"{login_mod.USER_API_BASE}/authn/v1/session/load-account/ws-2").mock(
                return_value=httpx.Response(200)
            )
            respx.get(PROVISIONS_URL).mock(
                return_value=httpx.Response(200, json={"provisions": []})
            )
            await login_mod.switch_workspace("hj-tes1")

        saved = json.loads(login_mod.TOKEN_CACHE.read_text())
        assert saved["region"] == "us-west"  # unchanged fallback


class TestFromUserTokenRegionOverride:
    def _write_glp_token(self, tmp_path, monkeypatch, region="us-west"):
        token_path = tmp_path / "token.json"
        monkeypatch.setattr("proliant.com.login.TOKEN_CACHE", token_path)
        payload = {
            "access_token": "acc", "refresh_token": "reftok",
            "expires_at": time.time() + 3600, "region": region,
            "workspace_id": "ws-1", "workspace_name": "default-ws",
            "ccs_session": "cookie",
            "glp_client_id": "glp-id", "glp_client_secret": "glp-secret",
        }
        token_path.write_text(json.dumps(payload))

    def test_no_override_uses_cached_region(self, tmp_path, monkeypatch):
        self._write_glp_token(tmp_path, monkeypatch, region="eu-central")
        session = COMSession.from_user_token()
        assert session.region == "eu-central"

    def test_explicit_override_wins(self, tmp_path, monkeypatch):
        self._write_glp_token(tmp_path, monkeypatch, region="eu-central")
        session = COMSession.from_user_token(region_override="ap-northeast")
        assert session.region == "ap-northeast"

    def test_load_forwards_region_override(self, tmp_path, monkeypatch):
        self._write_glp_token(tmp_path, monkeypatch, region="us-west")
        session = COMSession.load(region="eu-central")
        assert session.region == "eu-central"


class TestFetchRegions:
    @pytest.mark.asyncio
    async def test_marks_active_region_and_defaults_to_provisioned_only(self, tmp_path, monkeypatch):
        from proliant.com import regions as regions_mod

        token_path = tmp_path / "token.json"
        monkeypatch.setattr(login_mod, "TOKEN_CACHE", token_path)
        token_path.write_text(json.dumps({
            "access_token": "acc", "refresh_token": "reftok",
            "expires_at": time.time() + 3600, "region": "us-west",
            "ccs_session": "cookie", "workspace_id": "ws-1",
        }))

        session = COMSession(client_id="cid", client_secret="sec", region="us-west")
        session._ccs_session = "cookie"

        provisions = [
            _provision("us-west"),
            _provision("eu-central", status=""),  # unprovisioned -- excluded by default
        ]
        with respx.mock:
            respx.get(PROVISIONS_URL).mock(
                return_value=httpx.Response(200, json={"provisions": provisions})
            )
            result = await regions_mod.fetch_regions(session)

        assert len(result) == 1
        assert result[0].code == "us-west"
        assert result[0].active is True

    @pytest.mark.asyncio
    async def test_show_unprovisioned_includes_available_slots(self, tmp_path, monkeypatch):
        from proliant.com import regions as regions_mod

        token_path = tmp_path / "token.json"
        monkeypatch.setattr(login_mod, "TOKEN_CACHE", token_path)
        token_path.write_text(json.dumps({
            "access_token": "acc", "refresh_token": "reftok",
            "expires_at": time.time() + 3600, "region": "us-west",
            "ccs_session": "cookie", "workspace_id": "ws-1",
        }))

        session = COMSession(client_id="cid", client_secret="sec", region="us-west")
        session._ccs_session = "cookie"

        provisions = [_provision("us-west"), _provision("ap-northeast", status="")]
        with respx.mock:
            respx.get(PROVISIONS_URL).mock(
                return_value=httpx.Response(200, json={"provisions": provisions})
            )
            result = await regions_mod.fetch_regions(session, show_unprovisioned=True)

        assert len(result) == 2
        codes = {r.code: r.provisioned for r in result}
        assert codes == {"us-west": True, "ap-northeast": False}

    @pytest.mark.asyncio
    async def test_no_login_session_raises_value_error(self, tmp_path, monkeypatch):
        from proliant.com import regions as regions_mod

        monkeypatch.setattr(login_mod, "TOKEN_CACHE", tmp_path / "missing.json")
        session = COMSession(client_id="cid", client_secret="sec", region="us-west")

        with pytest.raises(ValueError, match="requires a login session"):
            await regions_mod.fetch_regions(session)

    @pytest.mark.asyncio
    async def test_api_client_session_raises_value_error(self, tmp_path, monkeypatch):
        from proliant.com import regions as regions_mod

        token_path = tmp_path / "token.json"
        monkeypatch.setattr(login_mod, "TOKEN_CACHE", token_path)
        token_path.write_text(json.dumps({"access_token": "acc", "region": "us-west"}))  # no ccs_session
        session = COMSession(client_id="cid", client_secret="sec", region="us-west")

        with pytest.raises(ValueError, match="user login session"):
            await regions_mod.fetch_regions(session)


class TestRegionsCliParsing:
    """'proliant com regions list/use' and singular 'region use' alias --
    mirrors TestWorkspacesUseAlias in test_cli_workspace_devices.py."""

    def test_regions_list_parses(self):
        from proliant.com.cli import _build_parser

        parser = _build_parser()
        args = parser.parse_args(["regions", "list"])
        assert args.command == "regions"
        assert args.what == "list"

    def test_regions_list_all_flag_parses(self):
        from proliant.com.cli import _build_parser

        parser = _build_parser()
        args = parser.parse_args(["regions", "list", "--all"])
        assert args.all is True

    def test_regions_use_parses(self):
        from proliant.com.cli import _build_parser

        parser = _build_parser()
        args = parser.parse_args(["regions", "use", "eu-central"])
        assert args.command == "regions"
        assert args.what == "use"
        assert args.region_name == "eu-central"

    def test_region_use_singular_alias_parses(self):
        from proliant.com.cli import _build_parser

        parser = _build_parser()
        args = parser.parse_args(["region", "use", "eu-central"])
        assert args.command == "region"
        assert args.what == "use"
        assert args.region_name == "eu-central"

    @pytest.mark.asyncio
    async def test_regions_use_dispatches_to_switch_region(self):
        from proliant.com import cli

        args = argparse.Namespace(command="regions", what="use", region_name="eu-central")
        with patch("proliant.com.login.switch_region", new_callable=AsyncMock,
                   return_value="eu-central") as mock_switch:
            await cli._async_main(args)
        mock_switch.assert_awaited_once_with("eu-central")

    @pytest.mark.asyncio
    async def test_region_use_singular_dispatches_to_switch_region(self):
        from proliant.com import cli

        args = argparse.Namespace(command="region", what="use", region_name="us-west")
        with patch("proliant.com.login.switch_region", new_callable=AsyncMock,
                   return_value="us-west") as mock_switch:
            await cli._async_main(args)
        mock_switch.assert_awaited_once_with("us-west")

    @pytest.mark.asyncio
    async def test_regions_list_dispatches_and_prints_table(self):
        from proliant.com import cli
        from proliant.com.regions import Region

        args = argparse.Namespace(command="regions", what="list", all=False)
        fake_session = MagicMock()
        fake_regions = [Region(code="us-west", location="Oregon", provisioned=True,
                                instance_id="i-1", active=True, raw={})]
        with patch("proliant.com.cli._ensure_session", new_callable=AsyncMock,
                   return_value=fake_session), \
             patch("proliant.com.regions.fetch_regions", new_callable=AsyncMock,
                   return_value=fake_regions) as mock_fetch, \
             patch("proliant.com.cli.print_regions_table") as mock_print:
            await cli._async_main(args)

        mock_fetch.assert_awaited_once_with(fake_session, show_unprovisioned=False)
        mock_print.assert_called_once_with(fake_regions)

    def test_region_names_completer_returns_empty_on_error(self):
        from proliant.com.cli import _region_names_completer

        with patch("proliant.com.auth.COMSession.load", side_effect=Exception("no session")):
            assert _region_names_completer("") == []

