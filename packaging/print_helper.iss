; Inno Setup script for SehatDesk-Print-Setup-<version>.exe, installed on every PC that prints,
; including the server PC. Run through packaging/build_windows.ps1.

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

[Setup]
; Never change AppId: it is what makes a new installer update the existing install.
AppId={{B2E7A4D1-6C38-4F5A-8E21-7A9D3C10F642}
AppName=SehatDesk Print
AppVersion={#AppVersion}
AppPublisher=SehatDesk
DefaultDirName={autopf}\SehatDesk Print
DefaultGroupName=SehatDesk
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=SehatDesk-Print-Setup-{#AppVersion}
SetupIconFile=sehat_desk.ico
UninstallDisplayIcon={app}\sehat_print.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
AppMutex=SehatDeskPrintMutex
CloseApplications=force
RestartApplications=no

[InstallDelete]
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\dist\sehat_print\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\SehatDesk Print"; Filename: "{app}\sehat_print.exe"
Name: "{commonstartup}\SehatDesk Print"; Filename: "{app}\sehat_print.exe"

[Run]
Filename: "{app}\sehat_print.exe"; Description: "Start SehatDesk Print now"; Flags: nowait postinstall skipifsilent runasoriginaluser

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM sehat_print.exe"; Flags: runhidden; RunOnceId: "stophelper"
