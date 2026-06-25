"""Shared pytest fixtures."""
import time
import pytest

from proliant.com.auth import COMSession

FAKE_TOKEN = "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.fake.token"


@pytest.fixture
def session():
    """COMSession with user-token mode and pre-injected token — never hits real HPE APIs."""
    s = COMSession(
        client_id="test-client-id",
        client_secret="test-client-secret",
        region="us-west",
    )
    s._access_token = FAKE_TOKEN
    s._token_expiry = time.monotonic() + 3600
    s._user_token = True
    s._refresh_token = "fake-refresh-token"
    return s


@pytest.fixture
def client_creds_session():
    """COMSession in client-credentials mode — routes device calls to GLP global API."""
    s = COMSession(
        client_id="test-client-id",
        client_secret="test-client-secret",
        region="us-west",
    )
    s._access_token = FAKE_TOKEN
    s._token_expiry = time.monotonic() + 3600
    return s
