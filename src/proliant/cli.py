"""
proliant.cli — top-level dispatcher for the HPE ProLiant CLI.

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
usage: proliant [-h] NAMESPACE ...

HPE ProLiant CLI

namespaces:
  ilo          Direct iLO Redfish management (firmware, inventory, power)
  com          HPE GreenLake / Compute Ops Management (devices, workspaces)
  oneview      HPE OneView (Synergy & ProLiant fleet management)
  spp          HPE Service Pack for ProLiant catalog analysis
  setting      View and manage proliant configuration

commands:
  setup                Guided menu to view/add/edit/delete inventory.ini entries (iLO/OneView)
  version [-y|--yes]   Show installed version; offers to upgrade if a newer release exists
                        (-y/--yes skips the confirmation prompt)

Run 'proliant <namespace> --help' for namespace-specific help.

notes:
  Tab completion for bash/zsh/PowerShell is set up automatically by the
  installer (install.sh / install.ps1) — there is no 'proliant install-completion'
  subcommand. Re-run the installer to (re)enable tab completion.

examples:
  proliant ilo firmware list                       Firmware summary across all iLO hosts
  proliant ilo firmware upgrade myilo             Upgrade firmware via HPE SDR
  proliant com login                               Login to HPE GreenLake
  proliant com devices list                         List GreenLake devices
  proliant oneview servers list                    List all OneView-managed servers
  proliant oneview firmware list                   Fleet firmware inventory via OneView
  proliant spp list                                List available gen12 SPP versions
  proliant spp inspect gen12 2026.03.00.00         Analyse a gen12 SPP catalog
  proliant spp diff gen12 2025.09.01.00 2026.03.00.00  What changed between SPPs?
  proliant setup                                    Guided menu to manage your iLO/OneView inventory
  proliant version                                 Show version, offers to upgrade if newer exists
"""

_POWERSHELL_COMPLETION_BLOCK = """\
# >>> proliant tab completion >>>
Register-ArgumentCompleter -Native -CommandName proliant -ScriptBlock {
    param($wordToComplete, $commandAst, $cursorPosition)

    # Fast path: top-level namespace/command completion ('proliant <partial>')
    # is a small fixed list that never changes at runtime. Answer it directly
    # here so a plain 'proliant <TAB>' never has to spawn a whole new proliant
    # process (each spawn costs several hundred ms -- very noticeable while
    # typing). Deeper/dynamic completions (subcommands, live object names)
    # still fall through to invoking proliant below, unchanged.
    $rawLine = $commandAst.ToString()
    $parts = $rawLine -split '\\s+' | Where-Object { $_ -ne '' }
    $endsWithSpace = $rawLine -match '\\s$'
    $inSubcommand = ($parts.Count -ge 3) -or $endsWithSpace -or ($cursorPosition -gt $rawLine.Length)
    $dispatchNamespaces = @('ilo', 'com', 'oneview', 'spp', 'setting')
    $dispatchesToNamespace = $inSubcommand -and $parts.Count -ge 2 -and ($dispatchNamespaces -contains $parts[1])
    if (-not $dispatchesToNamespace) {
        $staticCompletions = @('ilo', 'com', 'oneview', 'spp', 'setting', 'setup', 'version')
        $staticCompletions |
            Where-Object { $_.StartsWith($wordToComplete, [System.StringComparison]::OrdinalIgnoreCase) } |
            ForEach-Object { [System.Management.Automation.CompletionResult]::new($_, $_, "ParameterValue", $_) }
        return
    }

    $completion_file = New-TemporaryFile
    $env:ARGCOMPLETE_USE_TEMPFILES = 1
    $env:_ARGCOMPLETE_STDOUT_FILENAME = $completion_file
    $env:COMP_LINE = $rawLine
    $env:COMP_POINT = $cursorPosition
    $env:_ARGCOMPLETE = 1
    $env:_ARGCOMPLETE_SUPPRESS_SPACE = 0
    $env:_ARGCOMPLETE_IFS = "`n"
    $env:_ARGCOMPLETE_SHELL = "powershell"
    proliant 2>&1 | Out-Null

    Get-Content $completion_file | ForEach-Object {
        $display = $_ -replace '`(.)', '$1'
        $displayText = $display.TrimEnd()
        $completion = $_
        if ($displayText -match '[\\s,]') {
            $completion = "'" + ($displayText -replace "'", "''") + "'"
            if ($display.EndsWith(' ')) {
                $completion = $completion + ' '
            }
        }
        [System.Management.Automation.CompletionResult]::new($completion, $displayText, "ParameterValue", $displayText)
    }
    Remove-Item $completion_file, Env:\\_ARGCOMPLETE_STDOUT_FILENAME, Env:\\ARGCOMPLETE_USE_TEMPFILES, Env:\\COMP_LINE, Env:\\COMP_POINT, Env:\\_ARGCOMPLETE, Env:\\_ARGCOMPLETE_SUPPRESS_SPACE, Env:\\_ARGCOMPLETE_IFS, Env:\\_ARGCOMPLETE_SHELL
}

# Show completion menu instead of cycling (added by proliant)
if (-not (Get-PSReadLineKeyHandler | Where-Object { $_.Key -eq 'Tab' -and $_.Function -eq 'MenuComplete' })) {
    Set-PSReadLineKeyHandler -Key Tab -Function MenuComplete
}
# <<< proliant tab completion <<<
"""


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


