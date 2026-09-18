; Side-by-side Diagnostic4 installer. Authentication remains untouched.
#ifndef BundleRoot
  #error BundleRoot is required
#endif
#ifndef OutputDir
  #error OutputDir is required
#endif
[Setup]
AppId={{D4F4C42A-9D03-4E62-9E18-2C7E0C7C41D4}
AppName=Yomu Sekai - Native Ads Diagnostic 4
AppVersion=0.9.0-beta.44
DefaultDirName={localappdata}\Programs\YomuSekai-NativeAdsD4
DefaultGroupName=Yomu Sekai - Native Ads D4
PrivilegesRequired=lowest
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
OutputDir={#OutputDir}
OutputBaseFilename=YomuSekai-NativeAds-Diagnostic4-Setup-x64
Compression=lzma2
SolidCompression=yes
CloseApplications=no
DisableProgramGroupPage=yes
Uninstallable=yes
ChangesEnvironment=no
CreateUninstallRegKey=yes
UninstallDisplayName=Yomu Sekai - Native Ads Diagnostic 4
[Files]
Source: "{#BundleRoot}\YomuSekai\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
[Icons]
Name: "{group}\Yomu Sekai - Native Ads D4"; Filename: "{app}\YomuSekai-NativeAdsD4.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\Yomu Sekai - Native Ads D4"; Filename: "{app}\YomuSekai-NativeAdsD4.exe"; WorkingDir: "{app}"
[Run]
Filename: "{app}\YomuSekai-NativeAdsD4.exe"; Description: "Iniciar Yomu Sekai - Native Ads D4"; Flags: postinstall skipifsilent nowait
