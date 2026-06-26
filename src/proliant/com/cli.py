"""
hpecom.cli
~~~~~~~~~~
Command-line interface.

Usage::

    proliant com login                         Okta Verify push login (prompts for email)
    proliant com login --email you@hpe.com     Pre-fill email, skip prompt
    proliant com login --password              Username + password login (external/gmail accounts)
    proliant com login --api-client            Login with HPE GreenLake API client credentials
    proliant com login --api-client --client-id ID --client-secret SECRET  Non-interactive

    proliant com logout                        Remove cached credentials and token

    proliant com devices list                   All devices in workspace
    proliant com devices list --type COMPUTE    Filter by type (COMPUTE, NETWORK, STORAGE)
    proliant com devices list --fields name,serial,service
    proliant com devices list --fields name,serial,added,added-by
    proliant com devices list --fields name,ilo-name,serial,location
    proliant com devices list --sort added      Sort by date added
    proliant com devices list --sort added-by   Sort by who added the device
    proliant com devices list --fields name,serial,added,added-by --sort added
    proliant com devices list --raw             Unprocessed API response

    proliant com servers list                   Server-focused view of workspace devices
    proliant com servers describe <serial>      Show one server in detail

    proliant com bundles list                   Active SPP firmware bundles in COM
    proliant com bundles list --all             Include inactive/superseded bundles
    proliant com bundles list --gen 12          Gen12 bundles only
    proliant com bundles list --gen 11          Gen11 bundles only
    proliant com bundles list --type patch      PATCH bundles only (base/patch/hotfix)
    proliant com bundles list --raw             Unprocessed API response

    proliant com workspaces list                All workspaces (active one marked with *)
    proliant com workspaces list --raw          Unprocessed API response

    proliant com workspace use <name-or-id>     Switch active workspace
    proliant com devices add --serial-number SN Add a compute device
    proliant com reports memory                 Fleet memory report
    proliant com reports gpu                    Fleet GPU report

Available --fields for 'devices list':
    name, ilo-name, type, model, serial, part, service, sub-key, location,
    added, updated, added-by

Note: Run 'proliant com login' before any resource command (like kubectl/gcloud/aws/az).
"""

# PYTHON_ARGCOMPLETE_OK
import argparse
import difflib
import json
import sys
from typing import Optional

import argcomplete

