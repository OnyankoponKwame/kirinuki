; Inno Setup script for Kirinuki. Run build.ps1 first to stage dist\Kirinuki\app,
; then compile this with Inno Setup (ISCC.exe) to produce dist\KirinukiSetup.exe.
; See .github/workflows/build-windows-installer.yml for the automated version.

#define MyAppName "Kirinuki"
#define MyAppVersion "0.1.5"

[Setup]
AppId={{A1B2C3D4-E5F6-4A7B-8C9D-0123456789AB}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName={localappdata}\{#MyAppName}\app
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\..\dist
OutputBaseFilename=KirinukiSetup
Compression=lzma2
SolidCompression=yes
SetupIconFile=icon.ico

[Languages]
Name: "japanese"; MessagesFile: "compiler:Languages\Japanese.isl"

[Tasks]
Name: "desktopicon"; Description: "デスクトップにショートカットを作成する"; GroupDescription: "追加のアイコン:"

[Files]
Source: "..\..\dist\Kirinuki\app\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\launcher.py"""; WorkingDir: "{app}"; IconFilename: "{app}\icon.ico"
Name: "{userdesktop}\{#MyAppName}"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\launcher.py"""; WorkingDir: "{app}"; Tasks: desktopicon; IconFilename: "{app}\icon.ico"

[Run]
Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\launcher.py"""; WorkingDir: "{app}"; Description: "{#MyAppName} を起動する"; Flags: postinstall nowait skipifsilent
