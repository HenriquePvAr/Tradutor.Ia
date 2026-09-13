; Tradutor.Ia Beta installer — 14D.2 frozen contract.
; BundleRoot is mandatory and must be supplied by the controlled build command.
#ifndef BundleRoot
  #error BundleRoot must point to the validated 14B.2 ONEDIR directory
#endif
#ifndef OutputDir
  #error OutputDir must point to the dedicated local installer artifact directory
#endif
#ifndef ProductVersion
  #error ProductVersion must be supplied from app_version.BUILD_VERSION by the release tool
#endif

[Setup]
AppId={{cff6c710-2b7d-4a09-8f25-e32ea363430f}
AppName=Yomu Sekai
AppVersion={#ProductVersion}
SetupIconFile={#SourcePath}\..\assets\branding\generated\yomu-sekai.ico
DefaultDirName={localappdata}\Programs\YomuSekai
DefaultGroupName=Yomu Sekai
PrivilegesRequired=lowest
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
OutputDir={#OutputDir}
OutputBaseFilename=YomuSekai-{#ProductVersion}-Setup-x64
Compression=lzma2
SolidCompression=yes
CloseApplications=yes
RestartApplications=no
DisableProgramGroupPage=yes
Uninstallable=yes
ChangesEnvironment=no
CreateUninstallRegKey=yes
UninstallDisplayName=Yomu Sekai
SetupLogging=yes
UninstallFilesDir={app}

[Languages]
Name: "brazilianportuguese"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"

[Tasks]
Name: "desktopicon"; Description: "Criar atalho na área de trabalho"; GroupDescription: "Atalhos:"; Flags: unchecked

[Files]
; BundleRoot is the PyInstaller candidate root; copy the ONEDIR contents, not its
; wrapper directory, so the executable lands at {app}\YomuSekai.exe.
Source: "{#BundleRoot}\YomuSekai\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

; Remove only disposable per-user execution state.  The device identity is deliberately
; stored under %LOCALAPPDATA%\YomuSekai\device-identity.bin and is not covered here, so
; reinstalling the app does not silently register a second device.
[UninstallDelete]
Type: filesandordirs; Name: "{localappdata}\TradutorIA\runtime\jobs.sqlite3"
Type: filesandordirs; Name: "{localappdata}\TradutorIA\runtime\ui_history.json"
Type: filesandordirs; Name: "{localappdata}\TradutorIA\runtime\ui_hidden_history.json"
Type: filesandordirs; Name: "{localappdata}\TradutorIA\runtime\logs"
Type: filesandordirs; Name: "{localappdata}\TradutorIA\runtime\cache"
Type: filesandordirs; Name: "{localappdata}\TradutorIA\runtime\output"
Type: filesandordirs; Name: "{localappdata}\TradutorIA\runtime\temp"
Type: filesandordirs; Name: "{localappdata}\TradutorIA\cache"
Type: filesandordirs; Name: "{localappdata}\TradutorIA\output"
Type: filesandordirs; Name: "{localappdata}\TradutorIA\temp"
; Legacy beta layouts that could otherwise resurrect RETOMAR cards after reinstall.
Type: filesandordirs; Name: "{localappdata}\TradutorIA\jobs.sqlite3"
Type: filesandordirs; Name: "{localappdata}\TradutorIA\ui_history.json"
Type: filesandordirs; Name: "{localappdata}\TradutorIA\ui_hidden_history.json"
Type: filesandordirs; Name: "{localappdata}\TradutorIA\logs"
Type: filesandordirs; Name: "{localappdata}\TradutorIA\.cache"

; Close only the product's own executable tree before Inno removes {app}.  This
; covers the desktop shell plus its frozen UI/worker children and prevents an
; orphaned YomuSekai.exe from keeping the install directory alive.
[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /T /IM YomuSekai.exe"; Flags: runhidden waituntilterminated skipifdoesntexist; RunOnceId: "YomuSekaiShutdown"

[Icons]
Name: "{group}\Yomu Sekai"; Filename: "{app}\YomuSekai.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\Yomu Sekai"; Filename: "{app}\YomuSekai.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\YomuSekai.exe"; Description: "Iniciar o Yomu Sekai"; Flags: postinstall skipifsilent nowait
