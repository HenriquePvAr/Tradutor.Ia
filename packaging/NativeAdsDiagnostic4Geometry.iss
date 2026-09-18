; Side-by-side D4 geometry diagnostic installer.
#ifndef BundleRoot
  #error BundleRoot is required
#endif
#ifndef OutputDir
  #error OutputDir is required
#endif
[Setup]
AppId={{D4G4C42A-9D03-4E62-9E18-2C7E0C7C41D4}
AppName=Yomu Sekai - Native Ads D4 Geometry Fix
AppVersion=0.9.0-beta.44
DefaultDirName={localappdata}\Programs\YomuSekai-NativeAdsD4G
DefaultGroupName=Yomu Sekai - Native Ads D4G
PrivilegesRequired=lowest
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
OutputDir={#OutputDir}
OutputBaseFilename=YomuSekai-NativeAds-Diagnostic4-GeometryFix-Setup-x64
Compression=lzma2
SolidCompression=yes
CloseApplications=no
DisableProgramGroupPage=yes
Uninstallable=yes
ChangesEnvironment=no
CreateUninstallRegKey=yes
UninstallDisplayName=Yomu Sekai - Native Ads D4 Geometry Fix
[Files]
Source: "{#BundleRoot}\YomuSekai\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
[Icons]
Name: "{group}\Yomu Sekai - Native Ads D4G"; Filename: "{app}\YomuSekai-NativeAdsD4G.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\Yomu Sekai - Native Ads D4G"; Filename: "{app}\YomuSekai-NativeAdsD4G.exe"; WorkingDir: "{app}"
[Run]
Filename: "{app}\YomuSekai-NativeAdsD4G.exe"; Description: "Iniciar Yomu Sekai - Native Ads D4G"; Flags: postinstall skipifsilent nowait
