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
  spp          HPE Service Pack for ProLiant catalog analysis
  oneview      HPE OneView (Synergy & ProLiant fleet management)
  qs           HPE QuickSpecs browser (list revisions, read specs)
  config       View and manage pcli configuration

commands:
  update       Download and install the latest pcli release

Run 'pcli <namespace> --help' for namespace-specific help.

examples:
  pcli ilo list firmwares                       Firmware summary across all iLO hosts
  pcli ilo upgrade --host myilo                Upgrade firmware via HPE SDR
  pcli com login                               Login to HPE GreenLake
  pcli com list devices                         List GreenLake devices
  pcli spp list                                List available gen12 SPP versions
  pcli spp inspect gen12 2026.03.00.00         Analyse a gen12 SPP catalog
  pcli spp diff gen12 2025.09.01.00 2026.03.00.00  What changed between SPPs?
  pcli oneview list servers                    List all OneView-managed servers
  pcli oneview list firmware                   Fleet firmware inventory via OneView
  pcli qs list --model dl380gen12              List QuickSpec revisions for DL380 Gen12
  pcli qs describe a00073551enw               Read the DL380 Gen12 QuickSpec
  pcli config list inventory                   Show iLO hosts and OneView in hosts-ilo.ini
  pcli update                                  Upgrade pcli to the latest release
