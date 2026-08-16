; ─────────────────────────────────────────────────────────────────────────────
; Inno Setup 6 script — NovelBridge AR
;
; The GitHub Actions workflow passes the version via the CLI:
;   ISCC.exe /DMyAppVersion="v1.0.0" installer\setup.iss
;
; Build locally (after running PyInstaller):
;   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\setup.iss
; ─────────────────────────────────────────────────────────────────────────────

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0-dev"
#endif

#define MyAppName      "NovelBridge AR"
#define MyAppExeName   "NovelBridgeAR.exe"
#define MyAppPublisher "NovelBridge AR"
#define MyAppURL       "https://github.com/Youcef-fareh/novel_bridge-ar"
#define MyAppIcon    "..\icon.ico"

[Setup]
; ── Identity ──────────────────────────────────────────────────────────────
AppId={{B3A7E1F2-9C4D-4E8A-B1F3-2D5A6C7E8F9A}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
AppUpdatesURL={#MyAppURL}/releases

; ── Install paths ──────────────────────────────────────────────────────────
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes

; ── Output ─────────────────────────────────────────────────────────────────
; This path is what the GitHub Actions workflow references as the upload target
OutputDir=Output
OutputBaseFilename=NovelBridgeAR_Setup

; ── Appearance & behavior ──────────────────────────────────────────────────
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}
SetupIconFile={#MyAppIcon}

; ── Minimum OS requirement ─────────────────────────────────────────────────
MinVersion=10.0.0                              ; Windows 10+

; ── Privileges ────────────────────────────────────────────────────────────
PrivilegesRequired=lowest                      ; installs per-user by default
PrivilegesRequiredOverridesAllowed=dialog      ; allow elevation if desired

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; ── Main application (PyInstaller one-folder output) ──────────────────────
; The spec outputs to dist\NovelBridgeAR\
Source: "..\dist\NovelBridgeAR\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; Start Menu shortcut
Name: "{group}\{#MyAppName}";          Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"

; Desktop shortcut (optional, off by default)
Name: "{autodesktop}\{#MyAppName}";    Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
; Offer to launch immediately after install
Filename: "{app}\{#MyAppExeName}"; \
  Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; \
  Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Clean up any runtime-generated files left inside the install dir
Type: filesandordirs; Name: "{app}\data"
Type: filesandordirs; Name: "{app}\output"