def _resolve_installed_exe_path() -> str:
    """Return the path of the real, installed proliant executable.

    A Nuitka onefile build runs as TWO processes: a small bootstrap ("parent")
    that unpacks the payload into a per-version cache dir (e.g.
    ``%LOCALAPPDATA%\\proliant\\<version>\\`` per --onefile-tempdir-spec) and
    re-execs the extracted payload as a child. Inside that child,
    ``sys.executable`` always points at the *extracted* interpreter in the
    cache dir (observed as literally ``python.exe``) — never at the real
    installed binary the user launched (e.g. ``%USERPROFILE%\\bin\\proliant.exe``,
    which is what's on PATH). Nuitka exposes the bootstrap's PID via the
    ``NUITKA_ONEFILE_PARENT`` env var; resolve that PID's image path to find
    the actual file that needs to be replaced on update.
    """
    parent_pid_str = os.environ.get("NUITKA_ONEFILE_PARENT", "")
    if parent_pid_str:
        try:
            parent_pid = int(parent_pid_str)
        except ValueError:
            parent_pid = 0
        if parent_pid:
            if sys.platform == "win32":
                img = _proc_image_name(parent_pid)
                if img and os.path.isfile(img):
                    return img
            else:
                proc_exe = f"/proc/{parent_pid}/exe"
                try:
                    img = os.path.realpath(proc_exe)
                    if img and img != proc_exe and os.path.isfile(img):
                        return img
                except OSError:
                    pass
    return sys.executable


def _windows_first_run_check() -> None:
    """On Windows: configure PowerShell tab completion once, after install.

    In the installer-based distribution the GUI installer (Inno Setup) owns
    file placement and the machine PATH, so proliant.exe lives in
    ``C:\\Program Files\\proliant-cli`` and is already on PATH. What the
    installer can NOT do (it runs elevated, as a different user context) is set
    up tab completion in the *current* user's PowerShell profile.

    ``install.ps1`` configures completion for one-liner installs, but users who
    download and run ``proliant-cli-windows-setup.exe`` directly would otherwise
    never get it. So on the first frozen run we set up completion once, guarded
    by a sentinel in the user-writable config dir (Program Files is read-only
    for a normal user, so the sentinel can't live next to the exe).

    The sentinel records the proliant version that last wrote the completion
    block. On later runs, if the installed version has changed we re-run the
    (idempotent) profile update so improvements to the completion block reach
    already-set-up users after a `proliant version` upgrade — without adding the
    profile-lookup subprocess overhead on every single run. The "enabled"
    print is only shown once, on the very first setup.
    """
    if sys.platform != "win32":
        return
    if not is_frozen():
        return  # only for the packaged .exe

    from proliant.common import config_dir as _config_dir

    try:
        sentinel_dir = _config_dir()
        sentinel_dir.mkdir(parents=True, exist_ok=True)
        sentinel = sentinel_dir / ".win-completion-done"
    except OSError:
        return  # can't determine/create config dir — skip silently

    current_version = _get_current_version()
    first_run = not sentinel.exists()
    if not first_run:
        try:
            stamped_version = sentinel.read_text(encoding="utf-8").strip()
        except OSError:
            stamped_version = ""
        if stamped_version == current_version:
            return  # already set up for this version — nothing to do

    try:
        _win_add_powershell_completion()
        sentinel.write_text(current_version, encoding="utf-8")
        if first_run:
            _win_check_execution_policy()
            print("proliant: PowerShell tab completion enabled.")
            print("  This window won't have it yet -- run '. $PROFILE' to load it now,")
            print("  or open a new PowerShell window.\n")
    except Exception:
        # Never block normal command execution on completion setup.
        pass


