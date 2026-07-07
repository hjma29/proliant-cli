"""
proliant.setup.wizard
~~~~~~~~~~~~~~~~~~~~~~
Guided, step-by-step first-run setup for inventory.ini.

Walks the user through adding one or more iLO servers (and, optionally, a
OneView appliance), live-testing each connection before it is saved.
Entries are written to inventory.ini immediately after each one is
confirmed, so an interrupted run (Ctrl+C, closed terminal) never loses
work already done. Safe to re-run any time -- merges into an existing file
instead of overwriting it.
"""

from __future__ import annotations

import configparser
from pathlib import Path

from rich.console import Console
from rich.prompt import Confirm, Prompt

from proliant.common import config_dir
from proliant.common.prompts import prompt_password_async

console = Console()

_INI_HEADER = (
    "# proliant inventory.ini -- created by 'proliant setup'\n"
    "#\n"
    "# [defaults]  shared iLO credentials (can be overridden per server)\n"
    "# [section]   one section per server; section name = display name\n"
    "#             'host' is the only required field (IP or hostname, no https://)\n"
    "# add 'type = oneview' to a section to store a OneView appliance instead\n"
    "# of an iLO host -- 'proliant ilo' commands skip those automatically.\n"
)


def _default_dest() -> Path:
    return config_dir() / "inventory.ini"


def _load_ini(dest: Path) -> configparser.ConfigParser:
    # interpolation=None: passwords may legitimately contain a literal '%'.
    cfg = configparser.ConfigParser(interpolation=None)
    if dest.exists():
        cfg.read(dest)
    return cfg


