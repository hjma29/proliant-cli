"""
hpecom.cli
~~~~~~~~~~
Command-line interface.

Usage::

    pcli com login                         Okta Verify push login (prompts for email)
    pcli com login --email you@hpe.com     Pre-fill email, skip prompt
    pcli com login --password              Username + password login (external/gmail accounts)
    pcli com login --api-client            Login with HPE GreenLake API client credentials
    pcli com login --api-client --client-id ID --client-secret SECRET  Non-interactive

    pcli com logout                        Remove cached credentials and token

    pcli com get devices                   All devices in workspace
    pcli com get devices --type COMPUTE    Filter by type (COMPUTE, NETWORK, STORAGE)
    pcli com get devices --fields name,serial,service
    pcli com get devices --fields name,serial,added,added-by
    pcli com get devices --fields name,ilo-name,serial,location
    pcli com get devices --sort added      Sort by date added
    pcli com get devices --sort added-by   Sort by who added the device
    pcli com get devices --fields name,serial,added,added-by --sort added
    pcli com get devices --raw             Raw JSON

    pcli com get bundles                   Active SPP firmware bundles in COM
    pcli com get bundles --all             Include inactive/superseded bundles
    pcli com get bundles --gen 12          Gen12 bundles only
    pcli com get bundles --gen 11          Gen11 bundles only
    pcli com get bundles --type patch      PATCH bundles only (base/patch/hotfix)
    pcli com get bundles --raw             Raw JSON

    pcli com get workspaces                All workspaces (active one marked with *)
    pcli com get workspaces --raw          Raw JSON

    pcli com use workspace <name-or-id>    Switch active workspace

Available --fields for 'get devices':
    name, ilo-name, type, model, serial, part, service, sub-key, location,
    added, updated, added-by

Note: Run 'pcli com login' before any get/use command (like kubectl/gcloud/aws/az).
"""

# PYTHON_ARGCOMPLETE_OK
import argparse
import json
import sys
from typing import Optional

import argcomplete
from rich.console import Console
from rich.table import Table
from rich import box

from pcli.com.auth import COMSession, CredentialsError, AuthError
from pcli.com.client import run
from pcli.com import devices as _devices
from pcli.com import workspaces as _workspaces
from pcli.com import firmware as _firmware

console = Console()


# ---------------------------------------------------------------------------
# Argcomplete dynamic completers
# ---------------------------------------------------------------------------

def _workspace_names_completer(prefix, **kwargs):
    """Return cached workspace names for tab completion."""
    try:
        from pcli.com.login import load_token
        data = load_token() or {}
        names = [w.get("company_name", "") for w in data.get("workspaces", [])]
        return [n for n in names if n.startswith(prefix)]
    except Exception:
        return []


# Login command
# ---------------------------------------------------------------------------

async def _cmd_login(args: argparse.Namespace) -> None:
    from pcli.com.login import okta_verify_login
    from rich.prompt import Prompt

    # ── Agent / service-account login (client credentials) ────────────────
    if getattr(args, "api_client", False):
        await _cmd_login_agent(args)
        return

    # ── Interactive Okta Verify login ──────────────────────────────────────
    email = getattr(args, "email", None) or ""
    if not email:
        try:
            email = Prompt.ask("[bold]HPE GreenLake email[/bold]").strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[yellow]Login cancelled.[/yellow]")
            sys.exit(0)

    if not email:
        console.print("[red]Email is required.[/red]")
        sys.exit(1)

    region = getattr(args, "region", None) or "us-west"

    # ── Username + password login (external/gmail accounts) ────────────────
    if getattr(args, "password", False):
        import getpass as _getpass
        from pcli.com.login import password_login
        try:
            passwd = _getpass.getpass("Password: ")
        except (KeyboardInterrupt, EOFError):
            console.print("\n[yellow]Login cancelled.[/yellow]")
            sys.exit(0)
        try:
            await password_login(email=email, password=passwd, region=region)
        except Exception as e:
            console.print(f"[red]Login failed:[/red] {e}")
            sys.exit(1)
        return

    try:
        await okta_verify_login(email=email, region=region)
    except Exception as e:
        console.print(f"[red]Login failed:[/red] {e}")
        sys.exit(1)


