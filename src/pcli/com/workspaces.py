"""
hpecom.workspaces
~~~~~~~~~~~~~~~~~
HPE GreenLake workspace operations — corresponds to GLP-Workspaces.psm1.

Covers: Get-HPEGLWorkspace (list current workspace info)
"""

from dataclasses import dataclass
from typing import Optional

import httpx

from pcli.com.auth import COMSession
from pcli.com.client import COMClient
from pcli.com.login import USER_API_BASE


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

async def fetch_workspaces(session: COMSession) -> list[Workspace]:
    """Return all workspaces for the current account.

    Uses the cached workspace list from token.json (saved at login).
    Marks the currently active workspace with active=True.

    Corresponds to: Get-HPEGLWorkspace
    """
    if not session._user_token:
        raise ValueError(
            "fetch_workspaces() requires a user OAuth token session. "
            "Run 'pcli com login' first."
        )

    from pcli.com.login import load_token
    data = load_token()
    cached_ws = (data or {}).get("workspaces", [])

    if cached_ws:
        return [
            Workspace.from_api(ws, session._workspace_id, session.region)
            for ws in cached_ws
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