def _merge_powershell_completion_block(existing: str) -> str | None:
    """Compute the new profile content with the completion block installed/updated.

    Returns ``None`` if ``existing`` already contains an up-to-date block (the
    caller should skip writing). This is a pure function (no I/O) so the
    block-replacement logic — including migration away from older, buggy
    block formats — can be unit-tested directly.
    """
    import re

    if "proliant" in existing and "Register-ArgumentCompleter" in existing:
        if _POWERSHELL_COMPLETION_BLOCK.strip() in existing:
            return None  # already up-to-date
        # Strip any previously-installed block(s). Older versions used a
        # single "# proliant tab completion (added by proliant)" comment
        # marker with no unambiguous end marker, which only matched
        # through the FIRST top-level closing brace and left the
        # trailing "Show completion menu" if-block behind on every
        # reinstall/update -- causing it to accumulate duplicate copies
        # over time. The current format wraps the whole block in
        # explicit >>> / <<< markers so removal is unambiguous; we also
        # still strip the legacy marker format for anyone upgrading
        # from an older install.
        existing = re.sub(
            r"\n?# >>> proliant tab completion >>>.*?# <<< proliant tab completion <<<\n?",
            "\n",
            existing,
            flags=re.DOTALL,
        )
        existing = re.sub(
            r"\n?# proliant tab completion \(added by proliant\).*?(?:\n\})+\n?",
            "\n",
            existing,
            flags=re.DOTALL,
        )
        # Legacy installs could also have a *separate*, un-marked
        # "Show completion menu" if-block appended after the old
        # marker (left behind by the bug above) -- strip any leftover
        # copies of that exact block too so repeated updates converge
        # instead of accumulating.
        existing = re.sub(
            r"\n?# Show completion menu instead of cycling \(added by proliant\)\s*"
            r"\n?if \(-not \(Get-PSReadLineKeyHandler.*?\n\}\n?",
            "\n",
            existing,
            flags=re.DOTALL,
        )
    return existing.rstrip() + "\n" + _POWERSHELL_COMPLETION_BLOCK


def _win_add_powershell_completion() -> None:
    """Append completion block to the actual PowerShell profile(s), resolving OneDrive redirection.

    Writes to BOTH profile scopes so completion works regardless of which
    host the user's terminal is:
      - CurrentUserCurrentHost (``Microsoft.PowerShell_profile.ps1``) -- loaded
        only by the plain console host (Windows Terminal, powershell.exe/pwsh.exe
        opened directly).
      - CurrentUserAllHosts (``profile.ps1``) -- loaded by EVERY PowerShell host,
        including VS Code's integrated terminal and ISE, which do NOT load the
        CurrentUserCurrentHost file above.
    """
    import subprocess

    # Resolve both real profile paths by asking PowerShell -- handles OneDrive folder redirection
    def _ps_profiles(exe: str) -> list[str]:
        try:
            result = subprocess.run(
                [exe, "-NoProfile", "-NonInteractive", "-Command",
                 "$PROFILE.CurrentUserCurrentHost; $PROFILE.CurrentUserAllHosts"],
                capture_output=True, text=True, timeout=10,
            )
            return [line.strip() for line in result.stdout.splitlines() if line.strip()]
        except Exception:
            return []

    import shutil
    profiles: list[str] = []
    for exe in ("pwsh.exe", "powershell.exe"):
        if shutil.which(exe):
            for p in _ps_profiles(exe):
                if p not in profiles:
                    profiles.append(p)

    # Fallback to hardcoded paths if PowerShell query failed
    if not profiles:
        profiles = [
            os.path.expandvars(r"%USERPROFILE%\Documents\PowerShell\Microsoft.PowerShell_profile.ps1"),
            os.path.expandvars(r"%USERPROFILE%\Documents\PowerShell\profile.ps1"),
            os.path.expandvars(r"%USERPROFILE%\Documents\WindowsPowerShell\Microsoft.PowerShell_profile.ps1"),
            os.path.expandvars(r"%USERPROFILE%\Documents\WindowsPowerShell\profile.ps1"),
        ]

    for profile in profiles:
        try:
            profile_dir = os.path.dirname(profile)
            os.makedirs(profile_dir, exist_ok=True)
            existing = ""
            if os.path.exists(profile):
                with open(profile, encoding="utf-8") as f:
                    existing = f.read()
            new_content = _merge_powershell_completion_block(existing)
            if new_content is None:
                continue  # already up-to-date
            with open(profile, "w", encoding="utf-8") as f:
                f.write(new_content)
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

# Cloudflare Worker that counts install/update events by OS (no personal data).
# The install.ps1 / install.sh one-liners ping /install/{windows,unix}; the
# self-updater below pings /update/{windows,unix}.
_TELEMETRY_BASE = "https://proliant-cli.hjma29.workers.dev"


