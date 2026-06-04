"""
pcli.oneview.client
~~~~~~~~~~~~~~~~~~~~
Async HPE OneView REST client built on httpx.

Authentication flow:
  1. GET  /rest/version  (no auth) → currentVersion integer
  2. POST /rest/login-sessions  {userName, password, loginMsgAck: true}
     → { sessionID: "abc..." }
  3. All requests include headers:
       auth: <sessionID>
       X-API-Version: <currentVersion>
  4. DELETE /rest/login-sessions  on exit (logout)

The client is used as an async context manager::

    async with OneViewClient(host, username, password) as client:
        servers = await client.get_all("/rest/server-hardware")
"""

from __future__ import annotations

import warnings
from typing import Any

import httpx

from pcli.common.http import BaseAsyncClient

_TIMEOUT = httpx.Timeout(timeout=60.0, connect=10.0)
_PAGE_SIZE = 500  # OneView default max is 65535, but 500 is safe and fast


class OneViewError(RuntimeError):
    """Raised for OneView API errors with a human-readable message."""


class OneViewClient(BaseAsyncClient):
    """Async REST client for a single HPE OneView appliance."""

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        *,
        api_version: int | None = None,
    ) -> None:
        self._base_url = f"https://{host.rstrip('/')}"
        self._username = username
        self._password = password
        self._requested_version = api_version  # None = auto-negotiate
        self._http: httpx.AsyncClient | None = None
        self._token: str | None = None
        self._api_version: int = 0  # set during __aenter__

    # ── Context manager ───────────────────────────────────────────────────

    async def __aenter__(self) -> "OneViewClient":
        # Suppress httpx SSL warnings — OneView uses self-signed certs
        warnings.filterwarnings("ignore", category=Warning, module="httpx")

        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            verify=False,
            timeout=_TIMEOUT,
            headers={"Content-Type": "application/json"},
        )

        # Step 1: negotiate API version (no auth required)
        try:
            resp = await self._http.get("/rest/version")
            resp.raise_for_status()
            version_data = resp.json()
        except Exception as exc:
            await self._http.aclose()
            raise OneViewError(f"Cannot reach OneView appliance at {self._base_url}: {exc}") from exc

        self._api_version = self._requested_version or version_data.get("currentVersion", 800)

        # Step 2: login
        try:
            resp = await self._http.post(
                "/rest/login-sessions",
                json={
                    "userName": self._username,
                    "password": self._password,
                    "loginMsgAck": True,
                },
                headers=self._headers,
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            await self._http.aclose()
            detail = self._error_detail(exc.response)
            raise OneViewError(f"OneView login failed (HTTP {exc.response.status_code}): {detail}") from exc
        except Exception as exc:
            await self._http.aclose()
            raise OneViewError(f"OneView login failed: {exc}") from exc

        self._token = resp.json().get("sessionID", "")
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._token and self._http:
            try:
                await self._http.delete("/rest/login-sessions", headers=self._headers)
            except Exception:
                pass
        if self._http:
            await self._http.aclose()
            self._http = None
        self._token = None

    # ── Headers ───────────────────────────────────────────────────────────

    @property
    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {
            "X-API-Version": str(self._api_version),
            "Content-Type": "application/json",
        }
        if self._token:
            h["auth"] = self._token
        return h

    @property
    def api_version(self) -> int:
        return self._api_version

    # ── HTTP helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _safe_json(resp: httpx.Response) -> dict[str, Any]:
        if not resp.content:
            return {}
        try:
            data = resp.json()
        except ValueError:
            return {"raw": resp.text.strip()[:500]}
        return data if isinstance(data, dict) else {"value": data}

    @classmethod
    def _error_detail(cls, resp: httpx.Response) -> str:
        payload = cls._safe_json(resp)
        if not payload:
            return resp.reason_phrase
        # OneView error format: {"errorCode": "...", "message": "...", "details": "..."}
        msg = payload.get("message", "")
        detail = payload.get("details", "")
        return f"{msg} {detail}".strip() or str(payload)[:200]

    def _raise_for_status(self, resp: httpx.Response, method: str, uri: str) -> None:
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = self._error_detail(resp)
            raise OneViewError(
                f"{method} {uri} failed — HTTP {resp.status_code}: {detail}"
            ) from exc

    def _http_client(self) -> httpx.AsyncClient:
        if self._http is None:
            raise RuntimeError("Use 'async with OneViewClient(...) as client:' before requests")
        return self._http

    # ── CRUD ──────────────────────────────────────────────────────────────

    async def get(self, uri: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = await self._http_client().get(uri, headers=self._headers, params=params)
        self._raise_for_status(resp, "GET", uri)
        return self._safe_json(resp)

    async def get_all(self, uri: str, **extra_params: Any) -> list[dict[str, Any]]:
        """Fetch all members across pages. Returns flat list."""
        results: list[dict[str, Any]] = []
        start = 0
        while True:
            params = {"start": start, "count": _PAGE_SIZE, **extra_params}
            data = await self.get(uri, params=params)
            members = data.get("members", [])
            results.extend(members)
            total = data.get("total", len(results))
            if len(results) >= total:
                break
            start += _PAGE_SIZE
        return results

    async def post(self, uri: str, body: dict[str, Any]) -> dict[str, Any]:
        resp = await self._http_client().post(uri, json=body, headers=self._headers)
        self._raise_for_status(resp, "POST", uri)
        return self._safe_json(resp)

    async def patch(self, uri: str, body: list[dict[str, Any]]) -> dict[str, Any]:
        resp = await self._http_client().patch(uri, json=body, headers=self._headers)
        self._raise_for_status(resp, "PATCH", uri)
        return self._safe_json(resp)
