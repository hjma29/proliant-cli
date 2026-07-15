"""Autocomplete regression tests for proliant CLI parsers."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from importlib import import_module
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"


def _complete(line: str, *, comp_point: int | None = None) -> list[str]:
    """Run argcomplete through the top-level proliant entry point."""
    with tempfile.NamedTemporaryFile(delete=False) as output_file:
        output_path = Path(output_file.name)

    env = os.environ.copy()
    env.update(
        {
            "ARGCOMPLETE_USE_TEMPFILES": "1",
            "_ARGCOMPLETE_STDOUT_FILENAME": str(output_path),
            "COMP_LINE": line,
            "COMP_POINT": str(len(line) if comp_point is None else comp_point),
            "_ARGCOMPLETE": "1",
            "_ARGCOMPLETE_SUPPRESS_SPACE": "0",
            "_ARGCOMPLETE_IFS": "\n",
            "_ARGCOMPLETE_SHELL": "powershell",
            "PYTHONPATH": str(SRC_DIR) + os.pathsep + env.get("PYTHONPATH", ""),
        }
    )

    try:
        result = subprocess.run(
            [sys.executable, "-m", "proliant.cli"],
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=20,
        )
        assert result.returncode == 0, result.stderr or result.stdout
        return output_path.read_text().splitlines() if output_path.exists() else []
    finally:
        output_path.unlink(missing_ok=True)


def _value_actions_without_completion() -> list[str]:
    modules = {
        "ilo": "proliant.ilo.cli",
        "com": "proliant.com.cli",
        "oneview": "proliant.oneview.cli",
        "spp": "proliant.spp.cli",
        "setting": "proliant.setting.cli",
    }
    missing: list[str] = []

    def walk(parser: argparse.ArgumentParser, path: list[str]) -> None:
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                seen: set[int] = set()
                for name, subparser in action.choices.items():
                    parser_id = id(subparser)
                    if parser_id in seen:
                        continue
                    seen.add(parser_id)
                    walk(subparser, path + [name])
                continue

            if isinstance(action, argparse._HelpAction):
                continue

            is_flag = isinstance(
                action,
                (
                    argparse._StoreTrueAction,
                    argparse._StoreFalseAction,
                    argparse._VersionAction,
                ),
            )
            has_completion = getattr(action, "choices", None) is not None or hasattr(action, "completer")
            if not is_flag and action.dest != argparse.SUPPRESS and not has_completion:
                names = action.option_strings or [action.dest]
                missing.append(f"{' '.join(path)} :: {'/'.join(names)}")

    for namespace, module_name in modules.items():
        module = import_module(module_name)
        walk(module._build_parser(), [namespace])

    return missing


def _actions_using_argcomplete_suppress_completer() -> list[str]:
    """Flags whose completer is argcomplete's own SuppressCompleter.

    argcomplete treats a SuppressCompleter as "hide this flag from '--<TAB>'
    completion entirely" (see ArgcompleteFinder._get_option_completions),
    not just "don't complete a value for it". `suppress_file_completion()`
    in proliant.common.completers must never be implemented with it, or
    every flag using that helper becomes invisible to tab completion.
    """
    from argcomplete.completers import SuppressCompleter

    modules = {
        "ilo": "proliant.ilo.cli",
        "com": "proliant.com.cli",
        "oneview": "proliant.oneview.cli",
        "spp": "proliant.spp.cli",
        "setting": "proliant.setting.cli",
    }
    offenders: list[str] = []

    def walk(parser: argparse.ArgumentParser, path: list[str]) -> None:
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                seen: set[int] = set()
                for name, subparser in action.choices.items():
                    parser_id = id(subparser)
                    if parser_id in seen:
                        continue
                    seen.add(parser_id)
                    walk(subparser, path + [name])
                continue

            completer = getattr(action, "completer", None)
            if isinstance(completer, SuppressCompleter):
                names = action.option_strings or [action.dest]
                offenders.append(f"{' '.join(path)} :: {'/'.join(names)}")

    for namespace, module_name in modules.items():
        module = import_module(module_name)
        walk(module._build_parser(), [namespace])

    return offenders


def test_every_value_argument_declares_completion_behavior():
    assert _value_actions_without_completion() == []


def test_no_flag_uses_argcomplete_suppress_completer():
    assert _actions_using_argcomplete_suppress_completer() == []


def test_top_level_completion_lists_namespaces():
    completions = set(_complete("proliant "))
    assert {"ilo", "com", "oneview", "spp", "setting", "version"} <= completions
    assert "qs" not in completions


def test_powershell_trailing_space_loss_still_delegates_to_namespace():
    line_without_space = "proliant oneview"
    completions = set(
        _complete(line_without_space, comp_point=len(line_without_space) + 1)
    )
    assert {"servers", "firmware", "networks", "server-profiles", "reports"} <= completions


def test_oneview_static_value_completion():
    completions = set(_complete("proliant oneview servers list --fields "))
    assert {"name", "model", "serial", "ilo", "ilo_ip", "power", "state", "profile"} <= completions


def test_spp_completion_is_enabled_after_top_level_delegation():
    completions = set(_complete("proliant spp "))
    assert {"list", "inspect", "part-number", "download", "diff"} <= completions


def test_spp_type_completion():
    completions = set(_complete("proliant spp inspect gen12 2026.03.00.00 --type "))
    assert {"ilo", "bios", "nic", "storage", "disk", "power", "system"} <= completions


def test_freeform_values_do_not_fall_back_to_workspace_files():
    freeform_lines = [
        "proliant oneview mac list --address ",
        "proliant com login --email ",
        "proliant ilo network set static srv1 --ip ",
    ]
    for line in freeform_lines:
        assert _complete(line) == []


def test_freeform_value_flags_still_appear_in_flag_name_completion():
    # Regression test: a completer that suppresses file-path fallback for a
    # flag's *value* must not also hide the flag's *name* from `--<TAB>`
    # completion. Covers the live incident where `--concurrency` never
    # appeared in tab completion because suppress_file_completion() used
    # argcomplete's SuppressCompleter, which argcomplete treats as "hide this
    # option outright".
    completions = set(_complete("proliant oneview update enclosure LE01 --"))
    assert "--concurrency" in completions

    completions = set(_complete("proliant oneview mac list --"))
    assert "--address" in completions

    completions = set(_complete("proliant ilo network set static srv1 --"))
    assert "--ip" in completions


def test_powershell_bridge_quotes_completions_with_commas_and_spaces():
    from proliant.cli import _POWERSHELL_COMPLETION_BLOCK

    assert "$display = $_ -replace '`(.)', '$1'" in _POWERSHELL_COMPLETION_BLOCK
    assert "$displayText = $display.TrimEnd()" in _POWERSHELL_COMPLETION_BLOCK
    assert "$completion = \"'\" + ($displayText -replace \"'\", \"''\") + \"'\"" in _POWERSHELL_COMPLETION_BLOCK

    shell = shutil.which("pwsh") or shutil.which("powershell")
    if shell is None:
        return

    script = r"""
