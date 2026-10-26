; Inno Setup 6 script for RDP Nexus.
; Built by scripts\build.py, which passes /DAppVersion, /DSourceDir and /DOutputDir.
; Per-user install (no administrator rights): {autopf} resolves to %LOCALAPPDATA%\Programs.

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\RDPNexus"
#endif
#ifndef OutputDir
  #define OutputDir "..\release"
#endif

#define AppName "RDP Nexus"
#define AppExe "RDPNexus.exe"
#define AppPublisher "ADevlooper"
#define AppURL "https://github.com/ADevlooper/RDP-Nexus"

[Setup]
AppId={{8F3C2B71-5D7A-4E8B-9C21-6A4D3E2F1B90}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
AppUpdatesURL={#AppURL}/releases
VersionInfoVersion={#AppVersion}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#OutputDir}
OutputBaseFilename=RDPNexus-{#AppVersion}-setup
SetupIconFile=..\assets\app.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; Refuse to install/uninstall underneath a running copy (same mutex the app creates).
AppMutex=RDPNexus.SingleInstance
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "startup"; Description: "Launch {#AppName} when I sign in"; GroupDescription: "Startup:"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; Comment: "RDP-only keyboard and pointer automation"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
; Optional launch at sign-in (the app's own "Launch when I sign in" switch edits the same value).
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "{#AppName}"; ValueData: """{app}\{#AppExe}"" --minimized"; Tasks: startup
; Always remove that value on uninstall, however it was created.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "{#AppName}"; Flags: uninsdeletevalue dontcreatekey

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Log files are app-generated; user settings in %APPDATA%\RDP Nexus are kept.
Type: filesandordirs; Name: "{localappdata}\RDP Nexus\logs"
