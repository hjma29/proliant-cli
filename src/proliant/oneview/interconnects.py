"""
proliant.oneview.interconnects
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Logical Interconnects (LI), Logical Interconnect Groups (LIG),
Interconnect hardware, and the MAC forwarding-information-base.

Key endpoints:
  GET /rest/logical-interconnects                   -> all LIs
  GET /rest/logical-interconnect-groups             -> all LIGs
  GET /rest/interconnects                           -> IC hardware
  GET {ic_uri}/statistics                           -> live CPU/memory (instant)
  GET {ic_uri}/utilization                          -> CPU/memory/power/temperature history
  GET {li_uri}/forwarding-information-base          -> MAC address table
"""
from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from proliant.oneview.client import OneViewClient


# ── Logical Interconnects (LI) ────────────────────────────────────────────────

def parse_li(raw: dict) -> dict:
    return {
        "name":        raw.get("name", ""),
        "consistency": raw.get("consistencyStatus", ""),
        "stacking":    raw.get("stackingHealth", ""),
        "state":       raw.get("state", ""),
        "status":      raw.get("status", ""),
        "lig_uri":     raw.get("logicalInterconnectGroupUri", ""),
        "uri":         raw.get("uri", ""),
    }


async def list_lis(client: "OneViewClient") -> list[dict]:
    raw_lis, raw_ligs = await asyncio.gather(
        client.get_all("/rest/logical-interconnects"),
        client.get_all("/rest/logical-interconnect-groups"),
    )
    lig_map = {lg["uri"]: lg.get("name", "") for lg in raw_ligs}
    lis = [parse_li(li) for li in raw_lis]
    for li in lis:
        li["lig_name"] = lig_map.get(li["lig_uri"], "")
    return sorted(lis, key=lambda li: li["name"])


# ── Logical Interconnect Groups (LIG) ────────────────────────────────────────

def parse_lig(raw: dict) -> dict:
    return {
        "name":   raw.get("name", ""),
        "state":  raw.get("state", ""),
        "status": raw.get("status", ""),
        "uri":    raw.get("uri", ""),
    }


async def list_ligs(client: "OneViewClient") -> list[dict]:
    raw = await client.get_all("/rest/logical-interconnect-groups")
    return sorted([parse_lig(lg) for lg in raw], key=lambda lg: lg["name"])


# ── Interconnect hardware ─────────────────────────────────────────────────────

def parse_ic(raw: dict) -> dict:
    return {
        "name":   raw.get("name", ""),
        "model":  raw.get("model", ""),
        "state":  raw.get("state", ""),
        "status": raw.get("status", ""),
        "serial": raw.get("serialNumber", ""),
        "li_uri": raw.get("logicalInterconnectUri", ""),
        "uri":    raw.get("uri", ""),
    }


async def list_interconnects(client: "OneViewClient") -> list[dict]:
    raw_ics, raw_lis = await asyncio.gather(
        client.get_all("/rest/interconnects"),
        client.get_all("/rest/logical-interconnects"),
    )
    li_map = {li["uri"]: li.get("name", "") for li in raw_lis}
    ics = [parse_ic(ic) for ic in raw_ics]
    for ic in ics:
        ic["li_name"] = li_map.get(ic["li_uri"], "")
    return sorted(ics, key=lambda ic: ic["name"])


# ── MAC address table ─────────────────────────────────────────────────────────

def parse_mac_entry(raw: dict) -> dict:
    return {
        "mac":        raw.get("macAddress", ""),
        "ic_name":    raw.get("interconnectName", ""),
        "ic_uri":     raw.get("interconnectUri", ""),
        "port":       raw.get("networkInterface", ""),
        "network":    raw.get("networkName", ""),
        "net_uri":    raw.get("networkUri", ""),
        "vlan":       raw.get("externalVlan", ""),
        "entry_type": raw.get("entryType", ""),
        "last_updated": _mac_last_updated(raw),
        "profile":    "",
        "connection": "",
        "internal_vlan": False,
    }


def _mac_last_updated(raw: dict) -> str:
    for key in (
        "lastUpdated",
        "lastUpdate",
        "lastUpdatedTime",
        "lastSeen",
        "lastSeenTime",
        "timestamp",
        "modified",
        "created",
    ):
        value = raw.get(key)
        if value:
            return str(value)
    return ""


