# proliant-cli Windows installer
# Usage: irm https://raw.githubusercontent.com/hjma29/proliant-cli/main/install.ps1 | iex
#
# Downloads the latest GUI installer (proliant-cli-windows-setup.exe) and runs
# it. The installer requests elevation (one UAC prompt) and installs
# machine-wide into "C:\Program Files\proliant-cli\", adds that folder to the
# system PATH, and registers an Add/Remove Programs entry.
#
# This script itself runs as the current (non-elevated) user so it can also set
# up PowerShell tab completion in YOUR profile after the install finishes.

$ErrorActionPreference = "Stop"

# Silent install ping — counts installs by OS (no personal data sent)
Start-Job { Invoke-WebRequest "https://proliant-cli.hjma29.workers.dev/install/windows" -UseBasicParsing -ErrorAction SilentlyContinue } | Out-Null

$Repo = "hjma29/proliant-cli"
$AssetName = "proliant-cli-windows-setup.exe"

Write-Host ""
Write-Host "proliant-cli installer" -ForegroundColor Cyan
Write-Host "══════════════════════════════════════" -ForegroundColor Cyan

# Resolve latest release download URL
Write-Host "Fetching latest release..." -ForegroundColor Gray
$release = Invoke-RestMethod "https://api.github.com/repos/$Repo/releases/latest"
$asset = $release.assets | Where-Object { $_.name -eq $AssetName } | Select-Object -First 1

if (-not $asset) {
    Write-Error "Could not find $AssetName in latest release."
    exit 1
}

$version = $release.tag_name
$url = $asset.browser_download_url
Write-Host "Downloading $version..." -ForegroundColor Gray

$setup = Join-Path ([System.IO.Path]::GetTempPath()) $AssetName

# Download with curl.exe (ships in Win10/11). Use quiet output because curl's
# progress meter writes to stderr, which becomes a terminating NativeCommandError
# under Windows PowerShell when $ErrorActionPreference is Stop.
$curl = Get-Command curl.exe -ErrorAction SilentlyContinue
if ($curl) {
    $curlOutput = & $curl.Source -L --fail --silent --show-error -o $setup $url 2>&1
    $curlExit = $LASTEXITCODE
    if ($curlExit -ne 0) {
        if ($curlOutput) { $curlOutput | ForEach-Object { Write-Error $_ } }
        Write-Error "Download failed (curl exit $curlExit)."
        exit 1
    }
} else {
    $prev = $ProgressPreference
    $ProgressPreference = 'SilentlyContinue'
    try {
        Invoke-WebRequest -Uri $url -OutFile $setup -UseBasicParsing
    } finally {
        $ProgressPreference = $prev
    }
}

# Run the installer. Its manifest requests admin, so Windows shows a single UAC
# prompt and the GUI wizard walks the user through the install.
Write-Host ""
Write-Host "  Launching the installer (accept the UAC prompt)..." -ForegroundColor Yellow
$proc = Start-Process -FilePath $setup -Wait -PassThru
Remove-Item $setup -ErrorAction SilentlyContinue

if ($proc.ExitCode -ne 0) {
    Write-Error "Installer exited with code $($proc.ExitCode)."
    exit 1
}

Write-Host ""
Write-Host "  Installed proliant-cli $version to C:\Program Files\proliant-cli" -ForegroundColor Green

# ── Tab completion (dynamic, via argcomplete) ───────────────────────────────
# The installer handles files + PATH (machine scope, elevated). Tab completion
# belongs in YOUR user profile, so it is configured here in the user context.
$completionBlock = @'
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
    $parts = $rawLine -split '\s+' | Where-Object { $_ -ne '' }
    $endsWithSpace = $rawLine -match '\s$'
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
        if ($displayText -match '[\s,]') {
            $completion = "'" + ($displayText -replace "'", "''") + "'"
            if ($display.EndsWith(' ')) {
                $completion = $completion + ' '
            }
        }
        [System.Management.Automation.CompletionResult]::new($completion, $displayText, "ParameterValue", $displayText)
    }
    Remove-Item $completion_file, Env:\_ARGCOMPLETE_STDOUT_FILENAME, Env:\ARGCOMPLETE_USE_TEMPFILES, Env:\COMP_LINE, Env:\COMP_POINT, Env:\_ARGCOMPLETE, Env:\_ARGCOMPLETE_SUPPRESS_SPACE, Env:\_ARGCOMPLETE_IFS, Env:\_ARGCOMPLETE_SHELL
}

