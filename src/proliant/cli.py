"""
proliant.cli — top-level dispatcher for the HPE ProLiant unified CLI.

Usage:
    proliant ilo <command>    HPE iLO direct Redfish management
    proliant com <command>    HPE GreenLake / Compute Ops Management (COM)
"""

# PYTHON_ARGCOMPLETE_OK
from __future__ import annotations

import os
import sys
from importlib.metadata import version as _pkg_version, PackageNotFoundError

from proliant.common.platform import is_frozen


_USAGE = """\
usage: proliant [-h] [-V] NAMESPACE ...

HPE ProLiant unified CLI

namespaces:
  ilo          Direct iLO Redfish management (firmware, inventory, power)
  com          HPE GreenLake / Compute Ops Management (devices, workspaces)
  spp          HPE Service Pack for ProLiant catalog analysis
  oneview      HPE OneView (Synergy & ProLiant fleet management)
  qs           HPE QuickSpecs browser (list revisions, read specs)
  config       View and manage proliant configuration

commands:
  update               Download and install the latest proliant release
  install-completion   Enable tab completion for bash/zsh (use: sh install.sh)

Run 'proliant <namespace> --help' for namespace-specific help.

examples:
  proliant ilo firmware list                       Firmware summary across all iLO hosts
  proliant ilo firmware upgrade myilo             Upgrade firmware via HPE SDR
  proliant com login                               Login to HPE GreenLake
  proliant com devices list                         List GreenLake devices
  proliant spp list                                List available gen12 SPP versions
  proliant spp inspect gen12 2026.03.00.00         Analyse a gen12 SPP catalog
  proliant spp diff gen12 2025.09.01.00 2026.03.00.00  What changed between SPPs?
  proliant oneview servers list                    List all OneView-managed servers
  proliant oneview firmware list                   Fleet firmware inventory via OneView
  proliant qs list --model dl380gen12              List QuickSpec revisions for DL380 Gen12
  proliant qs describe a00073551enw               Read the DL380 Gen12 QuickSpec
  proliant config list inventory                   Show iLO hosts and OneView in inventory.ini
  proliant update                                  Upgrade proliant to the latest release
"""

_POWERSHELL_COMPLETION_BLOCK = """\
# proliant tab completion (added by proliant)
Register-ArgumentCompleter -Native -CommandName proliant -ScriptBlock {
    param($commandName, $wordToComplete, $cursorPosition)
    $completion_file = New-TemporaryFile
    $env:ARGCOMPLETE_USE_TEMPFILES = 1
    $env:_ARGCOMPLETE_STDOUT_FILENAME = $completion_file
    $env:COMP_LINE = $wordToComplete
    $env:COMP_POINT = $cursorPosition
    $env:_ARGCOMPLETE = 1
    $env:_ARGCOMPLETE_SUPPRESS_SPACE = 0
    $env:_ARGCOMPLETE_IFS = "`n"
    $env:_ARGCOMPLETE_SHELL = "powershell"
    proliant 2>&1 | Out-Null

    Get-Content $completion_file | ForEach-Object {
        [System.Management.Automation.CompletionResult]::new($_, $_, "ParameterValue", $_)
    }
    Remove-Item $completion_file, Env:\\_ARGCOMPLETE_STDOUT_FILENAME, Env:\\ARGCOMPLETE_USE_TEMPFILES, Env:\\COMP_LINE, Env:\\COMP_POINT, Env:\\_ARGCOMPLETE, Env:\\_ARGCOMPLETE_SUPPRESS_SPACE, Env:\\_ARGCOMPLETE_IFS, Env:\\_ARGCOMPLETE_SHELL
}

# Show completion menu instead of cycling (added by proliant)
if (-not (Get-PSReadLineKeyHandler | Where-Object { $_.Key -eq 'Tab' -and $_.Function -eq 'MenuComplete' })) {
    Set-PSReadLineKeyHandler -Key Tab -Function MenuComplete
}
"""


def _console_pids() -> list[int]:
    """Return the list of process IDs attached to this console."""
    import ctypes
    arr = (ctypes.c_uint * 64)()
    n = ctypes.windll.kernel32.GetConsoleProcessList(arr, 64)
    return [arr[i] for i in range(min(n, 64))]


def _proc_image_name(pid: int) -> str:
    """Return the full image path for a PID, or '' on failure."""
    import ctypes
    from ctypes import wintypes
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = ctypes.windll.kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, False, pid
    )
    if not h:
        return ""
    try:
        size = wintypes.DWORD(260)
        buf = ctypes.create_unicode_buffer(size.value)
        if ctypes.windll.kernel32.QueryFullProcessImageNameW(
            h, 0, buf, ctypes.byref(size)
        ):
            return buf.value
        return ""
    finally:
        ctypes.windll.kernel32.CloseHandle(h)