def _downlink_port_num(iface: str) -> int | None:
    """Return the interconnect downlink port number from a FIB port label.

    Downlink labels are server-facing, e.g. ``downlink 6:1-2`` → 6.  The
    leading number equals the ``interconnectPort`` of the server profile
    connection cabled to that downlink.  Uplink labels (``Q5:2``) → None.
    """
    s = (iface or "").strip().lower()
    if not s.startswith("downlink"):
        return None
    head = s[len("downlink"):].strip().split(":", 1)[0].strip()
    try:
        return int(head)
    except ValueError:
        return None


async def build_profile_maps(
    client: "OneViewClient",
) -> tuple[
    dict[str, tuple[str, str]],
    dict[tuple[str, int], str],
    dict[tuple[str, int], list[tuple[str, str, frozenset]]],
]:
    """Build server-profile lookup maps for MAC table enrichment.

    Returns ``(mac_map, port_map, port_conns)``:
      * ``mac_map``  — ``mac.lower()`` → ``(profile_name, connection_name)``;
        exact match for a connection's assigned virtual MAC.
      * ``port_map`` — ``(interconnect_uri, interconnectPort)`` → ``profile_name``;
        resolves any MAC learned on a downlink to the owning server profile,
        even VM/guest MACs behind the server.
      * ``port_conns`` — ``(interconnect_uri, interconnectPort)`` → list of
        ``(connection_name, network_uri, netset_member_uris)``; used to pin the
        specific connection by matching the learned network/VLAN.
    """
    profiles, netsets = await asyncio.gather(
        client.get_all("/rest/server-profiles"),
        client.get_all("/rest/network-sets"),
    )
    ns_members = {
        ns["uri"]: frozenset(ns.get("networkUris") or [])
        for ns in netsets
        if ns.get("uri")
    }

    mac_map: dict[str, tuple[str, str]] = {}
    port_map: dict[tuple[str, int], str] = {}
    port_conns: dict[tuple[str, int], list[tuple[str, str, frozenset]]] = {}
    for p in profiles:
        pname = p.get("name", "") or ""
        cs = p.get("connectionSettings") or {}
        for c in cs.get("connections") or p.get("connections") or []:
            mac = (c.get("mac") or "").lower()
            cname = c.get("name", "") or ""
            if mac:
                mac_map[mac] = (pname, cname)
            ic = c.get("interconnectUri")
            port = c.get("interconnectPort")
            if ic and isinstance(port, int):
                key = (ic, port)
                port_map.setdefault(key, pname)
                nu = c.get("networkUri") or ""
                port_conns.setdefault(key, []).append(
                    (cname, nu, ns_members.get(nu, frozenset()))
                )
    return mac_map, port_map, port_conns


async def build_tunnel_port_map(
    client: "OneViewClient",
) -> dict[tuple[str, str], str]:
    """Map ``(interconnect_uri, uplink_port)`` → tunnel network name.

    Virtual Connect carries a *tunnel* network transparently and represents it
    with an internal VLAN (e.g. 4094), so MACs learned on a tunnel uplink come
    back from the FIB with a blank ``networkName`` and that internal VLAN.  This
    map lets the MAC table attribute such entries to the owning tunnel network.
    """
    uplinks, nets, ics = await asyncio.gather(
        client.get_all("/rest/uplink-sets"),
        client.get_all("/rest/ethernet-networks"),
        client.get_all("/rest/interconnects"),
    )
    net_type = {n["uri"]: n.get("ethernetNetworkType", "")
                for n in nets if n.get("uri")}
    net_name = {n["uri"]: n.get("name", "") for n in nets if n.get("uri")}

    ic_by_encl_bay: dict[tuple[str, str], str] = {}
    for ic in ics:
        entries = (ic.get("interconnectLocation") or {}).get("locationEntries", [])
        loc = {e.get("type"): e.get("value") for e in entries}
        if loc.get("Enclosure") and loc.get("Bay") and ic.get("uri"):
            ic_by_encl_bay[(loc["Enclosure"], loc["Bay"])] = ic["uri"]

    out: dict[tuple[str, str], str] = {}
    for u in uplinks:
        # A tunnel uplink set either declares the type itself or carries a
        # single Tunnel-type ethernet network.
        tunnel_name = ""
        if u.get("ethernetNetworkType") == "Tunnel":
            for nu in u.get("networkUris") or []:
                tunnel_name = net_name.get(nu, "")
                if tunnel_name:
                    break
        if not tunnel_name:
            for nu in u.get("networkUris") or []:
                if net_type.get(nu) == "Tunnel":
                    tunnel_name = net_name.get(nu, "")
                    break
        if not tunnel_name:
            continue
        for pci in u.get("portConfigInfos") or []:
            loc = {e.get("type"): e.get("value")
                   for e in (pci.get("location") or {}).get("locationEntries", [])}
            ic_uri = ic_by_encl_bay.get((loc.get("Enclosure", ""), loc.get("Bay", "")))
            port = loc.get("Port", "")
            if ic_uri and port:
                out[(ic_uri, port)] = tunnel_name
    return out


