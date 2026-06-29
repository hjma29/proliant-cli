"""
proliant.oneview.topology
~~~~~~~~~~~~~~~~~~~~~~~~~~~
End-to-end connectivity maps for HPE Synergy, for network troubleshooting.

Builds an "ASCII map" of how a network (or a single MAC address) travels
through the Synergy fabric:

    upstream switch ─ uplink port ─ Virtual Connect interconnect
                    ─ downlink ─ server profile connection ─ server

Data sources (all read-only):
  GET /rest/ethernet-networks        → the network (VLAN id, type)
  GET /rest/network-sets             → network-set membership
  GET /rest/uplink-sets              → external uplink ports per network
  GET /rest/interconnects            → VC module name / bay / enclosure
  GET /rest/server-profiles          → connections (port, interconnect, MAC)
  GET /rest/server-hardware          → server name / device bay
  GET /rest/logical-interconnects    → LI name + forwarding-information-base

Key Synergy fact: a VC interconnect downlink port number equals the device
bay it serves, which also equals the connection's ``interconnectPort``.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from rich.tree import Tree

if TYPE_CHECKING:
    from proliant.oneview.client import OneViewClient


# ── data gathering ────────────────────────────────────────────────────────────

class _Fabric:
    """Indexed snapshot of the fabric, shared by network and MAC maps."""

    def __init__(self, nets, netsets, uplinks, ics, hw, lis):
        self.nets = nets
        self.netsets = netsets
        self.uplinks = uplinks
        self.lis = lis

        self.net_by_uri = {n["uri"]: n for n in nets if n.get("uri")}
        self.net_by_name = {n.get("name", "").lower(): n for n in nets}
        self.ns_members = {
            ns["uri"]: set(ns.get("networkUris") or [])
            for ns in netsets
            if ns.get("uri")
        }
        self.li_by_uri = {li["uri"]: li.get("name", "") for li in lis if li.get("uri")}

        self.ic_by_uri: dict[str, dict] = {}
        self.ic_by_encl_bay: dict[tuple[str, str], dict] = {}
        for ic in ics:
            entries = (ic.get("interconnectLocation") or {}).get("locationEntries", [])
            loc = {e.get("type"): e.get("value") for e in entries}
            rec = {
                "name": ic.get("name", ""),
                "bay": loc.get("Bay", ""),
                "enclosure": loc.get("Enclosure", ""),
            }
            if ic.get("uri"):
                self.ic_by_uri[ic["uri"]] = rec
            if rec["enclosure"] and rec["bay"]:
                self.ic_by_encl_bay[(rec["enclosure"], rec["bay"])] = rec

        self.hw_by_uri = {
            h["uri"]: {"name": h.get("name", ""), "bay": h.get("position", "")}
            for h in hw
            if h.get("uri")
        }


async def _gather(client: "OneViewClient") -> _Fabric:
    nets, netsets, uplinks, ics, hw, lis = await asyncio.gather(
        client.get_all("/rest/ethernet-networks"),
        client.get_all("/rest/network-sets"),
        client.get_all("/rest/uplink-sets"),
        client.get_all("/rest/interconnects"),
        client.get_all("/rest/server-hardware"),
        client.get_all("/rest/logical-interconnects"),
    )
    return _Fabric(nets, netsets, uplinks, ics, hw, lis)


def _short_ic(name: str) -> str:
    """'Enclosure-01, interconnect 6' → 'interconnect 6'."""
    return name.split(", ")[-1] if name else name


def _carries_network(conn_net_uri: str, net_uri: str, fabric: _Fabric) -> bool:
    """True if a connection (single network or network-set) carries the network."""
    if not conn_net_uri:
        return False
    if conn_net_uri == net_uri:
        return True
    return net_uri in fabric.ns_members.get(conn_net_uri, set())


# ── network map ───────────────────────────────────────────────────────────────

async def build_network_map(
    client: "OneViewClient",
    network_name: str,
    profiles: list[dict] | None = None,
    fabric: _Fabric | None = None,
) -> dict:
    """Build the end-to-end topology for one ethernet network.

    Returns a structured dict consumed by :func:`render_network_map`.
    """
    if fabric is None:
        fabric = await _gather(client)
    if profiles is None:
        profiles = await client.get_all("/rest/server-profiles")

    net = fabric.net_by_name.get(network_name.lower())
    if not net:
        known = ", ".join(sorted(n.get("name", "") for n in fabric.nets))
        raise ValueError(f"Network '{network_name}' not found. Known: {known}")
    net_uri = net["uri"]

    # Upstream: uplink sets that include this network
    uplinks = []
    for u in fabric.uplinks:
        if net_uri not in (u.get("networkUris") or []):
            continue
        ports = []
        for pci in u.get("portConfigInfos") or []:
            entries = (pci.get("location") or {}).get("locationEntries", [])
            loc = {e.get("type"): e.get("value") for e in entries}
            ic = fabric.ic_by_encl_bay.get((loc.get("Enclosure", ""), loc.get("Bay", "")))
            ports.append({
                "ic_name": ic["name"] if ic else f"bay {loc.get('Bay', '?')}",
                "bay": loc.get("Bay", ""),
                "port": loc.get("Port", ""),
            })
        ports.sort(key=lambda p: (str(p["bay"]), p["port"]))
        uplinks.append({
            "uplink_set": u.get("name", ""),
            "li_name": fabric.li_by_uri.get(u.get("logicalInterconnectUri", ""), ""),
            "ports": ports,
        })
    uplinks.sort(key=lambda u: u["uplink_set"])

    # Downstream: server profile connections that carry this network
    servers: list[dict] = []
    for p in profiles:
        cs = p.get("connectionSettings") or {}
        conns = []
        for c in cs.get("connections") or p.get("connections") or []:
            if not _carries_network(c.get("networkUri") or "", net_uri, fabric):
                continue
            ic = fabric.ic_by_uri.get(c.get("interconnectUri") or "", {})
            ic_port = c.get("interconnectPort")
            conns.append({
                "name": c.get("name", "") or "(unnamed)",
                "port_id": c.get("portId", ""),
                "mac": c.get("mac", ""),
                "ic_name": ic.get("name", ""),
                "ic_bay": ic.get("bay", ""),
                "ic_uri": c.get("interconnectUri") or "",
                "downlink": ic_port if isinstance(ic_port, int) else "",
                "highlight": False,
            })
        if not conns:
            continue
        hw = fabric.hw_by_uri.get(p.get("serverHardwareUri") or "", {})
        servers.append({
            "profile": p.get("name", ""),
            "server_name": hw.get("name", ""),
            "bay": hw.get("bay", p.get("enclosureBay", "")),
            "connections": conns,
        })
    servers.sort(key=lambda s: (str(s["bay"]), s["profile"]))

    return {
        "network": {
            "name": net.get("name", ""),
            "vlan": net.get("vlanId", ""),
            "type": net.get("ethernetNetworkType", ""),
            "uri": net_uri,
        },
        "uplinks": uplinks,
        "servers": servers,
    }


# ── MAC trace ─────────────────────────────────────────────────────────────────

async def trace_mac(client: "OneViewClient", mac: str) -> list[dict]:
    """Trace where a MAC address lives and how it reaches the fabric.

    Returns one network-map per (network, downlink) the MAC was learned on,
    with the owning server connection highlighted.  Empty list if not found.
    """
    fabric = await _gather(client)
    profiles = await client.get_all("/rest/server-profiles")
    mac_l = mac.lower()

    # Find the MAC in every active LI forwarding table
    active = [li for li in fabric.lis if li.get("stackingHealth", "") != "NotApplicable"]

    async def _fib(li: dict) -> list[dict]:
        data = await client.get(
            li["uri"] + "/forwarding-information-base",
            params={"filter": [f"macAddress='{mac}'"]},
        )
        return data.get("members", [])

    fib_lists = await asyncio.gather(*[_fib(li) for li in active])
    hits = [m for sub in fib_lists for m in sub]
    if not hits:
        return []

    # One map per distinct network the MAC was seen on
    maps: list[dict] = []
    seen_nets: set[str] = set()
    for hit in hits:
        net_uri = hit.get("networkUri", "")
        net = fabric.net_by_uri.get(net_uri)
        if not net or net_uri in seen_nets:
            continue
        seen_nets.add(net_uri)

        nm = await build_network_map(
            client, net.get("name", ""), profiles=profiles, fabric=fabric
        )

        # Highlight the owning connection: prefer exact MAC, else match the
        # interconnect + downlink the FIB entry was actually learned on.
        iface = (hit.get("networkInterface") or "").lower()
        hit_ic = hit.get("interconnectUri", "")
        dl = None
        if iface.startswith("downlink"):
            head = iface[len("downlink"):].strip().split(":", 1)[0].strip()
            dl = int(head) if head.isdigit() else None
        for s in nm["servers"]:
            for c in s["connections"]:
                if (c["mac"] or "").lower() == mac_l:
                    c["highlight"] = True
                elif dl is not None and c["downlink"] == dl and c["ic_uri"] == hit_ic:
                    c["highlight"] = True
        nm["mac"] = mac
        nm["learned_on"] = hit.get("networkInterface", "")
        nm["entry_type"] = hit.get("entryType", "")
        maps.append(nm)

    return maps


# ── rendering ─────────────────────────────────────────────────────────────────

def render_network_map(m: dict, mac: str = "") -> Tree:
    """Render a network map dict as a Rich tree (the ASCII diagram)."""
    net = m["network"]
    title = f"[bold cyan]{net['name']}[/bold cyan]  ·  VLAN {net['vlan']}  ·  {net['type']}"
    if mac:
        extra = f"  ·  MAC [bold]{mac}[/bold]"
        if m.get("learned_on"):
            extra += f" learned on [dim]{m['learned_on']}[/dim]"
        title += extra
    tree = Tree(title)

    # Upstream
    up = tree.add("[bold]▲ Upstream uplinks[/bold]")
    if m["uplinks"]:
        for u in m["uplinks"]:
            li = f"  [dim](LI {u['li_name']})[/dim]" if u["li_name"] else ""
            ub = up.add(f"Uplink Set [green]{u['uplink_set']}[/green]{li}")
            for p in u["ports"]:
                ub.add(
                    f"{_short_ic(p['ic_name'])} [dim](bay {p['bay']})[/dim] "
                    f"── uplink [yellow]{p['port']}[/yellow] ▶ upstream switch"
                )
    else:
        up.add("[dim]none — internal network (no external uplink)[/dim]")

    # Downstream
    down = tree.add("[bold]▼ Downlink servers[/bold]")
    if m["servers"]:
        for s in m["servers"]:
            bay = f"bay {s['bay']}" if s["bay"] != "" else "unassigned"
            name = s["server_name"] or "[dim](no server hardware)[/dim]"
            sb = down.add(
                f"[dim]{bay}[/dim]  [cyan]{s['profile']}[/cyan]  ·  {name}"
            )
            for c in s["connections"]:
                dl = f"downlink {c['downlink']}" if c["downlink"] != "" else "downlink ?"
                ic = f"{_short_ic(c['ic_name'])} (bay {c['ic_bay']})" if c["ic_name"] else "?"
                line = (
                    f"[white]{c['name']}[/white]  {c['port_id']} "
                    f"── [yellow]{dl}[/yellow] ─ {ic}"
                )
                if c["highlight"]:
                    line = "[bold green]●[/bold green] " + line + "  [bold green]◀ HERE[/bold green]"
                sb.add(line)
    else:
        down.add("[dim]none — no server profile connection uses this network[/dim]")

    return tree
