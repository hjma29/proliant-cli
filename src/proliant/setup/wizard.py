"""
proliant.setup.wizard
~~~~~~~~~~~~~~~~~~~~~~
Guided, menu-driven setup for inventory.ini.

Shows current entries and offers add / edit / delete actions, live-testing
each iLO/OneView connection before it is saved. Entries are written to
inventory.ini immediately after each confirmed change, so an interrupted
run (Ctrl+C, closed terminal) never loses work already done. Safe to
re-run any time -- merges into an existing file instead of overwriting it.
"""

from __future__ import annotations

import configparser
from pathlib import Path

from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.table import Table

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


def _entries(cfg: configparser.ConfigParser) -> list[str]:
    """Section names in file order, excluding [defaults]."""
    return [s for s in cfg.sections() if s.lower() != "defaults"]


def _entry_type(cfg: configparser.ConfigParser, name: str) -> str:
    return "oneview" if cfg.get(name, "type", fallback="").strip().lower() == "oneview" else "ilo"


def _effective_username(cfg: configparser.ConfigParser, name: str) -> str:
    default_user = cfg.get("defaults", "username", fallback="Administrator")
    return cfg.get(name, "username", fallback=default_user)


def _print_entries(cfg: configparser.ConfigParser, entries: list[str]) -> None:
    if not entries:
        console.print("  (no entries yet)")
        return
    table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2, 0, 0))
    table.add_column("#", justify="right")
    table.add_column("Name")
    table.add_column("Type")
    table.add_column("Host")
    table.add_column("Username")
    for i, name in enumerate(entries, start=1):
        host = cfg.get(name, "host", fallback="")
        table.add_row(str(i), name, _entry_type(cfg, name), host, _effective_username(cfg, name))
    console.print(table)


def _select_entry(entries: list[str], verb: str) -> str | None:
    """Prompt for an entry by number. Returns the section name, or None if cancelled."""
    raw = Prompt.ask(f"  Which entry do you want to {verb}? (number, blank to cancel)", default="").strip()
    if not raw:
        return None
    try:
        idx = int(raw)
    except ValueError:
        console.print("  [red]Not a valid number -- cancelled.[/red]")
        return None
    if not (1 <= idx <= len(entries)):
        console.print("  [red]Out of range -- cancelled.[/red]")
        return None
    return entries[idx - 1]


def _prompt_menu(options: list[tuple[str, str]], prompt: str = "  Select", default: str | None = None) -> str:
    """Print a numbered menu (key, label) pairs and prompt for a selection by number.

    Returns the key of the chosen option. Re-prompts on invalid/out-of-range input.
    """
    for i, (_, label) in enumerate(options, start=1):
        console.print(f"    {i}. {label}")
    keys = [key for key, _ in options]
    default_num = str(keys.index(default) + 1) if default in keys else None
    while True:
        raw = Prompt.ask(prompt, default=default_num).strip()
        if not raw:
            console.print(f"  [red]Please enter a number 1-{len(options)}.[/red]")
            continue
        try:
            idx = int(raw)
        except ValueError:
            console.print(f"  [red]'{raw}' is not a valid number -- pick 1-{len(options)}.[/red]")
            continue
        if not (1 <= idx <= len(options)):
            console.print(f"  [red]Out of range -- pick 1-{len(options)}.[/red]")
            continue
        return keys[idx - 1]


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


async def _edit_ilo_server(cfg: configparser.ConfigParser, name: str, dest: Path) -> None:
    """Edit an existing iLO server entry in place."""
    console.print(f"\n[bold cyan]Edit iLO server '{name}'[/bold cyan]")
    current_host = cfg.get(name, "host", fallback="")
    default_user = cfg.get("defaults", "username", fallback="Administrator")
    default_pass = cfg.get("defaults", "password", fallback="")
    current_user = cfg.get(name, "username", fallback=default_user)
    current_pass = cfg.get(name, "password", fallback=default_pass)

    host = Prompt.ask("  iLO IP / hostname", default=current_host).strip() or current_host
    username = Prompt.ask("  Username", default=current_user).strip() or current_user
    password = await prompt_password_async("  Password (leave blank to keep unchanged): ")
    test_password = password or current_pass

    console.print(f"  Testing connection to {host}...")
    ok, message = await _test_ilo(host, username, test_password)
    console.print(f"  [green]OK - {message}[/green]" if ok else f"  [red]FAILED - {message}[/red]")
    if not ok and not Confirm.ask("  Save changes anyway?", default=False):
        console.print("  Discarded -- entry unchanged.")
        return

    cfg.set(name, "host", host)
    if username != default_user:
        cfg.set(name, "username", username)
    elif cfg.has_option(name, "username"):
        cfg.remove_option(name, "username")
    if password:
        if password != default_pass:
            cfg.set(name, "password", password)
        elif cfg.has_option(name, "password"):
            cfg.remove_option(name, "password")
    _save_ini(cfg, dest)
    console.print(f"  [green]Saved changes to {dest}[/green]")


