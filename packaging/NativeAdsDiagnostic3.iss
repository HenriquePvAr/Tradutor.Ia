; Side-by-side diagnostic installer. Never upgrades the official Yomu install.
#ifndef BundleRoot
  #error BundleRoot is required
#endif
#ifndef OutputDir
  #error OutputDir is required
#endif

[Setup]
AppId={{B8A5B0F2-2F29-4B95-9E14-4D3E9B9D3D31}
AppName=Yomu Sekai - Native Ads Diagnostic 3
AppVersion=0.9.0-beta.44
DefaultDirName={localappdata}\Programs\YomuSekai-NativeAdsD3
DefaultGroupName=Yomu Sekai - Native Ads D3
PrivilegesRequired=lowest
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
OutputDir={#OutputDir}
OutputBaseFilename=YomuSekai-NativeAds-Diagnostic3-Setup-x64
Compression=lzma2
SolidCompression=yes
CloseApplications=no
DisableProgramGroupPage=yes
Uninstallable=yes
ChangesEnvironment=no
CreateUninstallRegKey=yes
UninstallDisplayName=Yomu Sekai - Native Ads Diagnostic 3

[Files]
Source: "{#BundleRoot}\YomuSekai\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\Yomu Sekai - Native Ads D3"; Filename: "{app}\YomuSekai-NativeAdsD3.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\Yomu Sekai - Native Ads D3"; Filename: "{app}\YomuSekai-NativeAdsD3.exe"; WorkingDir: "{app}"

[Run]
Filename: "{app}\YomuSekai-NativeAdsD3.exe"; Description: "Iniciar Yomu Sekai - Native Ads D3"; Flags: postinstall skipifsilent nowait