async def _cmd_login_agent(args: argparse.Namespace) -> None:
    """Non-interactive login using HPE GreenLake API client credentials."""
    import getpass
    from pcli.com.login import CREDS_FILE
    from pcli.com.auth import COMSession, TOKEN_URL, AuthError
    import httpx

    client_id     = getattr(args, "client_id", None) or ""
    client_secret = getattr(args, "client_secret", None) or ""
    region        = getattr(args, "region", None) or "us-west"

    if not client_id:
        try:
            client_id = input("Client ID: ").strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[yellow]Login cancelled.[/yellow]")
            sys.exit(0)

    if not client_secret:
        try:
            client_secret = getpass.getpass("Client Secret: ").strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[yellow]Login cancelled.[/yellow]")
            sys.exit(0)

    if not client_id or not client_secret:
        console.print("[red]Client ID and Client Secret are required.[/red]")
        sys.exit(1)

    # Validate by fetching a token
    with console.status("[bold cyan]Validating credentials..."):
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    TOKEN_URL,
                    data={"grant_type": "client_credentials"},
                    auth=(client_id, client_secret),
                )
                resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            console.print(
                f"[red]Authentication failed ({e.response.status_code}).[/red] "
                "Check your Client ID and Secret."
            )
            sys.exit(1)
        except Exception as e:
            console.print(f"[red]Connection error:[/red] {e}")
            sys.exit(1)

    # Save to credentials.yml
    import yaml
    CREDS_FILE.parent.mkdir(parents=True, exist_ok=True)
    CREDS_FILE.write_text(
        yaml.dump({"client_id": client_id, "client_secret": client_secret,
                   "region": region}, default_flow_style=False)
    )
    CREDS_FILE.chmod(0o600)
    console.print(
        f"[bold green]✓ API client credentials saved.[/bold green] "
        f"[dim]{CREDS_FILE}[/dim]"
    )
    console.print("[dim]Run any hpecom command — no Okta needed.[/dim]")


# ---------------------------------------------------------------------------
# Logout command
# ---------------------------------------------------------------------------

def _cmd_logout(_args: argparse.Namespace) -> None:
    from pcli.com.login import TOKEN_CACHE, CREDS_FILE, load_token, delete_glp_api_credential

    # Clean up GLP API credential before deleting token
    data = load_token()
    if data and data.get("glp_credential_name") and data.get("ccs_session"):
        try:
            import asyncio
            asyncio.run(delete_glp_api_credential(
                data["access_token"], data["ccs_session"], data["glp_credential_name"]
            ))
        except Exception:
            pass  # best-effort

    removed = []
    for path in (TOKEN_CACHE, CREDS_FILE):
        if path.exists():
            path.unlink()
            removed.append(path.name)

    if removed:
        console.print(f"[green]✓ Logged out.[/green] Removed: {', '.join(removed)}")
    else:
        console.print("[yellow]Not logged in (no credentials found).[/yellow]")


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Device field definitions
# ---------------------------------------------------------------------------

