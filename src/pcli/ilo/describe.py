"""Single-server describe command for iLO."""

from __future__ import annotations

import sys

from pcli.common.display import get_console
from pcli.ilo import inventory
from pcli.ilo.client import ILOClient, ServerDownOrUnreachableError



async def run_describe(host: dict) -> None:
    """Show full details for a single server: identity, iLO, CPU, GPU, memory, firmware."""
    from rich import box as rich_box
    from rich.panel import Panel
    from rich.table import Table

    console = get_console()

    async with ILOClient(host["url"], host["username"], host["password"]) as c:
        try:
            with console.status("[dim]Fetching server details…[/dim]"):
                # Sequential requests — iLO 6 (Gen11) does not handle concurrent
                # requests per session reliably; gather caused ConnectError on Gen11.
                sys_uri = await c.get_system_uri()
                mgr_uri = await c.get_manager_uri()
                system  = await c.get(sys_uri)
                manager = await c.get(mgr_uri)
                upd_svc = await c.get("/redfish/v1/UpdateService")
                proto   = await c.get("/redfish/v1/Managers/1/NetworkProtocol/")
                fw_list = await inventory.fetch_firmware_inventory_full(c)
                cpus    = await inventory.fetch_cpu_report_data(c)
                gpus    = await inventory.fetch_gpu_report_data(c)
                dimms   = await inventory.fetch_memory_population(c)
        except Exception as exc:
            console.print(f"[red]Error fetching server details: {type(exc).__name__}: {exc}[/red]")
            sys.exit(1)

    model      = system.get("Model", "—")
    serial     = system.get("SerialNumber", "—")
    sku        = system.get("SKU", "—")
    bios_ver   = system.get("BiosVersion", "—")
    power      = system.get("PowerState", "—")
    health_obj = (system.get("Status") or {})
    health_str = health_obj.get("Health", "—")
    uuid       = system.get("UUID", "—")

    # UID indicator LED — iLO 7 uses LocationIndicatorActive (bool),
    # iLO 6 uses IndicatorLED (str: "Lit" / "Off" / "Blinking")
    _uid_active = system.get("LocationIndicatorActive")
    _uid_led    = system.get("IndicatorLED")
    if _uid_active is not None:
        uid_str = "[bold yellow]On[/bold yellow]" if _uid_active else "[dim]Off[/dim]"
    elif _uid_led is not None:
        uid_str = "[bold yellow]On[/bold yellow]" if _uid_led == "Lit" else (
            "[bold yellow]Blinking[/bold yellow]" if _uid_led == "Blinking" else "[dim]Off[/dim]"
        )
    else:
        uid_str = "—"

    mgr_model  = manager.get("Model", "—")
    mgr_fw     = manager.get("FirmwareVersion", "—") or "—"
    mgr_host   = manager.get("HostName", "—") or "—"
    ilo_status = (manager.get("Status") or {}).get("Health", "—")

    # iLO-side cloud connect status (Oem.Hpe.CloudConnect)
    cloud      = (manager.get("Oem") or {}).get("Hpe", {}).get("CloudConnect") or {}
    cloud_ext  = cloud.get("ExtendedStatusInfo") or {}

    upd_state  = (upd_svc.get("Oem") or {}).get("Hpe", {}).get("State", "")

    _HS = {"OK": "green", "Warning": "yellow", "Critical": "red"}

    def _h(v: str | None) -> str:
        s = _HS.get(v or "", "")
        return f"[{s}]{v}[/{s}]" if s else (v or "—")

    # ── Firmware-update pending warning ───────────────────────────────────────
    if upd_state == "Complete":
        console.print(
            "[bold yellow]⚠  Firmware update completed — host restart required to activate.[/bold yellow]"
        )

    # ── Header ────────────────────────────────────────────────────────────────
    console.print(Panel(
        f"[bold]{host['name']}[/bold]   [dim]{model}[/dim]",
        expand=False,
    ))

    # ── Identity ──────────────────────────────────────────────────────────────
    id_t = Table(box=rich_box.SIMPLE, show_header=False, padding=(0, 2))
    id_t.add_column(style="dim", no_wrap=True)
    id_t.add_column()
    id_t.add_row("Serial",     serial)
    id_t.add_row("Product ID", sku)
    id_t.add_row("UUID",       uuid)
    id_t.add_row("BIOS",       bios_ver)
    id_t.add_row("Power",      _h(power))
    id_t.add_row("Health",     _h(health_str))
    id_t.add_row("UID",        uid_str)
    console.print(id_t)

    # ── iLO ───────────────────────────────────────────────────────────────────
    console.print("[bold]iLO[/bold]")
    ilo_t = Table(box=rich_box.SIMPLE, show_header=False, padding=(0, 2))
    ilo_t.add_column(style="dim", no_wrap=True)
    ilo_t.add_column()
    ilo_t.add_row("Model",    mgr_model)
    ilo_t.add_row("Firmware", mgr_fw)
    ilo_t.add_row("Hostname", mgr_host)
    ilo_t.add_row("Health",   _h(ilo_status))
    console.print(ilo_t)

    # ── Protocols ─────────────────────────────────────────────────────────────
    _PROTO_KEYS = ["IPMI", "SSH", "HTTPS", "HTTP", "SNMP", "VirtualMedia", "KVMIP"]
    proto_rows = [(k, proto.get(k, {})) for k in _PROTO_KEYS if proto.get(k)]
    if proto_rows:
        console.print("[bold]Protocols[/bold]")
        pt = Table(box=rich_box.SIMPLE, show_header=True, header_style="bold cyan", padding=(0, 2))
        pt.add_column("Protocol", no_wrap=True)
        pt.add_column("Enabled", justify="center")
        pt.add_column("Port", justify="right")
        for name, v in proto_rows:
            enabled = v.get("ProtocolEnabled")
            port    = v.get("Port")
            enabled_str = "[green]Yes[/green]" if enabled else "[dim]No[/dim]"
            port_str    = str(port) if port else "—"
            pt.add_row(name, enabled_str, port_str)
        console.print(pt)

    # ── COM ───────────────────────────────────────────────────────────────────
    if cloud:
        cc_status = cloud.get("CloudConnectStatus", "N/A")
        cc_type   = cloud.get("ConnectionType", "N/A")
        cc_ws     = cloud.get("WorkspaceId") or "(not registered)"
        cc_net    = cloud_ext.get("NetworkConfig", "N/A")
        cc_web    = cloud_ext.get("WebConnectivity", "N/A")
        cc_cfg    = cloud_ext.get("iLOConfigForCloudConnect", "N/A")

        console.print("[bold]COM[/bold]")
        com_t = Table(box=rich_box.SIMPLE, show_header=False, padding=(0, 2))
        com_t.add_column(style="dim", no_wrap=True)
        com_t.add_column()
        com_t.add_row("Status",       cc_status)
        com_t.add_row("Type",         cc_type)
        com_t.add_row("Workspace ID", cc_ws)
        com_t.add_row("Network",      cc_net)
        com_t.add_row("Web",          cc_web)
        com_t.add_row("iLO Config",   cc_cfg)
        console.print(com_t)

    # ── CPU ───────────────────────────────────────────────────────────────────
    if cpus:
        console.print("[bold]CPU[/bold]")
        cpu_t = Table(box=rich_box.SIMPLE, show_header=True, header_style="bold cyan", padding=(0, 2))
        cpu_t.add_column("Socket", no_wrap=True)
        cpu_t.add_column("Model")
        cpu_t.add_column("Cores", justify="right")
        cpu_t.add_column("Threads", justify="right")
        cpu_t.add_column("Max MHz", justify="right", style="dim")
        for cpu in cpus:
            cpu_t.add_row(
                cpu.get("socket", "—"),
                cpu.get("model", "—"),
                str(cpu.get("cores", "—")),
                str(cpu.get("threads", "—")),
                str(cpu.get("speed_mhz", "—")),
            )
        console.print(cpu_t)

    # ── GPU ───────────────────────────────────────────────────────────────────
    if gpus:
        console.print("[bold]GPU[/bold]")
        gpu_t = Table(box=rich_box.SIMPLE, show_header=True, header_style="bold cyan", padding=(0, 2))
        gpu_t.add_column("Name")
        gpu_t.add_column("Model")
        for gpu in gpus:
            gpu_t.add_row(gpu.get("name", "—"), gpu.get("model", "—"))
        console.print(gpu_t)

    # ── Memory population map ─────────────────────────────────────────────────
    if dimms:
        populated  = [d for d in dimms if d["present"]]
        empty_cnt  = sum(1 for d in dimms if not d["present"])
        total_gb   = sum(d["cap_gb"] for d in populated)
        console.print("[bold]Memory[/bold]")
        mem_t = Table(box=rich_box.SIMPLE, show_header=True, header_style="bold cyan", padding=(0, 2))
        mem_t.add_column("Slot",     no_wrap=True)
        mem_t.add_column("Capacity", justify="right")
        mem_t.add_column("Type",     no_wrap=True)
        mem_t.add_column("Speed",    justify="right")
        mem_t.add_column("Part Number", style="dim")
        for d in dimms:
            if d["present"]:
                speed_s = f"{d['speed']} MT/s" if d["speed"] else "—"
                cap_s   = f"{d['cap_gb']} GB"
                mem_t.add_row(d["slot"], cap_s, d["type"] or "—", speed_s, d["part"] or "—")
            else:
                mem_t.add_row(f"[dim]{d['slot']}[/dim]", "[dim]empty[/dim]", "", "", "")
        console.print(mem_t)
        console.print(
            f"  [dim]{len(populated)} DIMMs populated, {empty_cnt} empty"
            f" — {total_gb} GB total[/dim]"
        )

    # ── Firmware ──────────────────────────────────────────────────────────────
    if fw_list:
        if upd_state == "Complete":
            state_str = "[bold yellow]Complete[/bold yellow]  [dim](use --firmware-update for details)[/dim]"
        elif upd_state == "Idle":
            state_str = "[dim]Idle[/dim]"
        else:
            state_str = f"[dim]{upd_state}[/dim]" if upd_state else "[dim]—[/dim]"
        console.print(f"[bold]Firmware[/bold]   Update State: {state_str}")
        fw_t = Table(box=rich_box.SIMPLE, show_header=True, header_style="bold cyan", padding=(0, 2))
        fw_t.add_column("Component", no_wrap=True)
        fw_t.add_column("Version")
        fw_t.add_column("Location", style="dim")
        for fw in fw_list:
            loc = (fw.get("Oem") or {}).get("Hpe", {}).get("DeviceContext", "")
            fw_t.add_row(fw.get("Name", ""), fw.get("Version", ""), loc)
        console.print(fw_t)