def _save_ini(cfg: configparser.ConfigParser, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", encoding="utf-8") as fh:
        fh.write(_INI_HEADER + "\n")
        cfg.write(fh)


def _existing_names(cfg: configparser.ConfigParser) -> set[str]:
    return {s.lower() for s in cfg.sections() if s.lower() != "defaults"}


async def _test_ilo(host: str, username: str, password: str) -> tuple[bool, str]:
    """Try to log into an iLO host. Returns (ok, message)."""
    from proliant.ilo.client import ServerDownOrUnreachableError, ilo_session

    host_dict = {
        "name": host,
        "url": f"https://{host}",
        "username": username,
        "password": password,
    }
    try:
        async with ilo_session(host_dict, show_hint=True):
            pass
        return True, "Connected successfully."
    except ServerDownOrUnreachableError as exc:
        return False, f"Unreachable: {exc}"
    except RuntimeError as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001 -- never let a test crash the wizard
        return False, f"{type(exc).__name__}: {exc}"


async def _test_oneview(host: str, username: str, password: str) -> tuple[bool, str]:
    """Try to log into a OneView appliance. Returns (ok, message)."""
    from proliant.oneview.client import OneViewClient, OneViewError

    try:
        async with OneViewClient(host, username, password):
            pass
        return True, "Connected successfully."
    except OneViewError as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001 -- never let a test crash the wizard
        return False, f"{type(exc).__name__}: {exc}"


def _prompt_name(existing: set[str], label: str, default: str | None = None) -> str:
    while True:
        name = Prompt.ask(f"  {label} name", default=default).strip()
        if not name:
            console.print("  [red]Name cannot be empty.[/red]")
            continue
        if name.lower() == "defaults":
            console.print("  [red]'defaults' is reserved -- pick another name.[/red]")
            continue
        if name.lower() in existing:
            console.print(f"  [red]'{name}' already exists in inventory.ini -- pick another name.[/red]")
            continue
        return name


async def _add_ilo_server(
    cfg: configparser.ConfigParser, existing: set[str], dest: Path
) -> bool:
    """Prompt for one iLO server, test it, and save. Returns True if added."""
    console.print("\n[bold cyan]Add an iLO server[/bold cyan]")
    name = _prompt_name(existing, "Server")
    host = Prompt.ask("  iLO IP / hostname").strip()
    if not host:
        console.print("  [red]Host cannot be empty -- skipping this entry.[/red]")
        return False

    default_user = cfg.get("defaults", "username", fallback="Administrator")
    default_pass = cfg.get("defaults", "password", fallback="")
    username = Prompt.ask("  Username", default=default_user).strip() or default_user
    password = await prompt_password_async("  Password: ")
    if not password:
        password = default_pass

    console.print(f"  Testing connection to {host}...")
    ok, message = await _test_ilo(host, username, password)
    console.print(f"  [green]OK - {message}[/green]" if ok else f"  [red]FAILED - {message}[/red]")
    if not ok and not Confirm.ask("  Save this entry anyway?", default=False):
        console.print("  Discarded.")
        return False

    if not cfg.has_section(name):
        cfg.add_section(name)
    cfg.set(name, "host", host)
    if username != default_user:
        cfg.set(name, "username", username)
    # Only store the password on this section if it differs from [defaults] --
    # keeps inventory.ini free of redundant duplicate values.
    if password and password != default_pass:
        cfg.set(name, "password", password)
    _save_ini(cfg, dest)
    existing.add(name.lower())
    console.print(f"  [green]Saved to {dest}[/green]")
    return True


async def _add_oneview(
    cfg: configparser.ConfigParser, existing: set[str], dest: Path
) -> bool:
    """Prompt for a OneView appliance, test it, and save. Returns True if added."""
    console.print("\n[bold cyan]Add a OneView appliance[/bold cyan]")
    name = _prompt_name(existing, "OneView section", default="oneview")
    host = Prompt.ask("  OneView appliance IP / hostname").strip()
    if not host:
        console.print("  [red]Host cannot be empty -- skipping.[/red]")
        return False

    username = Prompt.ask("  Username", default="Administrator").strip() or "Administrator"
    password = await prompt_password_async("  Password: ")

    console.print(f"  Testing connection to {host}...")
    ok, message = await _test_oneview(host, username, password)
    console.print(f"  [green]OK - {message}[/green]" if ok else f"  [red]FAILED - {message}[/red]")
    if not ok and not Confirm.ask("  Save this entry anyway?", default=False):
        console.print("  Discarded.")
        return False

    if not cfg.has_section(name):
        cfg.add_section(name)
    cfg.set(name, "host", host)
    cfg.set(name, "username", username)
    if password:
        cfg.set(name, "password", password)
    cfg.set(name, "type", "oneview")
    _save_ini(cfg, dest)
    existing.add(name.lower())
    console.print(f"  [green]Saved to {dest}[/green]")
    return True


async def run_setup_wizard(dest: Path | None = None) -> None:
    """Interactive, step-by-step onboarding for inventory.ini.

    Adds one or more iLO servers -- and, optionally, a OneView appliance --
    live-testing each connection before it is saved. Merges into an
    existing inventory.ini rather than overwriting it, so this is safe to
    run again any time to add more servers.
    """
    dest = dest or _default_dest()
    cfg = _load_ini(dest)
    existing = _existing_names(cfg)

    console.print("\n[bold]proliant setup[/bold] -- let's add your servers to inventory.ini\n")
    if dest.exists():
        plural = "y" if len(existing) == 1 else "ies"
        console.print(f"Found existing config: [bold]{dest}[/bold] ({len(existing)} entr{plural})")
    else:
        console.print(f"This will create: [bold]{dest}[/bold]")

    added_any = False
    try:
        while True:
            if await _add_ilo_server(cfg, existing, dest):
                added_any = True
            if not Confirm.ask("\nAdd another iLO server?", default=False):
                break

        if Confirm.ask("\nAdd a OneView appliance too?", default=False):
            if await _add_oneview(cfg, existing, dest):
                added_any = True
    except (KeyboardInterrupt, EOFError):
        console.print("\n\n[yellow]Setup interrupted.[/yellow]")
        if added_any:
            console.print(f"  Entries saved so far are kept in: [bold]{dest}[/bold]")
        return

    console.print("\n[bold green]Setup complete![/bold green]")
    if added_any or existing:
        console.print(f"  Config: [bold]{dest}[/bold]")
        console.print("  Try: [bold]proliant ilo list firmwares[/bold]\n")
    else:
        console.print("  No servers were added. Run [bold]proliant setup[/bold] again any time.\n")
