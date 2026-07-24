"""
proliant.spp.cli — SPP subcommand: list, inspect, diff.

Usage:
    proliant spp list [--gen gen10|gen11|gen12]
    proliant spp inspect <gen> <version> [--type ilo,bios,nic,storage] [--model DL325]
    proliant spp inspect <file.fwpkg>
    proliant spp diff <gen> <v1> <v2> [--type ...] [--model ...] [--all]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import argcomplete
from rich.console import Console
from rich.table import Table
from rich import box

from proliant.common.completers import comma_sep_completer, suppress_file_completion, cached_names
from proliant.spp.catalog import (
    SUPPORTED_GENS,
    TYPE_FILTERS,
    SppComponent,
    DiffEntry,
    _norm_gen,
    list_versions,
    list_all_versions,
    fetch_catalog,
    filter_components,
    diff_catalogs,
    _packages_dir,
    _package_url,
    _complete_marker,
    is_spp_downloaded,
    get_verified_package,
)

console = Console()

_GEN_COMPLETIONS = tuple(SUPPORTED_GENS + ("10", "11", "12", "all"))


def _prefix_matches(values: tuple[str, ...] | list[str], prefix: str) -> list[str]:
    return [value for value in values if value.lower().startswith(prefix.lower())]


def _gen_completer(prefix: str, **_kwargs) -> list[str]:
    return _prefix_matches(_GEN_COMPLETIONS, prefix)


def _spp_type_completer(prefix: str, **kwargs) -> list[str]:
    return comma_sep_completer(tuple(TYPE_FILTERS))(prefix, **kwargs)


def _spp_version_completer(prefix: str, parsed_args: argparse.Namespace, **_kwargs) -> list[str]:
    gen = getattr(parsed_args, "gen", None)
    if not gen or str(gen).lower() == "all":
        return []
    try:
        norm_gen = _norm_gen(gen)
        # list_versions() fetches the SDR index page over HTTP; cache it
        # briefly so repeated TAB presses don't re-hit the network each time.
        names = cached_names(f"spp-versions-{norm_gen}", lambda: list_versions(norm_gen))
        return _prefix_matches(names, prefix)
    except Exception:
        return []


def _local_package_completer(prefix: str, parsed_args: argparse.Namespace, **_kwargs) -> list[str]:
    gen = getattr(parsed_args, "gen", None)
    version = getattr(parsed_args, "version", None)
    if not gen or not version:
        return []
    try:
        package_dir = _packages_dir(_norm_gen(gen), version)
        if not package_dir.exists():
            return []
        return _prefix_matches([path.name for path in package_dir.glob("*.fwpkg")], prefix)
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="proliant spp",
        description="Analyse HPE Service Pack for ProLiant (SPP) catalogs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  proliant spp list                                     List all gen12 SPP versions
  proliant spp list --gen gen11                         List gen11 versions
  proliant spp list --gen all                           List all gens
  proliant spp inspect gen12 2026.03.00.00              All components in gen12 SPP
  proliant spp inspect gen12 2026.03.00.00 --type ilo,bios,nic,storage
  proliant spp inspect gen12 2026.03.00.00 --model DL325
  proliant spp part-number P26264-001 gen12 2026.03.00.00   Inspect fwpkg(s) for a part number
  proliant spp diff gen12 2025.09.01.00 2026.03.00.00   What changed in gen12?
  proliant spp diff gen12 2025.09.01.00 2026.03.00.00 --type bios,ilo
  proliant spp diff gen12 2025.09.01.00 2026.03.00.00 --all  Include unchanged
  proliant spp inspect firmware.fwpkg                   Inspect a local .fwpkg file
""",
    )
    sub = p.add_subparsers(dest="cmd", metavar="COMMAND")

    # ── list ─────────────────────────────────────────────────────────────────
    p_list = sub.add_parser("list", help="List available SPP versions")
    list_gen_arg = p_list.add_argument(
        "--gen",
        default="gen12",
        metavar="GEN",
        help="Generation to list: gen10, gen11, gen12, or 'all' (default: gen12)",
    )
    list_gen_arg.completer = _gen_completer

    # ── inspect ──────────────────────────────────────────────────────────────
    p_inspect = sub.add_parser("inspect", help="Show components in an SPP version")
    inspect_gen_arg = p_inspect.add_argument("gen", metavar="GEN", help="Generation, e.g. gen12")
    inspect_gen_arg.completer = _gen_completer
    inspect_version_arg = p_inspect.add_argument("version", metavar="VERSION", help="SPP version, e.g. 2026.03.00.00")
    inspect_version_arg.completer = _spp_version_completer
    inspect_package_arg = p_inspect.add_argument(
        "package",
        metavar="FILE.fwpkg",
        nargs="?",
        help="Inspect a specific .fwpkg file (downloaded if not already local)",
    )
    inspect_package_arg.completer = _local_package_completer
    inspect_type_arg = p_inspect.add_argument(
        "--type", "-t",
        dest="types",
        metavar="TYPES",
        help=f"Comma-separated types to show: {', '.join(TYPE_FILTERS)}",
    )
    inspect_type_arg.completer = _spp_type_completer
    inspect_model_arg = p_inspect.add_argument(
        "--model", "-m",
        metavar="MODEL",
        help="Filter by server model substring, e.g. DL325, DL380",
    )
    inspect_model_arg.completer = suppress_file_completion()
    p_inspect.add_argument(
        "--firmware-only", "-f",
        action="store_true",
        help="Show only Firmware/ComboFirmware components (hide drivers/software)",
    )
    p_inspect.add_argument(
        "--force",
        action="store_true",
        help="Re-download catalog even if already cached",
    )

    # ── inspect part-number ───────────────────────────────────────────────────
    p_pn = sub.add_parser(
        "part-number",
        help="Find and inspect the fwpkg for a specific part number",
    )
    pn_part_arg = p_pn.add_argument("part_number", metavar="PART-NUMBER", help="HPE part number, e.g. P26264-001")
    pn_part_arg.completer = suppress_file_completion()
    pn_gen_arg = p_pn.add_argument("gen", metavar="GEN", help="Generation, e.g. gen12")
    pn_gen_arg.completer = _gen_completer
    pn_version_arg = p_pn.add_argument("version", metavar="VERSION", help="SPP version, e.g. 2026.03.00.00")
    pn_version_arg.completer = _spp_version_completer
    p_pn.add_argument("--force", action="store_true", help="Re-download catalog even if already cached")

    # ── download ──────────────────────────────────────────────────────────────
    p_dl = sub.add_parser("download", help="Download .fwpkg files from an SPP version")
    dl_gen_arg = p_dl.add_argument("gen", metavar="GEN", help="Generation, e.g. gen12")
    dl_gen_arg.completer = _gen_completer
    dl_version_arg = p_dl.add_argument("version", metavar="VERSION", help="SPP version, e.g. 2026.03.00.00")
    dl_version_arg.completer = _spp_version_completer
    dl_type_arg = p_dl.add_argument(
        "--type", "-t",
        dest="types",
        metavar="TYPES",
        help=f"Comma-separated types to download: {', '.join(TYPE_FILTERS)}",
    )
    dl_type_arg.completer = _spp_type_completer
    dl_model_arg = p_dl.add_argument(
        "--model", "-m",
        metavar="MODEL",
        help="Filter by server model substring, e.g. DL325, DL380",
    )
    dl_model_arg.completer = suppress_file_completion()
    p_dl.add_argument(
        "--force",
        action="store_true",
        help="Re-download even if file already exists locally",
    )

    # ── diff ─────────────────────────────────────────────────────────────────
    p_diff = sub.add_parser("diff", help="Compare two SPP versions")
    diff_gen_arg = p_diff.add_argument("gen", metavar="GEN", help="Generation, e.g. gen12")
    diff_gen_arg.completer = _gen_completer
    diff_v1_arg = p_diff.add_argument("v1", metavar="FROM", help="Older SPP version")
    diff_v1_arg.completer = _spp_version_completer
    diff_v2_arg = p_diff.add_argument("v2", metavar="TO", help="Newer SPP version")
    diff_v2_arg.completer = _spp_version_completer
    diff_type_arg = p_diff.add_argument(
        "--type", "-t",
        dest="types",
        metavar="TYPES",
        help="Comma-separated types to compare",
    )
    diff_type_arg.completer = _spp_type_completer
    diff_model_arg = p_diff.add_argument(
        "--model", "-m",
        metavar="MODEL",
        help="Filter by server model substring",
    )
    diff_model_arg.completer = suppress_file_completion()
    p_diff.add_argument(
        "--all", "-a",
        dest="show_all",
        action="store_true",
        help="Also show unchanged components",
    )
    p_diff.add_argument(
        "--force",
        action="store_true",
        help="Re-download catalogs even if cached",
    )

    return p


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def _cmd_list(args: argparse.Namespace) -> None:
    gen_arg = args.gen.lower()

    if gen_arg == "all":
        gens_to_show = list(SUPPORTED_GENS)
    else:
        gens_to_show = [_norm_gen(gen_arg)]

    table = Table(
        title="HPE SPP Available Versions",
        box=box.ROUNDED,
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("Generation", style="bold", min_width=12)
    table.add_column("Version", min_width=16)
    table.add_column("Cached", justify="center")

    from proliant.spp.catalog import _catalog_path

    any_found = False
    for gen in gens_to_show:
        try:
            versions = list_versions(gen)
        except Exception as exc:
            console.print(f"[yellow]Warning: could not fetch {gen}: {exc}[/yellow]")
            continue
        for v in versions:
            cached = "✓" if _catalog_path(gen, v).exists() else ""
            table.add_row(gen.upper(), v, cached)
            any_found = True

    if not any_found:
        console.print("[red]No SPP versions found.[/red]")
        return

    console.print(table)
    console.print(
        "\n[dim]Run [bold]proliant spp inspect <gen> <version>[/bold] to analyse a version.[/dim]"
    )


def _cmd_inspect(args: argparse.Namespace) -> None:
    gen = _norm_gen(args.gen)
    version = args.version

    # If a specific .fwpkg was requested, find/download and inspect it
    if getattr(args, "package", None):
        filename = args.package
        if not filename.lower().endswith(".fwpkg"):
            console.print("[red]Package argument must end with .fwpkg[/red]")
            sys.exit(1)
        # Try local absolute/relative path first, then packages dir
        import os
        local = Path(filename)
        if local.exists():
            _cmd_inspect_fwpkg(str(local))
            return
        # Load catalog to find the component (for metadata)
        with console.status(f"Loading SPP {gen.upper()} {version}…"):
            try:
                components = fetch_catalog(gen, version, force=args.force)
            except Exception as exc:
                console.print(f"[red]Error fetching catalog: {exc}[/red]")
                sys.exit(1)
        comp = next((c for c in components if c.filename == filename), None)
        if comp is None:
            console.print(f"[red]{filename} not found in SPP {gen.upper()} {version}[/red]")
            sys.exit(1)
        pkg_path = _get_local_package(gen, version, filename, components)
        _cmd_inspect_fwpkg(str(pkg_path), expected_sha256=comp.sha256)
        return

    types = [t.strip() for t in args.types.split(",")] if args.types else None
    model = args.model

    with console.status(f"Loading SPP {gen.upper()} {version}…"):
        try:
            components = fetch_catalog(gen, version, force=args.force)
        except Exception as exc:
            console.print(f"[red]Error fetching catalog: {exc}[/red]")
            sys.exit(1)

    filtered = filter_components(
        components,
        types=types,
        model=model,
        gen=gen,
        firmware_only=args.firmware_only,
    )

    if not filtered:
        console.print("[yellow]No components match the given filters.[/yellow]")
        return

    title_parts = [f"SPP {gen.upper()} {version}"]
    if types:
        title_parts.append(f"type={','.join(types)}")
    if model:
        title_parts.append(f"model={model}")

    table = Table(
        title=" · ".join(title_parts),
        box=box.ROUNDED,
        show_header=True,
        header_style="bold cyan",
        show_lines=False,
    )
    table.add_column("Type",       style="bold",   min_width=12)
    table.add_column("Name",                       min_width=30)
    table.add_column("File",       style="dim",    min_width=20, max_width=38, no_wrap=True)
    table.add_column("Update Method",              min_width=18)
    table.add_column("Release",    justify="right",min_width=10)

    # Sort: firmware first, then by type_tag, then name
    def _sort_key(c: SppComponent):
        fw_first = 0 if "firmware" in c.component_type.lower() else 1
        return (fw_first, c.type_tag, c.name)

    for c in sorted(filtered, key=_sort_key):
        method_style = _method_style(c.update_method)
        table.add_row(
            c.type_tag,
            c.name,
            _trunc_filename(c.filename),
            f"[{method_style}]{c.update_method}[/{method_style}]",
            c.release_date[:10] if c.release_date else "—",
        )

    console.print(table)
    console.print(
        f"\n[dim]{len(filtered)} component(s) shown"
        + (f" of {len(components)} total" if len(filtered) < len(components) else "")
        + "[/dim]"
    )


def _pnum_index_path(gen: str, version: str) -> "Path":
    from proliant.spp.catalog import _packages_dir
    return _packages_dir(gen, version) / "pnum_index.json"


def _build_pnum_index(gen: str, version: str) -> "dict[str, list[str]]":
    """Scan all downloaded fwpkg files and build a part-number → [filenames] index."""
    import zipfile, re as _re, json as _json
    from proliant.spp.catalog import _packages_dir

    pdir = _packages_dir(gen, version)
    pkgs = sorted(pdir.glob("*.fwpkg"))

    file_to_pnums: dict[str, list[str]] = {}
    for pkg in pkgs:
        pnums: set[str] = set()
        try:
            with zipfile.ZipFile(pkg) as zf:
                for name in zf.namelist():
                    raw = zf.read(name)
                    pnums.update(m.decode() for m in _re.findall(rb'P\d{5}-\d{3}', raw))
        except Exception:
            pass
        if pnums:
            file_to_pnums[pkg.name] = sorted(pnums)

    pnum_to_files: dict[str, list[str]] = {}
    for fname, pnums in file_to_pnums.items():
        for pn in pnums:
            pnum_to_files.setdefault(pn, []).append(fname)

    index = {"file_to_pnums": file_to_pnums, "pnum_to_files": pnum_to_files, "scanned": len(pkgs)}
    _pnum_index_path(gen, version).write_text(_json.dumps(index, indent=2))
    return pnum_to_files


def _load_pnum_index(gen: str, version: str, *, force: bool = False) -> "tuple[dict[str, list[str]], int, int]":
    """Load cached P-number index, rebuilding if missing or forced."""
    import json as _json
    from proliant.spp.catalog import _packages_dir

    pdir = _packages_dir(gen, version)
    total_local = len(list(pdir.glob("*.fwpkg")))
    idx_path = _pnum_index_path(gen, version)

    if not force and idx_path.exists():
        try:
            data = _json.loads(idx_path.read_text())
            scanned = data.get("scanned", 0)
            if scanned >= total_local:
                return data["pnum_to_files"], scanned, total_local
        except Exception:
            pass

    pnum_to_files = _build_pnum_index(gen, version)
    return pnum_to_files, total_local, total_local


def _cmd_inspect_part_number(args: argparse.Namespace) -> None:
    """Find all fwpkg files for a given HPE part number and inspect each one."""
    gen = _norm_gen(args.gen)
    version = args.version
    part_number = args.part_number.strip().upper()

    with console.status(f"Loading SPP {gen.upper()} {version} catalog…"):
        try:
            components = fetch_catalog(gen, version, force=args.force)
        except Exception as exc:
            console.print(f"[red]Error fetching catalog: {exc}[/red]")
            sys.exit(1)

    from proliant.spp.catalog import _packages_dir
    pdir = _packages_dir(gen, version)
    total_local = len(list(pdir.glob("*.fwpkg")))

    if total_local == 0:
        console.print(
            f"[yellow]No packages downloaded yet for SPP {gen.upper()} {version}.[/yellow]\n"
            f"  Run [bold]proliant spp download {gen} {version}[/bold] first, then retry."
        )
        sys.exit(1)

    with console.status(f"[dim]Scanning {total_local} local packages for {part_number}…[/dim]"):
        pnum_to_files, scanned, _ = _load_pnum_index(gen, version, force=args.force)

    matches = pnum_to_files.get(part_number, [])

    if not matches:
        hint = ""
        if scanned < len(components):
            hint = f"\n  [dim]Note: only {scanned} of {len(components)} catalog packages are downloaded locally.[/dim]"
        console.print(
            f"[yellow]No packages found for part number [bold]{part_number}[/bold] "
            f"in SPP {gen.upper()} {version} (searched {scanned} packages).{hint}[/yellow]"
        )
        sys.exit(1)

    console.print(
        f"\n[bold cyan]{len(matches)} package(s) found for {part_number} "
        f"in SPP {gen.upper()} {version}:[/bold cyan]\n"
    )

    comp_by_filename = {c.filename: c for c in components}
    for filename in matches:
        comp = comp_by_filename.get(filename)
        pkg_path = pdir / filename
        if not pkg_path.exists():
            console.print(f"[yellow]  {filename} — not downloaded locally, skipping[/yellow]")
            continue
        _cmd_inspect_fwpkg(
            str(pkg_path),
            expected_sha256=comp.sha256 if comp else "",
        )
        console.print()


def _ensure_spp_packages(gen: str, version: str, components: list) -> None:
    """Ensure all SPP packages are downloaded and verified.

    Downloads in parallel (6 workers), tracks progress as total bytes with
    speed + ETA. Only fetches sidecar .json for FWPKG-v2 packages.
    """
    import threading
    import urllib.request as _ur
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from rich.progress import (
        Progress, BarColumn, DownloadColumn,
        TransferSpeedColumn, TimeRemainingColumn,
    )
    from proliant.spp.catalog import _sha256_file

    if is_spp_downloaded(gen, version):
        return

    dest_dir = _packages_dir(gen, version)
    dest_dir.mkdir(parents=True, exist_ok=True)

    # .exe and .zip are Windows SoftPaqs — not hosted on the Linux SDR
    _SDR_SKIP_EXTS = {".exe", ".zip"}

    # Build unique file info: filename → (sha256, size_bytes, has_sidecar)
    file_info: dict[str, tuple[str, int, bool]] = {}
    for c in components:
        if c.filename and c.filename not in file_info:
            if not any(c.filename.lower().endswith(e) for e in _SDR_SKIP_EXTS):
                file_info[c.filename] = (c.sha256, c.size_bytes, c.has_sidecar)

    # Determine what still needs downloading (verify existing files)
    to_download: list[tuple[str, str, int, bool]] = []
    already = 0
    for filename, (sha256, size_bytes, has_sidecar) in sorted(file_info.items()):
        dest = dest_dir / filename
        if dest.exists():
            if sha256 and _sha256_file(dest) == sha256.lower():
                already += 1
                continue
            dest.unlink()  # corrupt or stale — re-download
        to_download.append((filename, sha256, size_bytes, has_sidecar))

    total_bytes = sum(s for _, _, s, _ in to_download)
    total_gb = total_bytes / 1024 ** 3

    console.rule(f"[bold cyan]SPP {gen.upper()} {version} — Package Download[/bold cyan]")
    console.print(
        f"\n  [bold]{len(file_info)}[/bold] packages total  "
        f"([dim]~{total_gb:.1f} GB to download[/dim])\n"
        + (f"  [green]{already} already cached[/green], " if already else "  ")
        + f"[yellow]{len(to_download)} remaining[/yellow]\n"
        f"\n"
        f"  Each file is SHA256-verified. Files saved to:\n"
        f"    [dim]{dest_dir}[/dim]\n"
    )
    try:
        answer = console.input("  Proceed with download? [Y/n] ", markup=False).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    if answer not in ("", "y", "yes"):
        console.print("\n[yellow]Download cancelled.[/yellow]")
        sys.exit(0)
    console.print()

    errors: list[str] = []
    skipped_404: list[str] = []
    lock = threading.Lock()
    _UA = {"User-Agent": "proliant/1.0"}

    def _download_one(filename: str, sha256: str, has_sidecar: bool) -> None:
        dest = dest_dir / filename
        url = _package_url(gen, version, filename)
        try:
            req = _ur.Request(url, headers=_UA)
            with _ur.urlopen(req, timeout=120) as resp:
                with open(dest, "wb") as fh:
                    while True:
                        buf = resp.read(131072)  # 128 KB chunks
                        if not buf:
                            break
                        fh.write(buf)
                        progress.advance(task, len(buf))
        except Exception as exc:
            dest.unlink(missing_ok=True)
            if "404" in str(exc):
                with lock:
                    skipped_404.append(filename)
            else:
                with lock:
                    errors.append(f"{filename}: {exc}")
            return

        # SHA256 verify
        if sha256 and _sha256_file(dest) != sha256.lower():
            dest.unlink()
            with lock:
                errors.append(
                    f"{filename}: checksum mismatch (expected {sha256[:16]}…)"
                )
            return

        # Fetch companion sidecar .json — only for FWPKG-v2 packages
        if has_sidecar:
            stem = filename.rsplit(".", 1)[0]
            json_dest = dest_dir / (stem + ".json")
            if not json_dest.exists():
                try:
                    jr = _ur.Request(
                        _package_url(gen, version, stem + ".json"), headers=_UA
                    )
                    with _ur.urlopen(jr, timeout=30) as jr_resp:
                        json_dest.write_bytes(jr_resp.read())
                except Exception:
                    pass  # best-effort

    with Progress(
        "[progress.description]{task.description}",
        BarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        task = progress.add_task(
            f"[bold]SPP {gen.upper()} {version}",
            total=total_bytes or None,
        )
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = {
                pool.submit(_download_one, fname, sha256, has_sidecar): fname
                for fname, sha256, _, has_sidecar in to_download
            }
            for fut in as_completed(futures):
                fut.result()

    if skipped_404:
        console.print(
            f"\n[dim]{len(skipped_404)} file(s) not available on Linux SDR "
            f"(Windows-only packages — skipped)[/dim]"
        )
    if errors:
        console.print(f"\n[red]{len(errors)} file(s) failed:[/red]")
        for e in errors:
            console.print(f"  [red]✗[/red] {e}")
        sys.exit(1)

    import json as _json
    _complete_marker(gen, version).write_text(
        _json.dumps({"gen": gen, "version": version, "files": len(file_info)})
    )
    console.print(f"\n[green]✓ {len(file_info)} packages downloaded and verified.[/green]")


def _get_local_package(
    gen: str, version: str, filename: str, components: list
) -> "Path":
    """Return verified local path for a specific fwpkg.

    Calls _ensure_spp_packages() first to guarantee the full SPP is present
    and all checksums are valid.
    """
    _ensure_spp_packages(gen, version, components)
    path = get_verified_package(gen, version, filename, components)
    if path is None:
        console.print(
            f"[red]Checksum verification failed for {filename}. "
            "The file may be corrupted — re-run 'proliant spp download --force' to re-download.[/red]"
        )
        sys.exit(1)
    return path


def _strip_html(html: str) -> str:
    """Strip HTML tags and normalise whitespace for terminal display."""
    import re
    text = re.sub(r'<br\s*/?>', '\n', html, flags=re.IGNORECASE)
    text = re.sub(r'<li\b[^>]*>', '• ', text, flags=re.IGNORECASE)
    text = re.sub(r'<p\b[^>]*>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</p>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<ul\b[^>]*>|</ul>', '', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'&nbsp;', ' ', text)
    text = re.sub(r'&lt;', '<', text)
    text = re.sub(r'&gt;', '>', text)
    text = re.sub(r'&amp;', '&', text)
    lines = [l.rstrip() for l in text.splitlines()]
    result: list[str] = []
    prev_blank = False
    for line in lines:
        blank = not line.strip()
        if blank and prev_blank:
            continue
        result.append(line)
        prev_blank = blank
    return '\n'.join(result).strip()


def _lang_en(entries: list) -> str:
    """Extract the English value from a [{Lang/lang, Value/x_late}, ...] list.

    Handles both Gen12 style {Lang, Value} and Gen11 style {lang, x_late}.
    """
    for e in entries:
        lang = e.get("Lang") or e.get("lang", "")
        if lang.lower() == "en":
            return e.get("Value") or e.get("x_late", "")
    if entries:
        e = entries[0]
        return e.get("Value") or e.get("x_late", "")
    return ""


def _pkg_field(pkg: dict, *keys: str) -> object:
    """Look up a field trying each key in order (handles Gen12 CamelCase vs Gen11 snake_case)."""
    for k in keys:
        v = pkg.get(k)
        if v is not None:
            return v
    return None


def _cmd_inspect_fwpkg(path: str, *, expected_sha256: str = "") -> None:
    """Inspect a local .fwpkg file and display its contents and install metadata."""
    import zipfile
    import json
    import os

    fpath = os.path.abspath(path)
    if not os.path.exists(fpath):
        console.print(f"[red]File not found: {fpath}[/red]")
        sys.exit(1)

    try:
        zf = zipfile.ZipFile(fpath)
    except zipfile.BadZipFile:
        console.print(f"[red]Not a valid fwpkg (zip) file: {fpath}[/red]")
        sys.exit(1)

    fname = os.path.basename(fpath)
    total_size = os.path.getsize(fpath)

    # ── Checksum verification ─────────────────────────────────────────────
    sha_status = ""
    if expected_sha256:
        from proliant.spp.catalog import _sha256_file
        actual = _sha256_file(Path(fpath))
        if actual == expected_sha256.lower():
            sha_status = "  [green]✓ SHA256 verified[/green]"
        else:
            console.print(
                f"[red]✗ SHA256 mismatch for {fname}[/red]\n"
                f"  expected: {expected_sha256}\n  got: {actual}"
            )
            sys.exit(1)

    # ── Load sidecar / embedded JSON ──────────────────────────────────────
    # Gen11: payload.json embedded in ZIP; Gen12+: sidecar {stem}.json
    payload: dict = {}
    try:
        with zf.open("payload.json") as f:
            payload = json.load(f)
    except Exception:
        pass

    if not payload:
        stem = os.path.splitext(fpath)[0]
        sidecar = Path(stem + ".json")
        if sidecar.exists():
            try:
                payload = json.loads(sidecar.read_text())
            except Exception:
                pass

    pkg = payload.get("Package") or payload.get("package") or {}
    updatable_by = payload.get("UpdatableBy", payload.get("updatableBy", []))

    description = _lang_en(_pkg_field(pkg, "Description", "description") or [])
    install_notes_html = _lang_en(_pkg_field(pkg, "InstallationNotes", "installation_notes") or [])
    install_section = _pkg_field(pkg, "Installation", "installation") or {}
    reboot_required = str(
        install_section.get("RebootRequired") or install_section.get("reboot_required", "")
    ).lower()
    upgrade_req = str(_pkg_field(pkg, "UpgradeRequirements", "upgrade_requirements") or "")

    rev_history = _pkg_field(pkg, "RevisionHistory", "revision_history") or []
    enhancements_html = rev_history[0].get("Enhancements", rev_history[0].get("enhancements", "")) if rev_history else ""
    bugfixes_html = rev_history[0].get("BugFixes", rev_history[0].get("bug_fixes", "")) if rev_history else ""

    pkg_files_meta = _pkg_field(pkg, "Files", "files") or [{}]
    file_list_from_json: list[str] = pkg_files_meta[0].get("FileList", pkg_files_meta[0].get("file_list", [])) if pkg_files_meta else []

    readme_text = ""
    for name in zf.namelist():
        if name.lower() in ("readme.txt", "readme.md"):
            try:
                readme_text = zf.read(name).decode(errors="replace")
            except Exception:
                pass
            break

    devices = payload.get("Devices", {}).get("Device", [])
    seen: set[tuple] = set()
    unique_devices: list[dict] = []
    for dev in devices:
        key = (dev.get("DeviceName", ""), dev.get("Version", ""))
        if key not in seen:
            seen.add(key)
            unique_devices.append(dev)

    # ── Header ────────────────────────────────────────────────────────────
    req_badge = {
        "recommended": "[yellow]Recommended[/yellow]",
        "critical":    "[red bold]Critical[/red bold]",
        "optional":    "[dim]Optional[/dim]",
    }.get(upgrade_req.lower(), "")
    reboot_badge = (
        "[yellow]Reboot required[/yellow]" if reboot_required == "yes"
        else "[green]No reboot[/green]" if reboot_required == "no"
        else ""
    )
    badges = "  ".join(b for b in [req_badge, reboot_badge] if b)

    console.print(f"\n[bold cyan]{fname}[/bold cyan]"
                  f"  [dim]{total_size / 1_048_576:.1f} MB[/dim]"
                  f"{sha_status}")
    if badges:
        console.print(f"  {badges}")
    if description:
        from rich.padding import Padding
        console.print(Padding(f"[dim]{description[:300]}{'…' if len(description) > 300 else ''}[/dim]", (1, 2, 0, 2)))

    # ── Files inside the package ──────────────────────────────────────────
    zip_entries = {info.filename: info.file_size for info in zf.infolist()}
    display_files = file_list_from_json if file_list_from_json else list(zip_entries.keys())

    ext_labels = {
        ".bin": "Firmware binary", ".hpb": "Firmware binary",
        ".flash": "Firmware binary", ".signed.flash": "Firmware binary",
        ".xml": "Component descriptor", ".json": "Install metadata",
        ".txt": "Readme / Notes", ".sig": "Signature",
        ".rpm": "RPM package", ".exe": "Windows installer",
    }

    files_table = Table(
        title="Files inside package",
        box=box.SIMPLE_HEAD,
        show_header=True,
        header_style="bold",
        padding=(0, 1),
    )
    files_table.add_column("Filename",  no_wrap=True)
    files_table.add_column("Size",      justify="right", no_wrap=True, style="dim")
    files_table.add_column("Type",      style="dim")

    for fn in display_files:
        sz = zip_entries.get(fn, 0)
        sz_str = f"{sz / 1_048_576:.1f} MB" if sz >= 1_048_576 else (f"{sz:,} B" if sz else "—")
        ext = ""
        fn_lower = fn.lower()
        for candidate in sorted(ext_labels, key=len, reverse=True):
            if fn_lower.endswith(candidate):
                ext = ext_labels[candidate]
                break
        files_table.add_row(fn, sz_str, ext)

    console.print()
    console.print(files_table)

    # ── Flash properties (from Devices) ──────────────────────────────────
    if unique_devices:
        def _yn(v: bool) -> str:
            return "[green]Yes[/green]" if v else "[dim]No[/dim]"

        flash_table = Table(
            title="Flash Properties",
            box=box.SIMPLE_HEAD,
            show_header=True,
            header_style="bold",
            padding=(0, 1),
        )
        flash_table.add_column("Target Device",  min_width=25)
        flash_table.add_column("Duration",       justify="right", no_wrap=True)
        flash_table.add_column("Reboot",         justify="center")
        flash_table.add_column("Direct Flash",   justify="center")
        flash_table.add_column("PLDM",           justify="center")
        flash_table.add_column("Update Via",     style="dim")

        for dev in unique_devices:
            imgs = dev.get("FirmwareImages", [{}])
            img = imgs[0] if imgs else {}
            dur = img.get("InstallDurationSec", 0)
            direct_flash = img.get("DirectFlashOK", img.get("DirectFlashOk", False))
            via = " + ".join({
                "Bmc": "iLO (BMC)", "Uefi": "UEFI", "RuntimeAgent": "OS Agent",
            }.get(m, m) for m in updatable_by) or "—"
            flash_table.add_row(
                dev.get("DeviceName", "—"),
                f"{dur}s" if dur else "—",
                _yn(img.get("ResetRequired", False)),
                _yn(direct_flash),
                _yn(img.get("PLDMImage", False)),
                via,
            )
        console.print(flash_table)

    # ── Release notes ─────────────────────────────────────────────────────
    if enhancements_html or bugfixes_html:
        from rich.rule import Rule
        console.print(Rule("[bold]Release Notes[/bold]", style="dim"))
        if enhancements_html:
            console.print("[bold]Enhancements[/bold]")
            console.print(_strip_html(enhancements_html))
        if bugfixes_html:
            console.print("\n[bold]Bug Fixes[/bold]")
            console.print(_strip_html(bugfixes_html))

    # ── Installation notes ────────────────────────────────────────────────
    if install_notes_html:
        from rich.rule import Rule
        console.print(Rule("[bold]Installation Notes[/bold]", style="dim"))
        console.print(_strip_html(install_notes_html))

    # ── Readme (embedded in ZIP) ──────────────────────────────────────────
    if readme_text and not install_notes_html:
        from rich.rule import Rule
        console.print(Rule("[bold]Readme[/bold]", style="dim"))
        lines = readme_text.splitlines()
        shown = '\n'.join(lines[:60])
        if len(lines) > 60:
            shown += f"\n[dim]… ({len(lines) - 60} more lines)[/dim]"
        console.print(shown)

    console.print()


def _cmd_download(args: argparse.Namespace) -> None:
    gen = _norm_gen(args.gen)
    version = args.version

    with console.status(f"Loading SPP {gen.upper()} {version}…"):
        try:
            components = fetch_catalog(gen, version)
        except Exception as exc:
            console.print(f"[red]Error fetching catalog: {exc}[/red]")
            sys.exit(1)

    if args.force:
        marker = _complete_marker(gen, version)
        if marker.exists():
            marker.unlink()

    _ensure_spp_packages(gen, version, components)
    dest_dir = _packages_dir(gen, version)
    console.print(f"\n[dim]Location: {dest_dir}[/dim]")


def _cmd_diff(args: argparse.Namespace) -> None:
    gen = _norm_gen(args.gen)
    types = [t.strip() for t in args.types.split(",")] if args.types else None
    model = args.model

    with console.status(f"Loading SPP {gen.upper()} {args.v1} and {args.v2}…"):
        try:
            old = fetch_catalog(gen, args.v1, force=args.force)
            new = fetch_catalog(gen, args.v2, force=args.force)
        except Exception as exc:
            console.print(f"[red]Error fetching catalog: {exc}[/red]")
            sys.exit(1)

    entries = diff_catalogs(old, new, types=types, model=model, gen=gen)

    if not args.show_all:
        entries = [e for e in entries if e.status != "unchanged"]

    if not entries:
        console.print("[green]No differences found.[/green]")
        return

    title_parts = [f"SPP {gen.upper()} diff: {args.v1} → {args.v2}"]
    if types:
        title_parts.append(f"type={','.join(types)}")
    if model:
        title_parts.append(f"model={model}")

    table = Table(
        title=" · ".join(title_parts),
        box=box.ROUNDED,
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("Status",   justify="center", min_width=9)
    table.add_column("Category",                   min_width=22)
    table.add_column("Name",                       min_width=30)
    table.add_column("File",     style="dim",      min_width=20, max_width=38, no_wrap=True)
    table.add_column(args.v1,    justify="right",  min_width=13, no_wrap=True)
    table.add_column(args.v2,    justify="right",  min_width=13, no_wrap=True)

    status_style = {
        "added":     "bold green",
        "removed":   "bold red",
        "changed":   "bold yellow",
        "unchanged": "dim",
    }

    order = {"changed": 0, "added": 1, "removed": 2, "unchanged": 3}
    for e in sorted(entries, key=lambda x: (order.get(x.status, 9), x.category, x.name)):
        sty = status_style.get(e.status, "")
        icon = {"added": "＋", "removed": "－", "changed": "⬆", "unchanged": " "}.get(e.status, "")
        table.add_row(
            f"[{sty}]{icon} {e.status}[/{sty}]",
            e.category,
            e.name,
            _trunc_filename(e.filename),
            e.new_version or "—",
        )

    console.print(table)

    counts = {s: sum(1 for e in entries if e.status == s) for s in ("added", "removed", "changed", "unchanged")}
    parts = []
    if counts["added"]:
        parts.append(f"[green]{counts['added']} added[/green]")
    if counts["removed"]:
        parts.append(f"[red]{counts['removed']} removed[/red]")
    if counts["changed"]:
        parts.append(f"[yellow]{counts['changed']} changed[/yellow]")
    if counts["unchanged"] and args.show_all:
        parts.append(f"[dim]{counts['unchanged']} unchanged[/dim]")
    console.print("\n" + "  ".join(parts))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _trunc_filename(name: str, n: int = 36) -> str:
    return name if len(name) <= n else name[:n - 3] + "..."


def _method_style(method: str) -> str:
    if "iLO" in method:
        return "green"
    if "UEFI" in method:
        return "yellow"
    if "Agent" in method:
        return "cyan"
    return "white"


def _resolve_fwpkg(name: str) -> str:
    """Resolve a .fwpkg filename to an absolute path.

    Search order:
    1. As-is (absolute or relative to cwd)
    2. Any cached SPP packages directory under spp/<gen>/<version>/packages/
    """
    from proliant.spp.catalog import _cache_dir
    p = Path(name)
    if p.exists():
        return str(p.resolve())

    cache = _cache_dir()
    matches = list(cache.glob(f"*/*/packages/{p.name}"))
    if len(matches) == 1:
        return str(matches[0])
    if len(matches) > 1:
        matches.sort(key=lambda x: x.stat().st_mtime, reverse=True)
        console.print(
            f"[yellow]Multiple cached copies found — using most recent: "
            f"{matches[0].parent.parent.name}/{matches[0].name}[/yellow]"
        )
        return str(matches[0])

    console.print(
        f"[red]File not found: {p.resolve()}[/red]\n"
        f"[dim]Tip: run [bold]proliant spp download gen12 <version>[/bold] "
        f"to download SPP packages first.[/dim]"
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    args_in = argv if argv is not None else sys.argv[1:]

    # If user passes: proliant spp inspect <file.fwpkg>  — route directly
    if (
        "_ARGCOMPLETE" not in os.environ
        and
        len(args_in) >= 2
        and args_in[0] == "inspect"
        and args_in[1].lower().endswith(".fwpkg")
    ):
        _cmd_inspect_fwpkg(_resolve_fwpkg(args_in[1]))
        return

    parser = _build_parser()
    # always_complete_options=False: don't suggest -h/--help (or any other
    # still-available flag) until the user actually types a leading "-".
    # argcomplete's default (True) mixes flags into every positional
    # completion unprompted, which is confusing noise on an empty NAME field.
    argcomplete.autocomplete(parser, always_complete_options=False)
    args = parser.parse_args(args_in)

    if not args.cmd:
        parser.print_help()
        sys.exit(0)

    if args.cmd == "list":
        _cmd_list(args)
    elif args.cmd == "inspect":
        _cmd_inspect(args)
    elif args.cmd == "part-number":
        _cmd_inspect_part_number(args)
    elif args.cmd == "download":
        _cmd_download(args)
    elif args.cmd == "diff":
        _cmd_diff(args)
    else:
        parser.print_help()
        sys.exit(2)