async def _edit_oneview(cfg: configparser.ConfigParser, name: str, dest: Path) -> None:
    """Edit an existing OneView appliance entry in place."""
    console.print(f"\n[bold cyan]Edit OneView appliance '{name}'[/bold cyan]")
    current_host = cfg.get(name, "host", fallback="")
    current_user = cfg.get(name, "username", fallback="Administrator")
    current_pass = cfg.get(name, "password", fallback="")

    host = Prompt.ask("  OneView appliance IP / hostname", default=current_host).strip() or current_host
    username = Prompt.ask("  Username", default=current_user).strip() or current_user
    password = await prompt_password_async("  Password (leave blank to keep unchanged): ")
    test_password = password or current_pass

    console.print(f"  Testing connection to {host}...")
    ok, message = await _test_oneview(host, username, test_password)
    console.print(f"  [green]OK - {message}[/green]" if ok else f"  [red]FAILED - {message}[/red]")
    if not ok and not Confirm.ask("  Save changes anyway?", default=False):
        console.print("  Discarded -- entry unchanged.")
        return

    cfg.set(name, "host", host)
    cfg.set(name, "username", username)
    if password:
        cfg.set(name, "password", password)
    cfg.set(name, "type", "oneview")
    _save_ini(cfg, dest)
    console.print(f"  [green]Saved changes to {dest}[/green]")


async def _edit_entry(cfg: configparser.ConfigParser, entries: list[str], dest: Path) -> None:
    name = _select_entry(entries, "edit")
    if name is None:
        console.print("  Cancelled.")
        return
    if _entry_type(cfg, name) == "oneview":
        await _edit_oneview(cfg, name, dest)
    else:
        await _edit_ilo_server(cfg, name, dest)


async def _delete_entry(
    cfg: configparser.ConfigParser, entries: list[str], existing: set[str], dest: Path
) -> None:
    name = _select_entry(entries, "delete")
    if name is None:
        console.print("  Cancelled.")
        return
    if not Confirm.ask(f"  Delete '{name}'? This cannot be undone.", default=False):
        console.print("  Cancelled.")
        return
    cfg.remove_section(name)
    existing.discard(name.lower())
    _save_ini(cfg, dest)
    console.print(f"  [green]Deleted '{name}' from {dest}[/green]")


async def run_setup_wizard(dest: Path | None = None) -> None:
    """Interactive menu for managing inventory.ini: view, add, edit, delete.

    Live-tests iLO/OneView connections before saving. Merges into an
    existing inventory.ini rather than overwriting it, so this is safe to
    run any time to add, change, or remove servers.
    """
    dest = dest or _default_dest()
    cfg = _load_ini(dest)
    existing = _existing_names(cfg)

    console.print("\n[bold]proliant setup[/bold] -- manage your servers in inventory.ini\n")
    if dest.exists():
        plural = "y" if len(existing) == 1 else "ies"
        console.print(f"Found existing config: [bold]{dest}[/bold] ({len(existing)} entr{plural})")
    else:
        console.print(f"This will create: [bold]{dest}[/bold]")

    try:
        while True:
            entries = _entries(cfg)
            console.print()
            _print_entries(cfg, entries)
            if entries:
                options = [
                    ("add", "Add a new entry"),
                    ("edit", "Edit an entry"),
                    ("delete", "Delete an entry"),
                    ("done", "Done"),
                ]
                default = "done"
            else:
                options = [("add", "Add a new entry"), ("done", "Done")]
                default = "add"
            console.print("\n[bold]What would you like to do?[/bold]")
            action = _prompt_menu(options, default=default)
            if action == "done":
                break
            if action == "add":
                kind = _prompt_menu(
                    [("ilo", "iLO server"), ("oneview", "OneView appliance")],
                    prompt="  Add a",
                    default="ilo",
                )
                if kind == "ilo":
                    await _add_ilo_server(cfg, existing, dest)
                else:
                    await _add_oneview(cfg, existing, dest)
            elif action == "edit":
                await _edit_entry(cfg, entries, dest)
            elif action == "delete":
                await _delete_entry(cfg, entries, existing, dest)
    except (KeyboardInterrupt, EOFError):
        console.print("\n\n[yellow]Setup interrupted.[/yellow]")
        if _entries(cfg):
            console.print(f"  Entries saved so far are kept in: [bold]{dest}[/bold]")
        return

    final_entries = _entries(cfg)
    console.print("\n[bold green]Setup complete![/bold green]")
    if final_entries:
        console.print(f"  Config: [bold]{dest}[/bold]")
        console.print("  Try: [bold]proliant ilo list firmwares[/bold]\n")
    else:
        console.print("  No servers configured. Run [bold]proliant setup[/bold] again any time.\n")