def _resolve_connection(
    net_uri: str,
    conns: list[tuple[str, str, frozenset]],
) -> str:
    """Pick the connection on a downlink that carries the learned network.

    Direct network match wins; falls back to network-set membership.  Returns
    "" when ambiguous or unmatched (e.g. the learned VLAN is on no connection).
    """
    if not net_uri:
        return ""
    for cname, nu, _members in conns:
        if nu and nu == net_uri:
            return cname
    for cname, _nu, members in conns:
        if net_uri in members:
            return cname
    return ""


def enrich_mac_entries(
    entries: list[dict],
    mac_map: dict[str, tuple[str, str]],
    port_map: dict[tuple[str, int], str],
    port_conns: dict[tuple[str, int], list[tuple[str, str, frozenset]]],
    tunnel_ports: dict[tuple[str, str], str] | None = None,
) -> None:
    """Populate ``profile`` / ``connection`` on each MAC entry, in place.

    Exact MAC match resolves both profile and connection.  Otherwise a downlink
    port maps to the owning server profile, and the connection is resolved by
    matching the entry's learned network against that profile's connections.

    Entries learned on a *tunnel* uplink come back with a blank network name
    (Virtual Connect uses an internal VLAN for tunnels); they are attributed to
    the owning tunnel network via ``tunnel_ports``.
    """
    tunnel_ports = tunnel_ports or {}
    for e in entries:
        if not e.get("network"):
            tname = tunnel_ports.get((e.get("ic_uri", ""), e.get("port", "")))
            if tname:
                e["network"] = tname
                e["internal_vlan"] = True
        mac = (e.get("mac") or "").lower()
        if mac in mac_map:
            e["profile"], e["connection"] = mac_map[mac]
            continue
        port_num = _downlink_port_num(e.get("port", ""))
        if port_num is None:
            continue
        key = (e.get("ic_uri", ""), port_num)
        prof = port_map.get(key)
        if prof:
            e["profile"] = prof
            e["connection"] = _resolve_connection(
                e.get("net_uri", ""), port_conns.get(key, [])
            )


