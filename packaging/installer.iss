; Inno Setup script for SehatDesk-Setup-<version>.exe. Run through packaging/build_windows.ps1.
; Only program files live in the install folder. Patient data is in %LOCALAPPDATA%\SehatDesk,
; which neither an update nor an uninstall touches. Setup asks, then removes the installed version
; with its own uninstaller before copying the new one ([Code]).

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

[Setup]
; Never change AppId: it is what makes a new installer update the existing install.
AppId={{6F1B7C52-3A94-4E0B-9C1D-5D2E8A7F4B10}
AppName=SehatDesk
AppVersion={#AppVersion}
AppPublisher=SehatDesk
DefaultDirName={autopf}\SehatDesk
DefaultGroupName=SehatDesk
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=SehatDesk-Setup-{#AppVersion}
SetupIconFile=sehat_desk.ico
UninstallDisplayIcon={app}\sehat_desk.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; The window holds this mutex (launcher/main.py): setup asks staff to close SehatDesk first.
AppMutex=SehatDeskLauncherMutex
CloseApplications=force
RestartApplications=no

[InstallDelete]
; Remove the old version's code, so a deleted module or migration cannot linger after an update.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\dist\sehat_desk\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autodesktop}\SehatDesk"; Filename: "{app}\sehat_desk.exe"
Name: "{group}\SehatDesk"; Filename: "{app}\sehat_desk.exe"
Name: "{group}\Uninstall SehatDesk"; Filename: "{uninstallexe}"

[Run]
; Other computers must reach the web server (TCP), and sehatdesk.local must be answered (UDP 5353).
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""SehatDesk"""; Flags: runhidden
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""SehatDesk"" dir=in action=allow program=""{app}\sehat_desk.exe"" enable=yes profile=any protocol=TCP"; Flags: runhidden
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""SehatDesk"" dir=in action=allow program=""{app}\sehat_desk.exe"" enable=yes profile=any protocol=UDP"; Flags: runhidden
; runasoriginaluser: setup is elevated, but the data folder must be the signed-in user's.
Filename: "{app}\sehat_desk.exe"; Description: "Start SehatDesk now"; Flags: nowait postinstall skipifsilent runasoriginaluser

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM sehat_desk.exe"; Flags: runhidden; RunOnceId: "stopserver"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""SehatDesk"""; Flags: runhidden; RunOnceId: "fwremove"

[Code]
const
  // The registry key Inno Setup writes for AppId, in the 64-bit view (64-bit install mode).
  UninstallKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{6F1B7C52-3A94-4E0B-9C1D-5D2E8A7F4B10}_is1';

function InitializeSetup(): Boolean;
var
  Installed: String;
begin
  Result := True;
  if not RegKeyExists(HKLM64, UninstallKey) then
    Exit;
  if not RegQueryStringValue(HKLM64, UninstallKey, 'DisplayVersion', Installed) then
    Installed := 'an older version';
  Result := SuppressibleMsgBox(
    'SehatDesk ' + Installed + ' is installed on this computer.' + #13#10#13#10 +
    'It will be uninstalled first, then version {#AppVersion} will be installed.' + #13#10 +
    'Patient data and backups are kept.' + #13#10#13#10 + 'Continue?',
    mbConfirmation, MB_YESNO, IDYES) = IDYES;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Uninstaller: String;
  ExitCode, Waited: Integer;
begin
  Result := '';
  if not RegQueryStringValue(HKLM64, UninstallKey, 'UninstallString', Uninstaller) then
    Exit;
  Uninstaller := RemoveQuotes(Uninstaller);
  // Its files are gone already: the new version simply goes on top.
  if not FileExists(Uninstaller) then
    Exit;
  if not Exec(Uninstaller, '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART', '', SW_HIDE,
    ewWaitUntilTerminated, ExitCode) or (ExitCode <> 0) then
  begin
    Result := 'The installed version of SehatDesk could not be removed. Your data was not changed. ' +
      'Close SehatDesk, then run this installer again.';
    Exit;
  end;
  // The uninstaller deletes itself just after it exits.
  Waited := 0;
  while FileExists(Uninstaller) and (Waited < 30) do
  begin
    Sleep(500);
    Waited := Waited + 1;
  end;
end;
