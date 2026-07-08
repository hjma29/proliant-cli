"""Tests for the --password flag removal and Okta Verify -> password fallback.

Covers:
  - login.OktaVerifyNotAvailable is raised by _select_okta_verify_push()
    when an account has no Okta Verify authenticator enrolled.
  - okta_verify_login() does NOT retry (burn 3 attempts) on that specific
    error -- it's a permanent condition for the account, not transient.
  - cli._cmd_login() catches it for @hpe.com accounts and falls back to
    password_login() automatically instead of failing outright.
  - The 'login' subcommand no longer has a --password/-p flag at all
    (it was a confusing boolean switch, redundant with domain-based
    auto-detection + this fallback).
"""
from __future__ import annotations

import argparse
from unittest.mock import AsyncMock

import pytest

from proliant.com import login as login_mod


class TestOktaVerifyNotAvailable:
    @pytest.mark.asyncio
    async def test_select_push_raises_specific_exception(self):
        idx_data = {
            "stateHandle": "sh0",
            "remediation": {
                "value": [
                    {"name": "select-authenticator-authenticate", "href": "https://x/select"},
                ]
            },
            "authenticators": {"value": [{"key": "okta_password", "displayName": "Password"}]},
        }
        with pytest.raises(login_mod.OktaVerifyNotAvailable) as exc_info:
            await login_mod._select_okta_verify_push(client=None, okta_base="https://x", idx_data=idx_data)

        assert "Okta Verify not available" in str(exc_info.value)
        assert "Password" in str(exc_info.value)

    def test_is_subclass_of_auth_flow_error(self):
        assert issubclass(login_mod.OktaVerifyNotAvailable, login_mod.AuthFlowError)


class TestOktaVerifyLoginDoesNotRetryOnPermanentCondition:
    @pytest.mark.asyncio
    async def test_no_retry_when_okta_verify_not_available(self, monkeypatch, capsys):
        login_mod._get_state_token = AsyncMock(return_value=("state-token-1", "https://fakeorg.okta.com"))
        login_mod._idx_introspect = AsyncMock(return_value={
            "stateHandle": "sh0",
            "remediation": {"value": [{"name": "identify", "href": "https://fakeorg.okta.com/identify"}]},
        })

        identify_response = {
            "stateHandle": "sh0",
            "remediation": {
                "value": [
                    {"name": "select-authenticator-authenticate", "href": "https://fakeorg.okta.com/select"},
                ]
            },
        }

        class _FakeResponse:
            def json(self):
                return identify_response

        async def _fake_post(*args, **kwargs):
            return _FakeResponse()

        class _FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            post = staticmethod(_fake_post)

        monkeypatch.setattr(login_mod.httpx, "AsyncClient", lambda **kwargs: _FakeClient())

        call_count = 0

        async def _raise_not_available(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise login_mod.OktaVerifyNotAvailable("Okta Verify not available. Authenticators: ['Password']")

        monkeypatch.setattr(login_mod, "_select_okta_verify_push", _raise_not_available)

        with pytest.raises(login_mod.OktaVerifyNotAvailable):
            await login_mod.okta_verify_login(email="someone@hpe.com")

        # Must fail on the first attempt -- retrying can't fix a missing
        # authenticator, so okta_verify_login should not loop 3 times.
        assert call_count == 1
        captured = capsys.readouterr()
        assert "Retry" not in (captured.out + captured.err)


class TestCmdLoginFallsBackToPassword:
    @pytest.mark.asyncio
    async def test_hpe_account_without_okta_verify_falls_back_to_password(self, monkeypatch, capsys):
        from proliant.com import cli

        async def _fake_prompt_password(_prompt):
            return "hunter2"

        okta_calls = []

        async def _fake_okta_verify_login(email, region=None):
            okta_calls.append(email)
            raise login_mod.OktaVerifyNotAvailable("Okta Verify not available. Authenticators: ['Password']")

        password_calls = []

        async def _fake_password_login(email, password, region=None):
            password_calls.append((email, password))
            return None  # success

        monkeypatch.setattr(cli, "_prompt_password_async", _fake_prompt_password)
        monkeypatch.setattr(login_mod, "okta_verify_login", _fake_okta_verify_login)
        monkeypatch.setattr(login_mod, "password_login", _fake_password_login)

        args = argparse.Namespace(email="someone@hpe.com", region=None, api_client=False)
        await cli._cmd_login(args)

        assert okta_calls == ["someone@hpe.com"]
        assert password_calls == [("someone@hpe.com", "hunter2")]
        captured = capsys.readouterr()
        out = captured.out + captured.err
        assert "falling back to password login" in out.lower()

    @pytest.mark.asyncio
    async def test_external_account_skips_okta_verify_entirely(self, monkeypatch):
        from proliant.com import cli

        async def _fake_prompt_password(_prompt):
            return "hunter2"

        okta_calls = []

        async def _fake_okta_verify_login(email, region=None):
            okta_calls.append(email)

        password_calls = []

        async def _fake_password_login(email, password, region=None):
            password_calls.append((email, password))

        monkeypatch.setattr(cli, "_prompt_password_async", _fake_prompt_password)
        monkeypatch.setattr(login_mod, "okta_verify_login", _fake_okta_verify_login)
        monkeypatch.setattr(login_mod, "password_login", _fake_password_login)

        args = argparse.Namespace(email="abc@gmail.com", region=None, api_client=False)
        await cli._cmd_login(args)

        assert okta_calls == []
        assert password_calls == [("abc@gmail.com", "hunter2")]


class TestPasswordFlagRemoved:
    def test_login_parser_has_no_password_flag(self):
        from proliant.com.cli import _build_parser

        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["login", "--password"])

    def test_login_help_does_not_mention_password_flag(self, capsys):
        from proliant.com.cli import _build_parser

        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["login", "-h"])
        captured = capsys.readouterr()
        assert "--password" not in captured.out
