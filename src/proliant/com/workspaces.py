"""
hpecom.workspaces
~~~~~~~~~~~~~~~~~
HPE GreenLake workspace operations — corresponds to GLP-Workspaces.psm1.

Covers: Get-HPEGLWorkspace (list current workspace info)
"""

from dataclasses import dataclass
from typing import Optional

import httpx

from proliant.com.auth import COMSession
from proliant.com.client import COMClient
from proliant.com.login import USER_API_BASE


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Workspace:
    """An HPE GreenLake workspace."""
    id: str
    name: str
    region: str
    status: str         # "ACTIVE", etc.
    address: str        # city, state, country
    description: str
    active: bool        # True = currently selected workspace
    raw: dict

    @classmethod
    def from_api(cls, ws: dict, active_id: str, region: str) -> "Workspace":
        addr = ws.get("address", {})
        city    = addr.get("city", "")
        state   = addr.get("state_or_region", "")
        country = addr.get("country_code", "")
        address = ", ".join(p for p in [city, state, country] if p)
        ws_id = ws.get("platform_customer_id", "")
        return cls(
            id=ws_id,
            name=ws.get("company_name", ""),
            region=region,
            status=ws.get("account_status", "ACTIVE"),
            address=address,
            description=ws.get("description", ""),
            active=(ws_id == active_id),
            raw=ws,
        )


# ---------------------------------------------------------------------------
# Fetch functions
# ---------------------------------------------------------------------------

async def fetch_workspaces(session: COMSession, refresh: bool = True) -> list[Workspace]:
    """Return all workspaces for the current account.

    By default, refreshes the workspace list live from GreenLake first (using
    the cached access token + ccs-session cookie — no re-login required) so a
    workspace created or joined after the last 'proliant com login' shows up
    immediately. Falls back to the cached list from token.json if the live
    refresh isn't possible (e.g. a pure --api-client session with no
    ccs_session) or fails. Marks the currently active workspace with active=True.

    Corresponds to: Get-HPEGLWorkspace
    """
    from proliant.com.login import load_token, refresh_workspaces as _refresh_workspaces

    data = load_token()
    cached_ws = (data or {}).get("workspaces", [])

    # A GLP client-credentials session (created at OAuth/Okta login) has
    # _user_token == False but still carries a cached workspace list from login.
    # Only reject when we have neither a user token nor any cached workspaces.
    if not session._user_token and not cached_ws:
        raise ValueError(
            "fetch_workspaces() requires a login session. "
            "Run 'proliant com login' first."
        )

    ws_list = cached_ws
    if refresh and data and data.get("ccs_session"):
        try:
            ws_list = await _refresh_workspaces()
        except Exception:
            ws_list = cached_ws  # best-effort -- fall back to the cached list

    if ws_list:
        return [
            Workspace.from_api(ws, session._workspace_id, session.region)
            for ws in ws_list
        ]

    # Fallback: build a single-entry list from session fields
    return [Workspace(
        id=session._workspace_id,
        name=session._workspace_name,
        region=session.region,
        status="ACTIVE",
        address="",
        description="",
        active=True,
        raw={},
    )]