"""

_POWERSHELL_COMPLETION_BLOCK = """\
# pcli tab completion (added by pcli)
Register-ArgumentCompleter -Native -CommandName pcli -ScriptBlock {
    param($wordToComplete, $commandAst, $cursorPosition)
    $t = @($commandAst.CommandElements | ForEach-Object { $_.ToString() })
    $pos = if ($wordToComplete -eq '') { $t.Count } else { $t.Count - 1 }
    $candidates = @()
    if ($pos -eq 1) {
        $candidates = @('ilo', 'com', 'spp', 'oneview', 'qs', 'config', 'update')
    } elseif ($pos -eq 2) {
        if ($t[1] -eq 'ilo') { $candidates = @('list', 'upgrade', 'init', 'report', 'set', 'describe') }
        elseif ($t[1] -eq 'com') { $candidates = @('login', 'logout', 'list', 'use', 'add', 'report') }
        elseif ($t[1] -eq 'spp') { $candidates = @('list', 'inspect', 'diff') }
        elseif ($t[1] -eq 'oneview') { $candidates = @('list', 'describe', 'report') }
        elseif ($t[1] -eq 'qs') { $candidates = @('list', 'describe', 'diff') }
        elseif ($t[1] -eq 'config') { $candidates = @('list') }
    } elseif ($pos -eq 3) {
        if ($t[1] -eq 'ilo') {
            if ($t[2] -eq 'list') { $candidates = @('firmwares','ilo','network','nic','storage','cpu','memory','com','full','disk-map','serial','update-method') }
            elseif ($t[2] -eq 'upgrade') { $candidates = @('components','queue','stage','flash','clear') }
            elseif ($t[2] -eq 'report') { $candidates = @('memory') }
            elseif ($t[2] -eq 'set') { $candidates = @('dhcp') }
            if ($t[2] -eq 'list') { $candidates = @('devices','workspaces','bundles') }
            elseif ($t[2] -eq 'use') { $candidates = @('workspace') }
            elseif ($t[2] -eq 'add') { $candidates = @('device') }
            elseif ($t[2] -eq 'report') { $candidates = @('memory') }
        } elseif ($t[1] -eq 'oneview') {
            if ($t[2] -eq 'list') { $candidates = @('servers','firmware','networks','networksets','uplinksets','server-profiles') }
            elseif ($t[2] -eq 'describe') { $candidates = @('uplinkset','networkset','server-profile') }
            elseif ($t[2] -eq 'report') { $candidates = @('memory') }
        } elseif ($t[1] -eq 'config') {
            if ($t[2] -eq 'list') { $candidates = @('inventory') }
        }
    }
    $candidates | Where-Object { $_ -like "$wordToComplete*" } | ForEach-Object {
        [System.Management.Automation.CompletionResult]::new($_, $_, 'ParameterValue', $_)
    }
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

    # Open PowerShell with the exe dir in PATH so pcli works immediately.
    # Set PATH inline (registry change not visible until new login session).
    # Use -NoExit so the window stays open for the user to work in.
    import subprocess
    import shutil
    shell = shutil.which("pwsh.exe") or shutil.which("powershell.exe") or "powershell.exe"
    ps_cmd = (
        f'$env:PATH = "{exe_dir}" + [IO.Path]::PathSeparator + $env:PATH; '
        f'Set-Location "{exe_dir}"; '
        f'Write-Host "pcli is ready. Type pcli to get started." -ForegroundColor Green'
    )
    subprocess.Popen(
        [shell, "-NoExit", "-NoLogo", "-Command", ps_cmd],
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
    """Append completion block to the actual PowerShell profile(s), resolving OneDrive redirection."""
    import subprocess
    import re

    # Resolve the real $PROFILE path by asking PowerShell — handles OneDrive folder redirection
    def _ps_profile(exe: str) -> str | None:
        try:
            result = subprocess.run(
                [exe, "-NoProfile", "-NonInteractive", "-Command", "$PROFILE"],
                capture_output=True, text=True, timeout=10,
            )
            p = result.stdout.strip()
            return p if p else None
        except Exception:
            return None

    import shutil
    profiles: list[str] = []
    for exe in ("pwsh.exe", "powershell.exe"):
        if shutil.which(exe):
            p = _ps_profile(exe)
            if p and p not in profiles:
                profiles.append(p)

    # Fallback to hardcoded paths if PowerShell query failed
    if not profiles:
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
            # Replace outdated argcomplete-style block or append if missing
            if "pcli" in existing and "Register-ArgumentCompleter" in existing:
                if _POWERSHELL_COMPLETION_BLOCK.strip() in existing:
                    continue  # already up-to-date
                # Strip old block and rewrite (matches both old and new block formats)
                existing = re.sub(
                    r"\n# pcli tab completion \(added by pcli\)\n.*?(?=\n[^#\n]|\Z)",
                    "",
                    existing,
                    flags=re.DOTALL,
                )
            with open(profile, "w", encoding="utf-8") as f:
                f.write(existing.rstrip() + "\n" + _POWERSHELL_COMPLETION_BLOCK)
        except Exception:
            pass  # silently skip if profile write fails



_GITHUB_REPO = "hjma29/proliant-cli"


def _get_current_version() -> str:
    try:
        return _pkg_version("pcli")
    except PackageNotFoundError:
        return "dev"


def _run_update() -> None:
    """Download and replace the current pcli binary with the latest GitHub release."""
    import urllib.request
    import json
    import tempfile
    import zipfile
    import shutil
    import subprocess

    print("Checking for updates...")
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        # Auto-read from gh CLI if available (works for private repos without manual setup)
        try:
            import subprocess
            result = subprocess.run(
                ["gh", "auth", "token"], capture_output=True, text=True, timeout=5
            )
            token = result.stdout.strip()
        except Exception:
            pass
    headers = {"User-Agent": "pcli-updater"}
    if token:
        headers["Authorization"] = f"token {token}"
    try:
        url = f"https://api.github.com/repos/{_GITHUB_REPO}/releases/latest"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            release = json.loads(resp.read())
    except Exception as e:
        print(f"ERROR: Could not reach GitHub: {e}", file=sys.stderr)
        if not token:
            print("  Tip: install gh CLI (github.com/cli/gh) and run 'gh auth login'.", file=sys.stderr)
            print("       Or set GITHUB_TOKEN env var.", file=sys.stderr)
        sys.exit(1)

    latest_tag = release.get("tag_name", "")
    latest_ver = latest_tag.lstrip("v")
    current_ver = _get_current_version()

    print(f"  Current version : {current_ver}")
    print(f"  Latest version  : {latest_ver}")

    if current_ver != "dev" and current_ver == latest_ver:
        print("✓ Already up to date.")
        sys.exit(0)

    # Determine asset name for this platform
    if sys.platform == "win32":
        asset_name = "proliant-cli-windows.zip"
    elif sys.platform == "darwin":
        asset_name = "proliant-cli-macos"
    else:
        asset_name = "proliant-cli-linux"

    asset = next(
        (a for a in release.get("assets", []) if a["name"] == asset_name),
        None,
    )
    if not asset:
        print(f"ERROR: No asset '{asset_name}' found in release {latest_tag}.", file=sys.stderr)
        sys.exit(1)

    print(f"  Downloading {asset_name}...")
    # Use the API assets endpoint with Accept: application/octet-stream for private repos
    asset_api_url = asset["url"]
    dl_headers = {**headers, "Accept": "application/octet-stream"}
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = os.path.join(tmpdir, asset_name)
        try:
            req = urllib.request.Request(asset_api_url, headers=dl_headers)
            with urllib.request.urlopen(req, timeout=120) as resp, open(tmp_path, "wb") as f:
                shutil.copyfileobj(resp, f)
        except Exception as e:
            print(f"ERROR: Download failed: {e}", file=sys.stderr)
            sys.exit(1)

        if sys.platform == "win32":
            # Extract pcli.exe from the zip
            with zipfile.ZipFile(tmp_path) as zf:
                names = zf.namelist()
                exe_name = next((n for n in names if n.lower() == "pcli.exe"), names[0])
                zf.extract(exe_name, tmpdir)
            new_exe = os.path.join(tmpdir, exe_name)
            current_exe = sys.executable
            # Can't overwrite running exe on Windows — use a helper script
            bat = os.path.join(tmpdir, "pcli_update.bat")
            with open(bat, "w") as f:
                f.write(
                    f'@echo off\n'
                    f'ping -n 3 127.0.0.1 >nul\n'  # wait ~2 seconds
                    f'copy /y "{new_exe}" "{current_exe}"\n'
                    f'echo pcli updated to {latest_ver}\n'
                )
            # Copy the bat out of the temp dir so it survives after tmpdir is deleted
            persistent_bat = os.path.join(os.environ.get("TEMP", "C:\\Temp"), "pcli_update.bat")
            shutil.copy(bat, persistent_bat)
            subprocess.Popen(
                ["cmd.exe", "/c", persistent_bat],
                creationflags=subprocess.CREATE_NEW_CONSOLE | subprocess.DETACHED_PROCESS,
                close_fds=True,
            )
            print(f"✓ Update to {latest_ver} in progress — a new window will confirm when done.")
        else:
            # Unix: can replace running binary in-place
            os.chmod(tmp_path, 0o755)
            current_exe = sys.executable
            # Atomic replace
            os.replace(tmp_path, current_exe)
            # Prevent tmpdir cleanup from failing on missing file
            open(tmp_path, "w").close()
            print(f"✓ Updated to {latest_ver}. Run 'pcli --version' to confirm.")


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


def _dispatch_spp(args: list[str]) -> None:
    try:
        from pcli.spp.cli import main as spp_main
    except ImportError as exc:
        print(
            f"pcli spp: missing dependencies — install with: pip install pcli[spp]\n({exc})",
            file=sys.stderr,
        )
        sys.exit(1)
    sys.argv = ["pcli spp"] + args
    spp_main()


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


def _dispatch_qs(args: list[str]) -> None:
    try:
        from pcli.qs.cli import main as qs_main
    except ImportError as exc:
        print(
            f"pcli qs: missing dependencies — install with: pip install pcli[qs]\n({exc})",
            file=sys.stderr,
        )
        sys.exit(1)
    sys.argv = ["pcli qs"] + args
    qs_main()


def _dispatch_oneview(args: list[str]) -> None:
    try:
        from pcli.oneview.cli import main as oneview_main
    except ImportError as exc:
        print(
            f"pcli oneview: missing dependencies — install with: pip install pcli\n({exc})",
            file=sys.stderr,
        )
        sys.exit(1)
    sys.argv = ["pcli oneview"] + args
    oneview_main()


def _dispatch_config(args: list[str]) -> None:
    from pcli.config.cli import main as config_main
    sys.argv = ["pcli config"] + args
    config_main()


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
        if len(parts) >= 2 and parts[1] == "spp":
            os.environ["_ARGCOMPLETE"] = "2"
            _dispatch_spp(parts[2:])
            return

        if len(parts) >= 2 and parts[1] == "oneview":
            os.environ["_ARGCOMPLETE"] = "2"
            _dispatch_oneview(parts[2:])
            return

        if len(parts) >= 2 and parts[1] == "qs":
            os.environ["_ARGCOMPLETE"] = "2"
            _dispatch_qs(parts[2:])
            return

        # Top-level: use argparse so argcomplete can offer 'ilo', 'com', 'spp', 'oneview'
        import argparse
        import argcomplete
        parser = argparse.ArgumentParser(prog="pcli", add_help=False)
        parser.add_argument("-V", "--version", action="store_true")
        sub = parser.add_subparsers(dest="namespace")
        sub.add_parser("ilo",     help="Direct iLO Redfish management")
        sub.add_parser("com",     help="HPE GreenLake / Compute Ops Management")
        sub.add_parser("spp",     help="HPE Service Pack for ProLiant analysis")
        sub.add_parser("oneview", help="HPE OneView fleet management")
        sub.add_parser("qs",      help="HPE QuickSpecs browser")
        sub.add_parser("update",  help="Upgrade pcli to the latest release")
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
    elif namespace == "spp":
        _dispatch_spp(list(args[1:]))
    elif namespace == "oneview":
        _dispatch_oneview(list(args[1:]))
    elif namespace == "qs":
        _dispatch_qs(list(args[1:]))
    elif namespace == "config":
        _dispatch_config(list(args[1:]))
    elif namespace == "update":
        _run_update()
    else:
        print(f"pcli: unknown namespace '{namespace}'\n", file=sys.stderr)
        print(_USAGE)
        sys.exit(2)


if __name__ == "__main__":
    main()
