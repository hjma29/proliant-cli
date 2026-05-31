"""Tests for hpecom.auth — OAuth2 session management."""
import time
import pytest
import respx
import httpx

from pcli.com.auth import COMSession, CredentialsError, AuthError, TOKEN_URL, REGION_MAP


class TestCOMSessionLoad:
    def test_explicit_credentials(self):
        s = COMSession.load(client_id="abc", client_secret="xyz", region="us-east")
        assert s.client_id == "abc"
        assert s.client_secret == "xyz"
        assert s.region == "us-east"

    def test_from_env(self, monkeypatch):
        monkeypatch.setenv("HPECOM_CLIENT_ID", "env-id")
        monkeypatch.setenv("HPECOM_CLIENT_SECRET", "env-secret")
        monkeypatch.setenv("HPECOM_REGION", "eu-central")
        s = COMSession.from_env()
        assert s.client_id == "env-id"
        assert s.region == "eu-central"

    def test_from_env_missing_raises(self, monkeypatch):
        monkeypatch.delenv("HPECOM_CLIENT_ID", raising=False)
        monkeypatch.delenv("HPECOM_CLIENT_SECRET", raising=False)
        with pytest.raises(CredentialsError):
            COMSession.from_env()

    def test_invalid_region_raises(self):
        s = COMSession(client_id="x", client_secret="y", region="mars")
        with pytest.raises(ValueError, match="Unknown region"):
            _ = s.base_url


class TestCOMSessionURLs:
    def test_base_url(self):
        s = COMSession(client_id="x", client_secret="y", region="us-west")
        assert s.base_url == REGION_MAP["us-west"]

    def test_com_url(self):
        s = COMSession(client_id="x", client_secret="y", region="us-west")
        url = s.com_url("/servers")
        assert "/compute-ops-mgmt/" in url
        assert url.endswith("/servers")

    def test_gl_url(self):
        s = COMSession(client_id="x", client_secret="y", region="us-west")
        url = s.gl_url("/devices")
        assert "/ui-doorway/" in url
        assert url.endswith("/devices")


class TestTokenFetch:
    @pytest.mark.asyncio
    async def test_fetch_token_success(self, session):
        """ensure_token returns cached token without hitting network."""
        async with httpx.AsyncClient() as client:
            token = await session.ensure_token(client)
        assert token == session._access_token

    @pytest.mark.asyncio
    async def test_fetch_token_refreshes_expired(self):
        """ensure_token POSTs to SSO when token is expired."""
        s = COMSession(client_id="test-id", client_secret="test-secret")
        # Force token to look expired
        s._access_token = "old-token"
        s._token_expiry = time.monotonic() - 1

        with respx.mock:
            respx.post(TOKEN_URL).mock(return_value=httpx.Response(
                200,
                json={"access_token": "new-token", "expires_in": 7200}
            ))
            async with httpx.AsyncClient() as client:
                token = await s.ensure_token(client)

        assert token == "new-token"
        assert s._access_token == "new-token"

    @pytest.mark.asyncio
    async def test_fetch_token_401_raises_auth_error(self):
        s = COMSession(client_id="bad-id", client_secret="bad-secret")

        with respx.mock:
            respx.post(TOKEN_URL).mock(return_value=httpx.Response(
                401, json={"error": "invalid_client"}
            ))
            async with httpx.AsyncClient() as client:
                with pytest.raises(AuthError):
                    await s.ensure_token(client)

    @pytest.mark.asyncio
    async def test_concurrent_token_refresh_only_fetches_once(self):
        """Multiple concurrent coroutines should only trigger one token fetch."""
        import asyncio
        s = COMSession(client_id="test-id", client_secret="test-secret")
        fetch_count = 0

        with respx.mock:
            def token_handler(request):
                nonlocal fetch_count
                fetch_count += 1
                return httpx.Response(200, json={"access_token": "tok", "expires_in": 7200})

            respx.post(TOKEN_URL).mock(side_effect=token_handler)

            async with httpx.AsyncClient() as client:
                # Fire 10 concurrent ensure_token calls
                await asyncio.gather(*[s.ensure_token(client) for _ in range(10)])

        # Lock ensures only 1 actual HTTP call
        assert fetch_count == 1
