"""
pcli.cli — top-level dispatcher for the HPE ProLiant unified CLI.

Usage:
    pcli ilo <command>    HPE iLO direct Redfish management
    pcli com <command>    HPE GreenLake / Compute Ops Management (COM)
"""

# PYTHON_ARGCOMPLETE_OK
from __future__ import annotations

import os
import sys
from importlib.metadata import version as _pkg_version, PackageNotFoundError


_USAGE = """\
usage: pcli [-h] [-V] NAMESPACE ...

HPE ProLiant unified CLI

namespaces:
  ilo          Direct iLO Redfish management (firmware, inventory, power)
  com          HPE GreenLake / Compute Ops Management (devices, workspaces)

Run 'pcli <namespace> --help' for namespace-specific help.

examples:
  pcli ilo get firmwares           Firmware summary across all iLO hosts
  pcli ilo upgrade --host myilo    Upgrade firmware via HPE SDR
  pcli com login                   Login to HPE GreenLake
  pcli com get devices             List GreenLake devices
  pcli com get devices --fields name,serial,added,added-by --sort added
"""


def _dispatch_ilo(args: list[str]) -> None:
    try:
        from pcli.ilo.cli import main as ilo_main
    except ImportError as exc:
        print(
            f"pcli ilo: missing dependencies — install with: pip install pcli[ilo]\n({exc})",
            file=sys.stderr,
        )
        sys.exit(1)
    sys.argv = ["pcli ilo"] + args
    ilo_main()


def _dispatch_com(args: list[str]) -> None:
    try:
        from pcli.com.cli import main as com_main
    except ImportError as exc:
        print(
            f"pcli com: missing dependencies — install with: pip install pcli[com]\n({exc})",
            file=sys.stderr,
        )
        sys.exit(1)
    sys.argv = ["pcli com"] + args
    com_main()


def main(argv: list[str] | None = None) -> None:
    args = argv if argv is not None else sys.argv[1:]

    # ── argcomplete support ─────────────────────────────────────────────────
    # When tab-completing, delegate to the sub-CLI's own argcomplete handler
    # as soon as we know the namespace. For top-level completion (just 'pcli<TAB>')
    # fall through to the argparse-based completion below.
    if "_ARGCOMPLETE" in os.environ:
        comp_line = os.environ.get("COMP_LINE", "")
        parts = comp_line.split()
        # parts[0] = "pcli", parts[1] = namespace (if typed)
        if len(parts) >= 2 and parts[1] == "ilo":
            # Tell argcomplete to skip 2 words (pcli + ilo) instead of 1
            os.environ["_ARGCOMPLETE"] = "2"
            _dispatch_ilo(parts[2:])
            return
        if len(parts) >= 2 and parts[1] == "com":
            os.environ["_ARGCOMPLETE"] = "2"
            _dispatch_com(parts[2:])
            return

        # Top-level: use argparse so argcomplete can offer 'ilo' and 'com'
        import argparse
        import argcomplete
        parser = argparse.ArgumentParser(prog="pcli", add_help=False)
        parser.add_argument("-V", "--version", action="store_true")
        sub = parser.add_subparsers(dest="namespace")
        sub.add_parser("ilo",  help="Direct iLO Redfish management")
        sub.add_parser("com",  help="HPE GreenLake / Compute Ops Management")
        argcomplete.autocomplete(parser)
        return  # autocomplete() exits; reaching here means no completion needed

    # ── normal execution ────────────────────────────────────────────────────
    if not args or args[0] in ("-h", "--help"):
        print(_USAGE)
        sys.exit(0)

    if args[0] in ("-V", "--version"):
        try:
            v = _pkg_version("pcli")
        except PackageNotFoundError:
            v = "dev"
        print(f"pcli {v}")
        sys.exit(0)

    namespace = args[0]

    if namespace == "ilo":
        _dispatch_ilo(list(args[1:]))
    elif namespace == "com":
        _dispatch_com(list(args[1:]))
    else:
        print(f"pcli: unknown namespace '{namespace}'\n", file=sys.stderr)
        print(_USAGE)
        sys.exit(2)


if __name__ == "__main__":
    main()
