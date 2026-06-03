"""
hpecom.devices
~~~~~~~~~~~~~~
GreenLake platform device operations — corresponds to GLP-Devices.psm1.

Covers: Get-HPEGLDevice, Add-HPEGLDeviceCompute, Connect-HPEGLDeviceComputeiLOtoCOM
"""

import asyncio
import re
from dataclasses import dataclass
from typing import Optional

import httpx

from pcli.com.auth import COMSession, AuthError
from pcli.com.client import COMClient

GLOBAL_API_BASE = "https://global.api.greenlake.hpe.com"
DEVICES_URI = f"{GLOBAL_API_BASE}/devices/v1/devices"
IDENTITY_URI = f"{GLOBAL_API_BASE}/identity/v1/users"

# Strip common verbose prefixes from model strings to keep them short
_MODEL_STRIP_RE = re.compile(
    r"^(?:HPE\s+)?(?:PROLIANT\s+)?(?:COMPUTE\s+)?", re.IGNORECASE
)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Device:
    """A device registered in the HPE GreenLake workspace."""
    id: str
    serial_number: str
    product_id: str
    device_type: str            # "COMPUTE", "STORAGE", "SWITCH", etc.
    display_name: str
    model: str
    service_name: str           # COM instance name (e.g. "HPECC_USWEST_1") or ""
    subscription_key: str | None
    tags: dict
    raw: dict                   # full API response

    @classmethod
    def from_api(cls, d: dict) -> "Device":
        """Parse a device dict from either ui-doorway or GLP global API."""
        raw_model = d.get("device_model") or d.get("model", "")
        # GLP global API uses camelCase; ui-doorway uses snake_case
        serial = d.get("serial_number") or d.get("serialNumber", "")
        part   = d.get("part_number") or d.get("partNumber", "")
        name   = d.get("name") or d.get("deviceName") or serial
        dtype  = d.get("device_type") or d.get("deviceType", "")
        svc    = d.get("application_instance_name", "")  # ui-doorway only
        # Subscription key: ui-doorway has flat field; GLP global nests it
        sub_key = d.get("subscription_key")
        if not sub_key:
            subs = d.get("subscription") or []
            if isinstance(subs, list) and subs:
                sub_key = subs[0].get("key")
        # resource_id (ui-doorway) or id (GLP global)
        rid = d.get("resource_id") or d.get("id", "")
        tags_raw = d.get("tags") or []
        if isinstance(tags_raw, list):
            tags = {t["name"]: t["value"] for t in tags_raw if isinstance(t, dict)}
        else:
            tags = {}
        return cls(
            id=rid,
            serial_number=serial,
            product_id=part,
            device_type=dtype,
            display_name=name,
            model=_MODEL_STRIP_RE.sub("", raw_model).strip(),
            service_name=svc,
            subscription_key=sub_key or None,
            tags=tags,
            raw=d,
        )


@dataclass
class DeviceAddResult:
    """Result of an add-device operation for a single device."""
    serial_number: str
    part_number: str
    status: str       # "Complete", "Warning", "Failed"
    detail: str       # human-readable detail / error


# ---------------------------------------------------------------------------
# Fetch functions
# ---------------------------------------------------------------------------

async def _fetch_via_glp_global(session: COMSession,
                                device_type: Optional[str]) -> list[Device]:
    """Fetch devices from GLP global API using client-credentials auth.

    Works even when the user refresh token has expired. Falls back to
    this automatically from fetch_devices() on AuthError.
    """
    params: dict = {"limit": 1000}   # avoid multi-page round-trips
    if device_type:
        params["filter"] = f"deviceType eq '{device_type}'"
    async with COMClient(session) as client:
        items = await client.get_all(DEVICES_URI, params=params)
        return [Device.from_api(d) for d in items]


async def fetch_devices(
    session: COMSession,
    device_type: Optional[str] = None,
) -> list[Device]:
    """Fetch all devices in the workspace (optionally filtered by type).

    Corresponds to: Get-HPEGLDevice
    Primary:  GET /ui-doorway/ui/v1/devices  (user token + ccs-session)
    Fallback: GET global.api.greenlake.hpe.com/devices/v1/devices
              (GLP client-credentials — works after Okta session expires)

    The fallback is transparent: ensure_token() silently switches to stored
    GLP client credentials when the Okta refresh token expires, so users
    stay logged in without running 'pcli com login' again.

    Args:
        session:     Authenticated COMSession
        device_type: Optional filter — "COMPUTE", "NETWORK", "STORAGE"
    """
    # If already in client-credentials mode (no user session at all),
    # skip ui-doorway and use GLP global API directly.
    if not session._user_token and not getattr(session, '_glp_client_id', ''):
        return await _fetch_via_glp_global(session, device_type)

    try:
        async with COMClient(session) as client:
            # After __aenter__, ensure_token() has run. If the Okta session
            # expired and the session was switched to client-credentials mode,
            # use the GLP global API instead of ui-doorway.
            if not session._user_token:
                params = {}
                if device_type:
                    params["filter"] = f"deviceType eq '{device_type}'"
                items = await client.get_all(DEVICES_URI, params=params or None)
                return [Device.from_api(d) for d in items]

            params = {}
            if device_type:
                params["deviceType"] = device_type
            url = session.gl_url("/devices")
            items = await client.get_all(url, params=params or None)
            return [Device.from_api(d) for d in items]
    except (AuthError, httpx.HTTPStatusError) as exc:
        # User session failed (401/403) — fall back to GLP global API
        is_auth_fail = isinstance(exc, AuthError) or (
            isinstance(exc, httpx.HTTPStatusError)
            and exc.response.status_code in (401, 403)
        )
        if not is_auth_fail:
            raise
        glp = session.glp_fallback_session()
        if glp is None:
            raise
        return await _fetch_via_glp_global(glp, device_type)