# All available columns for "pcli com get devices".
# Each entry: field_key → (header, rich_style, add_column_kwargs, value_getter)
# value_getter receives (device, user_cache) where user_cache maps uid → email.
_DEVICE_FIELDS: dict = {
    "name":     ("Name",     "bold cyan", {"no_wrap": True, "ratio": 4},
                 lambda d, _u: d.display_name),
    "ilo-name": ("iLO Name", "cyan",      {"no_wrap": True, "ratio": 3},
                 lambda d, _u: d.raw.get("deviceName") or d.raw.get("secondaryName") or "—"),
    "type":     ("Type",     "dim",       {"no_wrap": True, "min_width": 7, "max_width": 8},
                 lambda d, _u: d.device_type),
    "model":    ("Model",    "white",     {"no_wrap": True, "ratio": 2},
                 lambda d, _u: d.model),
    "serial":   ("Serial",   "green",     {"no_wrap": True, "min_width": 13},
                 lambda d, _u: d.serial_number),
    "part":     ("Part #",   "dim",       {"no_wrap": True, "min_width": 11},
                 lambda d, _u: d.product_id or "—"),
    "service":  ("Service",  "yellow",    {"no_wrap": True, "ratio": 2},
                 lambda d, _u: d.service_name or "—"),
    "sub-key":  ("Sub Key",  "dim",       {"no_wrap": True, "min_width": 9, "max_width": 10},
                 lambda d, _u: (d.subscription_key[:8] + "…") if d.subscription_key else "—"),
    "location": ("Location", "dim",       {"no_wrap": True, "ratio": 2},
                 lambda d, _u: (d.raw.get("location") or {}).get("locationName") or "—"),
    "added":    ("Added",    "dim",       {"no_wrap": True, "min_width": 10},
                 lambda d, _u: (d.raw.get("createdAt") or "")[:10] or "—"),
    "updated":  ("Updated",  "dim",       {"no_wrap": True, "min_width": 10},
                 lambda d, _u: (d.raw.get("updatedAt") or "")[:10] or "—"),
    "added-by": ("Added By", "dim",       {"no_wrap": True, "ratio": 2},
                 lambda d, u: u.get(
                     ((d.raw.get("contact") or {}).get("workspaceUser") or {}).get("id", ""),
                     "—"
                 )),
}

_DEVICE_DEFAULT_FIELDS = ("name", "type", "model", "serial", "service", "sub-key")

DEVICE_FIELD_NAMES = tuple(_DEVICE_FIELDS.keys())


def _comma_sep_completer(choices: tuple):
    """Argcomplete completer for comma-separated field lists like 'name,ser<TAB>'."""
    def completer(prefix: str, **kwargs):
        if "," in prefix:
            before, current = prefix.rsplit(",", 1)
            before += ","
        else:
            before, current = "", prefix
        return [before + c for c in choices if c.lower().startswith(current.lower())]
    return completer


def _parse_fields(fields_str: Optional[str], available: dict, defaults: tuple) -> list[str]:
    """Parse a comma-separated --fields string into a validated list of field keys."""
    if not fields_str:
        return list(defaults)
    keys = [f.strip().lower() for f in fields_str.split(",") if f.strip()]
    bad = [k for k in keys if k not in available]
    if bad:
        valid = ", ".join(available.keys())
        raise SystemExit(f"Unknown field(s): {', '.join(bad)}\nAvailable: {valid}")
    return keys


# ---------------------------------------------------------------------------
# Table printers
# ---------------------------------------------------------------------------

def print_devices_table(device_list: list, raw: bool = False,
                        fields: Optional[str] = None,
                        sort_by: Optional[str] = None,
                        user_cache: Optional[dict] = None) -> None:
    if raw:
        print(json.dumps([d.raw for d in device_list], indent=2))
        return

    if not device_list:
        console.print("[yellow]No devices found.[/yellow]")
        return

    selected = _parse_fields(fields, _DEVICE_FIELDS, _DEVICE_DEFAULT_FIELDS)
    uc = user_cache or {}

    # Sorting
    sort_key = (sort_by or "name").lower()
    if sort_key not in _DEVICE_FIELDS:
        raise SystemExit(f"Unknown sort field: {sort_key}\nAvailable: {', '.join(_DEVICE_FIELDS)}")
    sorted_list = sorted(device_list,
                         key=lambda d: _DEVICE_FIELDS[sort_key][3](d, uc).lower())

    table = Table(
        title=f"GreenLake Devices ({len(device_list)} total)",
        box=box.ROUNDED,
        show_lines=False,
        expand=True,
    )
    for key in selected:
        header, style, kwargs, _ = _DEVICE_FIELDS[key]
        table.add_column(header, style=style, **kwargs)

    for d in sorted_list:
        table.add_row(*[_DEVICE_FIELDS[key][3](d, uc) for key in selected])

    console.print(table)


