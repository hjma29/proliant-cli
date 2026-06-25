"""
hpecom.firmware
~~~~~~~~~~~~~~~
HPE Compute Ops Management firmware bundle operations.

Covers: listing SPP bundles (Service Pack for ProLiant) available in COM.
Bundles are hosted by HPE; customers cannot upload custom bundles.
"""

from dataclasses import dataclass

from proliant.com.auth import COMSession
from proliant.com.client import COMClient

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BUNDLES_PATH = "/compute-ops-mgmt/v1beta2/firmware-bundles"

_GEN_LABEL = {
    "BUNDLE_GEN_12": "Gen12",
    "BUNDLE_GEN_11": "Gen11",
    "BUNDLE_GEN_10": "Gen10/10+",
}

_TYPE_LABEL = {
    "BASE":    "BASE",
    "PATCH":   "PATCH",
    "HOTFIX":  "HOTFIX",
}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class FirmwareBundle:
    """An SPP (Service Pack for ProLiant) firmware bundle available in COM."""
    id: str
    display_name: str        # e.g. "SPP 2026.03.00.00 (30 Mar 2026)"
    release_version: str     # e.g. "2026.03.00.00"
    release_date: str        # "2026-03-30"
    generation: str          # "Gen12", "Gen11", "Gen10/10+"
    bundle_type: str         # "BASE", "PATCH", "HOTFIX"
    is_active: bool
    name: str                # full name
    resource_uri: str
    raw: dict

    @classmethod
    def from_api(cls, b: dict) -> "FirmwareBundle":
        gen_raw = b.get("bundleGeneration", "")
        return cls(
            id=b.get("id", ""),
            display_name=b.get("displayName", b.get("name", "")),
            release_version=b.get("releaseVersion", ""),
            release_date=(b.get("releaseDate") or "")[:10],
            generation=_GEN_LABEL.get(gen_raw, gen_raw),
            bundle_type=_TYPE_LABEL.get(b.get("bundleType", ""), b.get("bundleType", "")),
            is_active=b.get("isActive", False),
            name=b.get("name", ""),
            resource_uri=b.get("resourceUri", ""),
            raw=b,
        )

    @property
    def gen_number(self) -> int:
        """Return numeric generation (10, 11, 12) for filtering."""
        if "12" in self.generation:
            return 12
        if "11" in self.generation:
            return 11
        return 10


# ---------------------------------------------------------------------------
# Fetch functions
# ---------------------------------------------------------------------------

async def fetch_bundles(session: COMSession,
                        active_only: bool = True,
                        gen: int | None = None,
                        bundle_type: str | None = None) -> list[FirmwareBundle]:
    """Return SPP firmware bundles available in COM.

    Args:
        active_only: If True (default), return only isActive bundles.
        gen:         If set (10, 11, 12), filter to that server generation.
        bundle_type: If set ('base', 'patch', 'hotfix'), filter by type.
    """
    url = f"{session.base_url}{_BUNDLES_PATH}?limit=200"
    async with COMClient(session) as client:
        resp = await client.get(url)

    bundles = [FirmwareBundle.from_api(b) for b in resp.get("items", [])]

    if active_only:
        bundles = [b for b in bundles if b.is_active]
    if gen is not None:
        bundles = [b for b in bundles if b.gen_number == gen]
    if bundle_type is not None:
        bt = bundle_type.upper()
        bundles = [b for b in bundles if b.bundle_type == bt]

    # Sort: newest first, then by generation descending
    bundles.sort(key=lambda b: (b.release_version, b.gen_number), reverse=True)
    return bundles