async def fetch_compute_devices(session: COMSession) -> list[Device]:
    """Fetch only compute (server) devices. Shorthand for fetch_devices(type=COMPUTE)."""
    return await fetch_devices(session, device_type="COMPUTE")


async def resolve_user_ids(user_ids: set[str], glp_token: str) -> dict[str, str]:
    """Resolve a set of GLP user UUIDs to email addresses in parallel.

    Returns a dict mapping user_id → email (falls back to user_id if unresolvable).
    """
    if not user_ids:
        return {}

    async def _fetch_one(uid: str, client: httpx.AsyncClient) -> tuple[str, str]:
        try:
            r = await client.get(
                f"{IDENTITY_URI}/{uid}",
                headers={"Authorization": f"Bearer {glp_token}"},
            )
            if r.status_code == 200:
                data = r.json()
                email = data.get("username") or data.get("email") or uid
                return uid, email
        except Exception:
            pass
        return uid, uid  # fallback: show raw UUID

    async with httpx.AsyncClient(timeout=15) as client:
        results = await asyncio.gather(*[_fetch_one(uid, client) for uid in user_ids])
    return dict(results)


async def add_compute_devices(
    serial_numbers: list[str],
    part_numbers: list[str],
) -> list[DeviceAddResult]:
    """Add compute devices to the HPE GreenLake workspace.

    Corresponds to: Add-HPEGLDeviceCompute
    Endpoint: POST https://global.api.greenlake.hpe.com/devices/v1/devices

    Uses the GLP API token (client credentials flow) stored at login time.
    The global API requires a different token than the user OAuth token.

    Args:
        serial_numbers: List of device serial numbers
        part_numbers:   List of device part numbers (same length as serial_numbers)

    Returns:
        List of DeviceAddResult for each device.
    """
    from pcli.com.login import load_token, get_glp_api_token

    data = load_token()
    if not data:
        raise PermissionError("Not logged in. Run 'pcli com login' first.")

    glp_client_id     = data.get("glp_client_id", "")
    glp_client_secret = data.get("glp_client_secret", "")

    if not glp_client_id or not glp_client_secret:
        raise PermissionError(
            "No GLP API credential found. Run 'pcli com login' to create one.\n"
            "Note: This requires a fresh login to set up the credential."
        )

    # Get a fresh GLP API token
    glp_token = await get_glp_api_token(glp_client_id, glp_client_secret, data.get("workspace_id", ""))
    if not glp_token:
        raise PermissionError(
            "Failed to obtain GLP API token. The credential may be expired — "
            "run 'pcli com login' to create a new one."
        )

    compute_list = [
        {"serialNumber": sn, **({"partNumber": pn} if pn and pn.upper() != "NA" else {})}
        for sn, pn in zip(serial_numbers, part_numbers)
    ]

    payload = {"compute": compute_list, "network": [], "storage": []}

    results: list[DeviceAddResult] = []

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                DEVICES_URI,
                json=payload,
                headers={
                    "Authorization": f"Bearer {glp_token}",
                    "Accept":        "application/json",
                    "Content-Type":  "application/json",
                },
            )

        if r.status_code in (200, 201, 202, 207):
            # 202 Accepted = async job submitted; device onboarding in progress
            for sn, pn in zip(serial_numbers, part_numbers):
                results.append(DeviceAddResult(
                    serial_number=sn, part_number=pn,
                    status="Complete", detail="Device add accepted (async). Check workspace for status.",
                ))

        elif r.status_code == 409:
            # Conflict = already exists
            for sn, pn in zip(serial_numbers, part_numbers):
                results.append(DeviceAddResult(
                    serial_number=sn, part_number=pn,
                    status="Warning", detail="Device already present in the workspace.",
                ))
        else:
            err_msg = r.text[:300]
            for sn, pn in zip(serial_numbers, part_numbers):
                results.append(DeviceAddResult(
                    serial_number=sn, part_number=pn,
                    status="Failed", detail=f"HTTP {r.status_code}: {err_msg}",
                ))

    except Exception as exc:
        for sn, pn in zip(serial_numbers, part_numbers):
            results.append(DeviceAddResult(
                serial_number=sn, part_number=pn,
                status="Failed", detail=str(exc),
            ))

    return results


async def connect_ilo_to_com(
    session: COMSession,
    serial_number: str,
    product_id: str,
) -> dict:
    """Onboard a server to COM by connecting its iLO.

    Corresponds to: Connect-HPEGLDeviceComputeiLOtoCOM
    Endpoint: POST /compute-ops-mgmt/{COM_API_VERSION}/servers
    """
    async with COMClient(session) as client:
        url = session.com_url("/servers")
        payload = {
            "serialNumber": serial_number,
            "productId": product_id,
        }
        return await client.post(url, json=payload)

