"""
pcli.cli — top-level dispatcher for the HPE ProLiant unified CLI.

Usage:
    pcli ilo <command>    HPE iLO direct Redfish management
    pcli com <command>    HPE GreenLake / Compute Ops Management (COM)
"""

from __future__ import annotations

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
  pcli ilo show fleet              Firmware summary across all iLO hosts
  pcli ilo upgrade --host myilo    Upgrade firmware via HPE SDR
  pcli com login                   Login to HPE GreenLake
  pcli com get devices             List GreenLake devices
"""


def main(argv: list[str] | None = None) -> None:
    args = argv if argv is not None else sys.argv[1:]

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
        try:
            from pcli.ilo.cli import main as ilo_main
        except ImportError as exc:
            print(
                f"pcli ilo: missing dependencies — install with: pip install pcli[ilo]\n({exc})",
                file=sys.stderr,
            )
            sys.exit(1)
        sys.argv = ["pcli ilo"] + list(args[1:])
        ilo_main()

    elif namespace == "com":
        try:
            from pcli.com.cli import main as com_main
        except ImportError as exc:
            print(
                f"pcli com: missing dependencies — install with: pip install pcli[com]\n({exc})",
                file=sys.stderr,
            )
            sys.exit(1)
        sys.argv = ["pcli com"] + list(args[1:])
        com_main()

    else:
        print(f"pcli: unknown namespace '{namespace}'\n", file=sys.stderr)
        print(_USAGE)
        sys.exit(2)
