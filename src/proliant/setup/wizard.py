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

import asyncio
import configparser
import os
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
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
    "# [section]   one section per server; section name = friendly alias you\n"
    "#             choose (used with --host; need not match the iLO or OS hostname)\n"
    "#             'host' is the only required field (IP or hostname, no https://)\n"
    "# add 'type = oneview' to a section to store a OneView appliance instead\n"
    "# of an iLO host -- 'proliant ilo' commands skip those automatically.\n"
)

# Short labels shown in the Status column, and the rich style used for each.
_STATUS_STYLES = {
    "Reachable": "green",
    "Timeout": "yellow",
    "Auth failed": "yellow",
    "Unreachable": "red",
    "No host": "red",
    "Error": "red",
}


def _default_dest() -> Path:
    return config_dir() / "inventory.ini"


def _load_ini(dest: Path) -> configparser.ConfigParser:
    # interpolation=None: passwords may legitimately contain a literal '%'.
    cfg = configparser.ConfigParser(interpolation=None)
    if dest.exists():
        cfg.read(dest)
    return cfg


_MAX_BACKUPS = 3


def _backup_ini(dest: Path) -> None:
    """Rotate up to _MAX_BACKUPS copies of *dest* before it is overwritten.

    Newest backup is '<name>.bak1', oldest is '<name>.bak{_MAX_BACKUPS}'.
    Rotation shifts each existing backup up by one (bak1->bak2, ...), drops
    anything past the limit, then copies the current file to '.bak1'. A missing
    source file (first-ever save) is a no-op. Backup failures never block the
    save -- they only print a warning.
    """
    if not dest.exists():
        return
    try:
        # Drop the oldest, then shift the rest up one slot.
        oldest = dest.with_name(f"{dest.name}.bak{_MAX_BACKUPS}")
        if oldest.exists():
            oldest.unlink()
        for i in range(_MAX_BACKUPS - 1, 0, -1):
            src = dest.with_name(f"{dest.name}.bak{i}")
            if src.exists():
                src.replace(dest.with_name(f"{dest.name}.bak{i + 1}"))
        shutil.copy2(dest, dest.with_name(f"{dest.name}.bak1"))
    except OSError as exc:  # pragma: no cover - filesystem/permission specific
        console.print(f"  [yellow]Could not back up {dest.name} ({type(exc).__name__}: {exc}).[/yellow]")


def _save_ini(cfg: configparser.ConfigParser, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    _backup_ini(dest)
    with dest.open("w", encoding="utf-8") as fh:
        fh.write(_INI_HEADER + "\n")
        cfg.write(fh)


def _rename_section(cfg: configparser.ConfigParser, old_name: str, new_name: str) -> None:
    """Rename a section in place, preserving the overall section order on disk."""
    order = cfg.sections()
    data = {section: dict(cfg.items(section)) for section in order}
    for section in order:
        cfg.remove_section(section)
    for section in order:
        target = new_name if section == old_name else section
        cfg.add_section(target)
        for key, value in data[section].items():
            cfg.set(target, key, value)


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


def _print_entries(
    cfg: configparser.ConfigParser,
    entries: list[str],
    statuses: dict[str, str] | None = None,
) -> None:
    if not entries:
        console.print("  (no entries yet)")
        return
    table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2, 0, 0))
    table.add_column("#", justify="right")
    table.add_column("Name")
    table.add_column("Type")
    table.add_column("Host")
    table.add_column("Username")
    table.add_column("Status")
    for i, name in enumerate(entries, start=1):
        host = cfg.get(name, "host", fallback="")
        status = (statuses or {}).get(name, "?")
        style = _STATUS_STYLES.get(status, "dim")
        table.add_row(
            str(i),
            name,
            _entry_type(cfg, name),
            host,
            _effective_username(cfg, name),
            f"[{style}]{status}[/{style}]",
        )
    console.print(table)


def _select_entry(entries: list[str], verb: str) -> str | None:
    """Prompt for an entry by number. Returns the section name, or None if cancelled.

    Reprints a compact "#. name" list right above the prompt so users don't
    need to scroll back up to the full table to look up a number.
    """
    for i, name in enumerate(entries, start=1):
        console.print(f"    {i}. {name}")
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


