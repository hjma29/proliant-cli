"""
pcli.common.targets
~~~~~~~~~~~~~~~~~~~
Unified host target resolution for automation pipelines.

Supports multiple input methods:
  - Single host name:         --host myserver
  - Comma-separated:          --host server1,server2,server3
  - File (one name per line): --hosts-from targets.txt
  - Stdin:                    --hosts-from -  (or piped input)

This enables Unix-style chaining::

    pcli ilo firmware list --json | jq -r '.[] | select(.BIOS < "2.90") | .Server' \\
      | xargs -n1 pcli ilo firmware upgrade
"""

from __future__ import annotations

import sys
from typing import Callable


def resolve_hosts(
    host_arg: str | None,
    hosts_from: str | None,
    loader: Callable[[str | None], list[dict]],
) -> list[dict]:
    """Resolve target hosts from various input methods.

    Args:
        host_arg: Value of --host flag (single name or comma-separated)
        hosts_from: Value of --hosts-from flag (filename or "-" for stdin)
        loader: Function to load host config by name (e.g., config.load_hosts)

    Returns:
        List of host dicts matching the target specification

    Raises:
        SystemExit: On invalid input or no matches
    """
    # --hosts-from takes priority
    if hosts_from:
        names = _read_names_from(hosts_from)
        if not names:
            print("ERROR: No host names received from input", file=sys.stderr)
            sys.exit(1)
        return _resolve_names(names, loader)

    # --host with comma-separated names
    if host_arg and "," in host_arg:
        names = [n.strip() for n in host_arg.split(",") if n.strip()]
        return _resolve_names(names, loader)

    # Default: single host or all hosts
    return loader(host_arg)


def _read_names_from(source: str) -> list[str]:
    """Read host names from a file or stdin."""
    if source == "-":
        # Read from stdin (piped input)
        if sys.stdin.isatty():
            print("ERROR: --hosts-from - requires piped input", file=sys.stderr)
            sys.exit(1)
        lines = sys.stdin.read().splitlines()
    else:
        try:
            with open(source) as f:
                lines = f.read().splitlines()
        except OSError as exc:
            print(f"ERROR: Cannot read hosts file: {exc}", file=sys.stderr)
            sys.exit(1)

    # Strip whitespace, skip empty lines and comments
    return [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]


def _resolve_names(names: list[str], loader: Callable[[str | None], list[dict]]) -> list[dict]:
    """Resolve a list of host names to host dicts."""
    hosts = []
    all_hosts = loader(None)  # load all
    all_by_name = {h["name"].lower(): h for h in all_hosts}

    missing = []
    for name in names:
        host = all_by_name.get(name.lower())
        if host:
            hosts.append(host)
        else:
            missing.append(name)

    if missing:
        print(f"WARNING: Unknown hosts (skipped): {', '.join(missing)}", file=sys.stderr)

    if not hosts:
        print("ERROR: No valid hosts resolved from input", file=sys.stderr)
        sys.exit(1)

    return hosts


def add_target_args(parser) -> None:
    """Add --host and --hosts-from arguments to an argparse parser.

    Use this in place of manually adding --host to each subparser::

        add_target_args(my_parser)
    """
    group = parser.add_argument_group("target selection")
    group.add_argument(
        "--host", metavar="NAME[,NAME,...]",
        help="Target host(s) by name — comma-separated for multiple",
    )
    group.add_argument(
        "--hosts-from", metavar="FILE",
        help="Read target host names from FILE (one per line), or '-' for stdin",
    )
