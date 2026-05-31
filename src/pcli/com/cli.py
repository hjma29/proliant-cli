"""
hpecom.cli
~~~~~~~~~~
Command-line interface.

Usage::

    pcli com login                         Okta Verify push login (prompts for email)
    pcli com login --email you@hpe.com     Pre-fill email, skip prompt
    pcli com login --password              Username + password login (external/gmail accounts)
    pcli com login --api-client                          Login with HPE GreenLake API client credentials
    pcli com login --api-client --client-id ID --client-secret SECRET  Non-interactive

    pcli com logout                        Remove cached credentials and token

    pcli com get devices                   All devices in workspace
    pcli com get devices --type COMPUTE
    pcli com get devices --raw             Raw JSON

    pcli com get workspaces                All workspaces (active one marked with *)
    pcli com get workspaces --raw          Raw JSON

    pcli com use workspace <name-or-id>    Switch active workspace

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
# Table printers
# ---------------------------------------------------------------------------

def print_devices_table(device_list: list, raw: bool = False) -> None:
    if raw:
        print(json.dumps([d.raw for d in device_list], indent=2))
        return

    if not device_list:
        console.print("[yellow]No devices found.[/yellow]")
        return

    table = Table(
        title=f"GreenLake Devices ({len(device_list)} total)",
        box=box.ROUNDED,
        show_lines=False,
        expand=True,
    )
    table.add_column("Name",     style="bold cyan", no_wrap=True, ratio=4)
    table.add_column("Type",     style="dim",       no_wrap=True, min_width=7, max_width=8)
    table.add_column("Model",    style="white",     no_wrap=True, ratio=2)
    table.add_column("Serial",   style="green",     no_wrap=True, min_width=13)
    table.add_column("Service",  style="yellow",    no_wrap=True, ratio=2)
    table.add_column("Sub Key",  style="dim",       no_wrap=True, min_width=9, max_width=10)

    for d in sorted(device_list, key=lambda x: x.display_name.lower()):
        sub = d.subscription_key[:8] + "…" if d.subscription_key else "—"
        table.add_row(
            d.display_name,
            d.device_type,
            d.model,
            d.serial_number,
            d.service_name or "—",
            sub,
        )

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

    with console.status("[bold cyan]Fetching devices from GreenLake..."):
        try:
            device_list = await _devices.fetch_devices(session, device_type=device_type)
        except AuthError as e:
            console.print(f"[red]Auth error:[/red] {e}")
            sys.exit(1)
        except Exception as e:
            console.print(f"[red]Error:[/red] {e}")
            sys.exit(1)

    print_devices_table(device_list, raw=getattr(args, "raw", False))


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
    dev_p = get_sub.add_parser("devices", help="List all devices in workspace")
    dev_p.add_argument("--type", metavar="TYPE",
                       choices=["COMPUTE", "NETWORK", "STORAGE"],
                       help="Filter by device type")
    dev_p.add_argument("--raw", action="store_true", help="Print raw JSON")

    # pcli com get workspaces
    ws_p = get_sub.add_parser("workspaces", help="List all workspaces (* = active)")
    ws_p.add_argument("--raw", action="store_true", help="Print raw JSON")

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
    elif args.command == "use":
        if args.what == "workspace":
            run(_cmd_use_workspace(args))
    elif args.command == "add":
        if args.what == "device":
            run(_cmd_add_device(args))
