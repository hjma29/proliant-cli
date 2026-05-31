"""
hpecom.client
~~~~~~~~~~~~~
Async HTTP client built on httpx.AsyncClient.

Key design decisions:
  - Single shared AsyncClient per session (connection pool reuse)
  - HTTP/2 enabled: multiplexes multiple requests on one TCP connection
  - asyncio.gather() fires all host queries simultaneously
  - Auto-pagination: follows nextPageUri until all results collected
  - Token auto-refresh via COMSession.ensure_token()

Performance vs PowerShell:
  - PS Invoke-RestMethod: sequential by default, ~N * latency
  - This client: all N requests in flight simultaneously, ~1 * latency
"""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Coroutine, TypeVar

import httpx

from pcli.com.auth import COMSession

T = TypeVar("T")

# Connection pool limits — tune for fleet size
_LIMITS = httpx.Limits(
    max_connections=100,
    max_keepalive_connections=40,
    keepalive_expiry=30,
)

_TIMEOUT = httpx.Timeout(connect=10, read=30, write=10, pool=5)


# ---------------------------------------------------------------------------
# Result containers
# ---------------------------------------------------------------------------

@dataclass
class QueryResult:
    """Result of a single API query — success or failure."""
    ok: bool
    data: Any = None          # parsed JSON on success
    error: str | None = None  # error message on failure
    status_code: int | None = None


# ---------------------------------------------------------------------------
# COMClient — the core async HTTP engine
# ---------------------------------------------------------------------------

class COMClient:
    """Async HTTP client for HPE Compute Ops Management API.

    Use as an async context manager::

        async with COMClient(session) as client:
            result = await client.get("/servers")
            devices = await client.get_all("/servers")

    Or for multi-device fleet queries::

        results = await COMClient.query_all(session, fetch_fn, devices)
    """

    def __init__(self, session: COMSession):
        self.session = session
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "COMClient":
        self._client = httpx.AsyncClient(
            http2=True,          # HTTP/2 multiplexing
            limits=_LIMITS,
            timeout=_TIMEOUT,
            headers={"Content-Type": "application/json"},
        )
        # Pre-fetch token once so all parallel requests have it ready
        await self.session.ensure_token(self._client)
        return self

    async def __aexit__(self, *_) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    # ── core request ──────────────────────────────────────────────────────

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        assert self._client, "Use 'async with COMClient(session) as client:'"
        await self.session.ensure_token(self._client)
        headers = kwargs.pop("headers", {})
        headers.update(self.session.auth_headers)
        # Include workspace session cookie for user token sessions
        cookies = kwargs.pop("cookies", {})
        cookies.update(self.session.ccs_cookies)
        resp = await self._client.request(method, url, headers=headers, cookies=cookies or None, **kwargs)

        # On 401, force-refresh token + ccs-session and retry once.
        # The ccs-session can expire server-side independently of the access token.
        if resp.status_code == 401 and self.session._user_token and self.session._refresh_token:
            await self.session.force_refresh()
            headers.update(self.session.auth_headers)
            cookies.update(self.session.ccs_cookies)
            resp = await self._client.request(method, url, headers=headers, cookies=cookies or None, **kwargs)

        resp.raise_for_status()
        return resp

    # ── convenience methods ───────────────────────────────────────────────

    async def get(self, url: str, **kwargs) -> dict:
        resp = await self._request("GET", url, **kwargs)
        return resp.json()

    async def post(self, url: str, json: dict | None = None, **kwargs) -> dict:
        resp = await self._request("POST", url, json=json, **kwargs)
        return resp.json()

    async def patch(self, url: str, json: dict | None = None, **kwargs) -> dict:
        resp = await self._request("PATCH", url, json=json, **kwargs)
        return resp.json()

    async def delete(self, url: str, **kwargs) -> int:
        resp = await self._request("DELETE", url, **kwargs)
        return resp.status_code

    # ── pagination ────────────────────────────────────────────────────────

    async def get_all(self, url: str, params: dict | None = None) -> list[dict]:
        """GET with automatic pagination — collects all items across pages.

        HPE GreenLake APIs use varying envelope keys:
          - COM API:        { "items": [...], "nextPageUri": "..." }
          - ui-doorway:     { "devices": [...], "pagination": {...} }
        Follows pagination until exhausted.
        """
        all_items: list[dict] = []
        next_url: str | None = url
        req_params = params

        # Known list keys in order of preference
        _LIST_KEYS = ("items", "devices", "servers", "groups", "jobs", "firmware")

        while next_url:
            data = await self.get(next_url, params=req_params)
            req_params = None  # params only on first request

            # Find the list in the response
            items = None
            for key in _LIST_KEYS:
                if key in data and isinstance(data[key], list):
                    items = data[key]
                    break

            if items is None:
                all_items.append(data)
            else:
                all_items.extend(items)

            # Follow pagination
            next_page = (
                data.get("nextPageUri")
                or (data.get("next") or {}).get("href")
                or (data.get("pagination") or {}).get("next_uri")
            )
            if next_page:
                next_url = next_page if next_page.startswith("http") else self.session.ui_base_url + next_page
            else:
                next_url = None

        return all_items

    # ── parallel multi-query ──────────────────────────────────────────────

    async def gather(
        self,
        tasks: list[Coroutine],
    ) -> list[QueryResult]:
        """Run multiple coroutines in parallel, capturing errors per-task.

        Unlike asyncio.gather(return_exceptions=True), wraps each result in
        QueryResult so callers get structured success/error objects.

        Example::

            results = await client.gather([
                client.get(url1),
                client.get(url2),
                client.get(url3),
            ])
        """
        async def _safe(coro) -> QueryResult:
            try:
                data = await coro
                return QueryResult(ok=True, data=data)
            except httpx.HTTPStatusError as e:
                return QueryResult(ok=False,
                                   error=f"HTTP {e.response.status_code}: {e.response.text[:200]}",
                                   status_code=e.response.status_code)
            except Exception as e:
                return QueryResult(ok=False, error=str(e))

        return list(await asyncio.gather(*(_safe(t) for t in tasks)))


# ---------------------------------------------------------------------------
# Module-level helper: run async code from sync context
# ---------------------------------------------------------------------------

def run(coro: Coroutine) -> Any:
    """Run a coroutine from synchronous code (e.g., CLI entry points)."""
    return asyncio.run(coro)
