# proliant-cli Windows installer
# Usage: irm https://raw.githubusercontent.com/hjma29/proliant-cli/main/install.ps1 | iex

$ErrorActionPreference = "Stop"
$Repo = "hjma29/proliant-cli"
$BinName = "proliant.exe"
$InstallDir = "$env:USERPROFILE\bin"

Write-Host ""
Write-Host "proliant-cli installer" -ForegroundColor Cyan
Write-Host "══════════════════════════════════════" -ForegroundColor Cyan

# Resolve latest release download URL
Write-Host "Fetching latest release..." -ForegroundColor Gray
$release = Invoke-RestMethod "https://api.github.com/repos/$Repo/releases/latest"
$asset = $release.assets | Where-Object { $_.name -eq "proliant-cli-windows.exe" } | Select-Object -First 1

if (-not $asset) {
    Write-Error "Could not find proliant-cli-windows.exe in latest release."
    exit 1
}

$version = $release.tag_name
$url = $asset.browser_download_url
Write-Host "Downloading $version..." -ForegroundColor Gray

# Install to ~/bin
if (-not (Test-Path $InstallDir)) {
    New-Item -ItemType Directory -Path $InstallDir | Out-Null
}

$dest = Join-Path $InstallDir $BinName
Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing

# Add ~/bin to PATH if not already present
$userPath = [Environment]::GetEnvironmentVariable("PATH", "User")
# Remove any existing occurrence of InstallDir, then prepend it so a freshly
# installed proliant always wins over stale copies elsewhere on PATH.
$entries = ($userPath -split ';') | Where-Object { $_ -and ($_.TrimEnd('\') -ne $InstallDir.TrimEnd('\')) }
$newPath = (@($InstallDir) + $entries) -join ';'
if ($newPath -ne $userPath) {
    [Environment]::SetEnvironmentVariable("PATH", $newPath, "User")
    Write-Host ""
    Write-Host "  Added $InstallDir to the front of your PATH." -ForegroundColor Yellow
    Write-Host "  Restart your terminal (or open a new PowerShell window) to use 'proliant'." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "  Installed: $dest" -ForegroundColor Green
Write-Host "  Version:   $version" -ForegroundColor Green

# ── Tab completion (dynamic, via argcomplete) ───────────────────────────────
$completionBlock = @'
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
    Remove-Item $completion_file, Env:\_ARGCOMPLETE_STDOUT_FILENAME, Env:\ARGCOMPLETE_USE_TEMPFILES, Env:\COMP_LINE, Env:\COMP_POINT, Env:\_ARGCOMPLETE, Env:\_ARGCOMPLETE_SUPPRESS_SPACE, Env:\_ARGCOMPLETE_IFS, Env:\_ARGCOMPLETE_SHELL
}

# Show completion menu instead of cycling (added by proliant)
if (-not (Get-PSReadLineKeyHandler | Where-Object { $_.Key -eq 'Tab' -and $_.Function -eq 'MenuComplete' })) {
    Set-PSReadLineKeyHandler -Key Tab -Function MenuComplete
}
'@

try {
    $profilePath = $PROFILE.CurrentUserAllHosts
    $profileDir = Split-Path -Parent $profilePath
    if (-not (Test-Path $profileDir)) {
        New-Item -ItemType Directory -Path $profileDir -Force | Out-Null
    }
    $existing = if (Test-Path $profilePath) { (Get-Content $profilePath -Raw) -as [string] } else { "" }
    if (-not $existing) { $existing = "" }
    # Strip any previous proliant completion block, then append the current one.
    $cleaned = [System.Text.RegularExpressions.Regex]::Replace(
        $existing,
        '(?ms)^# proliant tab completion \(added by proliant\).*?^\}\s*',
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
Write-Host "Run 'proliant --version' in a new terminal to verify." -ForegroundColor Cyan
Write-Host ""