async def get_mac_table(
    client: "OneViewClient",
    address: str = "",
    vlan: int = 0,
) -> list[dict]:
    """Query MAC forwarding-information-base across all active LIs.

    OneView returns the forwarding table per LI (Virtual Connect domain).
    Requires at least one of address or vlan to avoid pulling the full table.
    Entries are enriched with the owning server profile / connection name.
    """
    raw_lis = await client.get_all("/rest/logical-interconnects")
    # Only VC stacking LIs (NotApplicable = standalone/non-VC)
    active_lis = [li for li in raw_lis if li.get("stackingHealth", "") != "NotApplicable"]

    filters: list[str] = []
    if address:
        filters.append(f"macAddress='{address}'")
    if vlan:
        filters.append(f"externalVlan='{vlan}'")

    params = {"filter": filters} if filters else None

    results: list[dict] = []
    lock = asyncio.Lock()
    maps: dict[str, tuple] = {}

    async def _query_one(li: dict) -> None:
        uri = li.get("uri", "") + "/forwarding-information-base"
        data = await client.get(uri, params=params)
        entries = [parse_mac_entry(e) for e in data.get("members", [])]
        async with lock:
            results.extend(entries)

    async def _load_profiles() -> None:
        try:
            maps["v"] = await build_profile_maps(client)
        except Exception:
            maps["v"] = ({}, {}, {})

    async def _load_tunnels() -> None:
        try:
            maps["t"] = await build_tunnel_port_map(client)
        except Exception:
            maps["t"] = {}

    await asyncio.gather(
        _load_profiles(), _load_tunnels(), *[_query_one(li) for li in active_lis]
    )

    # Deduplicate
    seen: set[tuple] = set()
    unique: list[dict] = []
    for m in results:
        key = (m["mac"], m["ic_name"], m["vlan"])
        if key not in seen:
            seen.add(key)
            unique.append(m)

    mac_map, port_map, port_conns = maps.get("v", ({}, {}, {}))
    tunnel_ports = maps.get("t", {})
    enrich_mac_entries(unique, mac_map, port_map, port_conns, tunnel_ports)

    return sorted(unique, key=lambda m: (m["mac"], m["ic_name"]))


# ── Single-interconnect detail (matches OneView's interconnect detail page) ───

def _ic_location_map(raw: dict) -> dict[str, str]:
    entries = (raw.get("interconnectLocation") or {}).get("locationEntries", [])
    return {e.get("type", ""): e.get("value", "") for e in entries}


def _ip_by_type(ip_list: list[dict] | None) -> dict[str, str]:
    return {e.get("ipAddressType", ""): e.get("ipAddress", "") for e in ip_list or []}


_SPEED_RE_G = re.compile(r"^(\d+)(?:x(\d+))?G$")
_SPEED_RE_M = re.compile(r"^(\d+)M$")


def parse_port_speed(raw: str | None) -> str:
    """Normalize a port's ``operationalSpeed`` for display.

    ``'Speed10G'`` -> ``'10'``; ``'Speed4x25G'`` -> ``'4x25'``;
    ``'Auto'``/``'SpeedUnknown'`` (no link) -> ``'unknown'``; blank -> ``''``.
    """
    if not raw:
        return ""
    if raw in ("Auto", "SpeedUnknown"):
        return "unknown"
    if raw.startswith("Speed"):
        rest = raw[len("Speed"):]
        m = _SPEED_RE_G.match(rest)
        if m:
            return f"{m.group(1)}x{m.group(2)}" if m.group(2) else m.group(1)
        m = _SPEED_RE_M.match(rest)
        if m:
            mbps = int(m.group(1))
            # 'Speed0M' means "no link negotiated" (same idea as SpeedUnknown)
            # -- OneView reports it for every Unlinked port -- not a real 0 Gb/s.
            return "" if mbps == 0 else str(mbps)
        return rest.lower() if rest else ""
    return raw


def format_adapter_port(port_id: str) -> str:
    """Server-profile connection ``portId`` -> GUI-style adapter port label.

    ``'Mezz 3:2-a'`` -> ``'Mezzanine 3:2'`` (drops the per-connection ``-a``
    suffix, since it names a virtual sub-function, not the physical port).
    Other adapter names (``'FlexLOM 1:1'``) pass through unchanged.
    """
    m = re.match(r"^Mezz\s+(\d+):(\d+)", port_id or "")
    if m:
        return f"Mezzanine {m.group(1)}:{m.group(2)}"
    return re.sub(r"-[a-zA-Z]$", "", (port_id or "").strip())


def _connected_to(neighbor: dict | None) -> str:
    if not neighbor:
        return "none"
    chassis = neighbor.get("remoteChassisId") or neighbor.get("remoteSystemName") or ""
    port = neighbor.get("remotePortId") or neighbor.get("remotePortDescription") or ""
    if chassis and port:
        return f"{chassis} ({port})"
    return chassis or port or "none"


