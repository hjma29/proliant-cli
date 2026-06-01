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

_POWERSHELL_COMPLETION_BLOCK = """\
# pcli tab completion (added by pcli)
Register-ArgumentCompleter -Native -CommandName pcli -ScriptBlock {
    param($wordToComplete, $commandAst, $cursorPosition)
    $tmp = [System.IO.Path]::GetTempFileName()
    $env:_ARGCOMPLETE_STDOUT_FILENAME = $tmp
    $env:COMP_LINE = $commandAst.ToString()
    $env:COMP_POINT = $cursorPosition
    $env:_ARGCOMPLETE = "1"
    $env:_ARGCOMPLETE_SHELL = "powershell"
    pcli 2>&1 | Out-Null
    Get-Content $tmp | ForEach-Object {
        [System.Management.Automation.CompletionResult]::new($_, $_, "ParameterValue", $_)
    }
    Remove-Item $tmp -ErrorAction SilentlyContinue
    Remove-Item Env:_ARGCOMPLETE_STDOUT_FILENAME, Env:_ARGCOMPLETE, Env:_ARGCOMPLETE_SHELL -ErrorAction SilentlyContinue
}
"""


def _windows_first_run_check() -> None:
    """On Windows: if the exe dir isn't in PATH, offer a one-time quick setup."""
    if sys.platform != "win32":
        return
    if not getattr(sys, "frozen", False):
        return  # only for the packaged .exe

    exe_dir = os.path.dirname(sys.executable)
    path_dirs = [p.lower().rstrip("\\") for p in os.environ.get("PATH", "").split(os.pathsep)]
    if exe_dir.lower().rstrip("\\") in path_dirs:
        return  # already set up

    print("Quick setup: add pcli to your PATH for easier access? [Y/n] ", end="", flush=True)
    try:
        answer = input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return

    if answer not in ("", "y", "yes"):
        return

    _win_add_to_path(exe_dir)
    _win_add_powershell_completion()
    print("✓ Done! Opening a new terminal window...\n")

    # Launch PowerShell (7 preferred, fall back to 5, then cmd).
    # The new window loads $PROFILE so Tab completion works immediately.
    import subprocess
    import shutil
    shell = shutil.which("pwsh.exe") or shutil.which("powershell.exe") or "powershell.exe"
    subprocess.Popen(
        [shell, "-NoExit", "-Command",
         f'cd "{exe_dir}"; Write-Host "Setup complete! Type pcli to get started.`n"; pcli'],
        creationflags=subprocess.CREATE_NEW_CONSOLE,
    )


def _win_add_to_path(directory: str) -> None:
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_ALL_ACCESS
        )
        try:
            current, _ = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            current = ""
        if directory.lower() not in current.lower():
            new_val = current.rstrip(";") + ";" + directory if current else directory
            winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, new_val)
        winreg.CloseKey(key)
    except Exception:
        pass  # silently skip if registry write fails


def _win_add_powershell_completion() -> None:
    """Append argcomplete registration block to PowerShell profile(s)."""
    profiles = [
        os.path.expandvars(r"%USERPROFILE%\Documents\PowerShell\Microsoft.PowerShell_profile.ps1"),
        os.path.expandvars(r"%USERPROFILE%\Documents\WindowsPowerShell\Microsoft.PowerShell_profile.ps1"),
    ]
    for profile in profiles:
        try:
            profile_dir = os.path.dirname(profile)
            os.makedirs(profile_dir, exist_ok=True)
            existing = ""
            if os.path.exists(profile):
                with open(profile, encoding="utf-8") as f:
                    existing = f.read()
            if "Register-ArgumentCompleter" in existing and "pcli" in existing:
                continue  # already registered
            with open(profile, "a", encoding="utf-8") as f:
                f.write("\n" + _POWERSHELL_COMPLETION_BLOCK)
        except Exception:
            pass  # silently skip if profile write fails


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
    _windows_first_run_check()

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
