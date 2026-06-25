"""
proliant.ilo.client
~~~~~~~~~~~~~~~~
Async Redfish session management built on httpx.AsyncClient.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import httpx

from proliant.common.http import BaseAsyncClient

ServerDownOrUnreachableError = httpx.ConnectError
_TIMEOUT = httpx.Timeout(timeout=60.0, connect=10.0)


class ILOClient(BaseAsyncClient):
    """Async Redfish client for a single iLO host."""

    def __init__(self, base_url: str, username: str, password: str):
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._http: httpx.AsyncClient | None = None
        self._token: str | None = None
        self._session_uri: str | None = None
        self._root: dict[str, Any] | None = None
        self._uri_cache: dict[str, str] = {}

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def _auth_headers(self) -> dict[str, str]:
        return {"X-Auth-Token": self._token} if self._token else {}

    async def __aenter__(self) -> "ILOClient":
        # iLO 6 and iLO 7 only support HTTP/1.1; http2=True causes httpx to
        # advertise h2 in TLS ALPN but falls back silently — no actual benefit.
        # Left as-is (harmless); http2=False is equally fine for iLO.
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            verify=False,
            http2=False,
            timeout=_TIMEOUT,
        )

        resp = await self._http.post(
            "/redfish/v1/SessionService/Sessions",
            json={"UserName": self._username, "Password": self._password},
        )
        self._raise_for_status(resp, "POST", "/redfish/v1/SessionService/Sessions")

        self._token = resp.headers.get("X-Auth-Token")
        body = self._safe_json(resp)
        self._session_uri = body.get("@odata.id")
        if not self._token:
            raise RuntimeError("Redfish login succeeded but X-Auth-Token header is missing")
        if not self._session_uri:
            raise RuntimeError("Redfish login succeeded but session @odata.id is missing")

        self._root = await self.get("/redfish/v1/")
        return self

    async def __aexit__(self, *_exc: object) -> None:
        http = self._http
        session_uri = self._session_uri
        auth_headers = dict(self._auth_headers)
        self._http = None
        self._token = None
        self._session_uri = None
        self._root = None
        self._uri_cache.clear()

        if http is None:
            return

        try:
            if session_uri:
                try:
                    resp = await http.delete(session_uri, headers=auth_headers)
                    if resp.status_code >= 400:
                        resp.raise_for_status()
                except Exception:
                    pass
        finally:
            await http.aclose()

    async def request(
        self,
        method: str,
        uri: str,
        *,
        json: dict[str, Any] | None = None,
        files: Any = None,
        timeout: httpx.Timeout | float | None = None,
    ) -> httpx.Response:
        http = self._ensure_http()
        resp = await http.request(
            method,
            uri,
            json=json,
            files=files,
            headers=self._auth_headers,
            timeout=timeout,
        )
        self._raise_for_status(resp, method, uri)
        return resp

    async def get(self, uri: str) -> dict[str, Any]:
        resp = await self.request("GET", uri)
        return self._safe_json(resp)

    async def post(self, uri: str, body: dict[str, Any]) -> dict[str, Any]:
        resp = await self.request("POST", uri, json=body)
        return self._safe_json(resp)

    async def delete(self, uri: str) -> int:
        resp = await self.request("DELETE", uri)
        return resp.status_code

    async def patch(self, uri: str, body: dict[str, Any]) -> dict[str, Any]:
        resp = await self.request("PATCH", uri, json=body)
        return self._safe_json(resp)

    async def _root_document(self) -> dict[str, Any]:
        if self._root is None:
            self._root = await self.get("/redfish/v1/")
        return self._root

    async def _first_member_uri(self, collection_key: str, label: str) -> str:
        if collection_key in self._uri_cache:
            return self._uri_cache[collection_key]

        root = await self._root_document()
        collection_uri = root.get(collection_key, {}).get("@odata.id")
        if not collection_uri:
            raise RuntimeError(f"Redfish root has no {label} collection")

        members = (await self.get(collection_uri)).get("Members", [])
        if not members:
            raise RuntimeError(f"No {label} members found in Redfish root")

        uri = members[0].get("@odata.id")
        if not uri:
            raise RuntimeError(f"First {label} member is missing @odata.id")

        self._uri_cache[collection_key] = uri
        return uri

    async def get_system_uri(self) -> str:
        return await self._first_member_uri("Systems", "Systems")

    async def get_chassis_uri(self) -> str:
        return await self._first_member_uri("Chassis", "Chassis")

    async def get_manager_uri(self) -> str:
        return await self._first_member_uri("Managers", "Managers")

    async def get_update_service_uri(self) -> str:
        if "UpdateService" in self._uri_cache:
            return self._uri_cache["UpdateService"]

        root = await self._root_document()
        uri = root.get("UpdateService", {}).get("@odata.id")
        if not uri:
            raise RuntimeError("Redfish root has no UpdateService URI")

        self._uri_cache["UpdateService"] = uri
        return uri

    async def get_firmware_inventory_uri(self) -> str:
        if "FirmwareInventory" in self._uri_cache:
            return self._uri_cache["FirmwareInventory"]

        update_service = await self.get(await self.get_update_service_uri())
        uri = update_service.get("FirmwareInventory", {}).get("@odata.id")
        if not uri:
            raise RuntimeError("UpdateService has no FirmwareInventory URI")

        self._uri_cache["FirmwareInventory"] = uri
        return uri


@asynccontextmanager
async def ilo_session(host: dict) -> AsyncIterator[ILOClient]:
    """Yield an authenticated async iLO client for one host."""
    async with ILOClient(host["url"], host["username"], host["password"]) as client:
        yield client