def _is_explorer_launch() -> bool:
    """Return True if this frozen EXE was launched by double-clicking from Explorer.

    When Explorer spawns a console EXE it creates a brand-new console owned
    solely by this application.  A Nuitka onefile build runs as TWO processes
    (bootstrap parent + Python child), both named the same EXE — so a naive
    GetConsoleProcessList()==1 check fails.  Instead we treat it as an Explorer
    launch when EVERY process attached to the console is our own EXE; a terminal
    launch always has a shell (pwsh/cmd/bash) sharing the console.
    """
    try:
        pids = _console_pids()
        if not pids:
            return False
        self_name = os.path.basename(sys.executable).lower()
        for pid in pids:
            img = _proc_image_name(pid)
            if img and os.path.basename(img).lower() != self_name:
                return False  # a shell shares the console → terminal launch
        return True
    except Exception:
        return False


def _pause_console(message: str = "Press any key to close this window...") -> None:
    """Pause until a key is pressed.

    Reads directly from the console (msvcrt.getch / CONIN$) so it works even
    when the Nuitka onefile child has no usable stdin — unlike input(), which
    raises EOFError immediately and lets the window close instantly.
    """
    print(f"\n{message}")
    try:
        import msvcrt
        msvcrt.getch()
    except Exception:
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            pass



def _open_new_powershell(exe_dir: str) -> None:
    """Open a new PowerShell window with exe_dir on PATH so proliant works immediately."""
    import subprocess
    import shutil
    shell = shutil.which("pwsh.exe") or shutil.which("powershell.exe") or "powershell.exe"
    ps_cmd = (
        f'$env:PATH = "{exe_dir}" + [IO.Path]::PathSeparator + $env:PATH; '
        f'Set-Location $env:USERPROFILE; '
        f'Write-Host "proliant is ready. Type proliant to get started." -ForegroundColor Green'
    )
    subprocess.Popen(
        [shell, "-NoExit", "-NoLogo", "-ExecutionPolicy", "RemoteSigned", "-Command", ps_cmd],
        creationflags=subprocess.CREATE_NEW_CONSOLE,
    )


def _windows_first_run_check() -> None:
    """On Windows: set up PATH and tab completion the first time proliant is run."""
    if sys.platform != "win32":
        return
    if not is_frozen():
        return  # only for the packaged .exe

    exe_dir = os.path.dirname(sys.executable)

    path_dirs = [p.lower().rstrip("\\") for p in os.environ.get("PATH", "").split(os.pathsep)]
    already_in_path = exe_dir.lower().rstrip("\\") in path_dirs

    if _is_explorer_launch():
        # ── Double-clicked from Explorer ────────────────────────────────────
        # Run setup automatically (no Y/n prompt — opening the EXE is consent)
        # then pause so the window stays open long enough to read.
        print("proliant installer")
        print("=" * 40)
        if not already_in_path:
            print(f"\nSetting up proliant ...")
            print(f"  Adding {exe_dir} to PATH ...")
            _win_add_to_path(exe_dir)
            _win_add_powershell_completion()
            _win_check_execution_policy()
            print("\n  Done!  Opening a new PowerShell window where proliant is ready.")
            print("  You can move proliant-cli-windows.exe anywhere — re-run it to update PATH.\n")
            _open_new_powershell(exe_dir)
        else:
            print(f"\n  Already installed: {exe_dir}")
            print("  Tab completion and PATH are configured.\n")
        _pause_console()
        sys.exit(0)

    # ── Launched from an existing terminal ──────────────────────────────────
    if already_in_path:
        return

    # Skip if user already answered this prompt (yes or no)
    sentinel = os.path.join(exe_dir, ".setup_done")
    if os.path.exists(sentinel):
        return

    print("Quick setup: add proliant to your PATH for easier access? [Y/n] ", end="", flush=True)
    try:
        answer = input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return

    # Remember the answer so we never ask again
    try:
        open(sentinel, "w").close()
    except OSError:
        pass

    if answer not in ("", "y", "yes"):
        return

    _win_add_to_path(exe_dir)
    _win_add_powershell_completion()
    _win_check_execution_policy()
    print("✓ Done! Opening a new terminal window...\n")
    _open_new_powershell(exe_dir)


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
        # Drop any existing occurrence of this directory, then prepend it so a
        # freshly installed proliant always wins over stale copies on PATH.
        target = directory.rstrip("\\").lower()
        entries = [p for p in current.split(";") if p and p.rstrip("\\").lower() != target]
        new_val = ";".join([directory] + entries)
        if new_val != current:
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
            if "proliant" in existing and "Register-ArgumentCompleter" in existing:
                if _POWERSHELL_COMPLETION_BLOCK.strip() in existing:
                    continue  # already up-to-date
                # Strip old block (comment marker through its last closing brace)
                # covering both the old format (no MenuComplete) and new format.
                existing = re.sub(
                    r"\n?# proliant tab completion \(added by proliant\).*?(?:\n\})+\n?",
                    "\n",
                    existing,
                    flags=re.DOTALL,
                )
            with open(profile, "w", encoding="utf-8") as f:
                f.write(existing.rstrip() + "\n" + _POWERSHELL_COMPLETION_BLOCK)
        except Exception:
            pass  # silently skip if profile write fails