def _uplink_port_visible(port: dict) -> bool:
    """Whether an uplink port row should be shown, matching the GUI.

    A splittable QSFP cage's *parent* row (e.g. ``'Q1'``) is hidden once it is
    populated and split into ``'Q1:1'``..``'Q1:4'`` -- OneView only shows the
    active subports then. When unpopulated (no transceiver), the parent is
    shown alongside its placeholder subports (verified live: an unpopulated
    cage's own row carries ``portStatusReason == "Unpopulated"``).
    """
    if ":" in (port.get("portName") or ""):
        return True
    return port.get("portStatusReason") == "Unpopulated"


def _port_sort_key(port_name: str) -> tuple[int, int]:
    m = re.match(r"^[A-Za-z]+(\d+)(?::(\d+))?$", port_name or "")
    if not m:
        return (0, 0)
    return (int(m.group(1)), int(m.group(2)) if m.group(2) else -1)


def _port_wwn(port: dict) -> str:
    fc = port.get("fcPortProperties") or {}
    return fc.get("portWwpn") or fc.get("wwpn") or fc.get("worldWideName") or "n/a"


def _metric(metric_list: list[dict] | None, name: str) -> dict | None:
    for m in metric_list or []:
        if m.get("metricName") == name:
            return m
    return None


def _latest_metric_value(metric_list: list[dict] | None, name: str) -> float | None:
    m = _metric(metric_list, name)
    if not m:
        return None
    samples = m.get("metricSamples") or []
    series = samples[-1] if samples else []
    return series[-1][-1] if series else None


def _to_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _component_version_for_product(components: list[dict], product_name: str) -> str:
    """Find this interconnect model's bundled firmware version in an SPP baseline.

    A baseline's ``fwComponents`` list has one entry per hardware component it
    covers (e.g. ``'HPE Virtual Connect SE 100Gb F32 Module for Synergy
    Firmware install package'``); matching the interconnect's own
    ``productName`` as a substring reliably picks the right one (verified
    live: exactly one match for this appliance's registered baselines).
    """
    needle = (product_name or "").strip().lower()
    if not needle:
        return ""
    for c in components:
        name = (c.get("name") or c.get("componentName") or "").lower()
        if needle in name:
            return c.get("componentVersion") or c.get("version") or ""
    return ""


def _resolve_baseline_uri(enclosure_uri: str, raw_les: list[dict]) -> str:
    """Firmware baselines are assigned at the Logical Enclosure -- neither the
    interconnect nor its Logical Interconnect carries its own baseline field
    (confirmed live: no firmware-named key on either resource)."""
    for le in raw_les:
        if enclosure_uri and enclosure_uri in (le.get("enclosureUris") or []):
            return (le.get("firmware") or {}).get("firmwareBaselineUri", "") or ""
    return ""


def _build_downlink_map(
    raw_profiles: list[dict], raw_server_hw: list[dict],
) -> dict[tuple[str, int], dict]:
    """``(interconnect_uri, interconnectPort)`` -> server hardware / adapter port / profile.

    Mirrors ``build_profile_maps()``'s connection-scanning approach but keeps
    the extra fields (``serverHardwareUri``, connection ``portId``) needed for
    the Downlink Ports table's Server Hardware / Adapter Port columns.
    """
    hw_name = {h.get("uri", ""): h.get("name", "") for h in raw_server_hw}
    out: dict[tuple[str, int], dict] = {}
    for p in raw_profiles:
        pname = p.get("name", "") or ""
        hw_uri = p.get("serverHardwareUri") or ""
        cs = p.get("connectionSettings") or {}
        for c in cs.get("connections") or p.get("connections") or []:
            ic_uri = c.get("interconnectUri")
            port = c.get("interconnectPort")
            if ic_uri and isinstance(port, int):
                out.setdefault((ic_uri, port), {
                    "server_profile": pname,
                    "server_hardware": hw_name.get(hw_uri, ""),
                    "adapter_port": format_adapter_port(c.get("portId", "")),
                })
    return out


_DOWNLINK_PORT_RE = re.compile(r"^\D*(\d+)$")


