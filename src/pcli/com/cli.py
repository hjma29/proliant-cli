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

    pcli com list devices                   All devices in workspace
    pcli com list devices --type COMPUTE    Filter by type (COMPUTE, NETWORK, STORAGE)
    pcli com list devices --fields name,serial,service
    pcli com list devices --fields name,serial,added,added-by
    pcli com list devices --fields name,ilo-name,serial,location
    pcli com list devices --sort added      Sort by date added
    pcli com list devices --sort added-by   Sort by who added the device
    pcli com list devices --fields name,serial,added,added-by --sort added
    pcli com list devices --raw             Unprocessed API response

    pcli com list bundles                   Active SPP firmware bundles in COM
    pcli com list bundles --all             Include inactive/superseded bundles
    pcli com list bundles --gen 12          Gen12 bundles only
    pcli com list bundles --gen 11          Gen11 bundles only
    pcli com list bundles --type patch      PATCH bundles only (base/patch/hotfix)
    pcli com list bundles --raw             Unprocessed API response

    pcli com list workspaces                All workspaces (active one marked with *)
    pcli com list workspaces --raw          Unprocessed API response

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

from pcli.common.display import get_console, make_table, print_json, print_memory_report, OutputMode, get_output_mode, set_output_mode
from pcli.common.runner import run_sync
from pcli.com.auth import COMSession, CredentialsError, AuthError
from pcli.com.client import COMClient
from pcli.com import devices as _devices
from pcli.com import workspaces as _workspaces
from pcli.com import firmware as _firmware
from pcli.com.describe import run_describe as _run_describe
from pcli.com.reports import run_report_gpu as _run_report_gpu, run_report_memory as _run_report_memory
from pcli.com.printers import (
    _DEVICE_FIELDS,
    _DEVICE_DEFAULT_FIELDS,
    _SERVER_DEFAULT_FIELDS,
    DEVICE_FIELD_NAMES,
    make_field_completer,
    parse_fields,
    print_devices_table,
    print_workspaces_table,
    print_bundles_table,
)


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
    except Exception:  # intentional: completion must never print to stdout
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
            get_console().print("\n[yellow]Login cancelled.[/yellow]")
            sys.exit(0)

    if not email:
        get_console().print("[red]Email is required.[/red]")
        sys.exit(1)

    region = getattr(args, "region", None) or "us-west"

    # ── Username + password login (external/gmail accounts) ────────────────
    if getattr(args, "password", False):
        import getpass as _getpass
        from pcli.com.login import password_login
        try:
            passwd = _getpass.getpass("Password: ")
        except (KeyboardInterrupt, EOFError):
            get_console().print("\n[yellow]Login cancelled.[/yellow]")
            sys.exit(0)
        try:
            await password_login(email=email, password=passwd, region=region)
        except Exception as e:
            get_console().print(f"[red]Login failed:[/red] {e}")
            sys.exit(1)
        return

    try:
        await okta_verify_login(email=email, region=region)
    except Exception as e:
        get_console().print(f"[red]Login failed:[/red] {e}")
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
            get_console().print("\n[yellow]Login cancelled.[/yellow]")
            sys.exit(0)

    if not client_secret:
        try:
            client_secret = getpass.getpass("Client Secret: ").strip()
        except (KeyboardInterrupt, EOFError):
            get_console().print("\n[yellow]Login cancelled.[/yellow]")
            sys.exit(0)

    if not client_id or not client_secret:
        get_console().print("[red]Client ID and Client Secret are required.[/red]")
        sys.exit(1)

    # Validate by fetching a token
    with get_console().status("[bold cyan]Validating credentials..."):
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    TOKEN_URL,
                    data={"grant_type": "client_credentials"},
                    auth=(client_id, client_secret),
                )
                resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            get_console().print(
                f"[red]Authentication failed ({e.response.status_code}).[/red] "
                "Check your Client ID and Secret."
            )
            sys.exit(1)
        except Exception as e:
            get_console().print(f"[red]Connection error:[/red] {e}")
            sys.exit(1)

    # Save to credentials.yml
    import yaml
    CREDS_FILE.parent.mkdir(parents=True, exist_ok=True)
    CREDS_FILE.write_text(
        yaml.dump({"client_id": client_id, "client_secret": client_secret,
                   "region": region}, default_flow_style=False)
    )
    CREDS_FILE.chmod(0o600)
    get_console().print(
        f"[bold green]✓ API client credentials saved.[/bold green] "
        f"[dim]{CREDS_FILE}[/dim]"
    )
    get_console().print("[dim]Run any hpecom command — no Okta needed.[/dim]")


