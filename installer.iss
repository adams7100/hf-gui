; Inno Setup script for HF-Downloader.
;
; Packages the PyInstaller output (built by build-exe.cmd):
;   dist\HF-Downloader\      the Qt 6 window, HF-Downloader.exe plus _internal\
;   dist\hffinish.exe        the command-line tool, one file
; into a Windows installer with a Start Menu entry, an optional desktop icon
; and the install folder on PATH (for `hffinish`). Installs per user by default
; (no admin prompt); the user can pick "all users" on the first page.
;
; Build with build-installer.cmd, or by hand:
;   ISCC.exe installer.iss          -> dist\HF-Downloader-setup-<version>.exe

#ifndef AppVersion
  #define AppVersion "1.2.0"
#endif
#define AppName "HF-Downloader"
#define AppExe "HF-Downloader.exe"
#define CliExe "hffinish.exe"
; AppId of the installer before it was renamed from "hffinish". An existing
; install under that id is uninstalled first (see PrepareToInstall) so the
; machine does not end up with two copies and two PATH entries.
#define OldAppId "{B7E2C1D4-5F6A-4B8C-9D0E-1F2A3B4C5D6E}"

[Setup]
AppId={{6F0C2A7B-3D41-4C9E-8B5A-2E7D1F9C4A38}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Devin Adams
AppPublisherURL=https://github.com/adams7100/hf-gui
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
LicenseFile=LICENSE
SetupIconFile=assets\hf-downloader.ico
OutputDir=dist
OutputBaseFilename={#AppName}-setup-{#AppVersion}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ChangesEnvironment=yes
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
MinVersion=10.0

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked
Name: "addtopath"; Description: "Add the hffinish command to the PATH (run it from any terminal)"; GroupDescription: "Command line:"

[InstallDelete]
; a PyInstaller folder build: drop the previous version's support files so
; nothing stale is left next to the new ones
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "dist\HF-Downloader\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "dist\{#CliExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; Comment: "Download Hugging Face models and move them out of the cache"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; Tasks: desktopicon

[Registry]
; Per-user install writes HKCU\Environment, all-users install writes the
; system environment key. NeedsAddPath avoids duplicate entries.
Root: HKA; Subkey: "{code:EnvKey}"; ValueType: expandsz; ValueName: "Path"; \
    ValueData: "{olddata};{app}"; Tasks: addtopath; Check: NeedsAddPath(ExpandConstant('{app}'))

[Run]
Filename: "{app}\{#CliExe}"; Parameters: "--help"; Flags: runhidden; StatusMsg: "Checking the installed program..."
Filename: "{app}\{#AppExe}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; nothing to run; PATH cleanup happens in CurUninstallStepChanged below

[Code]
function EnvKey(Param: string): string;
begin
  if IsAdminInstallMode then
    Result := 'SYSTEM\CurrentControlSet\Control\Session Manager\Environment'
  else
    Result := 'Environment';
end;

function RootKey: Integer;
begin
  if IsAdminInstallMode then
    Result := HKLM
  else
    Result := HKCU;
end;

function NeedsAddPath(Dir: string): Boolean;
var
  Path: string;
begin
  if not RegQueryStringValue(RootKey, EnvKey(''), 'Path', Path) then
  begin
    Result := True;
    exit;
  end;
  { Surround with semicolons so a prefix of another entry does not match. }
  Result := Pos(';' + Lowercase(Dir) + ';', ';' + Lowercase(Path) + ';') = 0;
end;

procedure RemoveFromPath(Dir: string);
var
  Path, Lower, Needle: string;
  P: Integer;
begin
  if not RegQueryStringValue(RootKey, EnvKey(''), 'Path', Path) then
    exit;
  Lower := ';' + Lowercase(Path) + ';';
  Needle := ';' + Lowercase(Dir) + ';';
  P := Pos(Needle, Lower);
  if P = 0 then
    exit;
  { P is 1-based into the padded string; the entry in Path starts at P-1
    (0-based) and spans Length(Dir) chars plus one separator. }
  Path := ';' + Path + ';';
  Delete(Path, P, Length(Dir) + 1);
  { Strip the padding again. }
  if (Length(Path) > 0) and (Path[1] = ';') then
    Delete(Path, 1, 1);
  if (Length(Path) > 0) and (Path[Length(Path)] = ';') then
    Delete(Path, Length(Path), 1);
  RegWriteExpandStringValue(RootKey, EnvKey(''), 'Path', Path);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    RemoveFromPath(ExpandConstant('{app}'));
end;

{ ---- removal of the install made under the old name "hffinish" ---- }

function OldUninstallString(var Cmd: string): Boolean;
var
  Key: string;
begin
  Key := 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{#OldAppId}_is1';
  Result := RegQueryStringValue(HKCU, Key, 'UninstallString', Cmd)
         or RegQueryStringValue(HKLM, Key, 'UninstallString', Cmd)
         or RegQueryStringValue(HKLM32, Key, 'UninstallString', Cmd);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Cmd: string;
  ResultCode: Integer;
begin
  Result := '';
  if not OldUninstallString(Cmd) then
    exit;
  Log('Removing the previous "hffinish" install: ' + Cmd);
  { The old uninstaller also drops its own folder from PATH. }
  if not Exec(RemoveQuotes(Cmd), '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART', '',
              SW_HIDE, ewWaitUntilTerminated, ResultCode) then
    Log('Could not run the previous uninstaller, continuing anyway.')
  else
    Log('Previous uninstaller exit code: ' + IntToStr(ResultCode));
end;
