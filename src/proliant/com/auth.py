"""
hpecom.auth
~~~~~~~~~~~
OAuth2 Client Credentials flow for HPE GreenLake / Compute Ops Management.

Credentials can be supplied via (in priority order):
  1. Explicit args:  COMSession(client_id=..., client_secret=..., region=...)
  2. Environment:    HPECOM_CLIENT_ID, HPECOM_CLIENT_SECRET, HPECOM_REGION
  3. Credentials file: ~/.config/proliant-cli/com/credentials.yml

Region values:
  us-west       -> https://us-west2-api.compute.cloud.hpe.com
  us-east       -> https://us-east2-api.compute.cloud.hpe.com
  eu-central    -> https://eu-central1-api.compute.cloud.hpe.com
  ap-southeast  -> https://ap-southeast1-api.compute.cloud.hpe.com
"""

import os
import time
import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx
import yaml

TOKEN_URL = "https://sso.common.cloud.hpe.com/as/token.oauth2"

REGION_MAP: dict[str, str] = {
    # Official endpoints (from developer.greenlake.hpe.com)
    "us-west":      "https://us-west.api.greenlake.hpe.com",
    "eu-central":   "https://eu-central.api.greenlake.hpe.com",
    "ap-northeast": "https://ap-northeast.api.greenlake.hpe.com",
    # Legacy aliases kept for backward compatibility
    "us-east":      "https://us-west.api.greenlake.hpe.com",   # no us-east in new API
    "ap-southeast": "https://ap-northeast.api.greenlake.hpe.com",
}

COM_API_VERSION = "v1beta2"
from proliant.common import config_dir as _config_dir
_CREDS_FILE = _config_dir() / "com" / "credentials.yml"
_REFRESH_BUFFER = 60  # refresh 60 s before expiry (tokens can be as short as 15 min)


class CredentialsError(Exception):
    pass


class AuthError(Exception):
    pass


