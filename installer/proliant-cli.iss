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
; in-place update (e.g. launched by `proliant update`).
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

[Code]
const
  EnvKey = 'SYSTEM\CurrentControlSet\Control\Session Manager\Environment';

function PathListContains(const PathList, Dir: string): Boolean;
begin
  Result := Pos(
    ';' + Uppercase(Dir) + ';',
    ';' + Uppercase(PathList) + ';') > 0;
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
begin
  if CurStep = ssPostInstall then
    AddToSystemPath();
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    RemoveFromSystemPath();
end;
