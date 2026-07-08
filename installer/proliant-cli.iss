; Inno Setup script for proliant-cli (Windows).
;
; Packages the Nuitka --standalone output folder into a single GUI installer
; that installs machine-wide into "C:\Program Files\proliant-cli\", adds that
; folder to the system PATH, and registers an Add/Remove Programs entry with a
; proper uninstaller.
;
; Build (in CI):
;   iscc /DMyAppVersion=1.2.3 /DSourceDir=dist\proliant-cli installer\proliant-cli.iss
;
; The typed command stays "proliant" (proliant.exe); the product/identity name
; is "proliant-cli" everywhere a directory or product name is shown.

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "dist\proliant-cli"
#endif

#define MyAppName "proliant-cli"
#define MyAppPublisher "HPE"
#define MyAppExeName "proliant.exe"
#define MyAppURL "https://github.com/hjma29/proliant-cli"

[Setup]
AppId={{7C4B2E9A-3F1D-4A2C-9E7B-9A1F2D6C4B10}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}/releases
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; Machine-wide install -> requires elevation (single UAC prompt).
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64compatible
OutputBaseFilename=proliant-cli-windows-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; We manage PATH ourselves in [Code]; ChangesEnvironment=yes makes Setup
; broadcast WM_SETTINGCHANGE so new terminals pick up PATH without a reboot.
ChangesEnvironment=yes
; Close & restart the app automatically if proliant.exe is running during an
; in-place update (e.g. launched by `proliant version`'s upgrade flow).
CloseApplications=yes
RestartApplications=no
UninstallDisplayName={#MyAppName} {#MyAppVersion}
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
; Copy the entire Nuitka standalone folder (exe + DLLs + compiled modules).
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"

[Run]
; Adds a checked-by-default checkbox to the Finished wizard page so users can
; jump straight into a shell instead of having to go find/open one themselves.
; Prefers Windows Terminal (if installed) over plain powershell.exe. Skipped
; entirely for silent installs (e.g. `proliant version`'s background
; self-update) -- postinstall/Finished-page entries never appear there anyway,
; but skipifsilent is added defensively to match the rest of this file.
Filename: "{code:GetTerminalExe}"; Description: "Launch a new terminal"; Flags: postinstall skipifsilent nowait

[Code]
const
  EnvKey = 'SYSTEM\CurrentControlSet\Control\Session Manager\Environment';

{ Only used to refresh Setup.exe's own in-memory environment block (see
  RefreshProcessPath below) -- has nothing to do with the registry writes,
  which still go through RegWriteStringValue. }
function SetEnvironmentVariable(lpName, lpValue: string): BOOL;
  external 'SetEnvironmentVariableW@kernel32.dll stdcall';

function PathListContains(const PathList, Dir: string): Boolean;
begin
  Result := Pos(
    ';' + Uppercase(Dir) + ';',
    ';' + Uppercase(PathList) + ';') > 0;
end;

function GetTerminalExe(Param: string): string;
var
  WTPath: string;
begin
  { Windows Terminal isn't preinstalled everywhere (e.g. Server Core / older
    Server images without it from the Store) -- fall back to the PowerShell
    that ships with every Windows install when it's not present. }
  WTPath := ExpandConstant('{localappdata}\Microsoft\WindowsApps\wt.exe');
  if FileExists(WTPath) then
    Result := WTPath
  else
    Result := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
end;

procedure AddToSystemPath();
var
  Paths: string;
  AppDir: string;
begin
  AppDir := ExpandConstant('{app}');
  if not RegQueryStringValue(HKLM, EnvKey, 'Path', Paths) then
    Paths := '';
  if PathListContains(Paths, AppDir) then
    exit;
  if (Paths <> '') and (Paths[Length(Paths)] <> ';') then
    Paths := Paths + ';';
  Paths := Paths + AppDir;
  RegWriteStringValue(HKLM, EnvKey, 'Path', Paths);
end;

procedure RefreshProcessPath(const AppDir: string);
var
  CurrentPath: string;
begin
  { ChangesEnvironment=yes broadcasts WM_SETTINGCHANGE so *other* already-
    running processes (like Explorer) refresh their own cached environment
    and pass the new PATH on to whatever they spawn next -- that's why a
    manually-opened new terminal picks up "proliant" right away. But
    Setup.exe's own process environment block was captured when it started,
    before the registry PATH was updated, and that broadcast does NOT
    retroactively rewrite it. The [Run] "Launch a new terminal" entry is
    spawned as a child of this very process, so without this it inherits
    the stale pre-install PATH and "proliant" appears missing until the
    user closes that window and opens a fresh one. Updating our own
    in-memory copy here fixes it for the auto-launched terminal too. }
  CurrentPath := GetEnv('Path');
  if not PathListContains(CurrentPath, AppDir) then
  begin
    if (CurrentPath <> '') and (CurrentPath[Length(CurrentPath)] <> ';') then
      CurrentPath := CurrentPath + ';';
    CurrentPath := CurrentPath + AppDir;
    SetEnvironmentVariable('Path', CurrentPath);
  end;
end;

procedure RemoveFromSystemPath();
var
  Paths: string;
  AppDir: string;
begin
  AppDir := ExpandConstant('{app}');
  if not RegQueryStringValue(HKLM, EnvKey, 'Path', Paths) then
    exit;
  if not PathListContains(Paths, AppDir) then
    exit;
  { Rebuild PATH without AppDir (case-insensitive, exact segment match). }
  Paths := ';' + Paths + ';';
  StringChangeEx(Paths, ';' + AppDir + ';', ';', True);
  { Strip the leading/trailing sentinel semicolons. }
  if (Length(Paths) >= 2) then
    Paths := Copy(Paths, 2, Length(Paths) - 2);
  RegWriteStringValue(HKLM, EnvKey, 'Path', Paths);
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  DoneMsg: string;
  ResultCode: Integer;
begin
  if CurStep = ssPostInstall then
  begin
    AddToSystemPath();
    RefreshProcessPath(ExpandConstant('{app}'));
    { proliant.exe wires up PowerShell tab completion (writes into $PROFILE)
      the first time it actually runs -- see _windows_first_run_check() in
      cli.py. Left alone, that means completion stays broken until the user
      happens to run some proliant command first, which they usually don't
      know to do (this is exactly what a real user hit: fresh install ->
      new PowerShell window -> tab-complete -> nothing, because nothing had
      ever invoked proliant.exe yet). Trigger that one-time setup ourselves,
      right now, so completion is already working the first time the user
      opens a terminal -- no extra step required. Best-effort: this runs
      elevated (as whichever account approved the UAC prompt -- normally the
      same signed-in user, just with an elevated token, so $PROFILE/%APPDATA%
      still resolve correctly); if it's ever a genuinely different account,
      or this fails for any reason, the ordinary first-run path in
      proliant.exe still covers it later for the real user -- so it's safe
      to ignore failures here. }
    Exec(ExpandConstant('{app}\proliant.exe'), '-h', '', SW_HIDE,
      ewWaitUntilTerminated, ResultCode);
  end;
  if CurStep = ssDone then
  begin
    { The standard Inno "Finished!" wizard page (interactive installs) says
      nothing about proliant-cli itself, and 'proliant version' (and any other
      silent invocation) runs with /SILENT, which shows only a progress bar
      and no wizard pages at all -- the installer would otherwise just
      disappear with zero indication of where it went. Show one explicit,
      minimal message in both cases. }
    DoneMsg := 'proliant-cli {#MyAppVersion} installed successfully.' + #13#10#13#10;
    DoneMsg := DoneMsg + 'Location: ' + ExpandConstant('{app}');
    MsgBox(DoneMsg, mbInformation, MB_OK);
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    RemoveFromSystemPath();
end;
