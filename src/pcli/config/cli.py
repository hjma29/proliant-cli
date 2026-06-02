"""
pcli.config.cli — config subcommands: list inventory.

Usage:
    pcli config list inventory
"""
from __future__ import annotations

import argparse
import configparser
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table
from rich import box

# Reuse the same config-file search logic from ilo/config
from pcli.ilo.config import HOSTS_FILE

console = Console()


# ── Commands ───────────────────────────────────────────────────────────────────

def _cmd_list_inventory() -> None:
    if not HOSTS_FILE.exists():
        console.print(
            f"[red]Config file not found:[/red] {HOSTS_FILE}\n"
            "Run [bold]pcli ilo init[/bold] to create a starter config."
        )
        sys.exit(1)

    cfg = configparser.ConfigParser()
    cfg.read(HOSTS_FILE)

    default_user = cfg.get("defaults", "username", fallback="Administrator")

    ilo_hosts: list[dict] = []
    ov_host: dict | None = None

    for section in cfg.sections():
        if section.lower() == "defaults":
            continue
        if section.lower() == "oneview":
            ov_host = {
                "host": cfg.get(section, "host", fallback=""),
                "username": cfg.get(section, "username", fallback=default_user),
            }
        else:
            ilo_hosts.append({
                "name": section,
                "host": cfg.get(section, "host", fallback=""),
                "username": cfg.get(section, "username", fallback=default_user),
            })

    console.print(f"[dim]Config: {HOSTS_FILE}[/dim]\n")

    # ── iLO table ──────────────────────────────────────────────────────────────
    if ilo_hosts:
        t = Table(
            title="[bold]iLO Hosts[/bold]",
            box=box.SIMPLE_HEAD,
            show_header=True,
            header_style="bold cyan",
            padding=(0, 1),
            title_justify="left",
        )
        t.add_column("#", justify="right", style="dim", no_wrap=True)
        t.add_column("Name", style="green", no_wrap=True)
        t.add_column("Address", no_wrap=True)
        t.add_column("Username")

        for i, h in enumerate(ilo_hosts, 1):
            t.add_row(str(i), h["name"], h["host"], h["username"])
        console.print(t)
    else:
        console.print("[yellow]No iLO hosts configured.[/yellow]")

    # ── OneView ────────────────────────────────────────────────────────────────
    if ov_host:
        t2 = Table(
            title="[bold]OneView[/bold]",
            box=box.SIMPLE_HEAD,
            show_header=True,
            header_style="bold cyan",
            padding=(0, 1),
            title_justify="left",
        )
        t2.add_column("Address", no_wrap=True)
        t2.add_column("Username")
        t2.add_row(ov_host["host"], ov_host["username"])
        console.print(t2)
    else:
        console.print("[dim]No [oneview] section configured.[/dim]")


# ── Argument parser ────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pcli config",
        description="Manage pcli configuration.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  pcli config list inventory    Show all iLO hosts and OneView appliance from hosts-ilo.ini
""",
    )
    sub = p.add_subparsers(dest="cmd", metavar="COMMAND")

    p_list = sub.add_parser("list", help="List configuration items")
    list_sub = p_list.add_subparsers(dest="item", metavar="ITEM")
    list_sub.add_parser("inventory", help="Show iLO hosts and OneView appliance")

    return p


def main(argv: list[str] | None = None) -> None:
    p = _build_parser()
    args = p.parse_args(argv)

    if args.cmd == "list":
        if args.item == "inventory":
            _cmd_list_inventory()
        else:
            p.parse_args(["list", "--help"])
    else:
        p.print_help()
