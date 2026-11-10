; rksubby V3 telepítő (Inno Setup 6). Építés: tools\build_v3.py
#ifndef AppVersion
  #define AppVersion "3.0.0"
#endif
#ifndef SrcDir
  #define SrcDir "..\build\v3\app\rksubby"
#endif
#ifndef OutDir
  #define OutDir "..\dist"
#endif

[Setup]
AppId={{6B1E3C2A-7D41-4E8B-9C3F-52A1D7E0B9F3}
AppName=rksubby V3
AppVersion={#AppVersion}
AppVerName=rksubby V3 {#AppVersion}
AppPublisher=RKnoise
AppPublisherURL=https://github.com/rknoise/rksubbyV2
AppSupportURL=https://github.com/rknoise/rksubbyV2/issues
DefaultDirName={localappdata}\Programs\rksubby V3
DefaultGroupName=rksubby V3
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutDir}
OutputBaseFilename=rksubby_V3_Setup
SetupIconFile=..\rkv3\assets\rksubby.ico
UninstallDisplayIcon={app}\rksubby.exe
UninstallDisplayName=rksubby V3
Compression=lzma2/ultra64
SolidCompression=yes
LZMANumBlockThreads=8
LZMAUseSeparateProcess=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
MinVersion=10.0.17763

[Languages]
Name: "hu"; MessagesFile: "compiler:Languages\Hungarian.isl"

[Tasks]
Name: "desktopicon"; Description: "Ikon az asztalon"; GroupDescription: "Parancsikonok:"

[Files]
Source: "{#SrcDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "OLVASS_EL.txt"; DestDir: "{app}"; Flags: ignoreversion isreadme

[Icons]
Name: "{autoprograms}\rksubby V3"; Filename: "{app}\rksubby.exe"; Comment: "Xbox kép 1080p120, felvétel, élő magyar felirat"
Name: "{autodesktop}\rksubby V3"; Filename: "{app}\rksubby.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\rksubby.exe"; Description: "rksubby V3 indítása"; Flags: nowait postinstall skipifsilent

[Code]
function HasDevice(Pattern: String): Boolean;
var
  Code: Integer;
begin
  Result := Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
    '-NoProfile -ExecutionPolicy Bypass -Command "if (Get-CimInstance Win32_PnPEntity | Where-Object { $_.Name -match ''' +
    Pattern + ''' }) { exit 0 } else { exit 1 }"', '', SW_HIDE, ewWaitUntilTerminated, Code) and (Code = 0);
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Msg: String;
begin
  if CurStep <> ssPostInstall then
    exit;
  Msg := '';
  if not HasDevice('GC573|Live Gamer|AVerMedia|Elgato|Capture') then
    Msg := Msg + '• Nem találok capture kártyát (pl. AVerMedia Live Gamer 4K / GC573). Az illesztőprogramját ' +
      'a gyártó oldaláról telepítsd – ezt nem lehet a telepítőbe csomagolni.' + #13#10#13#10;
  if not FileExists(ExpandConstant('{sys}\nvidia-smi.exe')) then
    Msg := Msg + '• Nem találok NVIDIA videokártyát. A program elindul, de a felvétel processzorral kódol, ' +
      'a beszédfelismerés pedig lassabb lesz.' + #13#10#13#10;
  if (Msg <> '') and (not WizardSilent) then
    MsgBox('Az rksubby V3 telepítve. Figyelem:' + #13#10#13#10 + Msg, mbInformation, MB_OK);
end;
