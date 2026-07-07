<#
.SYNOPSIS
    Automated test of the proliant Windows EXE inside the Win11-Sandbox Hyper-V VM.

.DESCRIPTION
    Reverts the VM to a clean checkpoint, copies the freshly built EXE into the
    guest over PowerShell Direct, runs the first-run installer, then reports the
    install side-effects (PATH, profile, execution policy, version) so you don't
    have to log into the VM and double-click by hand every time.

    PowerShell Direct (Invoke-Command -VMName) needs NO networking inside the
    guest -- it talks host->guest through the hypervisor. It does require:
      * Host: membership in "Hyper-V Administrators" (or an elevated shell).
      * Guest: the local account credentials for Win11-Sandbox.

    NOTE: This exercises the *terminal-launch* install path (PATH prompt, profile
    write, execution-policy set). The Explorer *double-click* path relies on
    console-process detection that cannot be reproduced headlessly, so spot-check
    that one manually after a release.

.PARAMETER ExePath
    Path on the host to the EXE under test. Defaults to the file you downloaded
    from the GitHub release into your Downloads folder.

.PARAMETER NoRevert
    Skip reverting to the clean checkpoint (test against current guest state).

.EXAMPLE
    ./tools/Test-WinExe.ps1
    # uses ~/Downloads/proliant-cli-windows.exe and reverts to Fresh-Windows first

.EXAMPLE
    ./tools/Test-WinExe.ps1 -ExePath .\dist\proliant.exe -NoRevert
#>
[CmdletBinding()]
param(
    [string]$VMName     = "Win11-Sandbox",
    [string]$Checkpoint = "Fresh-Windows",
    [string]$ExePath    = "$env:USERPROFILE\Downloads\proliant-cli-windows.exe",
    [string]$GuestUser  = "USER",
    [pscredential]$Credential,
    [switch]$NoRevert
)

$ErrorActionPreference = "Stop"
Import-Module Hyper-V -ErrorAction Stop

if (-not (Test-Path $ExePath)) { throw "EXE not found: $ExePath" }
$ExePath = (Resolve-Path $ExePath).Path
Write-Host "EXE under test : $ExePath" -ForegroundColor Cyan

# Guest credentials -- you type the password directly into this prompt.
if (-not $Credential) {
    $Credential = Get-Credential -UserName $GuestUser -Message "Win11-Sandbox guest login"
}

# 1. Revert to the clean checkpoint so every run starts from a pristine box.
if (-not $NoRevert) {
    Write-Host "Reverting '$VMName' to checkpoint '$Checkpoint'..." -ForegroundColor Cyan
    Restore-VMSnapshot -VMName $VMName -Name $Checkpoint -Confirm:$false
}

# 2. Make sure the VM is running.
if ((Get-VM -Name $VMName).State -ne 'Running') {
    Write-Host "Starting '$VMName'..." -ForegroundColor Cyan
    Start-VM -Name $VMName
}

# 3. Wait until PowerShell Direct accepts a connection (guest finished booting).
Write-Host "Waiting for guest to accept PowerShell Direct..." -ForegroundColor Cyan
$ready    = $false
$deadline = (Get-Date).AddMinutes(5)
do {
    Start-Sleep -Seconds 5
    try {
        Invoke-Command -VMName $VMName -Credential $Credential -ScriptBlock { $true } -ErrorAction Stop | Out-Null
        $ready = $true
    } catch { }
} until ($ready -or (Get-Date) -gt $deadline)
if (-not $ready) { throw "Guest never became ready for PowerShell Direct (5 min timeout)." }

# 4. Copy the EXE into the guest over the session.
$sess = New-PSSession -VMName $VMName -Credential $Credential
try {
    $dest = "C:\Users\$GuestUser\Downloads\proliant-cli-windows.exe"
    Invoke-Command -Session $sess -ScriptBlock {
        param($d) New-Item -ItemType Directory -Force -Path (Split-Path $d) | Out-Null
    } -ArgumentList $dest
    Copy-Item -Path $ExePath -Destination $dest -ToSession $sess -Force
    Write-Host "Copied EXE to guest: $dest" -ForegroundColor Green

    # 5. Run the first-run installer (terminal path) and capture the side-effects.
    #    Feeding "y" answers the "add to PATH?" prompt so setup actually runs.
    $result = Invoke-Command -Session $sess -ScriptBlock {
        param($exe, $user)
        $version = ("y`n" | & $exe version 2>&1 | Out-String).Trim()

        $userPath          = [Environment]::GetEnvironmentVariable("Path", "User")
        $exeDir            = Split-Path $exe
        $profilePath       = "C:\Users\$user\Documents\PowerShell\Microsoft.PowerShell_profile.ps1"
        $profileExists     = Test-Path $profilePath
        $profileHasProliant = $profileExists -and
            (Select-String -Path $profilePath -Pattern "proliant" -Quiet)

        [pscustomobject]@{
            Version            = $version
            UserPathHasExeDir  = [bool]($userPath -match [regex]::Escape($exeDir))
            ProfileExists      = $profileExists
            ProfileHasProliant = [bool]$profileHasProliant
            ExecutionPolicy    = (Get-ExecutionPolicy -Scope CurrentUser).ToString()
        }
    } -ArgumentList $dest, $GuestUser

    Write-Host "`n===== Install verification =====" -ForegroundColor Yellow
    $result | Format-List

    # Quick pass/fail summary.
    $ok = $result.UserPathHasExeDir -and $result.ProfileHasProliant -and
          ($result.ExecutionPolicy -in 'RemoteSigned','Unrestricted','Bypass')
    if ($ok) { Write-Host "RESULT: PASS" -ForegroundColor Green }
    else     { Write-Host "RESULT: CHECK ABOVE (one or more steps did not apply)" -ForegroundColor Red }
}
finally {
    Remove-PSSession $sess
}