def print_workspaces_table(workspace_list: list, raw: bool = False) -> None:
    if raw:
        print(json.dumps([w.raw for w in workspace_list], indent=2))
        return

    if not workspace_list:
        console.print("[yellow]No workspaces found.[/yellow]")
        return

    table = Table(
        title=f"GreenLake Workspaces ({len(workspace_list)} total)",
        box=box.ROUNDED,
        show_lines=False,
    )
    table.add_column("",           style="bold green", no_wrap=True)  # active marker
    table.add_column("Name",       style="bold cyan",  no_wrap=True)
    table.add_column("ID",         style="dim")
    table.add_column("Region",     style="green")
    table.add_column("Status",     style="yellow")
    table.add_column("Location",   style="white")
    table.add_column("Description",style="dim")

    for w in workspace_list:
        status_color = "green" if w.status == "ACTIVE" else "yellow"
        table.add_row(
            "* " if w.active else "  ",
            w.name,
            w.id,
            w.region,
            f"[{status_color}]{w.status}[/{status_color}]",
            w.address,
            w.description or "—",
        )

    console.print(table)
    console.print("[dim]  * = active workspace[/dim]")


# ---------------------------------------------------------------------------
# Auth helper — fail fast like gcloud/aws/az
# ---------------------------------------------------------------------------