async def run_describe_fw_update(host: dict) -> None:
    """Show firmware update status: UpdateService state, last bundle report, component repository."""
    from rich import box as rich_box
    from rich.table import Table

    console = get_console()

    async with ILOClient(host["url"], host["username"], host["password"]) as c:
        try:
            with console.status("[dim]Fetching firmware update details…[/dim]"):
                upd_svc    = await c.get("/redfish/v1/UpdateService")
                fw_list    = await inventory.fetch_firmware_inventory_full(c)
                comp_repo  = await inventory.fetch_component_repo_activates(c)
                # Last bundle completed report
                try:
                    bundle_meta    = await c.get("/redfish/v1/UpdateService/BundleUpdateReport/Completed")
                    bundle_entries = await c.get("/redfish/v1/UpdateService/BundleUpdateReport/Completed/Entries")
                    bundle_members = bundle_entries.get("Members", [])
                    bundle_items   = [await c.get(m["@odata.id"]) for m in bundle_members]
                except Exception:
                    bundle_meta, bundle_items = {}, []
                # Full component repository
                try:
                    repo_col    = await c.get("/redfish/v1/UpdateService/ComponentRepository")
                    repo_items  = [await c.get(m["@odata.id"]) for m in repo_col.get("Members", [])]
                except Exception:
                    repo_items = []
        except Exception as exc:
            console.print(f"[red]Error: {type(exc).__name__}: {exc}[/red]")
            return

    oem_upd    = (upd_svc.get("Oem") or {}).get("Hpe", {})
    upd_state  = oem_upd.get("State", "—")
    upd_pct    = oem_upd.get("FlashProgressPercent")
    upd_result = (oem_upd.get("Result") or {}).get("MessageId", "—")

    _ACT_STYLE = {
        "AfterReboot":      "[bold yellow]AfterReboot[/bold yellow]",
        "AfterDeviceReset": "[cyan]DeviceReset[/cyan]",
        "Immediately":      "[green]Immediately[/green]",
    }

    # ── UpdateService state ────────────────────────────────────────────────────
    console.print(f"\n[bold]UpdateService[/bold]")
    us_t = Table(box=rich_box.SIMPLE, show_header=False, padding=(0, 2))
    us_t.add_column(style="dim", no_wrap=True)
    us_t.add_column()
    state_disp = f"[bold yellow]{upd_state}[/bold yellow]" if upd_state == "Complete" else f"[dim]{upd_state}[/dim]"
    us_t.add_row("State",    state_disp)
    if upd_pct is not None:
        us_t.add_row("Progress", f"{upd_pct}%")
    us_t.add_row("Result",   upd_result)
    console.print(us_t)

    # ── Last bundle report ─────────────────────────────────────────────────────
    console.print("[bold]Last Bundle Report[/bold]")
    if bundle_items:
        bundle_name  = bundle_meta.get("Name", "—")
        bundle_dur   = bundle_meta.get("TotalInstallDurationInSec")
        dur_str      = f"  [dim]Duration: {bundle_dur}s[/dim]" if bundle_dur else ""
        console.print(f"  [dim]{bundle_name}[/dim]{dur_str}")
        lb_t = Table(box=rich_box.SIMPLE, show_header=True, header_style="bold cyan", padding=(0, 2))
        lb_t.add_column("Component", no_wrap=True)
        lb_t.add_column("Version")
        lb_t.add_column("Method", style="dim")
        lb_t.add_column("Operation", style="dim")
        lb_t.add_column("Status")
        for e in bundle_items:
            status = e.get("ComponentStatus", "—")
            status_str = f"[green]{status}[/green]" if status == "Completed" else f"[yellow]{status}[/yellow]"
            lb_t.add_row(
                e.get("DeviceName", e.get("Name", "—")),
                e.get("Version", "—"),
                e.get("BundleUpdateMethod", "—"),
                e.get("BundleUpdateOperation", "—"),
                status_str,
            )
        console.print(lb_t)
    else:
        console.print("  [dim]No completed bundle report.[/dim]")

    # ── Component repository ───────────────────────────────────────────────────
    console.print("[bold]Component Repository[/bold]")
    if repo_items:
        repo_items.sort(key=lambda r: r.get("Created") or "", reverse=True)
        cr_t = Table(box=rich_box.SIMPLE, show_header=True, header_style="bold cyan", padding=(0, 2))
        cr_t.add_column("Name")
        cr_t.add_column("Version")
        cr_t.add_column("Activates", no_wrap=True)
        cr_t.add_column("Criticality", style="dim")
        cr_t.add_column("Staged", style="dim")
        for r in repo_items:
            act_raw = r.get("Activates", "")
            cr_t.add_row(
                r.get("Name", "—"),
                r.get("Version", "—"),
                _ACT_STYLE.get(act_raw, act_raw or "—"),
                r.get("Criticality", "—"),
                (r.get("Created") or "—")[:10],
            )
        console.print(cr_t)
    else:
        console.print("  [dim]Component repository is empty.[/dim]")

    # ── Firmware inventory with repo markers ───────────────────────────────────
    if fw_list:
        console.print("[bold]Firmware Inventory[/bold]")
        fi_t = Table(box=rich_box.SIMPLE, show_header=True, header_style="bold cyan", padding=(0, 2))
        fi_t.add_column("Component", no_wrap=True)
        fi_t.add_column("Version")
        fi_t.add_column("Location", style="dim")
        fi_t.add_column("Activates", no_wrap=True)
        for fw in fw_list:
            oem     = (fw.get("Oem") or {}).get("Hpe", {})
            loc     = oem.get("DeviceContext", "")
            dc      = oem.get("DeviceClass", "")
            act_raw = comp_repo.get(dc, "") if dc else ""
            act_str = _ACT_STYLE.get(act_raw, "") if act_raw else ""
            name    = fw.get("Name", "")
            name_str = f"[yellow]*[/yellow] {name}" if act_raw else f"  {name}"
            fi_t.add_row(name_str, fw.get("Version", ""), loc, act_str)
        console.print(fi_t)


