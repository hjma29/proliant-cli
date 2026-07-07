"""
hpecom.regions
~~~~~~~~~~~~~~
HPE GreenLake Compute Ops Management region operations.

A single GreenLake workspace can have COM provisioned independently in more
than one region (e.g. 'us-west' AND 'eu-central'), each an independent
service instance with its own device/server inventory -- mirrors the region
switcher in the GreenLake GUI and corresponds to
Get-HPEGLService -Name 'Compute Ops Management' -ShowProvisioned in
Lionel Jullien's HPECOMCmdlets.
"""

from dataclasses import dataclass
from typing import Optional

from proliant.com.auth import COMSession


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Region:
    """A Compute Ops Management region instance within a workspace."""
    code: str            # e.g. "us-west"
    location: str        # human-readable, e.g. "Oregon, USA"
    provisioned: bool     # True if an active COM instance exists here
    instance_id: str      # application_instance_id
    active: bool          # True = currently selected region
    raw: dict

    @classmethod
    def from_api(cls, p: dict, active_code: str) -> "Region":
        code = p.get("region", "")
        return cls(
            code=code,
            location=p.get("location", "") or "—",
            provisioned=(p.get("provision_status") == "PROVISIONED"),
            instance_id=p.get("application_instance_id", "") or "—",
            active=(bool(code) and code == active_code),
            raw=p,
        )


# ---------------------------------------------------------------------------
# Fetch functions
# ---------------------------------------------------------------------------

async def fetch_regions(session: COMSession, show_unprovisioned: bool = False) -> list[Region]:
    """Return COM regions for the currently active workspace.

    By default only returns regions with an actual PROVISIONED COM instance
    (pass show_unprovisioned=True to also include available-but-not-yet-set-up
    region slots, e.g. to help a user provision a new one). Marks the
    currently active region (session.region) with active=True.

    Requires a user login session (ccs-session cookie) -- raises ValueError
    for a pure --api-client/GLP client-credentials session, same convention
    as workspaces.fetch_workspaces().
    """
    from proliant.com.login import load_token, fetch_com_regions, refresh_token_if_needed

    data = load_token()
    if not data:
        raise ValueError(
            "fetch_regions() requires a login session. Run 'proliant com login' first."
        )
    if not data.get("ccs_session"):
        raise ValueError(
            "Listing COM regions requires a user login session. "
            "Run 'proliant com login' (not --api-client) first."
        )

    data = await refresh_token_if_needed() or data
    access_token = data.get("access_token", "")
    ccs_session = data.get("ccs_session", "")

    provisions = await fetch_com_regions(access_token, ccs_session)
    if not show_unprovisioned:
        provisions = [p for p in provisions if p.get("provision_status") == "PROVISIONED"]

    regions = [Region.from_api(p, session.region) for p in provisions]
    regions.sort(key=lambda r: r.code)
    return regions