# ---------------------------------------------------------------------------
# Logout command
# ---------------------------------------------------------------------------

async def _cmd_logout(_args: argparse.Namespace) -> None:
    from pcli.com.login import TOKEN_CACHE, CREDS_FILE, load_token, delete_glp_api_credential

    # Clean up GLP API credential before deleting token
    data = load_token()
    if data and data.get("glp_credential_name") and data.get("ccs_session"):
        try:
            await delete_glp_api_credential(
                data["access_token"], data["ccs_session"], data["glp_credential_name"]
            )
        except Exception:
            pass  # intentional: GLP credential cleanup is best-effort

    removed = []
    for path in (TOKEN_CACHE, CREDS_FILE):
        if path.exists():
            path.unlink()
            removed.append(path.name)

    if removed:
        get_console().print(f"[green]✓ Logged out.[/green] Removed: {', '.join(removed)}")
    else:
        get_console().print("[yellow]Not logged in (no credentials found).[/yellow]")


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
    get_console().print(
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
    filter_text = (getattr(args, "filter_text", None) or "").strip().lower()
    filter_model = (getattr(args, "filter_model", None) or "").strip().lower()

    with get_console().status("[bold cyan]Fetching devices from GreenLake..."):
        try:
            device_list = await _devices.fetch_devices(session, device_type=device_type)
        except AuthError as e:
            get_console().print(f"[red]Auth error:[/red] {e}")
            sys.exit(1)
        except Exception as e:
            get_console().print(f"[red]Error:[/red] {e}")
            sys.exit(1)

    # --filter: substring match across serial, hostname, model, location
    if filter_text:
        def _matches_filter(d) -> bool:
            haystack = " ".join([
                d.serial_number or "",
                d.raw.get("deviceName") or "",
                d.raw.get("secondaryName") or "",
                d.model or "",
                (d.raw.get("location") or {}).get("locationName") or "",
            ]).lower()
            return filter_text in haystack
        device_list = [d for d in device_list if _matches_filter(d)]

    # --model: normalize hyphens/spaces/case for fuzzy model match (dl380-gen11 → DL380 GEN11)
    if filter_model:
        normalized_query = filter_model.replace("-", " ").replace("gen", "gen").upper()
        device_list = [d for d in device_list
                       if normalized_query in (d.model or "").upper()]

    # Resolve user IDs → emails only when added-by column is requested
    user_cache: dict = {}
    effective_defaults = _SERVER_DEFAULT_FIELDS if getattr(args, "what", None) == "servers" else _DEVICE_DEFAULT_FIELDS
    requested_fields = [f.strip().lower() for f in fields.split(",")] if fields else list(effective_defaults)
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
                with get_console().status("[dim]Resolving user names..."):
                    user_cache = await _devices.resolve_user_ids(user_ids, glp_token)

    print_devices_table(device_list, raw=getattr(args, "raw", False),
                        fields=fields, sort_by=sort_by, user_cache=user_cache,
                        default_fields=effective_defaults)


async def _cmd_show_workspaces(args: argparse.Namespace) -> None:
    session = await _ensure_session(args)

    with get_console().status("[bold cyan]Fetching workspace info from GreenLake..."):
        try:
            workspace_list = await _workspaces.fetch_workspaces(session)
        except AuthError as e:
            get_console().print(f"[red]Auth error:[/red] {e}")
            sys.exit(1)
        except Exception as e:
            get_console().print(f"[red]Error:[/red] {e}")
            sys.exit(1)

    print_workspaces_table(workspace_list, raw=getattr(args, "raw", False))


async def _cmd_show_bundles(args: argparse.Namespace) -> None:
    session = await _ensure_session(args)
    active_only = not getattr(args, "all", False)
    gen = getattr(args, "gen", None)
    bundle_type = getattr(args, "bundle_type", None)

    with get_console().status("[bold cyan]Fetching SPP bundles from COM..."):
        try:
            bundle_list = await _firmware.fetch_bundles(
                session,
                active_only=active_only,
                gen=gen,
                bundle_type=bundle_type,
            )
        except AuthError as e:
            get_console().print(f"[red]Auth error:[/red] {e}")
            sys.exit(1)
        except Exception as e:
            get_console().print(f"[red]Error:[/red] {e}")
            sys.exit(1)

    print_bundles_table(bundle_list, raw=getattr(args, "raw", False))


async def _cmd_use_workspace(args: argparse.Namespace) -> None:
    from pcli.com.login import switch_workspace
    name_or_id = args.workspace
    try:
        with get_console().status(f"[bold cyan]Switching to workspace '{name_or_id}'..."):
            resolved_name = await switch_workspace(name_or_id)
        get_console().print(f"[bold green]✓ Switched to workspace:[/bold green] {resolved_name}")
    except (CredentialsError, ValueError) as e:
        get_console().print(f"[red]Error:[/red] {e}")
        sys.exit(1)
    except Exception as e:
        get_console().print(f"[red]Error:[/red] {e}")
        sys.exit(1)


async def _cmd_add_device(args: argparse.Namespace) -> None:
    from pcli.com.devices import add_compute_devices

    serial = args.serial_number.strip()
    part   = (args.part_number or "").strip()

    get_console().print(f"[cyan]Adding device[/cyan] {serial}" + (f" (part: {part})" if part else "") + "…")

    try:
        results = await add_compute_devices([serial], [part])
    except PermissionError as e:
        get_console().print(f"[red]Error:[/red] {e}")
        sys.exit(1)
    except Exception as e:
        get_console().print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    exit_code = 0
    for r in results:
        if r.status == "Complete":
            get_console().print(f"[bold green]✓ {r.serial_number}[/bold green] — {r.detail}")
        elif r.status == "Warning":
            get_console().print(f"[yellow]⚠ {r.serial_number}[/yellow] — {r.detail}")
        else:
            get_console().print(f"[red]✗ {r.serial_number}[/red] — {r.detail}")
            exit_code = 1

    if exit_code:
        sys.exit(exit_code)


# ── pcli com report gpu ───────────────────────────────────────────────────────

async def _cmd_report_gpu(args: argparse.Namespace) -> None:
    session = await _ensure_session(args)
    await _run_report_gpu(session)


# ── pcli com report memory ────────────────────────────────────────────────────

async def _cmd_report_memory(args: argparse.Namespace) -> None:
    session = await _ensure_session(args)
    await _run_report_memory(session)



# ── pcli com describe ─────────────────────────────────────────────────────────

async def _cmd_describe_server(args: argparse.Namespace) -> None:
    session = await _ensure_session(args)
    await _run_describe(session, args.server)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pcli com",
        description="HPE Compute Ops Management Python CLI",
    )

    # Global flags
    parser.add_argument("--json", action="store_true", dest="json_output",
                        help="Output as JSON (for piping/scripting)")

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

    # ── list ──────────────────────────────────────────────────────────────
    get_p = subparsers.add_parser("list", help="List resources")
    get_sub = get_p.add_subparsers(dest="what", metavar="WHAT")
    get_sub.required = True

    # pcli com list devices
    dev_p = get_sub.add_parser(
        "devices",
        help="List all devices in workspace",
        description=(
            "List all devices registered in the GreenLake workspace.\n\n"
            "Examples:\n"
            "  pcli com list devices\n"
            "  pcli com list devices --type COMPUTE\n"
            "  pcli com list devices --fields name,serial,service\n"
            "  pcli com list devices --fields name,serial,added,added-by\n"
            "  pcli com list devices --fields name,ilo-name,serial,location\n"
            "  pcli com list devices --sort added\n"
            "  pcli com list devices --sort added-by\n"
            "  pcli com list devices --fields name,serial,added,added-by --sort added\n"
            "  pcli com list devices --fields name,type,model,serial,part,service,sub-key,location,added,updated,added-by\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    dev_p.add_argument("--type", metavar="TYPE",
                       choices=["COMPUTE", "NETWORK", "STORAGE"],
                       help="Filter by device type")
    dev_p.add_argument("--filter", metavar="TEXT", dest="filter_text",
                       help="Case-insensitive substring filter across serial, hostname, model, location")
    dev_p.add_argument("--model", metavar="MODEL", dest="filter_model",
                       help="Filter by model (e.g. dl380-gen11, dl325-gen12)")
    dev_p.add_argument("--raw", action="store_true", help="Dump unprocessed API response (bypasses pcli field parsing)")
    fields_arg = dev_p.add_argument(
        "--fields", metavar="FIELDS",
        help=(
            f"Comma-separated columns to display (case-insensitive). "
            f"Available: {', '.join(DEVICE_FIELD_NAMES)}. "
            f"Default: {', '.join(_DEVICE_DEFAULT_FIELDS)}"
        ),
    )
    fields_arg.completer = make_field_completer(DEVICE_FIELD_NAMES)  # type: ignore[attr-defined]
    dev_p.add_argument(
        "--sort", metavar="FIELD", dest="sort_by",
        choices=list(DEVICE_FIELD_NAMES),
        help=f"Sort by field (case-insensitive). Available: {', '.join(DEVICE_FIELD_NAMES)}. Default: name",
    )

    # "servers" as alias for "devices"
    get_sub.add_parser("servers", help="Alias for 'devices' — list all devices in workspace",
                       parents=[dev_p], add_help=False)

    # pcli com list workspaces
    ws_p = get_sub.add_parser("workspaces", help="List all workspaces (* = active)")
    ws_p.add_argument("--raw", action="store_true", help="Dump unprocessed API response (bypasses pcli field parsing)")

    # pcli com list bundles
    bun_p = get_sub.add_parser(
        "bundles",
        help="List SPP firmware bundles available in COM",
        description=(
            "List Service Pack for ProLiant (SPP) firmware bundles available in COM.\n\n"
            "By default shows only active (current) bundles. Bundles are organised\n"
            "by server generation (Gen10/11/12) and type (BASE, PATCH, HOTFIX).\n\n"
            "Examples:\n"
            "  pcli com list bundles                   Active bundles (all gens)\n"
            "  pcli com list bundles --all             Include superseded bundles\n"
            "  pcli com list bundles --gen 12          Gen12 only\n"
            "  pcli com list bundles --gen 11          Gen11 only\n"
            "  pcli com list bundles --type base       BASE bundles only\n"
            "  pcli com list bundles --type patch      PATCH bundles only\n"
            "  pcli com list bundles --gen 12 --type base   Latest Gen12 BASE SPPs\n"
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
    bun_p.add_argument("--raw", action="store_true", help="Dump unprocessed API response (bypasses pcli field parsing)")

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

    # ── report ────────────────────────────────────────────────────────────
    rep_p = subparsers.add_parser("report", help="Fleet inventory reports")
    rep_sub = rep_p.add_subparsers(dest="what", metavar="WHAT")
    rep_sub.required = True

    rep_mem_p = rep_sub.add_parser("memory", help="Memory part-number breakdown across fleet")
    rep_mem_p.add_argument("--raw", action="store_true", help="Dump unprocessed API response (bypasses pcli field parsing)")
    rep_gpu_p = rep_sub.add_parser("gpu", help="Discrete GPU inventory across fleet")
    rep_gpu_p.add_argument("--raw", action="store_true", help="Dump unprocessed API response (bypasses pcli field parsing)")

    # ── describe ──────────────────────────────────────────────────────────────
    desc_p = subparsers.add_parser("describe", help="Show details for a server")
    desc_p.add_argument("server", metavar="SERIAL_OR_NAME",
                        help="Server serial number or name, e.g. TWA25380A01")

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> None:
    parser = _build_parser()
    argcomplete.autocomplete(parser)
    args = parser.parse_args(argv)

    if getattr(args, "json_output", False):
        set_output_mode(OutputMode.JSON)

    run_sync(_async_main(args))


async def _async_main(args: argparse.Namespace) -> None:
    """Single async entry point — all commands are dispatched from here."""
    if args.command == "login":
        await _cmd_login(args)
    elif args.command == "logout":
        await _cmd_logout(args)
    elif args.command == "list":
        if args.what in ("devices", "servers"):
            await _cmd_show_devices(args)
        elif args.what == "workspaces":
            await _cmd_show_workspaces(args)
        elif args.what == "bundles":
            await _cmd_show_bundles(args)
    elif args.command == "use":
        if args.what == "workspace":
            await _cmd_use_workspace(args)
    elif args.command == "add":
        if args.what == "device":
            await _cmd_add_device(args)
    elif args.command == "describe":
        await _cmd_describe_server(args)
    elif args.command == "report":
        if args.what == "memory":
            await _cmd_report_memory(args)
        elif args.what == "gpu":
            await _cmd_report_gpu(args)