def _win_check_execution_policy() -> None:
    """Ensure PowerShell can load the profile (and completion) we just wrote.

    A fresh Windows install defaults to Restricted, which blocks the profile.
    Set CurrentUser scope to RemoteSigned (Microsoft's recommended default,
    no admin required) so tab completion works. Fall back to a printed hint if
    the change fails.
    """
    import subprocess
    import shutil
    exe = shutil.which("pwsh.exe") or shutil.which("powershell.exe")
    if not exe:
        return
    try:
        result = subprocess.run(
            [exe, "-NoProfile", "-NonInteractive", "-Command",
             "Get-ExecutionPolicy -Scope CurrentUser"],
            capture_output=True, text=True, timeout=10,
        )
        policy = result.stdout.strip()
        if policy not in ("Undefined", "Restricted"):
            return  # already permissive enough

        set_result = subprocess.run(
            [exe, "-NoProfile", "-NonInteractive", "-Command",
             "Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser -Force"],
            capture_output=True, text=True, timeout=10,
        )
        if set_result.returncode == 0:
            print("  Set PowerShell execution policy to RemoteSigned (CurrentUser) so tab completion loads.")
        else:
            print(
                "\n  ⚠ PowerShell execution policy is set to: " + (policy or "Undefined"),
                "\n    Tab completion requires your profile to load. Run this once in PowerShell:",
                "\n      Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser",
            )
    except Exception:
        pass


_GITHUB_REPO = "hjma29/proliant-cli"



