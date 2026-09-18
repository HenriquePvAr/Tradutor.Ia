; Side-by-side diagnostic installer for bounded native ad geometry updates.
#ifndef BundleRoot
  #error BundleRoot is required
#endif
#ifndef OutputDir
  #error OutputDir is required
#endif
[Setup]
AppId={{D4G-EVENT-STORM-FIX-20260915}
AppName=Yomu Sekai - Native Ads Event Storm Fix
AppVersion=0.9.0-beta.44
DefaultDirName={localappdata}\Programs\YomuSekai-NativeAdsEventStormFix
DefaultGroupName=Yomu Sekai - Native Ads Event Storm Fix
PrivilegesRequired=lowest
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
OutputDir={#OutputDir}
OutputBaseFilename=YomuSekai-NativeAds-EventStormFix-Setup-x64
Compression=lzma2
SolidCompression=yes
CloseApplications=no
DisableProgramGroupPage=yes
Uninstallable=yes
ChangesEnvironment=no
CreateUninstallRegKey=yes
[Files]
Source: "{#BundleRoot}\YomuSekai\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
[Icons]
Name: "{group}\Yomu Sekai - Native Ads Event Storm Fix"; Filename: "{app}\YomuSekai.exe"; WorkingDir: "{app}"
[Run]
Filename: "{app}\YomuSekai.exe"; Description: "Iniciar Yomu Sekai - Native Ads Event Storm Fix"; Flags: postinstall skipifsilent nowait