@dataclass
class COMSession:
    """OAuth2 session — holds credentials and manages token lifecycle.

    Thread-safe and async-safe: uses asyncio.Lock so concurrent
    coroutines never trigger simultaneous token refresh.

    Example::

        session = COMSession.load()           # from env or credentials.yml
        async with COMClient(session) as c:
            devices = await c.get_all(session.com_url("/servers"))
    """

    client_id:     str
    client_secret: str
    region:        str = "us-west"

    _access_token:  str   = field(default="",    init=False, repr=False)
    _token_expiry:  float = field(default=0.0,   init=False, repr=False)
    _lock: asyncio.Lock   = field(default=None,  init=False, repr=False)
    _user_token:    bool  = field(default=False, init=False, repr=False)
    _ccs_session:   str   = field(default="",    init=False, repr=False)
    _workspace_id:  str   = field(default="",    init=False, repr=False)
    _workspace_name: str  = field(default="",    init=False, repr=False)
    _refresh_token: str   = field(default="",    init=False, repr=False)
    _glp_client_id: str   = field(default="",    init=False, repr=False)
    _glp_client_secret: str = field(default="",  init=False, repr=False)

    def __post_init__(self):
        # asyncio.Lock must be created inside an event loop context,
        # so we lazily initialize it on first use.
        self._lock = None

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    # ── class factories ────────────────────────────────────────────────────

    @classmethod
    def from_env(cls) -> "COMSession":
        cid = os.environ.get("HPECOM_CLIENT_ID")
        sec = os.environ.get("HPECOM_CLIENT_SECRET")
        reg = os.environ.get("HPECOM_REGION", "us-west")
        if not cid or not sec:
            raise CredentialsError(
                "Set HPECOM_CLIENT_ID and HPECOM_CLIENT_SECRET, "
                f"or create {_CREDS_FILE}"
            )
        return cls(client_id=cid, client_secret=sec, region=reg)

    @classmethod
    def from_file(cls, path: Optional[Path] = None) -> "COMSession":
        p = path or _CREDS_FILE
        if not p.exists():
            raise CredentialsError(f"Credentials file not found: {p}")
        data = yaml.safe_load(p.read_text()) or {}
        cid = data.get("client_id")
        sec = data.get("client_secret")
        if not cid or not sec:
            raise CredentialsError(
                f"{p} must contain client_id and client_secret"
            )
        return cls(client_id=cid, client_secret=sec,
                   region=data.get("region", "us-west"))

    @classmethod
    def from_user_token(cls) -> "COMSession":
        """Load cached user OAuth token from a previous 'proliant com login'.

        Loads the token from disk (even if expired). Silent refresh happens
        automatically in ensure_token() when the first API call is made,
        just like gcloud/az.
        Raises CredentialsError if no token file exists or no refresh possible.
        """
        from proliant.com.login import load_token  # avoid circular import

        data = load_token()
        if not data:
            raise CredentialsError(
                "Not logged in. Run 'proliant com login' first."
            )

        # If expired and no refresh token, fail immediately with helpful message
        if data.get("expires_at", 0) <= time.time() + 60 and not data.get("refresh_token"):
            raise CredentialsError(
                "Session expired. Run 'proliant com login' to re-authenticate."
            )

        glp_cid = data.get("glp_client_id", "")
        glp_sec = data.get("glp_client_secret", "")

        # Always prefer GLP client-credentials token when available.
        # The regional compute-ops-mgmt endpoint requires a GLP token for routing;
        # the Okta user token does NOT carry workspace routing context.
        if glp_cid and glp_sec:
            sess = cls(client_id=glp_cid, client_secret=glp_sec,
                       region=data.get("region", "us-west"))
            sess._glp_client_id = glp_cid
            sess._glp_client_secret = glp_sec
            sess._workspace_id = data.get("workspace_id", "")
            sess._workspace_name = data.get("workspace_name", "")
            sess._ccs_session = data.get("ccs_session", "")
            # Use cached GLP token if still valid
            glp_tok = data.get("glp_access_token", "")
            glp_exp = data.get("glp_token_expires_at", 0)
            if glp_tok and glp_exp > time.time() + 60:
                sess._access_token = glp_tok
                sess._token_expiry = time.monotonic() + (glp_exp - time.time())
            # else: ensure_token() will fetch a fresh GLP token via client_credentials
            return sess

        # No GLP credentials — use Okta user token (limited: no COM API access)
        sess = cls(client_id="", client_secret="", region=data.get("region", "us-west"))
        sess._access_token = data.get("access_token", "")
        remaining = data.get("expires_at", time.time()) - time.time()
        sess._token_expiry = time.monotonic() + max(remaining, 0)
        sess._user_token = True
        sess._ccs_session = data.get("ccs_session", "")
        sess._workspace_id = data.get("workspace_id", "")
        sess._workspace_name = data.get("workspace_name", "")
        sess._glp_client_id = glp_cid
        sess._glp_client_secret = glp_sec
        # Always preserve refresh_token — it outlives the access token
        sess._refresh_token = data.get("refresh_token", "")
        return sess

    def glp_fallback_session(self) -> Optional["COMSession"]:
        """Return a client-credentials COMSession using stored GLP API keys.

        Used as a fallback when the user refresh token has expired — GLP API
        keys never expire and work with global.api.greenlake.hpe.com.
        Returns None if no GLP credentials are available.
        """
        cid = getattr(self, "_glp_client_id", "")
        sec = getattr(self, "_glp_client_secret", "")
        if cid and sec:
            return COMSession(client_id=cid, client_secret=sec, region=self.region)
        return None

    @classmethod
    def load(cls, client_id: Optional[str] = None,
             client_secret: Optional[str] = None,
             region: Optional[str] = None) -> "COMSession":
        """Smart loader: explicit args > env vars > credentials file.

        Raises CredentialsError if nothing is found — callers should catch
        this and trigger the credentials wizard (proliant com login).
        """
        if client_id and client_secret:
            return cls(client_id=client_id, client_secret=client_secret,
                       region=region or "us-west")
        try:
            return cls.from_env()
        except CredentialsError:
            pass
        try:
            return cls.from_file()
        except CredentialsError:
            pass
        return cls.from_user_token()


    # ── URL helpers ────────────────────────────────────────────────────────

    @property
    def base_url(self) -> str:
        url = REGION_MAP.get(self.region)
        if not url:
            raise ValueError(
                f"Unknown region '{self.region}'. Valid: {list(REGION_MAP)}"
            )
        return url

    @property
    def ui_base_url(self) -> str:
        """Base URL for ui-doorway endpoints (user token sessions use aquila-user-api)."""
        if self._user_token:
            return "https://aquila-user-api.common.cloud.hpe.com"
        return self.base_url

    @property
    def ccs_cookies(self) -> dict:
        """Return ccs-session cookie dict if available (user token sessions)."""
        if self._ccs_session:
            return {"ccs-session": self._ccs_session}
        return {}

    def com_url(self, path: str) -> str:
        """Build a full COM API URL. path should start with '/'."""
        return f"{self.base_url}/compute-ops-mgmt/{COM_API_VERSION}{path}"

    def gl_url(self, path: str) -> str:
        """Build a full GreenLake Platform URL (ui-doorway)."""
        return f"{self.ui_base_url}/ui-doorway/ui/v1{path}"

    # ── token management ───────────────────────────────────────────────────

    @property
    def is_token_valid(self) -> bool:
        """True if token will not expire within the proactive refresh buffer."""
        return bool(self._access_token) and time.monotonic() + _REFRESH_BUFFER < self._token_expiry

    async def ensure_token(self, client: httpx.AsyncClient) -> str:
        """Return valid Bearer token, silently refreshing if expired. Async-safe.

        Auth priority:
          1. Cached token still valid → return immediately
          2. User session: try Okta refresh token
          3. User session fallback: if Okta refresh expired, silently switch to
             stored GLP client-credentials (never expire) — user stays logged in
          4. Client-credentials session: POST client_credentials grant
        """
        if self.is_token_valid:
            return self._access_token

        if self._user_token:
            # Try Okta silent refresh first
            if self._refresh_token:
                async with self._get_lock():
                    if self.is_token_valid:
                        return self._access_token
                    await self._do_refresh()
                    if self.is_token_valid:
                        return self._access_token

            # Okta refresh token expired — fall back to stored GLP client credentials.
            # This keeps the user "logged in" indefinitely without interactive re-auth.
            glp_cid = getattr(self, "_glp_client_id", "")
            glp_sec = getattr(self, "_glp_client_secret", "")
            if glp_cid and glp_sec:
                # Switch this session to client-credentials mode permanently
                self.client_id = glp_cid
                self.client_secret = glp_sec
                self._user_token = False
                self._refresh_token = ""
                # Fall through to client-credentials path below
            else:
                raise AuthError(
                    "Session expired. Run 'proliant com login' to re-authenticate."
                )

        # Client-credentials path (API key flow or post-fallback)
        async with self._get_lock():
            # Double-check after acquiring lock — another coroutine may have refreshed
            if self.is_token_valid:
                return self._access_token

            try:
                resp = await client.post(
                    TOKEN_URL,
                    data={"grant_type": "client_credentials"},
                    auth=(self.client_id, self.client_secret),
                    timeout=15,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                resp.raise_for_status()
            except httpx.HTTPStatusError as e:
                raise AuthError(
                    f"Token fetch failed ({e.response.status_code}): "
                    f"{e.response.text}"
                ) from e
            except httpx.RequestError as e:
                raise AuthError(f"Token fetch request error: {e}") from e

            token_data = resp.json()
            self._access_token = token_data["access_token"]
            expires_in = token_data.get("expires_in", 7200)
            self._token_expiry = time.monotonic() + expires_in
            # Cache the GLP token to disk so the next CLI invocation can reuse it
            # without fetching a new token (saves ~0.3s per run).
            try:
                from proliant.com.login import cache_glp_token
                cache_glp_token(self._access_token, expires_in)
            except Exception:
                pass
            return self._access_token

    async def _do_refresh(self) -> None:
        """Perform token + ccs-session refresh and update in-memory state."""
        from proliant.com.login import refresh_token_if_needed
        data = await refresh_token_if_needed(ccs_session=self._ccs_session, force=True)
        if data:
            self._access_token = data["access_token"]
            remaining = data.get("expires_at", time.time() + 3600) - time.time()
            self._token_expiry = time.monotonic() + max(remaining, 0)
            self._refresh_token = data.get("refresh_token", self._refresh_token)
            self._ccs_session = data.get("ccs_session", self._ccs_session)

    async def force_refresh(self) -> None:
        """Force immediate re-auth regardless of token validity (e.g. after 401).
        Useful when the ccs-session has expired server-side but the access token
        is still technically valid.
        """
        async with self._get_lock():
            await self._do_refresh()

    @property
    def auth_headers(self) -> dict[str, str]:
        """Sync access to cached token (assumes ensure_token was called)."""
        if not self._access_token:
            raise AuthError("No token available — call ensure_token() first")
        return {"Authorization": f"Bearer {self._access_token}"}