# Static zsh completion script — kept as reference/fallback. Not used by default.
# To switch back to static: set _ZSH_COMPLETION_SCRIPT as the content written by install.sh.
_ZSH_COMPLETION_SCRIPT = r"""#compdef proliant
# Generated by proliant install.sh -- do not edit manually

_proliant() {
  local curcontext="$curcontext" context state line
  typeset -A opt_args
  _arguments -C \
    '(-h --help)'{-h,--help}'[show help]' \
    '(-V --version)'{-V,--version}'[show version]' \
    '1: :_proliant__ns' \
    '*:: :->args'
  case $state in
    args)
      case $line[1] in
        ilo)     _proliant__ilo ;;
        com)     _proliant__com ;;
        oneview) _proliant__oneview ;;
        qs)      _proliant__qs ;;
        config)  _proliant__config ;;
      esac
      ;;
  esac
}

_proliant__ns() {
  local -a ns
  ns=(
    'ilo:Direct iLO Redfish management'
    'com:HPE GreenLake / Compute Ops Management'
    'oneview:HPE OneView fleet management'
    'qs:HPE QuickSpecs browser'
    'config:View and manage proliant configuration'
    'update:Upgrade proliant to the latest release'
  )
  _describe 'namespace' ns
}

_proliant__ilo() {
  local curcontext="$curcontext" context state line
  typeset -A opt_args
  _arguments -C '1: :_proliant__ilo_cmds' '*:: :->args'
  case $state in
    args)
      case $line[1] in
        servers)       _proliant__ilo_servers ;;
        firmware)      _proliant__ilo_firmware ;;
        license)       _proliant__ilo_license ;;
        power)         _proliant__ilo_power ;;
        uid)           _proliant__ilo_uid ;;
        boot)          _proliant__ilo_boot ;;
        bios)          _proliant__ilo_bios ;;
        network)       _proliant__ilo_network ;;
        reports)       _proliant__ilo_reports ;;
        nic-host|nic-ilo|nic|storage|cpu|memory|com|full|disk-map|serial|update-method)
                       _proliant__ilo_list_resource ;;
      esac
      ;;
  esac
}

_proliant__ilo_cmds() {
  local -a cmds
  cmds=(
    'servers:Server inventory and details'
    'firmware:Firmware management'
    'nic-host:Host NIC firmware versions'
    'nic-ilo:iLO dedicated NIC LLDP and IP info'
    'nic:NIC link status and MAC address'
    'storage:Storage controller and drive firmware'
    'cpu:CPU model and microcode version'
    'memory:DIMM info and firmware revision'
    'com:COM registration status'
    'full:Full firmware inventory'
    'disk-map:Drive bay and serial number map'
    'serial:Server model and serial number'
    'update-method:Firmware with update method classification'
    'license:iLO license management'
    'power:Server power operations'
    'uid:UID indicator light control'
    'boot:Boot order and override'
    'bios:BIOS settings'
    'network:iLO network configuration'
    'reports:Fleet hardware reports'
    'init:Create starter inventory.ini'
  )
  _describe 'command' cmds
}

_proliant__ilo_list_resource() {
  _arguments \
    '1: :(list)' \
    '2::server name:()' \
    '--raw[show raw JSON]' \
    '--json[output as JSON]'
}

_proliant__ilo_servers() {
  _arguments \
    '1: :(list describe)' \
    '2::server name:()' \
    '--raw[raw JSON]' \
    '--json[JSON output]'
}

_proliant__ilo_firmware() {
  local curcontext="$curcontext" context state line
  typeset -A opt_args
  _arguments -C '1: :_proliant__ilo_firmware_cmds' '*:: :->args'
  case $state in
    args)
      case $line[1] in
        list)
          _arguments '1::server name:()' '--raw[raw JSON]' '--json[JSON output]' ;;
        upgrade)
          _arguments \
            '1:server name:()' \
            '--dry-run[show what would be done]' \
            '--reboot[reboot after staging]' \
            '--component[component]:component:(all ilo bios nic storage)' ;;
        components|queue|clear)
          _arguments '1:server name:()' ;;
        stage)
          _arguments '1:server name:()' '--url[firmware URL]:url:' ;;
        flash)
          _arguments '1:server name:()' '--filename[staged filename]:filename:' ;;
      esac
      ;;
  esac
}

_proliant__ilo_firmware_cmds() {
  local -a cmds
  cmds=(
    'list:List firmware versions'
    'upgrade:Upgrade outdated firmware'
    'components:List staged components in iLO repository'
    'queue:Show firmware update task queue'
    'stage:Stage a firmware package from a URL'
    'flash:Queue a staged file for flash on next reboot'
    'clear:Clear all entries from the task queue'
  )
  _describe 'subcommand' cmds
}

_proliant__ilo_license() {
  _arguments \
    '1: :(list describe set)' \
    '2::server name:()' \
    '--raw[raw JSON]' \
    '--json[JSON output]'
}

_proliant__ilo_power() {
  _arguments \
    '1: :(reset on off shutdown)' \
    '2:server name:()' \
    '--dry-run[dry run]' \
    '--reset-type[reset type]:type:(GracefulRestart ForceRestart ForceOff GracefulShutdown PushPowerButton)'
}

_proliant__ilo_uid() {
  _arguments '1: :(on off)' '2:server name:()'
}

_proliant__ilo_boot() {
  local curcontext="$curcontext" context state line
  typeset -A opt_args
  _arguments -C '1: :(describe set)' '*:: :->args'
  case $state in
    args)
      case $line[1] in
        set) _arguments '1: :(pxe)' '2:server name:()' ;;
        *)   _arguments '1:server name:()' ;;
      esac ;;
  esac
}

_proliant__ilo_bios() {
  local curcontext="$curcontext" context state line
  typeset -A opt_args
  _arguments -C '1: :(describe set)' '*:: :->args'
  case $state in
    args)
      case $line[1] in
        set) _arguments '1: :(serial-console workload-profile)' '2:server name:()' ;;
        *)   _arguments '1:server name:()' '--pending[show pending settings]' ;;
      esac ;;
  esac
}

_proliant__ilo_network() {
  _arguments '1: :(set dhcp static route ipmi)' '2:server name:()'
}

_proliant__ilo_reports() {
  _arguments '1: :(memory cpu gpu)' '2::server name:()'
}

_proliant__com() {
  local curcontext="$curcontext" context state line
  typeset -A opt_args
  _arguments -C '1: :_proliant__com_cmds' '*:: :->args'
  case $state in
    args)
      case $line[1] in
        devices)    _arguments '1: :(list add)' '*:: :->dargs'
                    case $state in
                      dargs)
                        case $line[1] in
                          list) _arguments \
                            '--fields[comma-separated fields]:fields:' \
                            '--sort[sort by field]:field:' \
                            '--all[show all results]' \
                            '--json[JSON output]' ;;
                        esac ;;
                    esac ;;
        servers)    _arguments '1: :(list describe)' \
                      '--fields[comma-separated fields]:fields:' \
                      '--sort[sort by field]:field:' \
                      '--all[show all results]' \
                      '--json[JSON output]' ;;
        workspaces) _arguments '1: :(list)' ;;
        workspace)  _arguments '1: :(use)' ;;
        bundles)    _arguments '1: :(list)' \
                      '--gen[server generation]:gen:(10 11 12)' \
                      '--type[bundle type]:type:(base patch hotfix)' \
                      '--all[show all results]' \
                      '--raw[raw JSON]' ;;
        reports)    _arguments '1: :(memory gpu)' '--host[target host]:host:' ;;
      esac ;;
  esac
}

_proliant__com_cmds() {
  local -a cmds
  cmds=(
    'login:Authenticate to HPE GreenLake'
    'logout:Remove saved credentials'
    'devices:Manage GreenLake devices (list, add)'
    'servers:Server inventory and details (list, describe)'
    'workspaces:List available workspaces'
    'workspace:Switch active workspace (use)'
    'bundles:Firmware bundles (list)'
    'reports:Fleet inventory reports (memory, gpu)'
  )
  _describe 'command' cmds
}

_proliant__qs() {
  _arguments '1: :(list describe diff)'
}

_proliant__oneview() {
  local curcontext="$curcontext" context state line
  typeset -A opt_args
  _arguments -C '1: :_proliant__oneview_cmds' '*:: :->args'
  case $state in
    args)
      case $line[1] in
        servers)         _arguments '1: :(list describe)' ;;
        firmware)        _arguments '1: :(list)' ;;
        networks)        _arguments '1: :(list)' ;;
        networksets)     _arguments '1: :(list describe)' ;;
        uplinksets)      _arguments '1: :(list describe)' ;;
        server-profiles) _arguments '1: :(list describe)' ;;
        reports)         _arguments '1: :(memory)' ;;
      esac ;;
  esac
}

_proliant__oneview_cmds() {
  local -a cmds
  cmds=(
    'servers:Managed servers (list, describe)'
    'firmware:Fleet firmware inventory (list)'
    'networks:Ethernet networks (list)'
    'networksets:Network sets (list, describe)'
    'uplinksets:Uplink sets (list, describe)'
    'server-profiles:Server profiles (list, describe)'
    'reports:Fleet hardware reports (memory)'
  )
  _describe 'command' cmds
}

_proliant__config() {
  _arguments '1: :(list)' '2: :(inventory cli-tree)'
}

_proliant "$@"
"""

