"""
proliant.com.servers
~~~~~~~~~~~~~~~~~~~~~
Real HPE Compute Ops Management server inventory.

This is NOT the GreenLake device-claim list (see proliant.com.devices). It is
GET /compute-ops-mgmt/v1/servers -- the source that backs the COM GUI's
"Servers" page and its Overview health-status widget.

Why this matters: a server can be synced into COM automatically via a linked
OneView appliance bridge without ever being individually "claimed" through
GreenLake's device-add flow. Those OneView-bridged servers are invisible to
proliant.com.devices.fetch_devices() (the GreenLake /devices claim API) but
are very much real, managed COM servers -- and this is why 'com devices list'
previously undercounted the GUI's server total.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from proliant.com.auth import COMSession
from proliant.com.client import COMClient

_SERVERS_PATH    = "/compute-ops-mgmt/v1/servers"
_GROUPS_PATH     = "/compute-ops-mgmt/v1/groups"
_APPLIANCES_PATH = "/compute-ops-mgmt/v1beta1/appliances"

_CONNECTION_TYPE_LABEL = {
    "DIRECT":  "Direct",
    "ONEVIEW": "OneView managed",
}


@dataclass
class Server:
    """A server from COM's own inventory, or a non-compute device adapted
    to the same shape (see device_to_server_row) so both 'com servers list'
    and 'com devices list' can share one table renderer."""
    id: str
    name: str
    serial_number: str
    model: str
    product_id: str
    manufacturer: str
    generation: str
    uuid: str
    health: str
    power_state: str
    state_label: str           # "Connected" / "Not connected" / "Not activated" / "—"
    connected: bool
    connection_type: str       # "Direct" / "OneView managed" / "—"
    maintenance_mode: bool
    auto_ilo_fw_update: bool
    subscription_tier: str
    subscription_state: str
    baseline: str               # resolved SPP version, or "—"
    group: str                  # COM server-group name, or "—"
    appliance_name: str         # OneView appliance hostname, or "—"
    oneview_name: str           # server's name as known to OneView, or "—"
    oneview_state: str          # OneView-side monitoring state, or "—"
    ilo_hostname: str
    ilo_ip: str
    ilo_version: str
    ilo_license: str
    operating_system: str
    cpu: str
    device_type: str = "COMPUTE"   # "COMPUTE" / "STORAGE" / "SWITCH" for merged devices view
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_api(cls, s: dict, *, group_by_serial: dict, baseline_by_id: dict,
                 appliance_by_id: dict) -> "Server":
        hw       = s.get("hardware") or {}
        bmc      = hw.get("bmc") or {}
        state    = s.get("state") or {}
        health   = hw.get("health") or {}
        oneview  = s.get("oneview") or {}
        appliance = s.get("appliance") or {}
        host     = s.get("host") or {}
        serial   = hw.get("serialNumber", "")

        connected = bool(state.get("connected"))
        if connected:
            state_label = "Connected"
        elif (state.get("subscriptionState") or "").upper() == "REQUIRED":
            state_label = "Not activated"
        else:
            state_label = "Not connected"

        bundle_uri = s.get("firmwareBundleUri") or ""
        bundle_id = bundle_uri.rsplit("/", 1)[-1] if bundle_uri else ""
        if bundle_id and bundle_id in baseline_by_id:
            baseline = baseline_by_id[bundle_id]
        elif bundle_uri:
            baseline = "Custom"
        else:
            baseline = "—"

        connection_type = s.get("connectionType") or ""

        return cls(
            id=s.get("id", ""),
            name=s.get("name") or serial or "—",
            serial_number=serial,
            model=hw.get("model") or "—",
            product_id=hw.get("productId") or "—",
            manufacturer=hw.get("manufacturer") or "—",
            generation=s.get("serverGeneration") or "—",
            uuid=hw.get("uuid") or "—",
            health=(health.get("summary") if health else None) or "UNKNOWN",
            power_state=hw.get("powerState") or "—",
            state_label=state_label,
            connected=connected,
            connection_type=_CONNECTION_TYPE_LABEL.get(connection_type, connection_type or "—"),
            maintenance_mode=bool(s.get("maintenanceMode")),
            auto_ilo_fw_update=bool(s.get("autoIloFwUpdate")),
            subscription_tier=state.get("subscriptionTier") or "—",
            subscription_state=state.get("subscriptionState") or "—",
            baseline=baseline,
            group=group_by_serial.get(serial, "—"),
            appliance_name=appliance_by_id.get(appliance.get("applianceId", ""), "—"),
            oneview_name=oneview.get("name") or "—",
            oneview_state=oneview.get("state") or "—",
            ilo_hostname=bmc.get("hostname") or "—",
            ilo_ip=bmc.get("ip") or "—",
            ilo_version=bmc.get("version") or "—",
            ilo_license=bmc.get("license") or "—",
            operating_system=host.get("osName") or "—",
            cpu=s.get("processorVendor") or "—",
            device_type="COMPUTE",
            raw=s,
        )


def device_to_server_row(d) -> Server:
    """Adapt a GreenLake-claimed Device (proliant.com.devices.Device) -- used
    for non-compute devices (storage, network) -- to the Server row shape so
    'com devices list' can render compute + non-compute rows in one table.

    Most COM-only fields (health, group, baseline, iLO detail, ...) simply
    aren't tracked for these devices at the GreenLake claim-inventory level,
    so they render as "—" rather than being guessed at.
    """
    hostname = d.raw.get("deviceName") or d.raw.get("name") or ""
    return Server(
        id=d.id,
        name=d.display_name or hostname or d.serial_number,
        serial_number=d.serial_number,
        model=d.model or "—",
        product_id=d.product_id or "—",
        manufacturer="—",
        generation="—",
        uuid="—",
        health="—",
        power_state="—",
        state_label="—",
        connected=False,
        connection_type="—",
        maintenance_mode=False,
        auto_ilo_fw_update=False,
        subscription_tier="—",
        subscription_state="—",
        baseline="—",
        group="—",
        appliance_name="—",
        oneview_name="—",
        oneview_state="—",
        ilo_hostname=hostname or "—",
        ilo_ip="—",
        ilo_version="—",
        ilo_license="—",
        operating_system="—",
        cpu="—",
        device_type=d.device_type or "—",
        raw=d.raw,
    )


# ---------------------------------------------------------------------------
# Fetch helpers
# ---------------------------------------------------------------------------

async def _fetch_group_map(client: COMClient, session: COMSession) -> dict[str, str]:
    """Return {serial_number: group_name} for servers that belong to a COM
    server group. Servers not in any group are simply absent from this map."""
    try:
        data = await client.get(f"{session.base_url}{_GROUPS_PATH}", params={"limit": 200})
    except Exception:  # intentional: group lookup is best-effort enrichment
        return {}
    mapping: dict[str, str] = {}
    for g in data.get("items", []):
        name = g.get("name", "—")
        for dev in (g.get("devices") or []):
            serial = dev.get("serial")
            if serial:
                mapping[serial] = name
    return mapping


async def _fetch_appliance_map(client: COMClient, session: COMSession) -> dict[str, str]:
    """Return {appliance_id: hostname} for OneView appliances bridged into COM."""
    try:
        data = await client.get(f"{session.base_url}{_APPLIANCES_PATH}", params={"limit": 50})
    except Exception:  # intentional: appliance lookup is best-effort enrichment
        return {}
    return {
        a.get("id", ""): a.get("hostname") or a.get("name") or "—"
        for a in data.get("items", [])
    }


async def _fetch_baseline_map(session: COMSession) -> dict[str, str]:
    """Return {bundle_id: release_version} across all (active + superseded)
    firmware bundles, so a server's assigned baseline resolves even if that
    bundle is no longer the latest/active one."""
    try:
        from proliant.com.firmware import fetch_bundles
        bundles = await fetch_bundles(session, active_only=False)
    except Exception:  # intentional: baseline lookup is best-effort enrichment
        return {}
    return {b.id: b.release_version for b in bundles}


async def fetch_servers(session: COMSession) -> list[Server]:
    """Fetch every server in COM's own inventory (matches the GUI's Servers
    page and Overview health widget count -- includes OneView-bridged
    servers that GreenLake's device-claim list omits).
    """
    async with COMClient(session) as client:
        servers_data, group_map, appliance_map, baseline_map = await asyncio.gather(
            client.get(f"{session.base_url}{_SERVERS_PATH}", params={"limit": 1000}),
            _fetch_group_map(client, session),
            _fetch_appliance_map(client, session),
            _fetch_baseline_map(session),
        )

    return [
        Server.from_api(s, group_by_serial=group_map, baseline_by_id=baseline_map,
                         appliance_by_id=appliance_map)
        for s in servers_data.get("items", [])
    ]


async def fetch_all_devices(session: COMSession, device_type: str | None = None) -> list[Server]:
    """Unified 'devices' view: COM's real inventory for COMPUTE, GreenLake's
    device-claim inventory for everything else (STORAGE/NETWORK/...).

    device_type=None      -> merge COM compute servers + GreenLake non-compute
                              devices (storage/network) into one list.
    device_type="COMPUTE" -> COM's real server inventory only (same data as
                              'com servers list').
    device_type=<other>   -> GreenLake claim-inventory devices of that type
                              only (COM has no concept of storage/network).
    """
    from proliant.com.devices import fetch_devices

    if device_type == "COMPUTE":
        return await fetch_servers(session)

    if device_type:
        gl_devices = await fetch_devices(session, device_type=device_type)
        return [device_to_server_row(d) for d in gl_devices]

    com_servers, gl_devices = await asyncio.gather(
        fetch_servers(session),
        fetch_devices(session),
    )
    non_compute = [
        device_to_server_row(d) for d in gl_devices
        if (d.device_type or "").upper() != "COMPUTE"
    ]
    return com_servers + non_compute
