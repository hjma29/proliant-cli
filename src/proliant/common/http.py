"""
proliant.common.http
~~~~~~~~~~~~~~~~
Shared async HTTP client base class.

Provides common helpers that ILOClient, COMClient, and OneViewClient
all need: safe JSON parsing, error detail extraction, status checking,
and the "ensure client exists" guard.
"""

from __future__ import annotations

import json
from typing import Any

import httpx


class BaseAsyncClient:
    """Mixin / base providing shared HTTP response helpers.

    Subclasses must set ``self._http`` to an ``httpx.AsyncClient`` instance
    during their ``__aenter__`` and clear it in ``__aexit__``.
    """

    _http: httpx.AsyncClient | None = None

    def _ensure_http(self) -> httpx.AsyncClient:
        """Return the active httpx client, or raise if not in context manager."""
        if self._http is None:
            cls_name = type(self).__name__
            raise RuntimeError(
                f"Use 'async with {cls_name}(...)' before issuing requests"
            )
        return self._http

    @staticmethod
    def _safe_json(resp: httpx.Response) -> dict[str, Any]:
        """Parse response body as JSON; return {} on empty/invalid body."""
        if not resp.content:
            return {}
        try:
            data = resp.json()
        except ValueError:
            text = resp.text.strip()
            return {"raw": text[:500]} if text else {}
        return data if isinstance(data, dict) else {"value": data}

    @classmethod
    def _error_detail(cls, resp: httpx.Response) -> str:
        """Extract a human-readable error detail string from a response."""
        payload = cls._safe_json(resp)
        if not payload:
            return resp.reason_phrase
        if "raw" in payload:
            return payload["raw"][:200]
        # OneView style: {message, details}
        msg = payload.get("message", "")
        detail = payload.get("details", "")
        if msg or detail:
            return f"{msg} {detail}".strip()[:200]
        return json.dumps(payload, default=str)[:200]

    @classmethod
    def _raise_for_status(cls, resp: httpx.Response, method: str, uri: str) -> None:
        """Raise RuntimeError with context on HTTP error responses."""
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status = resp.status_code
            # Auth failures: skip the verbose Redfish ExtendedInfo JSON and
            # show a plain, actionable reason instead.
            if status == 401:
                detail = "check username/password"
            elif status == 403:
                detail = "account lacks permission for this operation"
            else:
                detail = cls._error_detail(resp)
            raise RuntimeError(
                f"{method} {uri} failed — HTTP {status}: {detail}"
            ) from exc