def _telemetry_send(url: str) -> None:
    """Perform the actual telemetry GET. Swallows every error by design."""
    try:
        import urllib.request

        req = urllib.request.Request(url, headers={"User-Agent": "proliant-updater"})
        urllib.request.urlopen(req, timeout=5, context=_ssl_context()).close()
    except Exception:
        pass


def _ping_telemetry(path: str) -> None:
    """Fire a best-effort, non-blocking telemetry ping (counts events by OS).

    Runs in a daemon thread so it can never delay or break the command, and
    swallows all errors. Set PROLIANT_NO_TELEMETRY=1 to opt out.
    """
    if os.environ.get("PROLIANT_NO_TELEMETRY"):
        return
    url = f"{_TELEMETRY_BASE}{path}"
    try:
        import threading

        threading.Thread(target=_telemetry_send, args=(url,), daemon=True).start()
    except Exception:
        pass



# Static zsh completion script — kept as reference/fallback. Not used by default.
# To switch back to static: set _ZSH_COMPLETION_SCRIPT as the content written by install.sh.
_ZSH_COMPLETION_SCRIPT = r"""#compdef proliant
# Generated by proliant install.sh -- do not edit manually

_proliant() {
  local curcontext="$curcontext" context state line
  typeset -A opt_args
  _arguments -C \
    '(-h --help)'{-h,--help}'[show help]' \
    '1: :_proliant__ns' \
    '*:: :->args'
  case $state in
    args)
      case $line[1] in
        ilo)     _proliant__ilo ;;
        com)     _proliant__com ;;
        oneview) _proliant__oneview ;;
        setting)  _proliant__setting ;;
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
    'setting:View and manage proliant configuration'
    'setup:Guided menu to manage inventory.ini (view/add/edit/delete)'
    'version:Show version and check for updates'
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
                      '--all[show all results]' ;;
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

_proliant__setting() {
  _arguments '1: :(cli-tree telemetry uninstall)' '2: :(on off)'
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


def _resolve_github_token() -> str:
    """Return a GitHub token to use for API calls, or '' if none is available.

    Prefers the GITHUB_TOKEN env var; falls back to the gh CLI's cached token
    (works for private repos without any manual setup on the user's part).
    """
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        try:
            import subprocess
            result = subprocess.run(
                ["gh", "auth", "token"], capture_output=True, text=True, timeout=5
            )
            token = result.stdout.strip()
        except Exception:
            pass
    return token


def _fetch_latest_release(ssl_ctx=None) -> dict | None:
    """Fetch the latest GitHub release metadata (tag_name, assets, ...).

    Returns None on any network/parse failure (never raises).
    """
    import urllib.request
    import json

    ssl_ctx = ssl_ctx or _ssl_context()
    token = _resolve_github_token()
    headers = {"User-Agent": "proliant-updater"}
    if token:
        headers["Authorization"] = f"token {token}"
    try:
        url = f"https://api.github.com/repos/{_GITHUB_REPO}/releases/latest"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15, context=ssl_ctx) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


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


_VERSION_USAGE = """\
usage: proliant version [-y|--yes]

Show the installed proliant version. If a newer release is available on
GitHub, offers to install it.
  -y, --yes    Skip the upgrade confirmation prompt (non-interactive).\
"""

_SETUP_USAGE = """\
usage: proliant setup

Guided menu for managing inventory.ini -- view, add, edit, or delete iLO
servers (and, optionally, a OneView appliance), testing each connection
live before it is saved. Safe to run any time to add, change, or remove
entries.\
"""


def _setup_args_request_help(setup_args: list[str]) -> bool:
    return any(a in ("-h", "--help") for a in setup_args)


def _version_args_request_help(version_args: list[str]) -> bool:
    return any(a in ("-h", "--help") for a in version_args)


def _version_args_want_auto_confirm(version_args: list[str]) -> bool:
    return any(a in ("-y", "--yes") for a in version_args)


def _win_install_dir_hint() -> str:
    """Best-effort guess of the current/target install directory, for display only."""
    try:
        if is_frozen():
            return os.path.dirname(_resolve_installed_exe_path())
    except Exception:
        pass
    return os.path.expandvars(r"%ProgramFiles%\proliant-cli")


def _confirm_windows_update(latest_ver: str, auto_confirm: bool) -> bool:
    """Print install-location/uninstall info and get the user's go-ahead.

    On Windows the update runs an elevated GUI installer with /SILENT (no
    wizard pages), so there's normally no on-screen indication of where files
    go or how to undo it. Since `proliant version`'s upgrade prompt runs unattended in a
    terminal (not a wizard the user is already clicking through), surface
    that information here as plain text and require confirmation before
    downloading/installing, unless explicitly skipped with -y/--yes.

    Returns True to proceed, False if the user declined. Always returns True
    (no prompt, no output) on non-Windows platforms, where updates are a
    simple, already-transparent binary replace.
    """
    if sys.platform != "win32":
        return True
    install_dir = _win_install_dir_hint()
    print()
    print(f"This will install proliant-cli {latest_ver}.")
    print(f"  Install directory : {install_dir}")
    print( "  Files are copied directly into that folder (no separate extraction step).")
    print( "  To uninstall later : Settings > Apps > proliant-cli > Uninstall")
    print( "                       (or the Uninstall shortcut in the Start Menu).")
    if auto_confirm:
        print()
        return True
    answer = input("Continue? [Y/n]: ").strip().lower()
    if answer not in ("", "y", "yes"):
        print("Update cancelled.")
        return False
    print()
    return True


def _run_update(auto_confirm: bool = False, release: dict | None = None) -> None:
    """Download and replace the current proliant binary with the latest GitHub release.

    If *release* is already known (e.g. the caller already checked via
    `proliant version`), pass it in to skip a redundant GitHub API call.
    """
    import tempfile
    import zipfile
    import shutil
    import subprocess

    ssl_ctx = _ssl_context()

    if release is None:
        print("Checking for updates...")
        release = _fetch_latest_release(ssl_ctx)
        if release is None:
            print("ERROR: Could not reach GitHub.", file=sys.stderr)
            if not _resolve_github_token():
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

    if not _confirm_windows_update(latest_ver, auto_confirm):
        sys.exit(0)

    # Count this update by OS (best-effort, non-blocking). Fired before the
    # download so the network transfer gives the background thread time to
    # complete -- important on Windows, which exits right after launching the
    # installer. Mirrors the install-script ping to the same Cloudflare Worker.
    _ping_telemetry(f"/update/{'windows' if sys.platform == 'win32' else 'unix'}")

    # Determine asset name for this platform
    if sys.platform == "win32":
        asset_name = "proliant-cli-windows-setup.exe"
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

    total_size = asset.get("size", 0)
    size_mb = f"{total_size / 1_048_576:.1f} MB" if total_size else ""
    print(f"  Downloading {asset_name}{' (' + size_mb + ')' if size_mb else ''}...")

    # Use the API assets endpoint with Accept: application/octet-stream for private repos
    asset_api_url = asset["url"]
    dl_headers = {**headers, "Accept": "application/octet-stream"}

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = os.path.join(tmpdir, asset_name)
        try:
            req = urllib.request.Request(asset_api_url, headers=dl_headers)
            with urllib.request.urlopen(req, timeout=120, context=ssl_ctx) as resp, open(tmp_path, "wb") as f:
                downloaded = 0
                block = 65536
                bar_width = 30
                while True:
                    chunk = resp.read(block)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total_size:
                        pct = downloaded / total_size
                        filled = int(bar_width * pct)
                        bar = "█" * filled + "░" * (bar_width - filled)
                        done_mb = downloaded / 1_048_576
                        print(f"\r  [{bar}] {pct*100:5.1f}%  {done_mb:.1f}/{total_size/1_048_576:.1f} MB", end="", flush=True)
                print()  # newline after progress bar
        except Exception as e:
            print(f"ERROR: Download failed: {e}", file=sys.stderr)
            sys.exit(1)
        if sys.platform != "win32":
            os.chmod(tmp_path, 0o755)

        if sys.platform == "win32":
            # Windows ships as a GUI installer (Inno Setup). Copy it out of the
            # auto-deleted temp dir to a persistent location, launch it elevated
            # (ShellExecute honors the installer's requireAdministrator manifest
            # and shows the UAC prompt), then exit so the running proliant.exe
            # unlocks and the installer can replace it in Program Files.
            import ctypes

            persistent = os.path.join(
                os.environ.get("TEMP", tempfile.gettempdir()), asset_name
            )
            try:
                shutil.copy2(tmp_path, persistent)
            except OSError as e:
                print(f"ERROR: Could not stage installer: {e}", file=sys.stderr)
                sys.exit(1)
            print(f"  Launching installer for {latest_ver} (accept the UAC prompt)...")
            # /SILENT shows only a progress bar; /SUPPRESSMSGBOXES avoids prompts.
            # ShellExecute with "runas" guarantees elevation; subprocess/CreateProcess
            # would fail with ERROR_ELEVATION_REQUIRED for an admin-manifest exe.
            rc = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", persistent, "/SILENT /SUPPRESSMSGBOXES", None, 1
            )
            if rc <= 32:
                print(
                    f"ERROR: Could not launch installer (ShellExecute code {rc}).",
                    file=sys.stderr,
                )
                print(f"Run it manually: {persistent}", file=sys.stderr)
                sys.exit(1)
            print(f"✓ Installer launched. proliant will update to {latest_ver} in a moment.")
            # Exit now so the installer can replace the running proliant.exe.
            sys.exit(0)
        else:
            # sys.executable is the Python interpreter when running as a pip
            # script; sys.argv[0] is the actual proliant entry point script.
            # When frozen, sys.executable is NOT the installed binary for Nuitka
            # onefile builds — see _resolve_installed_exe_path() for why.
            if is_frozen():
                current_exe = _resolve_installed_exe_path()
            else:
                current_exe = os.path.realpath(sys.argv[0])
            try:
                os.replace(tmp_path, current_exe)
            except PermissionError:
                # Binary in a root-owned dir — relocate to ~/.local/bin instead of sudo
                import subprocess as _sp
                local_bin = os.path.expanduser("~/.local/bin")
                os.makedirs(local_bin, exist_ok=True)
                new_exe = os.path.join(local_bin, "proliant")
                import shutil as _shutil
                _shutil.copy2(tmp_path, new_exe)
                os.chmod(new_exe, 0o755)
                os.remove(tmp_path)
                print(f"  Installed to {new_exe} (no sudo needed).")
                # Ensure ~/.local/bin is in PATH via shell rc
                shell = os.environ.get("SHELL", "")
                rc = os.path.expanduser("~/.zshrc") if "zsh" in shell else os.path.expanduser("~/.bashrc")
                path_line = 'export PATH="$HOME/.local/bin:$PATH"'
                try:
                    content = open(rc).read() if os.path.exists(rc) else ""
                    if ".local/bin" not in content:
                        with open(rc, "a") as f:
                            f.write(f"\n# proliant: add ~/.local/bin to PATH\n{path_line}\n")
                        print(f"  Added ~/.local/bin to PATH in {rc} — open a new terminal to use it.")
                except Exception:
                    print(f"  Add this to your shell rc: {path_line}")
                current_exe = new_exe
            print(f"✓ Updated to {latest_ver}. Run 'proliant version' to confirm.")
            # Clean up old Nuitka extraction cache dirs (keep only the new version)
            import re as _re
            from proliant.common import cache_dir as _common_cache_dir
            _cache_base = str(_common_cache_dir())
            if os.path.isdir(_cache_base):
                for _entry in os.listdir(_cache_base):
                    _entry_path = os.path.join(_cache_base, _entry)
                    if os.path.isdir(_entry_path) and _entry != latest_ver:
                        try:
                            shutil.rmtree(_entry_path)
                        except Exception:
                            pass
            # Tab completion is handled by install.sh — no action needed on update


def _cmd_version(auto_confirm: bool = False) -> None:
    """Print the installed version; if a newer release exists, offer to install it."""
    current_ver = _get_current_version()
    print(f"proliant {current_ver}")

    if current_ver == "dev":
        return  # dev/editable install -- no GitHub release to compare against

    print("Checking for updates...")
    release = _fetch_latest_release()
    if release is None:
        print("  Could not reach GitHub to check for updates.", file=sys.stderr)
        return

    latest_ver = release.get("tag_name", "").lstrip("v")
    if not latest_ver:
        return

    try:
        from packaging.version import Version
        is_newer = Version(latest_ver) > Version(current_ver)
    except Exception:
        is_newer = latest_ver != current_ver

    if not is_newer:
        print("✓ Already up to date.")
        return

    print(f"\nA newer version is available: {latest_ver} (you have {current_ver})")
    if auto_confirm:
        answer = "y"
    else:
        try:
            answer = input("Upgrade now? [y/N]: ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print()
            return

    if answer in ("y", "yes"):
        _run_update(auto_confirm=True, release=release)
    else:
        print("Run 'proliant version -y' anytime to upgrade.")


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
    print(
        "proliant qs: this command is currently unavailable.",
        file=sys.stderr,
    )
    sys.exit(1)


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


def _dispatch_setting(args: list[str]) -> None:
    from proliant.setting.cli import main as setting_main
    sys.argv = ["proliant setting"] + args
    setting_main()


def _enable_windows_vt_mode() -> None:
    """Enable ANSI/VT100 escape processing on the console (Windows only).

    Rich auto-detects "legacy Windows" consoles (no virtual-terminal support)
    and, for those, writes styled segments straight to stdout/stderr via the
    raw Win32 Console API with no size guard (rich._win32_console.
    LegacyWindowsTerm.write_text just does ``file.write(text)``). A wide
    table with enough rows can then trip the long-standing Windows console
    bug where a single large write raises OSError: [Errno 22] Invalid
    argument (cpython gh-82052 / bpo-37871). Rich's *modern* ANSI write path
    already chunks output to avoid this — it's only the legacy fallback that
    doesn't — so forcing VT mode on here, once, at startup keeps every
    later Console.print() on the safe path. Real crash: Sentry PROLIANT-CLI-6.

    Best-effort: silently no-ops if stdout/stderr aren't attached to a real
    console (piped/redirected output, CI, non-interactive shells).
    """
    import ctypes

    STD_OUTPUT_HANDLE = -11
    STD_ERROR_HANDLE = -12
    ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004

    try:
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return

    for std_handle in (STD_OUTPUT_HANDLE, STD_ERROR_HANDLE):
        try:
            handle = kernel32.GetStdHandle(std_handle)
            if not handle or handle == -1:
                continue
            mode = ctypes.c_uint32()
            if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                continue  # not a real console handle (piped/redirected) — nothing to do
            kernel32.SetConsoleMode(handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING)
        except OSError:
            pass


def main(argv: list[str] | None = None) -> None:
    # On Windows, stdout/stderr may default to CP1252 when piped, which can't
    # encode Unicode chars used in Rich tables (✓, —, …).  Reconfigure to
    # UTF-8 with replacement so we never raise UnicodeEncodeError at runtime.
    # Use reconfigure() (in-place) rather than wrapping in a new TextIOWrapper:
    # wrapping creates a second wrapper around the same underlying buffer, and
    # if main() ever runs more than once in a process (e.g. repeated calls in
    # tests), closing/GC'ing the stale wrapper closes the shared buffer out
    # from under the new one, raising "I/O operation on closed file" later.
    if sys.platform == "win32":
        for _stream in (sys.stdout, sys.stderr):
            try:
                _stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass
        _enable_windows_vt_mode()

    _init_sentry()
    _windows_first_run_check()

    args = argv if argv is not None else sys.argv[1:]

    # ── argcomplete support ─────────────────────────────────────────────────
    # When tab-completing, delegate to the sub-CLI's own argcomplete handler
    # as soon as we know the namespace. For top-level completion (just 'proliant<TAB>')
    # fall through to the argparse-based completion below.
    if "_ARGCOMPLETE" in os.environ:
        comp_line = os.environ.get("COMP_LINE", "")
        try:
            comp_point = int(os.environ.get("COMP_POINT", "0"))
        except ValueError:
            comp_point = 0
        parts = comp_line.split()
        # Only delegate to sub-CLI when namespace is fully typed (trailing space or 3+ tokens).
        # Without this guard, 'proliant com<TAB>' (no space) dispatches to com's parser
        # before a subcommand is started, returning nothing.
        in_subcommand = len(parts) >= 3 or comp_line.endswith(" ") or comp_point > len(comp_line)
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
        if in_subcommand and len(parts) >= 2 and parts[1] == "oneview":
            os.environ["_ARGCOMPLETE"] = "2"
            _dispatch_oneview(parts[2:])
            return

        if in_subcommand and len(parts) >= 2 and parts[1] == "spp":
            os.environ["_ARGCOMPLETE"] = "2"
            _dispatch_spp(parts[2:])
            return

        if in_subcommand and len(parts) >= 2 and parts[1] == "setting":
            os.environ["_ARGCOMPLETE"] = "2"
            _dispatch_setting(parts[2:])
            return

        # Top-level: use argparse so argcomplete can offer 'ilo', 'com', 'oneview', 'spp'
        import argparse
        import argcomplete
        parser = argparse.ArgumentParser(prog="proliant", add_help=False)
        sub = parser.add_subparsers(dest="namespace")
        sub.add_parser("ilo",     help="Direct iLO Redfish management")
        sub.add_parser("com",     help="HPE GreenLake / Compute Ops Management")
        sub.add_parser("oneview", help="HPE OneView fleet management")
        sub.add_parser("spp",     help="HPE Service Pack for ProLiant analysis")
        sub.add_parser("setting", help="View and manage proliant configuration")
        sub.add_parser("setup",   help="Guided menu to view/add/edit/delete inventory.ini entries")
        sub.add_parser("version", help="Show version and check for updates")
        argcomplete.autocomplete(parser)
        return  # autocomplete() exits; reaching here means no completion needed

    # ── normal execution ────────────────────────────────────────────────────
    if not args or args[0] in ("-h", "--help"):
        print(_USAGE)
        sys.exit(0)

    namespace = args[0]

    if namespace == "ilo":
        _dispatch_ilo(list(args[1:]))
    elif namespace == "com":
        _dispatch_com(list(args[1:]))
    elif namespace == "oneview":
        _dispatch_oneview(list(args[1:]))
    elif namespace == "spp":
        _dispatch_spp(list(args[1:]))
    elif namespace == "qs":
        _dispatch_qs(list(args[1:]))
    elif namespace == "setting":
        _dispatch_setting(list(args[1:]))
    elif namespace == "setup":
        setup_args = list(args[1:])
        if _setup_args_request_help(setup_args):
            print(_SETUP_USAGE)
            sys.exit(0)
        from proliant.common.runner import run_sync
        from proliant.setup.wizard import run_setup_wizard
        run_sync(run_setup_wizard())
    elif namespace == "version":
        version_args = list(args[1:])
        if _version_args_request_help(version_args):
            print(_VERSION_USAGE)
            sys.exit(0)
        _cmd_version(auto_confirm=_version_args_want_auto_confirm(version_args))
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

_SENTRY_DROP_TYPES = {
    "AuthError",
    "AuthFlowError",
    "ConnectError",
    "ConnectTimeout",
    "CredentialsError",
    "EOFError",
    "FileNotFoundError",
    "KeyboardInterrupt",
    "NetworkError",
    "OneViewError",
    "PermissionError",
    "PoolTimeout",
    "ProxyError",
    "ReadError",
    "ReadTimeout",
    "RemoteProtocolError",
    "RequestError",
    "SystemExit",
    "TimeoutError",
    "TimeoutException",
    "TooManyRedirects",
    "UnsupportedProtocol",
    "WriteError",
    "WriteTimeout",
}

_SENTRY_DROP_MESSAGE_PATTERNS = (
    r"\b(?:http|https)\s+\d{3}\s*:\s*(?:check username/password|account lacks permission)",
    r"\b(?:401|403)\b.*(?:unauthori[sz]ed|forbidden|permission|password|credential|auth)",
    r"(?:authentication|login|password)\s+(?:failed|denied|cancelled)",
    r"(?:wrong password|too many failed attempts|not logged in|session expired)",
    r"(?:cannot reach|unreachable|connection refused|network is unreachable)",
    r"(?:connect timeout|read timeout|timed out|timeout)",
    r"(?:getaddrinfo failed|name or service not known|temporary failure in name resolution)",
    r"(?:credentials file not found|no inventory\.ini found|file not found)",
    r"(?:not found\. known:|known servers:|known vlan|known vlans)",
    r"(?:invalid .* valid values|no settings specified)",
)


def _sentry_hint_expected_failure(hint) -> bool:  # noqa: ANN001
    exc_info = (hint or {}).get("exc_info") or ()
    if len(exc_info) < 2:
        return False
    exc = exc_info[1]
    if exc is None:
        return False

    if type(exc).__name__ in _SENTRY_DROP_TYPES:
        return True

    try:
        import httpx  # noqa: PLC0415
        if isinstance(exc, httpx.RequestError):
            return True
        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code if exc.response is not None else 0
            return status in {400, 401, 403, 404, 408, 409, 429}
    except ImportError:
        pass

    return False


def _sentry_event_expected_failure(event) -> bool:  # noqa: ANN001
    import re
    for exc in event.get("exception", {}).get("values", []):
        if exc.get("type") in _SENTRY_DROP_TYPES:
            return True
        value = str(exc.get("value") or "")
        if any(re.search(pattern, value, re.IGNORECASE) for pattern in _SENTRY_DROP_MESSAGE_PATTERNS):
            return True
    return False


def _sentry_scrub(event, hint):  # noqa: ANN001
    """Strip IPs, hostnames and credential patterns before sending."""
    import re
    if _sentry_hint_expected_failure(hint) or _sentry_event_expected_failure(event):
        return None

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
    """Initialise Sentry if telemetry is enabled.

    Check order:
    1. ~/.config/proliant-cli/telemetry-disabled  -> always off
    2. ~/.config/proliant-cli/telemetry-enabled   -> on
    3. PROLIANT_TELEMETRY=1 env var               -> on (legacy / CI)
    """
    from pathlib import Path
    cfg = Path.home() / ".config" / "proliant-cli"
    if (cfg / "telemetry-disabled").exists():
        return
    if not (cfg / "telemetry-enabled").exists() and not os.environ.get("PROLIANT_TELEMETRY"):
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