async def _ensure_session(args: argparse.Namespace) -> COMSession:
    """Return a valid COMSession or exit with a clear login prompt.

    Credential priority:
      1. --client-id / --client-secret flags
      2. HPECOM_CLIENT_ID / HPECOM_CLIENT_SECRET env vars
      3. ~/.config/pcli/com/credentials.yml
      4. Cached token from a previous 'pcli com login'
      5. ↳ none found → exit with message (like gcloud/aws/az)
    """
    try:
        return COMSession.load(
            client_id=getattr(args, "client_id", None),
            client_secret=getattr(args, "client_secret", None),
            region=getattr(args, "region", None),
        )
    except CredentialsError:
        pass

    # No credentials — fail fast with a clear message (industry standard)
    console.print(
        "[bold red]Not logged in.[/bold red] "
        "Please run:\n\n"
        "  [bold cyan]pcli com login[/bold cyan]\n\n"
        "to authenticate with HPE GreenLake."
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def _cmd_show_devices(args: argparse.Namespace) -> None:
    session = await _ensure_session(args)
    device_type = getattr(args, "type", None)
    fields = getattr(args, "fields", None)
    sort_by = getattr(args, "sort_by", None)

    with console.status("[bold cyan]Fetching devices from GreenLake..."):
        try:
            device_list = await _devices.fetch_devices(session, device_type=device_type)
        except AuthError as e:
            console.print(f"[red]Auth error:[/red] {e}")
            sys.exit(1)
        except Exception as e:
            console.print(f"[red]Error:[/red] {e}")
            sys.exit(1)

    # Resolve user IDs → emails only when added-by column is requested
    user_cache: dict = {}
    requested_fields = [f.strip().lower() for f in fields.split(",")] if fields else list(_DEVICE_DEFAULT_FIELDS)
    if "added-by" in requested_fields:
        user_ids = {
            ((d.raw.get("contact") or {}).get("workspaceUser") or {}).get("id", "")
            for d in device_list
        } - {""}
        if user_ids:
            from pcli.com.login import load_token
            token_data = load_token() or {}
            glp_token = token_data.get("glp_access_token", "")
            if glp_token:
                with console.status("[dim]Resolving user names..."):
                    user_cache = await _devices.resolve_user_ids(user_ids, glp_token)

    print_devices_table(device_list, raw=getattr(args, "raw", False),
                        fields=fields, sort_by=sort_by, user_cache=user_cache)


async def _cmd_show_workspaces(args: argparse.Namespace) -> None:
    session = await _ensure_session(args)

    with console.status("[bold cyan]Fetching workspace info from GreenLake..."):
        try:
            workspace_list = await _workspaces.fetch_workspaces(session)
        except AuthError as e:
            console.print(f"[red]Auth error:[/red] {e}")
            sys.exit(1)
        except Exception as e:
            console.print(f"[red]Error:[/red] {e}")
            sys.exit(1)

    print_workspaces_table(workspace_list, raw=getattr(args, "raw", False))


def print_bundles_table(bundle_list: list, raw: bool = False) -> None:
    if raw:
        import json as _json
        print(_json.dumps([b.raw for b in bundle_list], indent=2))
        return

    if not bundle_list:
        console.print("[yellow]No bundles found.[/yellow]")
        return

    table = Table(
        title=f"COM SPP Firmware Bundles ({len(bundle_list)} shown)",
        box=box.ROUNDED,
        show_lines=False,
        expand=True,
    )
    table.add_column("Gen",     style="bold cyan",  no_wrap=True, min_width=8)
    table.add_column("Type",    style="dim",        no_wrap=True, min_width=6)
    table.add_column("Version", style="white",      no_wrap=True, min_width=14)
    table.add_column("Release", style="dim",        no_wrap=True, min_width=10)
    table.add_column("Active",  style="green",      no_wrap=True, min_width=6)
    table.add_column("Display Name", style="white", no_wrap=True, ratio=2)

    for b in bundle_list:
        active_str = "[green]✓[/green]" if b.is_active else "[dim]—[/dim]"
        table.add_row(
            b.generation,
            b.bundle_type,
            b.release_version,
            b.release_date,
            active_str,
            b.display_name,
        )

    console.print(table)


async def _cmd_show_bundles(args: argparse.Namespace) -> None:
    session = await _ensure_session(args)
    active_only = not getattr(args, "all", False)
    gen = getattr(args, "gen", None)
    bundle_type = getattr(args, "bundle_type", None)

    with console.status("[bold cyan]Fetching SPP bundles from COM..."):
        try:
            bundle_list = await _firmware.fetch_bundles(
                session,
                active_only=active_only,
                gen=gen,
                bundle_type=bundle_type,
            )
        except AuthError as e:
            console.print(f"[red]Auth error:[/red] {e}")
            sys.exit(1)
        except Exception as e:
            console.print(f"[red]Error:[/red] {e}")
            sys.exit(1)

    print_bundles_table(bundle_list, raw=getattr(args, "raw", False))


async def _cmd_use_workspace(args: argparse.Namespace) -> None:
    from pcli.com.login import switch_workspace
    name_or_id = args.workspace
    try:
        with console.status(f"[bold cyan]Switching to workspace '{name_or_id}'..."):
            resolved_name = await switch_workspace(name_or_id)
        console.print(f"[bold green]✓ Switched to workspace:[/bold green] {resolved_name}")
    except (CredentialsError, ValueError) as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)


async def _cmd_add_device(args: argparse.Namespace) -> None:
    from pcli.com.devices import add_compute_devices

    serial = args.serial_number.strip()
    part   = (args.part_number or "").strip()

    console.print(f"[cyan]Adding device[/cyan] {serial}" + (f" (part: {part})" if part else "") + "…")

    try:
        results = await add_compute_devices([serial], [part])
    except PermissionError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    exit_code = 0
    for r in results:
        if r.status == "Complete":
            console.print(f"[bold green]✓ {r.serial_number}[/bold green] — {r.detail}")
        elif r.status == "Warning":
            console.print(f"[yellow]⚠ {r.serial_number}[/yellow] — {r.detail}")
        else:
            console.print(f"[red]✗ {r.serial_number}[/red] — {r.detail}")
            exit_code = 1

    if exit_code:
        sys.exit(exit_code)