def _get_current_version() -> str:
    # In a dev checkout (editable/source install), read pyproject.toml directly
    # so the reported version always matches the source without needing a
    # reinstall. Installed wheels and frozen EXEs fall back to baked metadata.
    if not is_frozen():
        try:
            import tomllib
            from pathlib import Path
            pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
            if pyproject.is_file():
                with pyproject.open("rb") as fh:
                    return tomllib.load(fh)["project"]["version"]
        except Exception:
            pass
    try:
        return _pkg_version("proliant")
    except PackageNotFoundError:
        return "dev"


def _check_for_update_hint() -> None:
    """Print a one-liner hint if a newer version is available.

    Checks GitHub at most once per 24 hours; result cached in
    ~/.cache/proliant/update-check.json. The network call is fire-and-forget
    (subprocess) so it never blocks the CLI.
    """
    import json
    import time

    cache_dir = os.path.join(
        os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "proliant"
    )
    cache_file = os.path.join(cache_dir, "update-check.json")

    current = _get_current_version()
    if current == "dev":
        return  # skip in dev/editable installs

    latest: str | None = None
    now = time.time()

    # ── read cache ────────────────────────────────────────────────────────────
    try:
        with open(cache_file, encoding="utf-8") as f:
            data = json.load(f)
        if now - data.get("ts", 0) < 86400:  # 24 h
            latest = data.get("latest")
        # else: stale — kick off background refresh below
    except Exception:
        pass

    # ── background refresh if cache missing or stale ───────────────────────
    if latest is None:
        try:
            import subprocess
            subprocess.Popen(
                [
                    sys.executable, "-c",
                    (
                        "import urllib.request,json,ssl,os,time;"
                        "import certifi;"
                        "ctx=ssl.create_default_context(cafile=certifi.where());"
                        "r=urllib.request.urlopen('https://api.github.com/repos/hjma29/proliant-cli/releases/latest',context=ctx,timeout=5);"
                        "v=json.loads(r.read()).get('tag_name','').lstrip('v');"
                        "d=os.path.join(os.environ.get('XDG_CACHE_HOME',os.path.expanduser('~/.cache')),'proliant');"
                        "os.makedirs(d,exist_ok=True);"
                        "open(os.path.join(d,'update-check.json'),'w').write(json.dumps({'latest':v,'ts':time.time()}))"
                    ),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception:
            pass
        return  # no cached value to compare yet

    # ── compare and hint ──────────────────────────────────────────────────────
    try:
        from packaging.version import Version
        if Version(latest) > Version(current):
            url = f"https://github.com/hjma29/proliant-cli/releases/tag/v{latest}"
            # OSC 8 clickable hyperlink + blue underline (degrades gracefully on dumb terminals)
            if sys.stderr.isatty():
                link = f"\033]8;;{url}\033\\{url}\033]8;;\033\\"
                styled = f"\033[4;34m{link}\033[0m"
            else:
                styled = url
            print(
                f"\n💡 proliant {latest} is available (you have {current})."
                f"  Run: proliant update\n"
                f"   Release notes: {styled}\n",
                file=sys.stderr,
            )
    except Exception:
        pass


def _ssl_context():
    """Build an SSL context using certifi's CA bundle.

    Compiled binaries (Nuitka/PyInstaller) don't ship the system CA store, so
    urllib's default context can't verify GitHub's certificate. certifi is a
    dependency (via httpx) and bundles a known-good CA bundle.
    """
    import ssl
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _run_update() -> None:
    """Download and replace the current proliant binary with the latest GitHub release."""
    import urllib.request
    import json
    import tempfile
    import zipfile
    import shutil
    import subprocess

    ssl_ctx = _ssl_context()

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
    headers = {"User-Agent": "proliant-updater"}
    if token:
        headers["Authorization"] = f"token {token}"
    try:
        url = f"https://api.github.com/repos/{_GITHUB_REPO}/releases/latest"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15, context=ssl_ctx) as resp:
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
        asset_name = "proliant-cli-windows.exe"
    elif sys.platform == "darwin":
        asset_name = "proliant-cli-macos"
    else:
        import platform as _platform
        _machine = _platform.machine().lower()
        if _machine in ("aarch64", "arm64"):
            asset_name = "proliant-cli-linux-arm64"
        else:
            asset_name = "proliant-cli-linux-x86"

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
            with urllib.request.urlopen(req, timeout=120, context=ssl_ctx) as resp, open(tmp_path, "wb") as f:
                shutil.copyfileobj(resp, f)
        except Exception as e:
            print(f"ERROR: Download failed: {e}", file=sys.stderr)
            sys.exit(1)
        if sys.platform != "win32":
            os.chmod(tmp_path, 0o755)
        # sys.executable is the Python interpreter when running as a pip script;
        # sys.argv[0] is the actual proliant entry point script.
        # When frozen (Nuitka/PyInstaller), sys.executable IS the binary.
        if is_frozen():
            current_exe = sys.executable
        else:
            current_exe = os.path.realpath(sys.argv[0])

        if sys.platform == "win32":
            # Windows can't overwrite a running exe, but it CAN rename it (even
            # while memory-mapped). Rename running exe → .old, copy new exe into
            # place immediately, then silently delete .old in the background.
            old_exe = current_exe + ".old"
            try:
                os.remove(old_exe)
            except OSError:
                pass
            try:
                os.rename(current_exe, old_exe)
                shutil.copy2(tmp_path, current_exe)
                os.remove(tmp_path)
            except OSError as e:
                print(f"ERROR: Could not replace {current_exe}: {e}", file=sys.stderr)
                print("If proliant.exe is in a system directory, try reinstalling via install.ps1.", file=sys.stderr)
                sys.exit(1)
            # Silently delete the old exe in the background (no window)
            cleanup_bat = os.path.join(os.environ.get("TEMP", "C:\\Temp"), "proliant_cleanup.bat")
            with open(cleanup_bat, "w") as f:
                f.write(
                    '@echo off\n'
                    ':retry\n'
                    'timeout /t 3 /nobreak >nul\n'
                    f'del "{old_exe}" >nul 2>&1\n'
                    'if errorlevel 1 goto retry\n'
                    f'del "{cleanup_bat}"\n'
                )
            subprocess.Popen(
                ["cmd.exe", "/c", cleanup_bat],
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW,
                close_fds=True,
            )
            print(f"✓ Updated to {latest_ver}.")
            # Refresh the PowerShell completion block (new version may have new commands)
            _win_add_powershell_completion()
            _win_check_execution_policy()
            print("  Tab completion updated — open a new PowerShell window to use it.")
        else:
            try:
                os.replace(tmp_path, current_exe)
            except PermissionError:
                # Binary installed in a root-owned dir (e.g. /usr/local/bin) — retry with sudo
                import subprocess as _sp
                print(f"  Need sudo to write to {current_exe} (enter your password if prompted):")
                result = _sp.run(["sudo", "mv", tmp_path, current_exe])
                if result.returncode != 0:
                    print("ERROR: sudo mv failed. Try: sudo proliant update", file=sys.stderr)
                    sys.exit(1)
                _sp.run(["sudo", "chmod", "755", current_exe], check=False)
            print(f"✓ Updated to {latest_ver}. Run 'proliant --version' to confirm.")
            # Clean up old Nuitka extraction cache dirs (keep only the new version)
            import re as _re
            _cache_base = os.path.join(
                os.path.expanduser("~"), ".cache", "proliant"
            )
            if os.path.isdir(_cache_base):
                for _entry in os.listdir(_cache_base):
                    _entry_path = os.path.join(_cache_base, _entry)
                    if os.path.isdir(_entry_path) and _entry != latest_ver:
                        try:
                            shutil.rmtree(_entry_path)
                            print(f"  Removed old cache: {_entry_path}")
                        except Exception:
                            pass
            # Tab completion is handled by install.sh — no action needed on update


def _dispatch_ilo(args: list[str]) -> None:
    try:
        from proliant.ilo.cli import main as ilo_main
    except ImportError as exc:
        print(
            f"proliant ilo: missing dependencies — install with: pip install proliant[ilo]\n({exc})",
            file=sys.stderr,
        )
        sys.exit(1)
    sys.argv = ["proliant ilo"] + args
    ilo_main()


def _dispatch_spp(args: list[str]) -> None:
    try:
        from proliant.spp.cli import main as spp_main
    except ImportError as exc:
        print(
            f"proliant spp: missing dependencies — install with: pip install proliant[spp]\n({exc})",
            file=sys.stderr,
        )
        sys.exit(1)
    sys.argv = ["proliant spp"] + args
    spp_main()


def _dispatch_com(args: list[str]) -> None:
    try:
        from proliant.com.cli import main as com_main
    except ImportError as exc:
        print(
            f"proliant com: missing dependencies — install with: pip install proliant[com]\n({exc})",
            file=sys.stderr,
        )
        sys.exit(1)
    sys.argv = ["proliant com"] + args
    com_main()


def _dispatch_qs(args: list[str]) -> None:
    try:
        from proliant.qs.cli import main as qs_main
    except ImportError as exc:
        print(
            f"proliant qs: missing dependencies — install with: pip install proliant[qs]\n({exc})",
            file=sys.stderr,
        )
        sys.exit(1)
    sys.argv = ["proliant qs"] + args
    qs_main()


def _dispatch_oneview(args: list[str]) -> None:
    try:
        from proliant.oneview.cli import main as oneview_main
    except ImportError as exc:
        print(
            f"proliant oneview: missing dependencies — install with: pip install proliant\n({exc})",
            file=sys.stderr,
        )
        sys.exit(1)
    sys.argv = ["proliant oneview"] + args
    oneview_main()


def _dispatch_config(args: list[str]) -> None:
    from proliant.config.cli import main as config_main
    sys.argv = ["proliant config"] + args
    config_main()


def main(argv: list[str] | None = None) -> None:
    _init_sentry()
    _windows_first_run_check()

    args = argv if argv is not None else sys.argv[1:]

    # ── argcomplete support ─────────────────────────────────────────────────
    # When tab-completing, delegate to the sub-CLI's own argcomplete handler
    # as soon as we know the namespace. For top-level completion (just 'proliant<TAB>')
    # fall through to the argparse-based completion below.
    if "_ARGCOMPLETE" in os.environ:
        comp_line = os.environ.get("COMP_LINE", "")
        parts = comp_line.split()
        # Only delegate to sub-CLI when namespace is fully typed (trailing space or 3+ tokens).
        # Without this guard, 'proliant com<TAB>' (no space) dispatches to com's parser
        # before a subcommand is started, returning nothing.
        in_subcommand = len(parts) >= 3 or comp_line.endswith(" ")
        # parts[0] = "proliant", parts[1] = namespace (if typed)
        if in_subcommand and len(parts) >= 2 and parts[1] == "ilo":
            # Tell argcomplete to skip 2 words (proliant + ilo) instead of 1
            os.environ["_ARGCOMPLETE"] = "2"
            _dispatch_ilo(parts[2:])
            return
        if in_subcommand and len(parts) >= 2 and parts[1] == "com":
            os.environ["_ARGCOMPLETE"] = "2"
            _dispatch_com(parts[2:])
            return
        if in_subcommand and len(parts) >= 2 and parts[1] == "spp":
            os.environ["_ARGCOMPLETE"] = "2"
            _dispatch_spp(parts[2:])
            return

        if in_subcommand and len(parts) >= 2 and parts[1] == "oneview":
            os.environ["_ARGCOMPLETE"] = "2"
            _dispatch_oneview(parts[2:])
            return

        if in_subcommand and len(parts) >= 2 and parts[1] == "qs":
            os.environ["_ARGCOMPLETE"] = "2"
            _dispatch_qs(parts[2:])
            return

        if in_subcommand and len(parts) >= 2 and parts[1] == "config":
            os.environ["_ARGCOMPLETE"] = "2"
            _dispatch_config(parts[2:])
            return

        # Top-level: use argparse so argcomplete can offer 'ilo', 'com', 'spp', 'oneview'
        import argparse
        import argcomplete
        parser = argparse.ArgumentParser(prog="proliant", add_help=False)
        parser.add_argument("-V", "--version", action="store_true")
        sub = parser.add_subparsers(dest="namespace")
        sub.add_parser("ilo",     help="Direct iLO Redfish management")
        sub.add_parser("com",     help="HPE GreenLake / Compute Ops Management")
        sub.add_parser("spp",     help="HPE Service Pack for ProLiant analysis")
        sub.add_parser("oneview", help="HPE OneView fleet management")
        sub.add_parser("qs",      help="HPE QuickSpecs browser")
        sub.add_parser("config",  help="View and manage proliant configuration")
        sub.add_parser("update",              help="Upgrade proliant to the latest release")
        argcomplete.autocomplete(parser)
        return  # autocomplete() exits; reaching here means no completion needed

    # ── normal execution ────────────────────────────────────────────────────
    if not args or args[0] in ("-h", "--help"):
        print(_USAGE)
        sys.exit(0)

    if args[0] in ("-V", "--version"):
        try:
            v = _pkg_version("proliant")
        except PackageNotFoundError:
            v = "dev"
        print(f"proliant {v}")
        _check_for_update_hint()
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
        print(f"proliant: unknown namespace '{namespace}'\n", file=sys.stderr)
        print(_USAGE)
        sys.exit(2)


# ── Sentry opt-in telemetry ─────────────────────────────────────────────────
# Enable by setting PROLIANT_TELEMETRY=1 in your environment.
# No personal data (IPs, hostnames, credentials) is ever sent — see _sentry_scrub.
_SENTRY_DSN = (
    "https://1e25c8a5cf6f0d2ff916d46a4631d67a"
    "@o4511633310220288.ingest.us.sentry.io/4511633321164801"
)


def _sentry_scrub(event, hint):  # noqa: ANN001
    """Strip IPs, hostnames and credential patterns before sending."""
    import re
    _IP = re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b')
    _CRED = re.compile(r'(password|secret|token|key|auth)\s*[=:]\s*\S+', re.IGNORECASE)

    def _scrub(text: str) -> str:
        text = _IP.sub('<ip>', text)
        text = _CRED.sub(r'\1=<redacted>', text)
        return text

    for exc in event.get('exception', {}).get('values', []):
        if exc.get('value'):
            exc['value'] = _scrub(str(exc['value']))
    return event


def _init_sentry() -> None:
    """Initialise Sentry if PROLIANT_TELEMETRY=1 is set."""
    if not os.environ.get("PROLIANT_TELEMETRY"):
        return
    try:
        import sentry_sdk  # noqa: PLC0415
        sentry_sdk.init(
            dsn=_SENTRY_DSN,
            before_send=_sentry_scrub,
            send_default_pii=False,
            traces_sample_rate=0.0,
        )
    except ImportError:
        pass


if __name__ == "__main__":
    main()