async def run_describe_ilo_nic(host: dict) -> None:
    """Show iLO dedicated NIC details: DHCP/static, IP, DNS, routes, LLDP, MAC."""
    from rich import box as rich_box
    from rich.panel import Panel
    from rich.table import Table

    console = get_console()

    async with ILOClient(host["url"], host["username"], host["password"]) as c:
        try:
            with console.status("[dim]Fetching iLO NIC details…[/dim]"):
                nic = await inventory.fetch_ilo_nic_details(c)
        except Exception as exc:
            console.print(f"[red]Error fetching iLO NIC details: {type(exc).__name__}: {exc}[/red]")
            sys.exit(1)

    if not nic:
        console.print("[red]No iLO EthernetInterface data found.[/red]")
        sys.exit(1)

    if "not identified" in nic.get("selection_note", ""):
        console.print(f"[yellow]Warning: {nic['selection_note']}[/yellow]")

    console.print(Panel(
        f"[bold]{host['name']}[/bold]   [dim]iLO Dedicated Network Port[/dim]",
        expand=False,
    ))

    def _kv_table() -> Table:
        t = Table(box=rich_box.SIMPLE, show_header=False, padding=(0, 2))
        t.add_column(style="dim", no_wrap=True)
        t.add_column()
        return t

    # ── General ───────────────────────────────────────────────────────────────
    gen_t = _kv_table()
    speed = nic.get("speed_mbps")
    speed_str = f"{speed} Mbps" if speed else "—"
    gen_t.add_row("MAC",           nic.get("mac", "—"))
    gen_t.add_row("Link",          nic.get("link_status", "—"))
    gen_t.add_row("Speed",         speed_str)
    gen_t.add_row("Connected via", nic.get("connected_via", "—"))
    console.print(gen_t)

    # ── IPv4 ──────────────────────────────────────────────────────────────────
    console.print("[bold]IPv4[/bold]")
    ipv4_t = _kv_table()
    dhcp_on = nic.get("dhcp_enabled")
    mode = "DHCP" if dhcp_on else "Static"
    ipv4_t.add_row("Mode", f"[green]{mode}[/green]" if dhcp_on else f"[cyan]{mode}[/cyan]")

    cur = nic.get("current_ipv4")
    # Detect stale data: DHCP is on but API returned a Static-origin address
    cur_origin = (cur or {}).get("origin", "")
    stale = dhcp_on and cur and cur_origin.lower() not in ("dhcp", "")

    if cur:
        addr_label = f"[dim]Address (stale — DHCP active)[/dim]" if stale else "Address"
        ipv4_t.add_row(addr_label,        cur["address"])
        ipv4_t.add_row("Subnet Mask",     cur["subnet"])
        ipv4_t.add_row("Default Gateway", cur["gateway"])
        if cur_origin:
            ipv4_t.add_row("[dim]Origin[/dim]", f"[dim]{cur_origin}[/dim]")

    if stale:
        connected_url = nic.get("connected_via", "")
        ipv4_t.add_row("", "")
        ipv4_t.add_row("[yellow]⚠ Note[/yellow]",
                       f"[yellow]iLO API may show stale static address after DHCP change.\n"
                       f"  Actual DHCP address is in 'Connected via' above.[/yellow]")

    # Show configured static values when DHCP is active (so user knows the fallback)
    sta = nic.get("static_ipv4")
    if sta and dhcp_on and not stale:
        ipv4_t.add_row("", "")
        ipv4_t.add_row("[dim]Configured Static Address[/dim]",  f"[dim]{sta['address']}[/dim]")
        ipv4_t.add_row("[dim]Configured Static Subnet[/dim]",   f"[dim]{sta['subnet']}[/dim]")
        ipv4_t.add_row("[dim]Configured Static Gateway[/dim]",  f"[dim]{sta['gateway']}[/dim]")
    elif sta and not dhcp_on and not cur:
        ipv4_t.add_row("Address",         sta["address"])
        ipv4_t.add_row("Subnet Mask",     sta["subnet"])
        ipv4_t.add_row("Default Gateway", sta["gateway"])

    console.print(ipv4_t)

    # ── DNS ───────────────────────────────────────────────────────────────────
    dns = nic.get("dns_servers", [])
    console.print("[bold]DNS[/bold]")
    dns_t = _kv_table()
    if dns:
        labels = ["Primary", "Secondary", "Tertiary"]
        for i, server in enumerate(dns):
            label = labels[i] if i < len(labels) else f"DNS {i + 1}"
            dns_t.add_row(label, server)
    else:
        dns_t.add_row("", "[dim]None configured[/dim]")
    console.print(dns_t)

    # ── Static Routes ─────────────────────────────────────────────────────────
    routes = nic.get("static_routes", [])
    if routes:
        console.print("[bold]Static Routes[/bold]")
        rt_t = Table(box=rich_box.SIMPLE, show_header=True, header_style="bold cyan", padding=(0, 2))
        rt_t.add_column("Destination", no_wrap=True)
        rt_t.add_column("Subnet Mask", no_wrap=True)
        rt_t.add_column("Gateway",     no_wrap=True)
        for r in routes:
            rt_t.add_row(r["destination"], r["subnet"], r["gateway"])
        console.print(rt_t)

    # ── LLDP ──────────────────────────────────────────────────────────────────
    lldp = nic.get("lldp_enabled")
    console.print("[bold]LLDP[/bold]")
    lldp_t = _kv_table()
    if lldp is None:
        lldp_t.add_row("Status", "[dim]Not exposed by iLO[/dim]")
    elif lldp:
        lldp_t.add_row("Status", "[green]Enabled[/green]")
    else:
        lldp_t.add_row("Status", "Disabled")
    console.print(lldp_t)