# Show completion menu instead of cycling (added by proliant)
if (-not (Get-PSReadLineKeyHandler | Where-Object { $_.Key -eq 'Tab' -and $_.Function -eq 'MenuComplete' })) {
    Set-PSReadLineKeyHandler -Key Tab -Function MenuComplete
}
# <<< proliant tab completion <<<
'@

try {
    $profilePath = $PROFILE.CurrentUserAllHosts
    if (-not $profilePath) {
        $profileFolder = if ($PSVersionTable.PSEdition -eq 'Core') { 'PowerShell' } else { 'WindowsPowerShell' }
        $profilePath = Join-Path ([Environment]::GetFolderPath('MyDocuments')) "$profileFolder\profile.ps1"
    }
    $profileDir = Split-Path -Parent $profilePath
    if (-not (Test-Path $profileDir)) {
        New-Item -ItemType Directory -Path $profileDir -Force | Out-Null
    }
    $existing = if (Test-Path $profilePath) { (Get-Content $profilePath -Raw) -as [string] } else { "" }
    if (-not $existing) { $existing = "" }
    # Strip any previous proliant completion block, then append the current one.
    # Older versions used a single "# proliant tab completion (added by
    # proliant)" comment marker with no unambiguous end marker, which only
    # matched through the FIRST top-level closing brace and left the trailing
    # "Show completion menu" if-block behind on every reinstall -- causing it
    # to accumulate duplicate copies over time. The current format wraps the
    # whole block in explicit >>> / <<< markers so removal is unambiguous; we
    # still also strip the legacy formats for anyone upgrading from an older
    # install.
    $cleaned = [System.Text.RegularExpressions.Regex]::Replace(
        $existing,
        '(?s)\r?\n?# >>> proliant tab completion >>>.*?# <<< proliant tab completion <<<\r?\n?',
        ''
    )
    $cleaned = [System.Text.RegularExpressions.Regex]::Replace(
        $cleaned,
        '(?ms)^# proliant tab completion \(added by proliant\).*?^\}\s*',
        ''
    )
    $cleaned = [System.Text.RegularExpressions.Regex]::Replace(
        $cleaned,
        '(?s)\r?\n?# Show completion menu instead of cycling \(added by proliant\)\s*\r?\n?if \(-not \(Get-PSReadLineKeyHandler.*?\r?\n\}\r?\n?',
        ''
    ).TrimEnd()
    $newContent = if ($cleaned) { "$cleaned`r`n`r`n$completionBlock`r`n" } else { "$completionBlock`r`n" }
    Set-Content -Path $profilePath -Value $newContent -Encoding UTF8
} catch {
    # silently skip if profile write fails
}

# Ensure the profile (and tab completion) can actually load on fresh Windows,
# where the default execution policy (Restricted) silently blocks $PROFILE.
$policy = Get-ExecutionPolicy -Scope CurrentUser
if ($policy -in @('Undefined', 'Restricted')) {
    try {
        Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser -Force
        Write-Host ""
        Write-Host "  Set PowerShell execution policy to RemoteSigned (CurrentUser)" -ForegroundColor Yellow
        Write-Host "  so your profile and tab completion can load." -ForegroundColor Yellow
    } catch {
        Write-Host ""
        Write-Host "  Tab completion needs your profile to load. Run this once:" -ForegroundColor Yellow
        Write-Host "    Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser" -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "Getting started:" -ForegroundColor Cyan
Write-Host "  Open a new terminal (or run '. `$PROFILE' in this one) so PATH and" -ForegroundColor Cyan
Write-Host "  tab completion take effect, then:" -ForegroundColor Cyan
Write-Host "    proliant --help       View all commands" -ForegroundColor Cyan
Write-Host "    proliant ilo init     Create a starter inventory.ini" -ForegroundColor Cyan
Write-Host ""

