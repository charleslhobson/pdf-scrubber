; Inno Setup script: turns dist\PDF Scrubber\ into a one-file Windows installer.
;   ISCC.exe /DAppVersion=2.0 packaging\installer.iss
; Installs for the current user only, so no administrator password is needed.

#ifndef AppVersion
  #define AppVersion "0.0"
#endif

[Setup]
AppId={{6F2B8C4E-3D1A-4B7E-9C55-0A8E2D7F41B3}
AppName=PDF Scrubber
AppVersion={#AppVersion}
DefaultDirName={autopf}\PDF Scrubber
DefaultGroupName=PDF Scrubber
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=PDF-Scrubber-{#AppVersion}-windows-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName=PDF Scrubber

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Files]
Source: "..\dist\PDF Scrubber\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{autoprograms}\PDF Scrubber"; Filename: "{app}\PDF Scrubber.exe"
Name: "{autodesktop}\PDF Scrubber"; Filename: "{app}\PDF Scrubber.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\PDF Scrubber.exe"; Description: "Open PDF Scrubber"; Flags: nowait postinstall skipifsilent