from proliant.common.display import get_console, make_table, print_json, print_memory_report, OutputMode, get_output_mode, set_output_mode
from proliant.common.runner import run_sync
from proliant.com.auth import COMSession, CredentialsError, AuthError
from proliant.com.client import COMClient
from proliant.com import devices as _devices
from proliant.com import workspaces as _workspaces
from proliant.com import firmware as _firmware
from proliant.com.describe import run_describe as _run_describe
from proliant.com.reports import run_report_gpu as _run_report_gpu, run_report_memory as _run_report_memory
from proliant.com.printers import (
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
        from proliant.com.login import load_token
        data = load_token() or {}
        names = [w.get("company_name", "") for w in data.get("workspaces", [])]
        return [n for n in names if n.startswith(prefix)]
    except Exception:  # intentional: completion must never print to stdout
        return []


# Login command
# ---------------------------------------------------------------------------

async def _cmd_login(args: argparse.Namespace) -> None:
    from proliant.com.login import okta_verify_login
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
        from proliant.com.login import password_login
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
    from proliant.com.login import CREDS_FILE
    from proliant.com.auth import COMSession, TOKEN_URL, AuthError
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
    get_console().print("[dim]Run any proliant com command — no Okta needed.[/dim]")


# ---------------------------------------------------------------------------
# Logout command
# ---------------------------------------------------------------------------

async def _cmd_logout(_args: argparse.Namespace) -> None:
    from proliant.com.login import TOKEN_CACHE, CREDS_FILE, load_token, delete_glp_api_credential

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
        get_console().print("[green]✓ Logged out.[/green]")
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
      3. ~/.config/proliant/com/credentials.yml
      4. Cached token from a previous 'proliant com login'
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

    # No credentials — prompt for login directly (like az login / gcloud auth login)
    get_console().print("[bold yellow]Not logged in.[/bold yellow] Starting login...\n")
    await _cmd_login(args)

    # Retry loading session after successful login
    try:
        return COMSession.load(
            client_id=getattr(args, "client_id", None),
            client_secret=getattr(args, "client_secret", None),
            region=getattr(args, "region", None),
        )
    except CredentialsError as e:
        get_console().print(f"[red]Login did not produce valid credentials:[/red] {e}")
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
    effective_defaults = _SERVER_DEFAULT_FIELDS if getattr(args, "command", None) == "servers" else _DEVICE_DEFAULT_FIELDS
    requested_fields = [f.strip().lower() for f in fields.split(",")] if fields else list(effective_defaults)
    if "added-by" in requested_fields:
        user_ids = {
            ((d.raw.get("contact") or {}).get("workspaceUser") or {}).get("id", "")
            for d in device_list
        } - {""}
        if user_ids:
            from proliant.com.login import load_token
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
    from proliant.com.login import switch_workspace
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
    from proliant.com.devices import add_compute_devices

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


# ── proliant com reports gpu ──────────────────────────────────────────────────────

async def _cmd_report_gpu(args: argparse.Namespace) -> None:
    session = await _ensure_session(args)
    await _run_report_gpu(session)


# ── proliant com reports memory ───────────────────────────────────────────────────

async def _cmd_report_memory(args: argparse.Namespace) -> None:
    session = await _ensure_session(args)
    await _run_report_memory(session)



# ── proliant com servers describe ─────────────────────────────────────────────────

async def _cmd_describe_server(args: argparse.Namespace) -> None:
    session = await _ensure_session(args)
    await _run_describe(session, args.server)


class _SuggestingArgumentParser(argparse.ArgumentParser):
    """ArgumentParser that suggests close matches on invalid choice errors."""

    _GREEN  = "\033[32m"
    _YELLOW = "\033[33m"
    _BOLD   = "\033[1m"
    _RESET  = "\033[0m"

    def error(self, message: str) -> None:
        import re
        suggestion = None
        match = re.search(r"(invalid choice: '[^']+') \(choose from ([^)]+)\)", message)
        if match:
            bad_part = match.group(1)
            choices_str = match.group(2)
            choices = [c.strip().strip("'") for c in choices_str.split(",")]
            close = difflib.get_close_matches(
                re.search(r"'([^']+)'", bad_part).group(1), choices, n=1, cutoff=0.6
            )
            if close:
                suggestion = close[0]
            colored_choices = ", ".join(
                f"{self._YELLOW}{c}{self._RESET}" for c in choices
            )
            message = re.sub(
                r"\(choose from [^)]+\)",
                f"(choose from {colored_choices})",
                message,
            )
        if suggestion:
            sys.stderr.write(
                f"\n{self._GREEN}{self._BOLD}  Did you mean: '{suggestion}'?{self._RESET}\n\n"
            )
        self.print_usage(sys.stderr)
        sys.stderr.write(f"{self.prog}: error: {message}\n")
        sys.exit(2)


def _build_parser() -> argparse.ArgumentParser:
    parser = _SuggestingArgumentParser(
        prog="proliant com",
        description="HPE Compute Ops Management Python CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  proliant com login
  proliant com devices list
  proliant com devices add --serial-number TWA25380A01
  proliant com servers list
  proliant com servers describe TWA25380A01
  proliant com bundles list --gen 12
  proliant com workspaces list
  proliant com workspace use MyWorkspace
  proliant com reports memory
  proliant com reports gpu
""",
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

    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND",
                                       parser_class=_SuggestingArgumentParser)
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

    def _add_device_list_args(p: argparse.ArgumentParser, *, default_fields: tuple[str, ...]) -> None:
        p.add_argument("--type", metavar="TYPE",
                       choices=["COMPUTE", "NETWORK", "STORAGE"],
                       help="Filter by device type")
        p.add_argument("--filter", metavar="TEXT", dest="filter_text",
                       help="Case-insensitive substring filter across serial, hostname, model, location")
        p.add_argument("--model", metavar="MODEL", dest="filter_model",
                       help="Filter by model (e.g. dl380-gen11, dl325-gen12)")
        p.add_argument("--raw", action="store_true", help="Dump unprocessed API response (bypasses proliant field parsing)")
        fields_arg = p.add_argument(
            "--fields", metavar="FIELDS",
            help=(
                f"Comma-separated columns to display (case-insensitive). "
                f"Available: {', '.join(DEVICE_FIELD_NAMES)}. "
                f"Default: {', '.join(default_fields)}"
            ),
        )
        fields_arg.completer = make_field_completer(DEVICE_FIELD_NAMES)  # type: ignore[attr-defined]
        p.add_argument(
            "--sort", metavar="FIELD", dest="sort_by",
            choices=list(DEVICE_FIELD_NAMES),
            help=f"Sort by field (case-insensitive). Available: {', '.join(DEVICE_FIELD_NAMES)}. Default: name",
        )

    # ── devices ───────────────────────────────────────────────────────────
    devices_p = subparsers.add_parser("devices", help="Manage workspace devices")
    devices_sub = devices_p.add_subparsers(dest="what", metavar="ACTION",
                                           parser_class=_SuggestingArgumentParser)
    devices_sub.required = True

    dev_list_p = devices_sub.add_parser(
        "list",
        help="List all devices in workspace",
        description=(
            "List all devices registered in the GreenLake workspace.\n\n"
            "Examples:\n"
            "  proliant com devices list\n"
            "  proliant com devices list --type COMPUTE\n"
            "  proliant com devices list --fields name,serial,service\n"
            "  proliant com devices list --fields name,serial,added,added-by\n"
            "  proliant com devices list --fields name,ilo-name,serial,location\n"
            "  proliant com devices list --sort added\n"
            "  proliant com devices list --sort added-by\n"
            "  proliant com devices list --fields name,serial,added,added-by --sort added\n"
            "  proliant com devices list --fields name,type,model,serial,part,service,sub-key,location,added,updated,added-by\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_device_list_args(dev_list_p, default_fields=_DEVICE_DEFAULT_FIELDS)

    add_dev_p = devices_sub.add_parser(
        "add",
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

    # ── servers ───────────────────────────────────────────────────────────
    servers_p = subparsers.add_parser("servers", help="List or describe servers")
    servers_sub = servers_p.add_subparsers(dest="what", metavar="ACTION",
                                           parser_class=_SuggestingArgumentParser)
    servers_sub.required = True

    srv_list_p = servers_sub.add_parser(
        "list",
        help="List servers in workspace",
        description=(
            "List servers in the GreenLake workspace using server-focused default columns.\n\n"
            "Examples:\n"
            "  proliant com servers list\n"
            "  proliant com servers list --model dl325-gen12\n"
            "  proliant com servers list --fields name,serial,location\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_device_list_args(srv_list_p, default_fields=_SERVER_DEFAULT_FIELDS)

    desc_p = servers_sub.add_parser("describe", help="Show details for a server")
    desc_p.add_argument("server", metavar="SERIAL_OR_NAME",
                        help="Server serial number or name, e.g. TWA25380A01")

    # ── workspaces ────────────────────────────────────────────────────────
    workspaces_p = subparsers.add_parser("workspaces", help="List workspaces")
    workspaces_sub = workspaces_p.add_subparsers(dest="what", metavar="ACTION",
                                                 parser_class=_SuggestingArgumentParser)
    workspaces_sub.required = True
    ws_list_p = workspaces_sub.add_parser("list", help="List all workspaces (* = active)")
    ws_list_p.add_argument("--raw", action="store_true", help="Dump unprocessed API response (bypasses proliant field parsing)")

    # ── workspace ─────────────────────────────────────────────────────────
    workspace_p = subparsers.add_parser("workspace", help="Manage the active workspace")
    workspace_sub = workspace_p.add_subparsers(dest="what", metavar="ACTION",
                                               parser_class=_SuggestingArgumentParser)
    workspace_sub.required = True
    use_ws_p = workspace_sub.add_parser("use", help="Switch active workspace")
    use_ws_p.add_argument(
        "workspace", metavar="NAME_OR_ID",
        help="Workspace name or platform_customer_id",
    ).completer = _workspace_names_completer  # type: ignore[attr-defined]

    # ── bundles ───────────────────────────────────────────────────────────
    bundles_p = subparsers.add_parser("bundles", help="List SPP firmware bundles")
    bundles_sub = bundles_p.add_subparsers(dest="what", metavar="ACTION",
                                           parser_class=_SuggestingArgumentParser)
    bundles_sub.required = True

    bun_list_p = bundles_sub.add_parser(
        "list",
        help="List SPP firmware bundles available in COM",
        description=(
            "List Service Pack for ProLiant (SPP) firmware bundles available in COM.\n\n"
            "By default shows only active (current) bundles. Bundles are organised\n"
            "by server generation (Gen10/11/12) and type (BASE, PATCH, HOTFIX).\n\n"
            "Examples:\n"
            "  proliant com bundles list                   Active bundles (all gens)\n"
            "  proliant com bundles list --all             Include superseded bundles\n"
            "  proliant com bundles list --gen 12          Gen12 only\n"
            "  proliant com bundles list --gen 11          Gen11 only\n"
            "  proliant com bundles list --type base       BASE bundles only\n"
            "  proliant com bundles list --type patch      PATCH bundles only\n"
            "  proliant com bundles list --gen 12 --type base   Latest Gen12 BASE SPPs\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    bun_list_p.add_argument("--all", action="store_true",
                            help="Include inactive/superseded bundles (default: active only)")
    bun_list_p.add_argument("--gen", type=int, choices=[10, 11, 12], metavar="GEN",
                            help="Filter by server generation: 10, 11, or 12")
    bun_list_p.add_argument("--type", dest="bundle_type",
                            choices=["base", "patch", "hotfix"],
                            metavar="TYPE",
                            help="Filter by bundle type: base, patch, or hotfix")
    bun_list_p.add_argument("--raw", action="store_true", help="Dump unprocessed API response (bypasses proliant field parsing)")

    # ── reports ───────────────────────────────────────────────────────────
    reports_p = subparsers.add_parser("reports", help="Fleet inventory reports")
    reports_sub = reports_p.add_subparsers(dest="what", metavar="REPORT",
                                           parser_class=_SuggestingArgumentParser)
    reports_sub.required = True

    rep_mem_p = reports_sub.add_parser("memory", help="Memory part-number breakdown across fleet")
    rep_mem_p.add_argument("--raw", action="store_true", help="Dump unprocessed API response (bypasses proliant field parsing)")
    rep_gpu_p = reports_sub.add_parser("gpu", help="Discrete GPU inventory across fleet")
    rep_gpu_p.add_argument("--raw", action="store_true", help="Dump unprocessed API response (bypasses proliant field parsing)")

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

    try:
        run_sync(_async_main(args))
    except (AuthError, CredentialsError) as exc:
        console = get_console()
        console.print(f"\n[yellow]Session expired or not logged in.[/yellow] ({exc})")
        console.print("Please log in to continue.\n")
        try:
            from rich.prompt import Prompt
            from proliant.com.login import okta_verify_login
            email = Prompt.ask("[bold]HPE GreenLake email[/bold]").strip()
            if not email:
                console.print("[red]Email is required.[/red]")
                sys.exit(1)
            run_sync(okta_verify_login(email=email, region="us-west"))
            console.print("\n[green]✓ Logged in.[/green] Re-run your command to continue.\n")
        except (KeyboardInterrupt, EOFError):
            console.print("\n[yellow]Login cancelled.[/yellow]")
        sys.exit(1)


async def _async_main(args: argparse.Namespace) -> None:
    """Single async entry point — all commands are dispatched from here."""
    if args.command == "login":
        await _cmd_login(args)
    elif args.command == "logout":
        await _cmd_logout(args)
    elif args.command == "devices":
        if args.what == "list":
            await _cmd_show_devices(args)
        elif args.what == "add":
            await _cmd_add_device(args)
    elif args.command == "servers":
        if args.what == "list":
            await _cmd_show_devices(args)
        elif args.what == "describe":
            await _cmd_describe_server(args)
    elif args.command == "workspaces":
        if args.what == "list":
            await _cmd_show_workspaces(args)
    elif args.command == "workspace":
        if args.what == "use":
            await _cmd_use_workspace(args)
    elif args.command == "bundles":
        if args.what == "list":
            await _cmd_show_bundles(args)
    elif args.command == "reports":
        if args.what == "memory":
            await _cmd_report_memory(args)
        elif args.what == "gpu":
            await _cmd_report_gpu(args)