def _open_in_editor(path: Path) -> None:
    """Open *path* in the user's default editor/handler, cross-platform.

    Prefers the $EDITOR (or $VISUAL) environment variable when set, so users
    can control which editor launches. Falls back to the OS default handler:
    os.startfile on Windows, 'open' on macOS, 'xdg-open' on Linux.
    """
    if not path.exists():
        console.print(f"  [red]File does not exist yet: {path}[/red]")
        return

    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    try:
        if editor:
            subprocess.run([*editor.split(), str(path)], check=False)
        elif sys.platform.startswith("win"):
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=False)
        else:
            opener = shutil.which("xdg-open")
            if opener:
                subprocess.run([opener, str(path)], check=False)
            else:
                # Headless Linux (a bare server/VM over SSH, no desktop
                # session) has no xdg-open and often no $EDITOR set either.
                # Fall back to whatever terminal editor is actually
                # installed instead of just telling the user to do it by
                # hand -- nano/vim/vi cover the vast majority of distros.
                for candidate in ("nano", "vim", "vi"):
                    terminal_editor = shutil.which(candidate)
                    if terminal_editor:
                        subprocess.run([terminal_editor, str(path)], check=False)
                        console.print(f"  [green]Opened {path} in {candidate}.[/green]")
                        return
                console.print(
                    "  [yellow]No editor found. Set $EDITOR or open the file manually:[/yellow]"
                )
                console.print(f"  [bold]{path}[/bold]")
                return
        console.print(f"  [green]Opened {path} in your editor.[/green]")
    except Exception as exc:  # pragma: no cover - platform/editor specific
        console.print(f"  [red]Could not open editor ({type(exc).__name__}: {exc}).[/red]")
        console.print(f"  Edit the file manually: [bold]{path}[/bold]")


async def _test_ilo(host: str, username: str, password: str) -> tuple[bool, str]:
    """Try to log into an iLO host. Returns (ok, message).

    ``message`` is prefixed with "Timeout"/"Unreachable"/"Auth failed" when
    classifiable, so callers (see ``_status_label``) can show a short status.
    """
    from proliant.ilo.client import ilo_session

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
    except httpx.TimeoutException as exc:
        return False, f"Timeout: {exc}"
    except httpx.TransportError as exc:
        return False, f"Unreachable: {exc}"
    except RuntimeError as exc:
        message = str(exc)
        if "HTTP 401" in message:
            return False, "Auth failed: check username/password"
        if "HTTP 403" in message:
            return False, "Auth failed: account lacks permission for this operation"
        return False, message
    except Exception as exc:  # noqa: BLE001 -- never let a test crash the wizard
        return False, f"{type(exc).__name__}: {exc}"


async def _test_oneview(host: str, username: str, password: str) -> tuple[bool, str]:
    """Try to log into a OneView appliance. Returns (ok, message).

    ``message`` is prefixed with "Timeout"/"Unreachable"/"Auth failed" when
    classifiable, so callers (see ``_status_label``) can show a short status.
    """
    from proliant.oneview.client import OneViewClient, OneViewError

    try:
        async with OneViewClient(host, username, password):
            pass
        return True, "Connected successfully."
    except OneViewError as exc:
        message = str(exc)
        if "HTTP 401" in message:
            return False, "Auth failed: check username/password"
        if "HTTP 403" in message:
            return False, "Auth failed: account lacks permission for this operation"
        # OneViewError always wraps the underlying httpx exception via
        # 'raise ... from exc' -- inspect it to tell timeout from unreachable.
        cause = exc.__cause__
        if isinstance(cause, httpx.TimeoutException):
            return False, f"Timeout: {message}"
        if isinstance(cause, httpx.TransportError):
            return False, f"Unreachable: {message}"
        return False, message
    except Exception as exc:  # noqa: BLE001 -- never let a test crash the wizard
        return False, f"{type(exc).__name__}: {exc}"


def _status_label(ok: bool, message: str) -> str:
    """Classify a (ok, message) test result into a short Status-column label."""
    if ok:
        return "Reachable"
    for label in ("Timeout", "Unreachable", "Auth failed"):
        if message.startswith(label):
            return label
    return "Error"


async def _check_entry_status(cfg: configparser.ConfigParser, name: str) -> str:
    """Live-test one entry's connection right now. Returns a short status label."""
    host = cfg.get(name, "host", fallback="")
    if not host:
        return "No host"
    username = _effective_username(cfg, name)
    default_pass = cfg.get("defaults", "password", fallback="")
    password = cfg.get(name, "password", fallback=default_pass)
    if _entry_type(cfg, name) == "oneview":
        ok, message = await _test_oneview(host, username, password)
    else:
        ok, message = await _test_ilo(host, username, password)
    return _status_label(ok, message)


