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
        self.ns_name_by_uri = {
            ns["uri"]: ns.get("name", "") for ns in netsets if ns.get("uri")
        }
        self.li_by_uri = {li["uri"]: li.get("name", "") for li in lis if li.get("uri")}

        self.ic_by_uri: dict[str, dict] = {}
        self.ic_by_encl_bay: dict[tuple[str, str], dict] = {}
        # (enclosure, bay, portName) → {"switch", "port"} learned via LLDP
        self.neighbor_by_loc: dict[tuple[str, str, str], dict] = {}
        for ic in ics:
            entries = (ic.get("interconnectLocation") or {}).get("locationEntries", [])
            loc = {e.get("type"): e.get("value") for e in entries}
            rec = {
                "name": ic.get("name", ""),
                "bay": loc.get("Bay", ""),
                "enclosure": loc.get("Enclosure", ""),
                "uri": ic.get("uri", ""),
            }
            if ic.get("uri"):
                self.ic_by_uri[ic["uri"]] = rec
            if rec["enclosure"] and rec["bay"]:
                self.ic_by_encl_bay[(rec["enclosure"], rec["bay"])] = rec
            for p in ic.get("ports") or []:
                pname = p.get("portName") or ""
                nb = p.get("neighbor") or {}
                switch = (nb.get("remoteSystemName") or nb.get("remoteChassisId")
                          or nb.get("remoteMgmtAddress") or "")
                swport = (nb.get("remotePortId") or nb.get("remotePortDescription") or "")
                if pname and (switch or swport):
                    self.neighbor_by_loc[(rec["enclosure"], rec["bay"], pname)] = {
                        "switch": switch, "port": swport,
                    }

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
    network_name: str = "",
    profiles: list[dict] | None = None,
    fabric: _Fabric | None = None,
    vlan: int | None = None,
) -> dict:
    """Build the end-to-end topology for one ethernet network.

    The network may be selected either by ``network_name`` or by ``vlan`` ID.
    Returns a structured dict consumed by :func:`render_network_map`.
    """
    if fabric is None:
        fabric = await _gather(client)
    if profiles is None:
        profiles = await client.get_all("/rest/server-profiles")

    if vlan is not None and not network_name:
        matches = [n for n in fabric.nets if n.get("vlanId") == vlan]
        if not matches:
            known = ", ".join(
                str(v) for v in sorted(
                    {n.get("vlanId") for n in fabric.nets if n.get("vlanId") is not None}
                )
            )
            raise ValueError(f"No network found for VLAN {vlan}. Known VLANs: {known}")
        if len(matches) > 1:
            names = ", ".join(sorted(n.get("name", "") for n in matches))
            raise ValueError(
                f"VLAN {vlan} maps to multiple networks: {names}. "
                f"Re-run with --network-name to choose one."
            )
        net = matches[0]
    else:
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
        native_uri = u.get("nativeNetworkUri") or ""
        ports = []
        for pci in u.get("portConfigInfos") or []:
            entries = (pci.get("location") or {}).get("locationEntries", [])
            loc = {e.get("type"): e.get("value") for e in entries}
            ic = fabric.ic_by_encl_bay.get((loc.get("Enclosure", ""), loc.get("Bay", "")))
            nb = fabric.neighbor_by_loc.get(
                (loc.get("Enclosure", ""), loc.get("Bay", ""), loc.get("Port", "")), {}
            )
            ports.append({
                "ic_name": ic["name"] if ic else f"bay {loc.get('Bay', '?')}",
                "ic_uri": ic.get("uri", "") if ic else "",
                "bay": loc.get("Bay", ""),
                "port": loc.get("Port", ""),
                "neighbor_switch": nb.get("switch", ""),
                "neighbor_port": nb.get("port", ""),
                "highlight": False,
            })
        ports.sort(key=lambda p: (str(p["bay"]), p["port"]))
        networks = []
        for uri in (u.get("networkUris") or []):
            n = fabric.net_by_uri.get(uri, {})
            if not n.get("name"):
                continue
            networks.append({
                "name": n.get("name", ""),
                "vlan": n.get("vlanId", ""),
                "native": uri == native_uri,
            })
        network_sets = [
            fabric.ns_name_by_uri.get(uri, "")
            for uri in (u.get("networkSetUris") or [])
        ]
        uplinks.append({
            "uplink_set": u.get("name", ""),
            "li_name": fabric.li_by_uri.get(u.get("logicalInterconnectUri", ""), ""),
            "ports": ports,
            "networks": networks,
            "network_sets": [s for s in network_sets if s],
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


async def build_network_set_map(
    client: "OneViewClient",
    network_set_name: str,
    profiles: list[dict] | None = None,
    fabric: "_Fabric | None" = None,
) -> dict:
    """Build the end-to-end topology for a network set.

    Upstream: uplink sets that reference this network set via ``networkSetUris``.
    Downstream: server profile connections whose ``networkUri`` equals the
    network set URI (i.e. connections assigned directly to the network set,
    not individual member networks).
    """
    if fabric is None:
        fabric = await _gather(client)
    if profiles is None:
        profiles = await client.get_all("/rest/server-profiles")

    ns = next((s for s in fabric.netsets if s.get("name", "").lower() == network_set_name.lower()), None)
    if not ns:
        raise ValueError(f"Network set '{network_set_name}' not found.")
    ns_uri = ns["uri"]

    # Upstream: uplink sets that carry this network set
    uplinks = []
    for u in fabric.uplinks:
        if ns_uri not in (u.get("networkSetUris") or []):
            continue
        native_uri = u.get("nativeNetworkUri") or ""
        ports = []
        for pci in u.get("portConfigInfos") or []:
            entries = (pci.get("location") or {}).get("locationEntries", [])
            loc = {e.get("type"): e.get("value") for e in entries}
            ic = fabric.ic_by_encl_bay.get((loc.get("Enclosure", ""), loc.get("Bay", "")))
            nb = fabric.neighbor_by_loc.get(
                (loc.get("Enclosure", ""), loc.get("Bay", ""), loc.get("Port", "")), {}
            )
            ports.append({
                "ic_name": ic["name"] if ic else f"bay {loc.get('Bay', '?')}",
                "ic_uri": ic.get("uri", "") if ic else "",
                "bay": loc.get("Bay", ""),
                "port": loc.get("Port", ""),
                "neighbor_switch": nb.get("switch", ""),
                "neighbor_port": nb.get("port", ""),
                "highlight": False,
            })
        ports.sort(key=lambda p: (str(p["bay"]), p["port"]))
        # Member networks carried via this uplink set
        networks = []
        for uri in (u.get("networkUris") or []):
            n = fabric.net_by_uri.get(uri, {})
            if not n.get("name"):
                continue
            networks.append({
                "name": n.get("name", ""),
                "vlan": n.get("vlanId", ""),
                "native": uri == native_uri,
            })
        network_sets = [
            fabric.ns_name_by_uri.get(uri, "")
            for uri in (u.get("networkSetUris") or [])
        ]
        uplinks.append({
            "uplink_set": u.get("name", ""),
            "li_name": fabric.li_by_uri.get(u.get("logicalInterconnectUri", ""), ""),
            "ports": ports,
            "networks": networks,
            "network_sets": [s for s in network_sets if s],
        })
    uplinks.sort(key=lambda u: u["uplink_set"])

    # Downstream: server profile connections assigned directly to this network set
    servers: list[dict] = []
    for p in profiles:
        cs = p.get("connectionSettings") or {}
        conns = []
        for c in cs.get("connections") or p.get("connections") or []:
            if (c.get("networkUri") or "") != ns_uri:
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
            "name": ns.get("name", ""),
            "vlan": "",
            "type": "NetworkSet",
            "uri": ns_uri,
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

    # Tunnel-learned MACs return with a blank networkName/networkUri (Virtual
    # Connect represents the tunnel with an internal VLAN), so attribute them to
    # the owning tunnel network by the uplink port they were learned on.
    from proliant.oneview.interconnects import build_tunnel_port_map
    tunnel_ports = await build_tunnel_port_map(client)

    # One map per distinct network the MAC was seen on.  Real networks are
    # listed first, internal tunnel networks last.
    maps: list[dict] = []
    tunnel_maps: list[dict] = []
    seen_nets: set[str] = set()
    for hit in hits:
        net_uri = hit.get("networkUri", "")
        net = fabric.net_by_uri.get(net_uri)
        is_tunnel = False
        if not net:
            tname = tunnel_ports.get(
                (hit.get("interconnectUri", ""), hit.get("networkInterface", ""))
            )
            if not tname:
                continue
            net = fabric.net_by_name.get(tname.lower())
            if not net:
                continue
            net_uri = net["uri"]
            is_tunnel = True
        if net_uri in seen_nets:
            continue
        seen_nets.add(net_uri)

        nm = await build_network_map(
            client, net.get("name", ""), profiles=profiles, fabric=fabric
        )

        # Highlight the owning connection: prefer exact MAC, else match the
        # interconnect + downlink the FIB entry was actually learned on.
        iface = (hit.get("networkInterface") or "").lower()
        hit_ic = hit.get("interconnectUri", "")
        learned_on = hit.get("networkInterface", "")
        dl = None
        if iface.startswith("downlink"):
            head = iface[len("downlink"):].strip().split(":", 1)[0].strip()
            dl = int(head) if head.isdigit() else None
        matched_server = False
        for s in nm["servers"]:
            for c in s["connections"]:
                if (c["mac"] or "").lower() == mac_l:
                    c["highlight"] = True
                    matched_server = True
                elif dl is not None and c["downlink"] == dl and c["ic_uri"] == hit_ic:
                    c["highlight"] = True
                    matched_server = True
        # MACs learned on an uplink port live upstream — highlight that uplink
        # port instead of any server downlink.
        matched_uplink = False
        if not matched_server and not iface.startswith("downlink"):
            for u in nm["uplinks"]:
                for p in u["ports"]:
                    same_port = p["port"].lower() == learned_on.lower()
                    same_ic = (not hit_ic) or p.get("ic_uri") == hit_ic
                    if learned_on and same_port and same_ic:
                        p["highlight"] = True
                        matched_uplink = True
        nm["mac"] = mac
        nm["mac_on_uplink"] = matched_uplink
        nm["learned_on"] = learned_on
        nm["entry_type"] = hit.get("entryType", "")
        nm["learned_vlan"] = hit.get("externalVlan", "")
        nm["last_updated"] = _mac_last_updated(hit)
        nm["internal_vlan"] = is_tunnel
        if is_tunnel:
            tunnel_maps.append(nm)
        else:
            maps.append(nm)

    return maps + tunnel_maps


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


# ── rendering ─────────────────────────────────────────────────────────────────

def render_network_map(m: dict, mac: str = "") -> Tree:
    """Render a network map dict as a Rich tree (the ASCII diagram)."""
    net = m["network"]
    title = f"[bold cyan]{net['name']}[/bold cyan]  ·  VLAN {net['vlan']}  ·  {net['type']}"
    if mac:
        extra = f"  ·  MAC [bold]{mac}[/bold]"
        if m.get("learned_on"):
            extra += f" learned on [dim]{m['learned_on']}[/dim]"
        if m.get("last_updated"):
            extra += f"  ·  Last updated [dim]{m['last_updated']}[/dim]"
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
                    line = "[bold cyan]●[/bold cyan] " + line + "  [bold cyan]◀ Learned from[/bold cyan]"
                sb.add(line)
    else:
        down.add("[dim]none — no server profile connection uses this network[/dim]")

    return tree


# ── ASCII box diagram ─────────────────────────────────────────────────────────

class _Canvas:
    """A growable character grid for composing ASCII art."""

    def __init__(self) -> None:
        self.rows: list[list[str]] = []

    def _ensure(self, r: int, c: int) -> None:
        while len(self.rows) <= r:
            self.rows.append([])
        row = self.rows[r]
        while len(row) <= c:
            row.append(" ")

    def put(self, r: int, c: int, ch: str) -> None:
        self._ensure(r, c)
        self.rows[r][c] = ch

    def text(self, r: int, c: int, s: str) -> None:
        for i, ch in enumerate(s):
            self.put(r, c + i, ch)

    def render(self) -> str:
        return "\n".join("".join(row).rstrip() for row in self.rows)


def _make_box(lines: list[str]) -> tuple[list[str], int]:
    """Return (box_lines, total_width) for a rectangle around ``lines``."""
    inner = max((len(s) for s in lines), default=0)
    top = "┌" + "─" * (inner + 2) + "┐"
    bot = "└" + "─" * (inner + 2) + "┘"
    body = ["│ " + s.ljust(inner) + " │" for s in lines]
    return [top, *body, bot], len(top)


def render_network_map_ascii(m: dict, mac: str = "", color: bool = False) -> str:
    """Render a network map dict as a vertical ASCII box diagram.

    When ``color`` is True the returned string carries Rich markup tags that
    highlight the traced network, the LLDP switch/port neighbours, and the
    ``native`` tag (the caller must print with ``markup=True``). When False the
    output is plain text (no markup).

    A left-hand trunk descends from the uplink-set box; each server profile
    branches off to the right as its own box (connections folded inside)::

        uplink ports:  vc-3 Q5:2, vc-6 Q5:2
        networks:      VLAN-160
              │
        ┌─────┴────┐
        │ ACI-MAP  │
        │ VLAN-160 │
        └─────┬────┘
              │     ┌────────────────────────────┐
              ├─────┤ aci-FM-host1               │
              │     │ conn mgmt1 · net VLAN-160  │
              │     └────────────────────────────┘
              └─────┤ ...                        │

    This grows downward instead of sideways, so it never wraps the terminal.
    """
    net = m["network"]
    servers = m["servers"]
    uplinks = m["uplinks"]
    mac_on_uplink = bool(mac and m.get("mac_on_uplink"))

    # ── header ────────────────────────────────────────────────────────────────
    if mac:
        # MAC trace: the MAC is the filter, so lead with it.
        header = f"MAC {mac}"
        if m.get("learned_on"):
            header += f"  ·  learned on {m['learned_on']}"
        if m.get("last_updated"):
            header += f"  ·  Last updated {m['last_updated']}"
        net_vlan = net["vlan"]
        if (net_vlan == "" or net_vlan == 0 or net_vlan is None) and m.get("learned_vlan"):
            net_vlan = m["learned_vlan"]
        net_desc = f"VLAN {net_vlan}"
        if net.get("type"):
            net_desc += f", {net['type']}"
        header += f"  ·  {net['name']}  ({net_desc})"
    else:
        if net.get("type") == "NetworkSet":
            header = f"{net['name']}  ·  Network Set"
        else:
            header = f"{net['name']}  ·  VLAN {net['vlan']}"
            if net.get("type"):
                header += f"  ·  {net['type']}"

    # ── aggregate uplink ports, networks, network sets across the uplink set(s) ─
    uplink_set_name = uplinks[0]["uplink_set"] if uplinks else ""
    li_name = uplinks[0]["li_name"] if uplinks else ""
    port_lines: list[str] = []
    network_lines: list[str] = []
    network_sets: list[str] = []
    seen_nets: set[str] = set()
    for u in uplinks:
        for p in u["ports"]:
            line = f"vc-{p['bay']} {p['port']}" if p["bay"] else p["port"]
            sw = p.get("neighbor_switch", "")
            swp = p.get("neighbor_port", "")
            if sw or swp:
                line += f"  →  {(sw + ' ' + swp).strip()}"
            if p.get("highlight"):
                line += "  ◀ Learned from"
            port_lines.append(line)
        for n in u["networks"]:
            if n["name"] in seen_nets:
                continue
            seen_nets.add(n["name"])
            bits = []
            if n.get("vlan") != "" and n.get("vlan") is not None:
                bits.append(f"vlan {n['vlan']}")
            if n.get("native"):
                bits.append("native")
            tag = f"  ({', '.join(bits)})" if bits else ""
            network_lines.append(f"{n['name']}{tag}")
        for s in u["network_sets"]:
            if s not in network_sets:
                network_sets.append(s)

    # ── root box = the uplink set + its logical interconnect ────────────────────
    root_lines = [f"uplinkset: {uplink_set_name or '(none)'}"]
    if li_name:
        root_lines.append(f"Logical Interconnect: {li_name}")
    root_box, root_w = _make_box(root_lines)

    # MAC traces should focus on the learned endpoint, not every profile that
    # happens to use the same network. Network describes still show all servers.
    display_servers = servers
    if mac and not mac_on_uplink:
        display_servers = []
        for s in servers:
            learned_connections = [c for c in s["connections"] if c.get("highlight")]
            if learned_connections:
                focused = dict(s)
                focused["connections"] = learned_connections
                display_servers.append(focused)

    # ── child boxes = one per server profile (connections folded inside) ────────
    children: list[dict] = []
    for s in display_servers:
        box_lines = [s["profile"] or "(profile)"]
        if s["server_name"]:
            box_lines.append(s["server_name"])
        for c in s["connections"]:
            tag = "  ◀ Learned from" if c["highlight"] else ""
            box_lines.append(f"{c['name']}  ·  {net['name']}{tag}")
        box, w = _make_box(box_lines)
        children.append({"box": box, "w": w, "h": len(box)})

    if not children:
        canvas = _Canvas()
        canvas.text(0, 0, header)
        for i, line in enumerate(root_box):
            canvas.text(2 + i, 0, line)
        empty_message = "(no matching server profile connection found for this MAC)" if mac else \
            "(no server profile connection uses this network)"
        canvas.text(2 + len(root_box) + 1, 0, empty_message)
        out = canvas.render()
        if color:
            out = _colorize_diagram(out, m)
        return out

    # When a MAC was learned on an uplink it lives upstream — the local server
    # profiles do not own it, so suppress them and focus on the uplink path.
    show_servers = not mac_on_uplink

    # ── vertical layout: trunk on the left, profile boxes branching right ───────
    root_x = 0
    trunk_col = root_w // 2
    box_col = trunk_col + 6  # indent for the ├──── branch connector

    canvas = _Canvas()
    canvas.text(0, 0, header)
    r = 2

    # ── upstream trunk to the switches, with networks + ports to its right ──────
    ann_lines: list[str] = ["networks:"]
    ann_lines += [f"  {nl}" for nl in (network_lines or ["(none)"])]
    if network_sets:
        ann_lines.append(f"network set:  {', '.join(network_sets)}")
    ann_lines.append("uplink ports:")
    ann_lines += [f"  {pl}" for pl in (port_lines or ["(internal — no uplink)"])]

    canvas.put(r, trunk_col, "│")  # line continues up to the network switches
    r += 1
    for line in ann_lines:
        canvas.put(r, trunk_col, "│")
        canvas.text(r, trunk_col + 3, line)
        r += 1
    canvas.put(r, trunk_col, "│")  # stub into the root box
    r += 1

    # ── root box (uplink set) ────────────────────────────────────────────────────
    root_top = r
    for i, line in enumerate(root_box):
        canvas.text(root_top + i, root_x, line)
    canvas.put(root_top, trunk_col, "┴")                      # uplink line into top
    r = root_top + len(root_box)

    if not show_servers:
        # MAC lives upstream: close the diagram below the root box with a note.
        canvas.text(r + 1, root_x,
                    "↑ MAC learned upstream via the highlighted uplink port — "
                    "no local server profile owns it.")
        out = canvas.render()
        if color:
            out = _colorize_diagram(out, m)
        return out

    canvas.put(root_top + len(root_box) - 1, trunk_col, "┬")  # trunk drop from bottom

    # ── profile boxes stacked vertically, each tapped off the trunk ─────────────
    child_mids: list[int] = []
    cur = r + 1  # one trunk row below the root box
    for ch in children:
        top = cur
        for k, line in enumerate(ch["box"]):
            canvas.text(top + k, box_col, line)
        mid = top + ch["h"] // 2
        child_mids.append(mid)
        for col in range(trunk_col + 1, box_col):
            canvas.put(mid, col, "─")
        canvas.put(mid, box_col, "┤")  # branch enters the box's left border
        cur = top + ch["h"] + 1         # blank row between boxes

    # ── draw the trunk and its tees ──────────────────────────────────────────────
    for row in range(r, child_mids[-1] + 1):
        canvas.put(row, trunk_col, "│")
    for i, mid in enumerate(child_mids):
        canvas.put(mid, trunk_col, "└" if i == len(child_mids) - 1 else "├")

    out = canvas.render()
    if color:
        out = _colorize_diagram(out, m)
    return out


def _colorize_diagram(text: str, m: dict) -> str:
    """Wrap key tokens in Rich markup: the filter (MAC or network), LLDP
    switch/port neighbours, the ``native`` tag, and the ``◀ Learned from`` marker."""
    from rich.markup import escape

    mac = m.get("mac", "")
    net_name = (m.get("network") or {}).get("name", "")
    switches: set[str] = set()
    ports: set[str] = set()
    for u in m.get("uplinks") or []:
        for p in u.get("ports") or []:
            if p.get("neighbor_switch"):
                switches.add(p["neighbor_switch"])
            if p.get("neighbor_port"):
                ports.add(p["neighbor_port"])

    # Longest first so a name that is a substring of another is wrapped correctly.
    switch_list = sorted(switches, key=len, reverse=True)
    port_list = sorted(ports, key=len, reverse=True)

    styled = []
    for line in text.splitlines():
        line = escape(line)
        # The active filter is highlighted: the MAC on a MAC trace, otherwise
        # the traced network name.
        if mac:
            tok = escape(mac)
            line = line.replace(tok, f"[bold cyan]{tok}[/bold cyan]")
        elif net_name:
            tok = escape(net_name)
            line = line.replace(tok, f"[bold cyan]{tok}[/bold cyan]")
        for sw in switch_list:
            tok = escape(sw)
            line = line.replace(tok, f"[magenta]{tok}[/magenta]")
        for pt in port_list:
            tok = escape(pt)
            line = line.replace(tok, f"[magenta]{tok}[/magenta]")
        line = line.replace("native", "[bold yellow]native[/bold yellow]")
        line = line.replace("◀ Learned from", "[bold cyan]◀ Learned from[/bold cyan]")
        if m.get("internal_vlan") and m.get("learned_vlan"):
            tok = escape(f"VLAN {m['learned_vlan']}")
            line = line.replace(tok, f"[grey50]{tok}[/grey50]")
        styled.append(line)
    return "\n".join(styled)