async def describe_interconnect(client: "OneViewClient", name: str) -> dict:
    """Return a rich single-interconnect detail dict mirroring the OneView
    GUI's interconnect detail page (General / Hardware / Interconnect Link
    Ports / Uplink Ports / Downlink Ports / Utilization / Remote Support)."""
    raw_ics = await client.get_all("/rest/interconnects")
    matched = [ic for ic in raw_ics if ic.get("name", "").lower() == name.lower()]
    if not matched:
        known = ", ".join(sorted(ic.get("name", "") for ic in raw_ics))
        raise ValueError(f"Interconnect '{name}' not found. Known: {known}")
    raw = matched[0]
    uri = raw.get("uri", "")
    enc_uri = raw.get("enclosureUri", "")

    (
        raw_lis, raw_les, raw_drivers, raw_uplinksets, raw_profiles, raw_server_hw, stats, util,
    ) = await asyncio.gather(
        client.get_all("/rest/logical-interconnects"),
        client.get_all("/rest/logical-enclosures"),
        client.get_all("/rest/firmware-drivers"),
        client.get_all("/rest/uplink-sets"),
        client.get_all("/rest/server-profiles"),
        client.get_all("/rest/server-hardware"),
        client.get(f"{uri}/statistics"),
        client.get(f"{uri}/utilization"),
    )

    li_map = {li.get("uri", ""): li.get("name", "") for li in raw_lis}
    uplinkset_names = {u.get("uri", ""): u.get("name", "") for u in raw_uplinksets}
    uplinkset_types = {u.get("uri", ""): u.get("networkType", "") for u in raw_uplinksets}
    downlink_map = _build_downlink_map(raw_profiles, raw_server_hw)
    # Downlink port -> device bay is a fixed physical (midplane) wiring fact,
    # independent of any server profile -- a Linked port's own LLDP-style
    # neighbor names the connected server hardware by its own resource ID
    # (verified live), which is the authoritative source. The profile
    # connection map above is only a fallback for Unlinked-but-configured
    # ports (no neighbor to read) and for Adapter Port / Server Profile,
    # which aren't exposed anywhere outside the profile connection.
    hw_by_id = {h["uri"].rsplit("/", 1)[-1]: h.get("name", "") for h in raw_server_hw if h.get("uri")}

    baseline_uri = _resolve_baseline_uri(enc_uri, raw_les)
    baseline = next((d for d in raw_drivers if d.get("uri", "") == baseline_uri), None) if baseline_uri else None
    # The baseline resource splits its display name across two fields --
    # e.g. name="HPE Synergy Service Pack", version="SY-2023.05.01" -- the
    # GUI shows them joined ("HPE Synergy Service Pack SY-2023.05.01").
    baseline_name = " ".join(
        part for part in ((baseline or {}).get("name", ""), (baseline or {}).get("version", "")) if part
    )
    baseline_version = _component_version_for_product(
        (baseline or {}).get("fwComponents") or [], raw.get("productName", "")
    )

    loc = _ic_location_map(raw)
    ips = _ip_by_type(raw.get("ipAddressList"))

    general = {
        "logical_interconnect": li_map.get(raw.get("logicalInterconnectUri", ""), ""),
        "power": raw.get("powerState", ""),
        "state": raw.get("state", ""),
        "firmware_baseline_name": baseline_name,
        "firmware_baseline_uri": baseline_uri,
        "firmware_version_from_baseline": baseline_version,
        "installed_firmware_version": raw.get("firmwareVersion", ""),
        "mgmt_interface": raw.get("mgmtInterface") or "none",
        "stacking_domain_id": str(raw.get("stackingDomainId", "") or ""),
        "stacking_member_id": str(raw.get("stackingMemberId", "") or ""),
        "stacking_domain_role": raw.get("stackingDomainRole", ""),
        "host_name": raw.get("hostName", ""),
        "ipv4": ips.get("Ipv4Dhcp") or ips.get("Ipv4Static") or "",
        "ipv4_type": "DHCP" if "Ipv4Dhcp" in ips else ("Static" if "Ipv4Static" in ips else ""),
        "ipv6": ips.get("Ipv6LinkLocal") or ips.get("Ipv6Static") or "",
        "ipv6_type": "link-local" if "Ipv6LinkLocal" in ips else ("static" if "Ipv6Static" in ips else ""),
    }

    hardware = {
        "product_name": raw.get("productName", ""),
        "location": f"{raw.get('enclosureName', '')}, interconnect bay {loc.get('Bay', '')}".strip(", "),
        "mgmt_mac": raw.get("interconnectMAC", ""),
        "base_wwn": raw.get("baseWWN", ""),
        "serial_number": raw.get("serialNumber", ""),
        "part_number": raw.get("partNumber", ""),
        "spare_part_number": raw.get("sparePartNumber", ""),
        "health": raw.get("interconnectHardwareHealth", ""),
    }

    link_ports: list[dict] = []
    uplink_ports: list[dict] = []
    downlink_ports: list[dict] = []
    ports = sorted(raw.get("ports") or [], key=lambda p: _port_sort_key(p.get("portName", "")))
    for p in ports:
        ptype = p.get("portType")
        pname = p.get("portName", "")
        if ptype == "Extension":
            link_ports.append({
                "port": pname.upper(),
                "state": p.get("portStatus", ""),
                "connected_to": _connected_to(p.get("neighbor")),
            })
        elif ptype == "Uplink":
            if not _uplink_port_visible(p):
                continue
            uplinkset_uri = p.get("associatedUplinkSetUri") or ""
            uplink_ports.append({
                "port": pname,
                "type": uplinkset_types.get(uplinkset_uri, "") if uplinkset_uri else "",
                "state": p.get("portStatus", ""),
                "speed": parse_port_speed(p.get("operationalSpeed")),
                "uplink_set": uplinkset_names.get(uplinkset_uri, "") if uplinkset_uri else "",
                "port_wwn": _port_wwn(p),
                "connector_type": p.get("connectorType") or "n/a",
                "connected_to": _connected_to(p.get("neighbor")),
            })
        elif ptype == "Downlink":
            m = _DOWNLINK_PORT_RE.match(pname)
            port_num = int(m.group(1)) if m else None
            info = downlink_map.get((uri, port_num), {}) if port_num is not None else {}
            neighbor = p.get("neighbor")
            server_hw = info.get("server_hardware", "")
            if neighbor:
                server_hw = hw_by_id.get(neighbor.get("remoteChassisId", ""), "") or server_hw
            downlink_ports.append({
                "port": m.group(1) if m else pname,
                "state": p.get("portStatus", ""),
                "speed": parse_port_speed(p.get("operationalSpeed")),
                "server_hardware": server_hw,
                "adapter_port": info.get("adapter_port", ""),
                "server_profile": info.get("server_profile", ""),
            })

    util_list = (util or {}).get("metricList") or []
    cpu_metric = _metric(util_list, "Cpu")
    cpu_pct = None
    if cpu_metric:
        samples = cpu_metric.get("metricSamples") or []
        series = samples[-1] if samples else []
        vals = [v for _, v in series]
        if vals:
            cpu_pct = round(sum(vals) / len(vals), 1)
    if cpu_pct is None:
        cpu_pct = _to_float(((stats or {}).get("moduleStatistics") or {}).get("cpuUsage"))

    mem_used = _latest_metric_value(util_list, "Memory")
    mem_cap = (_metric(util_list, "Memory") or {}).get("metricCapacity")
    power_avg = _latest_metric_value(util_list, "PowerAverageWatts")
    power_cap = (_metric(util_list, "PowerAverageWatts") or {}).get("metricCapacity")

    utilization = {
        "cpu_pct": cpu_pct,
        "memory_used_mb": mem_used,
        "memory_capacity_mb": mem_cap,
        "memory_pct": round(mem_used / mem_cap * 100) if mem_used and mem_cap else None,
        "power_avg_w": power_avg,
        "power_capacity_w": power_cap,
        "temperature_f": _latest_metric_value(util_list, "Temperature"),
    }

    remote_support = raw.get("remoteSupport") or {}
    remote = {
        "enabled": (remote_support.get("supportState") or "").lower() == "enabled",
        "state": remote_support.get("supportState", ""),
    }

    return {
        "name": raw.get("name", ""),
        "status": raw.get("status", ""),
        "state": raw.get("state", ""),
        "uri": uri,
        "general": general,
        "hardware": hardware,
        "link_ports": link_ports,
        "uplink_ports": uplink_ports,
        "downlink_ports": downlink_ports,
        "utilization": utilization,
        "remote_support": remote,
    }