async def _check_all_statuses(cfg: configparser.ConfigParser, entries: list[str]) -> dict[str, str]:
    """Live-test every entry's connection concurrently, to minimize total wait time."""
    if not entries:
        return {}
    results = await asyncio.gather(*(_check_entry_status(cfg, name) for name in entries))
    return dict(zip(entries, results))


def _prompt_name(
    existing: set[str],
    label: str,
    default: str | None = None,
    hint: str | None = None,
) -> str:
    if hint:
        console.print(f"  [dim]{hint}[/dim]")
    while True:
        name = Prompt.ask(f"  {label}", default=default).strip()
        if not name:
            console.print("  [red]An alias is required -- pick a short label for this server.[/red]")
            continue
        if name.lower() == "defaults":
            console.print("  [red]'defaults' is reserved -- pick another name.[/red]")
            continue
        if name.lower() in existing:
            console.print(f"  [red]'{name}' already exists in inventory.ini -- pick another name.[/red]")
            continue
        return name


async def _add_ilo_server(
    cfg: configparser.ConfigParser,
    existing: set[str],
    dest: Path,
    statuses: dict[str, str] | None = None,
) -> bool:
    """Prompt for one iLO server, test it, and save. Returns True if added."""
    console.print("\n[bold cyan]Add an iLO server[/bold cyan]")
    name = _prompt_name(
        existing,
        "Server alias (friendly label)",
        hint="A short label you choose, e.g. 'dl380-prod'. Used with --host; "
        "need not match the iLO or OS hostname.",
    )
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
    if statuses is not None:
        statuses[name] = _status_label(ok, message)
    console.print(f"  [green]Saved to {dest}[/green]")
    return True


def _next_oneview_name(existing: set[str]) -> str:
    """Auto-generate an inventory.ini section name for a new OneView entry.

    Not prompted for -- the section name is an internal inventory.ini detail
    ('proliant oneview appliances use <name>' is the only place it's ever
    typed, and only needed once there's more than one appliance to pick
    between). Defaults to 'oneview', then 'oneview-2', 'oneview-3', ... for
    additional appliances. Users who want something more descriptive (e.g.
    a real appliance hostname) can still rename it afterwards via the
    wizard's Edit flow, which does prompt for the section name.
    """
    if "oneview" not in existing:
        return "oneview"
    i = 2
    while f"oneview-{i}" in existing:
        i += 1
    return f"oneview-{i}"


async def _add_oneview(
    cfg: configparser.ConfigParser,
    existing: set[str],
    dest: Path,
    statuses: dict[str, str] | None = None,
) -> bool:
    """Prompt for a OneView appliance, test it, and save. Returns True if added."""
    console.print("\n[bold cyan]Add a OneView appliance[/bold cyan]")
    name = _next_oneview_name(existing)
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
    if statuses is not None:
        statuses[name] = _status_label(ok, message)
    console.print(f"  [green]Saved to {dest}[/green]")
    return True


async def _edit_ilo_server(
    cfg: configparser.ConfigParser,
    name: str,
    dest: Path,
    statuses: dict[str, str] | None = None,
    existing: set[str] | None = None,
) -> str:
    """Edit an existing iLO server entry in place. Returns the entry's (possibly new) name."""
    console.print(f"\n[bold cyan]Edit iLO server '{name}'[/bold cyan]")
    current_host = cfg.get(name, "host", fallback="")
    default_user = cfg.get("defaults", "username", fallback="Administrator")
    default_pass = cfg.get("defaults", "password", fallback="")
    current_user = cfg.get(name, "username", fallback=default_user)
    current_pass = cfg.get(name, "password", fallback=default_pass)

    others = (existing or set()) - {name.lower()}
    new_name = _prompt_name(others, "Server alias (friendly label)", default=name)
    host = Prompt.ask("  iLO IP / hostname", default=current_host).strip() or current_host
    username = Prompt.ask("  Username", default=current_user).strip() or current_user
    password = await prompt_password_async("  Password (leave blank to keep unchanged): ")
    test_password = password or current_pass

    console.print(f"  Testing connection to {host}...")
    ok, message = await _test_ilo(host, username, test_password)
    console.print(f"  [green]OK - {message}[/green]" if ok else f"  [red]FAILED - {message}[/red]")
    if not ok and not Confirm.ask("  Save changes anyway?", default=False):
        console.print("  Discarded -- entry unchanged.")
        return name

    if new_name != name:
        _rename_section(cfg, name, new_name)
        if existing is not None:
            existing.discard(name.lower())
            existing.add(new_name.lower())
        if statuses is not None:
            statuses.pop(name, None)
        name = new_name

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
    if statuses is not None:
        statuses[name] = _status_label(ok, message)
    console.print(f"  [green]Saved changes to {dest}[/green]")
    return name