$raw = 'Enclosure-01,` bay` 1'
$display = $raw -replace '`(.)', '$1'
$displayText = $display.TrimEnd()
$completion = $raw
if ($displayText -match '[\s,]') {
    $completion = "'" + ($displayText -replace "'", "''") + "'"
    if ($display.EndsWith(' ')) {
        $completion = $completion + ' '
    }
}
function Show-Args { $args -join '|' }
$parsed = Invoke-Expression "Show-Args --server $completion"
if ($completion -ne "'Enclosure-01, bay 1'") {
    throw "unexpected completion text: $completion"
}
if ($parsed -ne '--server|Enclosure-01, bay 1') {
    throw "unexpected parsed args: $parsed"
}

$raw = 'enclosures '
$display = $raw -replace '`(.)', '$1'
$displayText = $display.TrimEnd()
$completion = $raw
if ($displayText -match '[\s,]') {
    $completion = "'" + ($displayText -replace "'", "''") + "'"
    if ($display.EndsWith(' ')) {
        $completion = $completion + ' '
    }
}
if ($completion -ne 'enclosures ') {
    throw "unexpected command completion text: $completion"
}
if ($displayText -ne 'enclosures') {
    throw "unexpected command display text: $displayText"
}
"""
    result = subprocess.run(
        [shell, "-NoLogo", "-NoProfile", "-Command", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_powershell_fast_path_skips_process_spawn_for_top_level_completion():
    """Top-level namespace completion ('proliant i<TAB>') must be answered
    directly by the PowerShell scriptblock, without ever invoking `proliant`
    -- process-spawn is the dominant cost of tab completion (~700-850ms per
    keystroke), and this fast path is what eliminates it. Genuine subcommand
    completions must still fall through unchanged."""
    from proliant.cli import _POWERSHELL_COMPLETION_BLOCK

    shell = shutil.which("pwsh") or shutil.which("powershell")
    if shell is None:
        return

    # Pull just the scriptblock body out of the real completion block (as a
    # plain variable instead of a Register-ArgumentCompleter argument) so it
    # can be invoked directly with synthetic inputs.
    scriptblock_text = _POWERSHELL_COMPLETION_BLOCK.replace(
        "Register-ArgumentCompleter -Native -CommandName proliant -ScriptBlock {",
        "$sb = {",
    ).split("# Show completion menu")[0]

    driver = r"""
function proliant { $global:invoked = $true }

$global:invoked = $false
$results = & $sb 'i' 'proliant i' 10
if ($global:invoked) { throw 'fast path should not invoke proliant for partial namespace' }
if (-not (@($results.CompletionText) -contains 'ilo')) {
    throw "expected 'ilo' in fast-path results: $(@($results.CompletionText) -join ',')"
}

$global:invoked = $false
$results2 = & $sb 'v' 'proliant v' 10
if ($global:invoked) { throw 'fast path should not invoke proliant for v prefix' }
if (@($results2).Count -ne 1 -or $results2[0].CompletionText -ne 'version') {
    throw "expected only 'version': $(@($results2.CompletionText) -join ',')"
}

$global:invoked = $false
$null = & $sb '' 'proliant oneview ' 17
if (-not $global:invoked) { throw 'trailing-space namespace completion must fall through to proliant' }

$global:invoked = $false
$null = & $sb '' 'proliant ilo ' 13
if (-not $global:invoked) { throw 'namespace-prefixed subcommand completion must fall through to proliant' }
"""

    script = scriptblock_text + "\n" + driver
    result = subprocess.run(
        [shell, "-NoLogo", "-NoProfile", "-Command", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr or result.stdout
