"""Fleet-wide inventory report commands for iLO."""

from __future__ import annotations

from pcli.common.display import get_console, print_memory_report
from pcli.common.runner import run_parallel
from pcli.ilo import inventory
from pcli.ilo.client import ilo_session
from pcli.ilo.config import MAX_WORKERS


async def run_report_memory(hosts: list[dict]) -> None:
    from pcli.com.inventory import aggregate_by_part_number

    c = get_console()
    with c.status("[dim]Fetching memory inventory across fleet…[/dim]"):
        results = await run_parallel(
            hosts, inventory.fetch_memory_report_data,
            session_factory=ilo_session, max_workers=MAX_WORKERS,
        )

    all_dimms: list[dict] = []
    for server_name, error, dimms in results:
        if error:
            c.print(f"[yellow]  {server_name}: {error}[/yellow]")
            continue
        for d in dimms:
            d["server"] = server_name
            all_dimms.append(d)

    if not all_dimms:
        c.print("[yellow]No memory inventory data returned.[/yellow]")
        return

    rows = aggregate_by_part_number(all_dimms)
    print_memory_report(rows, source="iLO")


async def run_report_cpu(hosts: list[dict]) -> None:
    from rich import box as rich_box
    from rich.table import Table

    c = get_console()
    with c.status("[dim]Fetching CPU inventory across fleet…[/dim]"):
        results = await run_parallel(
            hosts, inventory.fetch_cpu_report_data,
            session_factory=ilo_session, max_workers=MAX_WORKERS,
        )

    table = Table(
        title="CPU Inventory",
        box=rich_box.ROUNDED,
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("Server",  min_width=16, no_wrap=True)
    table.add_column("Socket",  no_wrap=True)
    table.add_column("Model",   min_width=30)
    table.add_column("Cores",   justify="right", no_wrap=True)
    table.add_column("Threads", justify="right", no_wrap=True)
    table.add_column("Max GHz", justify="right", no_wrap=True)

    for server_name, error, cpus in results:
        if error:
            table.add_row(server_name, "—", f"[yellow]{error}[/yellow]", "—", "—", "—")
            continue
        for i, cpu in enumerate(cpus):
            mhz = cpu["speed_mhz"]
            ghz = f"{mhz / 1000:.2f}" if isinstance(mhz, (int, float)) else "—"
            table.add_row(
                server_name if i == 0 else "",
                str(cpu["socket"]),
                cpu["model"],
                str(cpu["cores"]),
                str(cpu["threads"]),
                ghz,
            )

    c.print(table)


async def run_report_gpu(hosts: list[dict]) -> None:
    from rich import box as rich_box
    from rich.table import Table

    c = get_console()
    with c.status("[dim]Fetching GPU inventory across fleet…[/dim]"):
        results = await run_parallel(
            hosts, inventory.fetch_gpu_report_data,
            session_factory=ilo_session, max_workers=MAX_WORKERS,
        )

    groups: dict[str, dict] = {}
    for server_name, error, gpus in results:
        if error or not gpus:
            continue
        for gpu in gpus:
            key = gpu["name"]
            if key not in groups:
                groups[key] = {"count": 0, "servers": set()}
            groups[key]["count"] += 1
            groups[key]["servers"].add(server_name)

    if not groups:
        c.print("[yellow]No GPUs found across fleet.[/yellow]")
        return

    rows = sorted(groups.items(), key=lambda x: x[1]["count"], reverse=True)
    total = sum(v["count"] for _, v in rows)
    server_count = len({s for _, v in rows for s in v["servers"]})

    table = Table(
        title=f"GPU Inventory  ({total} GPUs across {server_count} servers)",
        box=rich_box.ROUNDED,
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("GPU Model", min_width=28)
    table.add_column("Count",     justify="right", no_wrap=True, style="bold")
    table.add_column("Servers",   min_width=20)

    for gpu_name, v in rows:
        table.add_row(
            gpu_name,
            str(v["count"]),
            ", ".join(sorted(v["servers"])),
        )

    c.print(table)