def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pcli com",
        description="HPE Compute Ops Management Python CLI",
    )

    # Global optional credential overrides
    parser.add_argument("--client-id",     metavar="ID",     dest="client_id",
                        help="GreenLake API client ID (overrides env/file)")
    parser.add_argument("--client-secret", metavar="SECRET", dest="client_secret",
                        help="GreenLake API client secret (overrides env/file)")
    parser.add_argument("--region",        metavar="REGION", default=None,
                        choices=["us-west", "eu-central", "ap-northeast"],
                        help="GreenLake region (default: us-west)")

    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    subparsers.required = True

    # ── login ─────────────────────────────────────────────────────────────
    login_p = subparsers.add_parser(
        "login",
        help="Login (Okta Verify push, password, or --api-client)",
    )
    login_p.add_argument(
        "--email", "-e", metavar="EMAIL",
        help="HPE GreenLake email address",
    )
    login_p.add_argument(
        "--password", "-p", action="store_true",
        help="Login with username + password (for external/gmail accounts)",
    )
    login_p.add_argument(
        "--api-client", action="store_true", dest="api_client",
        help="Login using HPE GreenLake API client credentials (no Okta needed)",
    )
    login_p.add_argument(
        "--client-id", metavar="ID", dest="client_id",
        help="Client ID for --api-client login",
    )
    login_p.add_argument(
        "--client-secret", metavar="SECRET", dest="client_secret",
        help="Client Secret for --api-client login",
    )
    login_p.add_argument(
        "--region", metavar="REGION",
        choices=["us-west", "eu-central", "ap-northeast"],
        help="GreenLake region (default: us-west)",
    )

    # ── logout ────────────────────────────────────────────────────────────
    subparsers.add_parser(
        "logout",
        help="Remove cached credentials and token",
    )

    # ── get ───────────────────────────────────────────────────────────────
    get_p = subparsers.add_parser("get", help="Get resources")
    get_sub = get_p.add_subparsers(dest="what", metavar="WHAT")
    get_sub.required = True

    # pcli com get devices
    dev_p = get_sub.add_parser(
        "devices",
        help="List all devices in workspace",
        description=(
            "List all devices registered in the GreenLake workspace.\n\n"
            "Examples:\n"
            "  pcli com get devices\n"
            "  pcli com get devices --type COMPUTE\n"
            "  pcli com get devices --fields name,serial,service\n"
            "  pcli com get devices --fields name,serial,added,added-by\n"
            "  pcli com get devices --fields name,ilo-name,serial,location\n"
            "  pcli com get devices --sort added\n"
            "  pcli com get devices --sort added-by\n"
            "  pcli com get devices --fields name,serial,added,added-by --sort added\n"
            "  pcli com get devices --fields name,type,model,serial,part,service,sub-key,location,added,updated,added-by\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    dev_p.add_argument("--type", metavar="TYPE",
                       choices=["COMPUTE", "NETWORK", "STORAGE"],
                       help="Filter by device type")
    dev_p.add_argument("--raw", action="store_true", help="Print raw JSON")
    fields_arg = dev_p.add_argument(
        "--fields", metavar="FIELDS",
        help=(
            f"Comma-separated columns to display (case-insensitive). "
            f"Available: {', '.join(DEVICE_FIELD_NAMES)}. "
            f"Default: {', '.join(_DEVICE_DEFAULT_FIELDS)}"
        ),
    )
    fields_arg.completer = _comma_sep_completer(DEVICE_FIELD_NAMES)  # type: ignore[attr-defined]
    dev_p.add_argument(
        "--sort", metavar="FIELD", dest="sort_by",
        choices=list(DEVICE_FIELD_NAMES),
        help=f"Sort by field (case-insensitive). Available: {', '.join(DEVICE_FIELD_NAMES)}. Default: name",
    )

    # pcli com get workspaces
    ws_p = get_sub.add_parser("workspaces", help="List all workspaces (* = active)")
    ws_p.add_argument("--raw", action="store_true", help="Print raw JSON")

    # pcli com get bundles
    bun_p = get_sub.add_parser(
        "bundles",
        help="List SPP firmware bundles available in COM",
        description=(
            "List Service Pack for ProLiant (SPP) firmware bundles available in COM.\n\n"
            "By default shows only active (current) bundles. Bundles are organised\n"
            "by server generation (Gen10/11/12) and type (BASE, PATCH, HOTFIX).\n\n"
            "Examples:\n"
            "  pcli com get bundles                   Active bundles (all gens)\n"
            "  pcli com get bundles --all             Include superseded bundles\n"
            "  pcli com get bundles --gen 12          Gen12 only\n"
            "  pcli com get bundles --gen 11          Gen11 only\n"
            "  pcli com get bundles --type base       BASE bundles only\n"
            "  pcli com get bundles --type patch      PATCH bundles only\n"
            "  pcli com get bundles --gen 12 --type base   Latest Gen12 BASE SPPs\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    bun_p.add_argument("--all", action="store_true",
                       help="Include inactive/superseded bundles (default: active only)")
    bun_p.add_argument("--gen", type=int, choices=[10, 11, 12], metavar="GEN",
                       help="Filter by server generation: 10, 11, or 12")
    bun_p.add_argument("--type", dest="bundle_type",
                       choices=["base", "patch", "hotfix"],
                       metavar="TYPE",
                       help="Filter by bundle type: base, patch, or hotfix")
    bun_p.add_argument("--raw", action="store_true", help="Print raw JSON")

    # ── use ───────────────────────────────────────────────────────────────
    use_p = subparsers.add_parser("use", help="Switch active resource")
    use_sub = use_p.add_subparsers(dest="what", metavar="WHAT")
    use_sub.required = True

    use_ws_p = use_sub.add_parser("workspace", help="Switch active workspace")
    use_ws_p.add_argument(
        "workspace", metavar="NAME_OR_ID",
        help="Workspace name or platform_customer_id",
    ).completer = _workspace_names_completer  # type: ignore[attr-defined]

    # ── add ───────────────────────────────────────────────────────────────
    add_p = subparsers.add_parser("add", help="Add resources to workspace")
    add_sub = add_p.add_subparsers(dest="what", metavar="WHAT")
    add_sub.required = True

    # pcli com add device
    add_dev_p = add_sub.add_parser(
        "device",
        help="Add a compute device to the workspace",
    )
    add_dev_p.add_argument(
        "--serial-number", "-s", metavar="SERIAL", dest="serial_number", required=True,
        help="Device serial number",
    )
    add_dev_p.add_argument(
        "--part-number", "-p", metavar="PART", dest="part_number", default="",
        help="Device part number (SKU). Optional — omit if unknown.",
    )

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> None:
    parser = _build_parser()
    argcomplete.autocomplete(parser)
    args = parser.parse_args(argv)

    if args.command == "login":
        run(_cmd_login(args))
    elif args.command == "logout":
        _cmd_logout(args)
    elif args.command == "get":
        if args.what == "devices":
            run(_cmd_show_devices(args))
        elif args.what == "workspaces":
            run(_cmd_show_workspaces(args))
        elif args.what == "bundles":
            run(_cmd_show_bundles(args))
    elif args.command == "use":
        if args.what == "workspace":
            run(_cmd_use_workspace(args))
    elif args.command == "add":
        if args.what == "device":
            run(_cmd_add_device(args))
