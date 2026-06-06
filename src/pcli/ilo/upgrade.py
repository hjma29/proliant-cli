"""Firmware upgrade commands for iLO."""

from __future__ import annotations

import json
import sys
from typing import Any

from pcli.ilo.client import ServerDownOrUnreachableError, ilo_session
from pcli.ilo import firmware, inventory
from pcli.ilo.printers import _print_fw_components, _print_fw_queue


async def run_upgrade_action(
    host: dict,
    action: str,
    *,
    dry_run: bool = False,
    url: str | None = None,
    filename: str | None = None,
) -> None:
    """Handle non-auto upgrade subcommands: components, queue, stage, flash, clear."""
    try:
        async with ilo_session(host) as client:
            if action == "components":
                _print_fw_components(host["name"], await firmware.get_component_repository(client))
            elif action == "queue":
                _print_fw_queue(host["name"], await firmware.get_task_queue(client))
            elif action == "stage":
                result = await firmware.stage_from_uri(client, url, dry_run=dry_run)
                if dry_run:
                    print(f"[dry-run] Would POST to: {result['target']}")
                    print(f"[dry-run] Payload: {json.dumps(result['payload'], indent=2)}")
                else:
                    print(f"Staging initiated on {host['name']}:")
                    print(json.dumps(result, indent=2))
            elif action == "flash":
                result = await firmware.add_to_task_queue(client, filename, dry_run=dry_run)
                if dry_run:
                    print(f"[dry-run] Would POST to: {result['target']}")
                    print(f"[dry-run] Payload: {json.dumps(result['payload'], indent=2)}")
                else:
                    print(f"Queued '{filename}' for flash on next reboot ({host['name']}):")
                    print(json.dumps(result, indent=2))
            elif action == "clear":
                uris = await firmware.clear_task_queue(client, dry_run=dry_run)
                if dry_run:
                    if uris:
                        print(f"[dry-run] Would delete {len(uris)} task queue entries:")
                        for uri in uris:
                            print(f"  {uri}")
                    else:
                        print("[dry-run] Task queue is already empty.")
                else:
                    print(f"Cleared {len(uris)} task queue entries from {host['name']}.")
    except ServerDownOrUnreachableError as exc:
        print(f"ERROR: {host['name']} unreachable: {exc}", file=sys.stderr)
        sys.exit(1)
    except (RuntimeError, TimeoutError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


async def run_fw_upgrade(
    host: dict,
    *,
    dry_run: bool = False,
    reboot: bool = False,
    component: str = "all",
) -> None:
    """Run the automated firmware upgrade flow for a single host."""
    from rich import box
    from rich.console import Console
    from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
    from rich.table import Table

    from pcli.ilo import sdr
    from pcli.ilo.power import reset_server

    console = Console()
    with console.status(f"[bold cyan]Connecting to {host['name']}..."):
        try:
            async with ilo_session(host) as client:
                all_fw_full = await inventory.fetch_firmware_inventory_full(client)
                nic_fw = await inventory.fetch_nic_firmware_inventory(client)
                model_info = (await client.get(await client.get_system_uri())).get("Model", "unknown")
        except ServerDownOrUnreachableError as exc:
            console.print(f"[red]ERROR:[/red] {host['name']} unreachable: {exc}")
            sys.exit(1)

    try:
        gen = sdr.detect_gen(model_info)
    except ValueError as exc:
        console.print(f"[red]ERROR:[/red] {exc}")
        sys.exit(1)

    with console.status(f"[bold cyan]Fetching HPE SDR for Gen{gen}..."):
        try:
            pack_date, pack_url = sdr.latest_pack_url(gen)
            pack_components = sdr.list_pack(pack_url)
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]ERROR:[/red] Cannot reach HPE SDR: {exc}")
            sys.exit(1)

    candidates = sdr.find_upgrades(all_fw_full + nic_fw, pack_components)
    skip_non_updatable = ("tpm", "video controller", "nvme drive", "ssd", "dimm", "memory", "processor", "microcode", "embedded video")
    non_updatable = [
        entry for entry in all_fw_full
        if not entry.get("Updateable", True)
        and entry.get("Version", "N/A") not in ("N/A", None, "")
        and not any(key in entry.get("Name", "").lower() for key in skip_non_updatable)
    ]

    table = Table(title=f"Firmware Audit — {host['name']} ({model_info})", box=box.ROUNDED, show_lines=False)
    table.add_column("Component", style="bold")
    table.add_column("Installed", style="yellow")
    table.add_column("SDR Latest", style="cyan")
    table.add_column("Status")

    updates = [candidate for candidate in candidates if candidate.needs_update]
    up_to_date = [candidate for candidate in candidates if not candidate.needs_update]

    def _component_match(candidate: Any, comp: str) -> bool:
        if comp == "all":
            return True
        name_lower = candidate.name.lower()
        if comp == "ilo":
            return name_lower.startswith("ilo")
        if comp == "bios":
            return "system rom" in name_lower or "bios" in name_lower
        if comp == "nic":
            return bool(getattr(candidate.sdr, "chip_model", None)) or (candidate.sdr is None and candidate.updateable)
        if comp == "storage":
            return any(key in name_lower for key in ("controller", "array", "boot controller", "ns204", "nvme"))
        return True

    if component != "all":
        original_count = len(updates)
        updates = [candidate for candidate in updates if _component_match(candidate, component)]
        if len(updates) < original_count:
            console.print(f"  [dim]Filtered to component=[bold]{component}[/bold]: {len(updates)} of {original_count} updates selected[/dim]")

    for candidate in updates:
        table.add_row(candidate.name, candidate.current, candidate.sdr.filename, "[green bold]UPDATE AVAILABLE[/green bold]")
    for candidate in up_to_date:
        sdr_col = candidate.sdr.filename if candidate.sdr else "—"
        status = "[dim]up to date[/dim]" if candidate.sdr else "[dim italic]no SDR package[/dim italic]"
        table.add_row(candidate.name, candidate.current, sdr_col, status)
    for entry in non_updatable:
        table.add_row(entry.get("Name", "?"), entry.get("Version", "?"), "—", "[dim italic]not updatable via iLO[/dim italic]")

    console.print()
    console.print(table)
    console.print(f"  SDR pack: [dim]{pack_date}[/dim]  |  [green]{len(updates)} update(s) available[/green], {len(up_to_date)} up to date\n")

    if not updates:
        console.print("[green]✓ All firmware is up to date.[/green]")
        return

    if dry_run:
        console.print("[yellow][dry-run] Would stage and queue:[/yellow]")
        for candidate in updates:
            console.print(f"  • {candidate.sdr.filename}  ({candidate.current} → SDR {candidate.sdr.version_str})")
        return

    def _upgrade_priority(candidate: Any) -> int:
        name_lower = candidate.name.lower()
        if name_lower.startswith("ilo"):
            return 0
        if "system rom" in name_lower or "bios" in name_lower:
            return 1
        return 2

    updates.sort(key=_upgrade_priority)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    ) as progress:
        for idx, candidate in enumerate(updates, 1):
            filename_str = candidate.sdr.filename
            label = f"[{idx}/{len(updates)}] {filename_str}"
            is_ilo = candidate.name.lower().startswith("ilo")
            task = progress.add_task(f"{label}  staging…", total=None)
            try:
                async with ilo_session(host) as client:
                    await firmware.stage_from_uri(client, candidate.sdr.url)
                progress.update(task, description=f"{label}  waiting for iLO to download…")
                async with ilo_session(host) as client:
                    await firmware.wait_for_stage(client, filename_str, timeout=300, poll_interval=10)
                progress.update(task, description=f"{label}  queueing for flash…")
                async with ilo_session(host) as client:
                    await firmware.add_to_task_queue(client, filename_str)
                progress.update(task, description=f"[green]✓[/green] {label}  queued  ({candidate.current} → {candidate.sdr.version_str})")
                if is_ilo:
                    progress.update(task, description=f"{label}  iLO restarting (~90s)…")
                    try:
                        await firmware.wait_for_online(host, offline_grace=15, timeout=180)
                        progress.update(task, description=f"[green]✓[/green] {label}  iLO back online  ({candidate.current} → {candidate.sdr.version_str})")
                    except TimeoutError:
                        progress.update(task, description=f"[yellow]⚠[/yellow] {label}  iLO restart timed out — continuing")
            except Exception as exc:  # noqa: BLE001
                progress.update(task, description=f"[red]✗[/red] {label}  FAILED: {exc}")
                console.print(f"[red]ERROR staging {filename_str}: {exc}[/red]")

    if reboot:
        console.print(f"\n[bold yellow]Rebooting {host['name']}...[/bold yellow]")
        try:
            async with ilo_session(host) as client:
                await reset_server(client, reset_type="GracefulRestart")
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]ERROR: reboot failed: {exc}[/red]")
            sys.exit(1)

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("Waiting for server to come back online…", total=None)
            try:
                await firmware.wait_for_online(host, offline_grace=30, timeout=600)
                progress.update(task, description="[green]✓ Server back online[/green]")
            except TimeoutError as exc:
                progress.update(task, description=f"[red]✗ {exc}[/red]")
                sys.exit(1)

        console.print("\n[bold]Verifying firmware versions after reboot...[/bold]")
        async with ilo_session(host) as client:
            new_fw = dict(await inventory.fetch_all_firmware(client))

        result_table = Table(box=box.SIMPLE)
        result_table.add_column("Component")
        result_table.add_column("Before")
        result_table.add_column("After")
        result_table.add_column("Result")
        for candidate in updates:
            after = new_fw.get(candidate.name, "?")
            ok = sdr.parse_inventory_version(after) >= candidate.sdr.version
            result_table.add_row(
                candidate.name,
                candidate.current,
                after,
                "[green]✓ Updated[/green]" if ok else "[yellow]⚠ Check manually[/yellow]",
            )
        console.print(result_table)

        async with ilo_session(host) as client:
            queue = await firmware.get_task_queue(client)
            stale = [t for t in queue if t.get("State") in ("Pending", "Complete")]
            if stale:
                await firmware.clear_task_queue(client)
                console.print(f"  [dim]Cleared {len(stale)} stale task(s) from queue.[/dim]")
    else:
        # Check if any non-iLO component was updated — those need a host restart
        needs_host_restart = any(
            not candidate.name.lower().startswith("ilo") for candidate in updates
        )
        if needs_host_restart:
            console.print(
                f"\n[bold yellow]⚠  Firmware update completed.[/bold yellow] "
                f"Some components require a [bold]host restart[/bold] to activate."
            )
            try:
                answer = console.input(
                    f"  Restart [bold]{host['name']}[/bold] now? [[bold]y[/bold]/N] "
                ).strip().lower()
            except (EOFError, KeyboardInterrupt):
                answer = ""
            if answer == "y":
                console.print(f"\n[bold yellow]Rebooting {host['name']}...[/bold yellow]")
                try:
                    async with ilo_session(host) as client:
                        await reset_server(client, reset_type="GracefulRestart")
                    console.print("[green]✓ Reboot initiated.[/green]")
                except Exception as exc:  # noqa: BLE001
                    console.print(f"[red]ERROR: reboot failed: {exc}[/red]")
            else:
                console.print(
                    f"\n[bold yellow]Updates queued.[/bold yellow] Reboot [bold]{host['name']}[/bold] to apply.\n"
                    f"  • Use [bold]--reboot[/bold] flag to reboot automatically\n"
                    f"  • Run [bold]pcli ilo upgrade queue --host {host['name']}[/bold] to check queue status"
                )
        else:
            console.print(
                f"\n[bold yellow]Updates queued.[/bold yellow] Reboot [bold]{host['name']}[/bold] to apply.\n"
                f"  • Use [bold]--reboot[/bold] flag to reboot automatically\n"
                f"  • Run [bold]pcli ilo upgrade queue --host {host['name']}[/bold] to check queue status"
            )