async def _edit_oneview(
    cfg: configparser.ConfigParser,
    name: str,
    dest: Path,
    statuses: dict[str, str] | None = None,
    existing: set[str] | None = None,
) -> str:
    """Edit an existing OneView appliance entry in place. Returns the entry's (possibly new) name."""
    console.print(f"\n[bold cyan]Edit OneView appliance '{name}'[/bold cyan]")
    current_host = cfg.get(name, "host", fallback="")
    current_user = cfg.get(name, "username", fallback="Administrator")
    current_pass = cfg.get(name, "password", fallback="")

    others = (existing or set()) - {name.lower()}
    new_name = _prompt_name(others, "OneView section", default=name)
    host = Prompt.ask("  OneView appliance IP / hostname", default=current_host).strip() or current_host
    username = Prompt.ask("  Username", default=current_user).strip() or current_user
    password = await prompt_password_async("  Password (leave blank to keep unchanged): ")
    test_password = password or current_pass

    console.print(f"  Testing connection to {host}...")
    ok, message = await _test_oneview(host, username, test_password)
    console.print(f"  [green]OK - {message}[/green]" if ok else f"  [red]FAILED - {message}[/red]")
    if not ok and not Confirm.ask("  Save changes anyway?", default=False):
        console.print("  Discarded -- entry unchanged.")
        return name

    if new_name != name:
        _rename_section(cfg, name, new_name)
        if existing is not None:
            existing.discard(name.lower())
            existing.add(new_name.lower())
        if statuses is not None:
            statuses.pop(name, None)
        name = new_name

    cfg.set(name, "host", host)
    cfg.set(name, "username", username)
    if password:
        cfg.set(name, "password", password)
    cfg.set(name, "type", "oneview")
    _save_ini(cfg, dest)
    if statuses is not None:
        statuses[name] = _status_label(ok, message)
    console.print(f"  [green]Saved changes to {dest}[/green]")
    return name


async def _edit_entry(
    cfg: configparser.ConfigParser,
    entries: list[str],
    dest: Path,
    statuses: dict[str, str] | None = None,
    existing: set[str] | None = None,
) -> None:
    name = _select_entry(entries, "edit")
    if name is None:
        console.print("  Cancelled.")
        return
    if _entry_type(cfg, name) == "oneview":
        await _edit_oneview(cfg, name, dest, statuses, existing)
    else:
        await _edit_ilo_server(cfg, name, dest, statuses, existing)


async def _delete_entry(
    cfg: configparser.ConfigParser,
    entries: list[str],
    existing: set[str],
    dest: Path,
    statuses: dict[str, str] | None = None,
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
    if statuses is not None:
        statuses.pop(name, None)
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

    entries = _entries(cfg)
    if entries:
        console.print("\nTesting connections...")
        statuses = await _check_all_statuses(cfg, entries)
    else:
        statuses: dict[str, str] = {}

    try:
        while True:
            entries = _entries(cfg)
            console.print()
            _print_entries(cfg, entries, statuses)
            if entries:
                options = [
                    ("add", "Add a new entry"),
                    ("edit", "Edit an entry"),
                    ("delete", "Delete an entry"),
                    ("open", "Open inventory.ini in editor"),
                    ("done", "Done"),
                ]
                default = "done"
            else:
                options = [
                    ("add", "Add a new entry"),
                    ("open", "Open inventory.ini in editor"),
                    ("done", "Done"),
                ]
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
                    await _add_ilo_server(cfg, existing, dest, statuses)
                else:
                    await _add_oneview(cfg, existing, dest, statuses)
            elif action == "edit":
                await _edit_entry(cfg, entries, dest, statuses, existing)
            elif action == "delete":
                await _delete_entry(cfg, entries, existing, dest, statuses)
            elif action == "open":
                if not dest.exists():
                    _save_ini(cfg, dest)
                _open_in_editor(dest)
                if Confirm.ask(
                    "  Reload inventory.ini and re-test connections when you're done editing?",
                    default=True,
                ):
                    cfg = _load_ini(dest)
                    existing = _existing_names(cfg)
                    entries = _entries(cfg)
                    if entries:
                        console.print("\nTesting connections...")
                        statuses = await _check_all_statuses(cfg, entries)
                    else:
                        statuses = {}
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
