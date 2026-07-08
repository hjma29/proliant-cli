"""Tests for friendly error handling in proliant.com.login.password_login().

Covers the fix for: an account with no password authenticator enrolled yet
(Okta IDX returns 'enroll-authenticator'/'select-authenticator-enroll'
remediations instead of a challenge) used to leak raw IDX jargon --
"Unexpected remediations after identify: [...]. Authenticators: [...]" --
and retried 3 times pointlessly before failing. Now it raises immediately
with a clear, actionable message, and 'proliant com login' (cli._cmd_login)
maps that message to friendly console output instead of printing it raw.
"""
from __future__ import annotations

import argparse
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from proliant.com import login as login_mod


OKTA_BASE = "https://fakeorg.okta.com"


def _mock_identify_chain(identify_json: dict):
    """Patch out the first two IDX steps and mock the identify POST response."""
    login_mod._get_state_token = AsyncMock(return_value=("state-token-1", OKTA_BASE))
    login_mod._idx_introspect = AsyncMock(return_value={"stateHandle": "sh0", "remediation": {"value": []}})
    return respx.post(f"{OKTA_BASE}/idp/idx/identify").mock(
        return_value=httpx.Response(200, json=identify_json)
    )


class TestPasswordLoginEnrollAuthenticatorFriendlyError:
    @pytest.mark.asyncio
    async def test_no_password_authenticator_raises_friendly_message(self, monkeypatch):
        identify_response = {
            "stateHandle": "sh0",
            "remediation": {
                "value": [
                    {"name": "enroll-authenticator", "href": f"{OKTA_BASE}/idp/idx/enroll-authenticator"},
                    {"name": "select-authenticator-enroll", "href": f"{OKTA_BASE}/idp/idx/select-authenticator-enroll"},
                ]
            },
            "authenticators": {"value": [{"type": "email"}]},
        }

        with respx.mock:
            route = _mock_identify_chain(identify_response)
            with pytest.raises(login_mod.AuthFlowError) as exc_info:
                await login_mod.password_login(email="abc@gmail.com", password="whatever")

            # Structural failure -- must not retry the identify call 3 times.
            assert route.call_count == 1

        msg = str(exc_info.value)
        assert "no password authenticator" in msg.lower()
        assert "unexpected remediations" not in msg.lower()
        assert "enroll-authenticator" not in msg  # no raw remediation-name jargon

    @pytest.mark.asyncio
    async def test_still_leaks_raw_message_for_truly_unknown_remediation(self):
        """Sanity check: an unrelated/unknown remediation combo keeps the
        original diagnostic (raw) message -- only the enrollment case gets
        the friendly rewrite."""
        identify_response = {
            "stateHandle": "sh0",
            "remediation": {
                "value": [
                    {"name": "some-brand-new-remediation", "href": f"{OKTA_BASE}/idp/idx/whatever"},
                ]
            },
            "authenticators": {"value": [{"type": "email"}]},
        }

        with respx.mock:
            _mock_identify_chain(identify_response)
            with pytest.raises(login_mod.AuthFlowError) as exc_info:
                await login_mod.password_login(email="abc@gmail.com", password="whatever")

        msg = str(exc_info.value)
        assert "unexpected remediations after identify" in msg.lower()


class TestCmdLoginMapsFriendlyMessage:
    @pytest.mark.asyncio
    async def test_cli_prints_friendly_message_not_raw_jargon(self, capsys, monkeypatch):
        from proliant.com import cli

        async def _fake_prompt_password(_prompt):
            return "whatever"

        async def _fake_password_login(email, password, region=None):
            raise login_mod.AuthFlowError(
                "Password login is not available for this account -- "
                "no password authenticator is enrolled yet in HPE "
                "GreenLake. Sign in via the GreenLake console once to "
                "set a password, then retry 'proliant com login --password'."
            )

        monkeypatch.setattr(cli, "_prompt_password_async", _fake_prompt_password)
        monkeypatch.setattr(login_mod, "password_login", _fake_password_login)

        args = argparse.Namespace(email="abc@gmail.com", password=True, region=None, api_client=False)
        with pytest.raises(SystemExit) as exc_info:
            await cli._cmd_login(args)

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "not available for this account" in captured.out.lower()
        assert "unexpected remediations" not in captured.out.lower()
