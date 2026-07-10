"""
proliant.oneview.client
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

import os
import warnings
from typing import Any, Callable

import httpx

from proliant.common.http import BaseAsyncClient


class _ProgressFile:
    """Wrap a binary file handle to report bytes read during a streamed upload.

    httpx builds the multipart body by calling ``read(CHUNK_SIZE)`` in a loop and
    determines the content length separately via ``fileno()``/``fstat`` (so the
    callback only fires for actual body streaming, never for length probing).
    Every other attribute (``seek``/``tell``/``fileno``/``close`` …) is delegated
    to the underlying handle. ``on_progress`` is throttled so a multi-GB upload
    emits a manageable number of updates rather than one per 64 KB chunk.
    """

    def __init__(self, fh, on_progress: Callable[[int, int], None], total: int):
        self._fh = fh
        self._cb = on_progress
        self._total = total
        self._read = 0
        # Emit at most ~500 updates across the whole file (min 4 MB apart).
        self._step = max(4 * 1024 * 1024, total // 500) if total else 4 * 1024 * 1024
        self._since = 0

    def read(self, size: int = -1) -> bytes:
        chunk = self._fh.read(size)
        if chunk:
            self._read += len(chunk)
            self._since += len(chunk)
            if self._since >= self._step or self._read >= self._total:
                self._since = 0
                try:
                    self._cb(self._read, self._total)
                except Exception:  # noqa: BLE001 — progress must never break the upload
                    pass
        return chunk

    def __getattr__(self, name):  # delegate seek/tell/fileno/close/etc.
        return getattr(self._fh, name)

_TIMEOUT = httpx.Timeout(timeout=60.0, connect=10.0)
# Tighter timeout used only for the initial handshake (version check + login)
# so a wrong/unreachable appliance IP fails fast with a clear error instead of
# leaving the terminal looking frozen for up to the full 60s read timeout.
# Once authenticated, regular requests still use the more generous _TIMEOUT
# above, since some genuine operations (large paginated fetches) can
# legitimately take longer than a login should ever take.
_CONNECT_TIMEOUT = httpx.Timeout(timeout=8.0, connect=5.0)
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
            resp = await self._http.get("/rest/version", timeout=_CONNECT_TIMEOUT)
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
                timeout=_CONNECT_TIMEOUT,
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

    def _raise_for_status(self, resp: httpx.Response, method: str, uri: str) -> None:
        """Override base to raise OneViewError instead of RuntimeError."""
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = self._error_detail(resp)
            raise OneViewError(
                f"{method} {uri} failed — HTTP {resp.status_code}: {detail}"
            ) from exc

    def _body_with_task_uri(self, resp: httpx.Response) -> dict[str, Any]:
        """Return the JSON body, ensuring a OneView task ``uri`` is present.

        Async OneView operations (firmware updates, etc.) reply HTTP 202 with
        the monitoring task URI in the ``Location`` response header, while the
        body is often empty or is the mutated resource (whose own ``uri`` is
        NOT a task). Callers that poll the task read ``uri`` from this body, so
        promote the ``Location`` task URI into ``uri`` whenever the body does
        not already carry one that points at ``/rest/tasks/``. Synchronous
        responses (no ``Location``) are returned unchanged.
        """
        body = self._safe_json(resp)
        if "/rest/tasks/" in (body.get("uri", "") or ""):
            return body
        loc = resp.headers.get("Location", "") or ""
        idx = loc.find("/rest/tasks/")
        if idx != -1:
            return {**body, "uri": loc[idx:]}
        return body

    # ── CRUD ──────────────────────────────────────────────────────────────

    async def get(self, uri: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            resp = await self._ensure_http().get(uri, headers=self._headers, params=params)
        except httpx.RequestError as exc:
            raise OneViewError(f"Cannot reach OneView appliance at {self._base_url}: {exc}") from exc
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
        resp = await self._ensure_http().post(uri, json=body, headers=self._headers)
        self._raise_for_status(resp, "POST", uri)
        return self._safe_json(resp)

    async def patch(
        self,
        uri: str,
        body: list[dict[str, Any]],
        *,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        # Some OneView PATCH operations (e.g. a logical-enclosure firmware
        # update) require an ``If-Match`` header; callers pass it via *headers*,
        # which is merged on top of the session auth/version headers.
        req_headers = self._headers
        if headers:
            req_headers = {**req_headers, **headers}
        resp = await self._ensure_http().patch(uri, json=body, headers=req_headers)
        self._raise_for_status(resp, "PATCH", uri)
        return self._body_with_task_uri(resp)

    async def put(self, uri: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = await self._ensure_http().put(
            uri, json=(body if body is not None else {}), headers=self._headers
        )
        self._raise_for_status(resp, "PUT", uri)
        return self._body_with_task_uri(resp)

    async def delete(self, uri: str) -> dict[str, Any]:
        """DELETE a resource. Returns the response body (often a task or empty).

        OneView protects in-use resources server-side (e.g. a firmware baseline
        assigned to a logical enclosure/server profile cannot be deleted), so a
        failed delete raises OneViewError rather than removing anything.
        """
        resp = await self._ensure_http().delete(uri, headers=self._headers)
        self._raise_for_status(resp, "DELETE", uri)
        return self._safe_json(resp)

    # ── Streaming multipart upload ────────────────────────────────────────

    async def upload_file(
        self,
        uri: str,
        file_path: str,
        *,
        filename: str | None = None,
        timeout: httpx.Timeout | None = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> dict[str, Any]:
        """Stream a local file to *uri* as multipart/form-data (field name ``file``).

        Used for the appliance firmware image upload
        (``POST /rest/appliance/firmware/image``), which OneView requires as a
        streamed ``multipart/form-data`` body with an ``uploadfilename`` header.

        The file is streamed from disk in chunks (httpx reads the open handle
        lazily), so a multi-GB update image never has to be buffered in memory.
        A fresh short-lived httpx client is used so the appliance sets the
        multipart boundary itself instead of inheriting the JSON
        ``Content-Type`` default carried by the session client. The same
        session token is reused via the ``auth`` header.

        If *on_progress* is given it is called as ``on_progress(bytes_sent,
        total_bytes)`` (throttled) as the body streams, enabling an upload
        progress bar for the large image.
        """
        filename = filename or os.path.basename(file_path)
        headers = {
            "X-API-Version": str(self._api_version),
            "uploadfilename": filename,
        }
        if self._token:
            headers["auth"] = self._token

        # No read/write/pool limit — a large image upload can legitimately take
        # much longer than a normal request; only the initial connect is bounded.
        upload_timeout = timeout or httpx.Timeout(None, connect=10.0)

        total = os.path.getsize(file_path)
        with open(file_path, "rb") as fh:
            source = _ProgressFile(fh, on_progress, total) if on_progress else fh
            files = {"file": (filename, source, "application/octet-stream")}
            async with httpx.AsyncClient(
                base_url=self._base_url, verify=False, timeout=upload_timeout
            ) as up:
                resp = await up.post(uri, files=files, headers=headers)
        self._raise_for_status(resp, "POST", uri)
        return self._safe_json(resp)
