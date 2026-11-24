#ifndef PayloadDir
  #error Pass the directory containing the verified NodeCompass preview as /DPayloadDir=...
#endif

[Setup]
AppId={{6DDD4790-6A92-4BA2-B13D-5CFF231343FA}
AppName=节点罗盘
AppVersion=0.3.1
VersionInfoVersion=0.3.1.0
VersionInfoDescription=节点罗盘安装程序
DefaultDirName={localappdata}\Programs\NodeCompass
DefaultGroupName=节点罗盘
DisableProgramGroupPage=yes
DisableDirPage=no
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
UninstallDisplayName=节点罗盘
UninstallDisplayIcon={app}\NodeCompass.exe
SetupIconFile=..\src\proxy_manager\assets\app.ico
OutputBaseFilename=NodeCompass-0.3.1-Setup-win-x64
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
ShowLanguageDialog=no
UsePreviousTasks=no

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式："; Flags: unchecked
Name: "autostart"; Description: "开机自启动（登录 Windows 后启动）"; GroupDescription: "启动选项："; Flags: unchecked

[Files]
Source: "{#PayloadDir}\NodeCompass.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#PayloadDir}\IP2Location-Python-LICENSE.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\节点罗盘"; Filename: "{app}\NodeCompass.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\节点罗盘"; Filename: "{app}\NodeCompass.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\NodeCompass.exe"; Description: "启动节点罗盘"; WorkingDir: "{app}"; Flags: postinstall nowait skipifsilent unchecked

[Code]
const
  StartupKey = 'Software\Microsoft\Windows\CurrentVersion\Run';
  StartupValue = 'ProxyManager';

function QuotePath(const Path: String): String;
begin
  { Match desktop_startup.py; these are absolute file/directory paths, not shell fragments. }
  if (Pos(' ', Path) > 0) or (Pos(#9, Path) > 0) then
    Result := '"' + Path + '"'
  else
    Result := Path;
end;

function StartupCommand(): String;
begin
  Result := QuotePath(ExpandConstant('{app}\NodeCompass.exe')) + ' --data-dir ' +
    QuotePath(ExpandConstant('{localappdata}\ProxyManager'));
end;

procedure RemoveOwnStartup();
var
  Existing: String;
begin
  { Do not remove startup entries pointing to a portable copy or a different profile. }
  if RegQueryStringValue(HKCU, StartupKey, StartupValue, Existing) and
     (CompareText(Existing, StartupCommand()) = 0) then
    if not RegDeleteValue(HKCU, StartupKey, StartupValue) then
      Log('Could not remove this installation''s startup entry.');
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if WizardIsTaskSelected('autostart') and (Length(StartupCommand()) > 260) then
    Result := '安装路径太长，无法设置开机自启动。请选择较短的安装路径，或取消勾选开机自启动。';
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    if WizardIsTaskSelected('autostart') then
    begin
      if not RegWriteStringValue(HKCU, StartupKey, StartupValue, StartupCommand()) then
        SuppressibleMsgBox('程序已安装，但未能设置开机自启动。稍后可在系统设置中重试。',
          mbError, MB_OK, IDOK);
    end
    else
      RemoveOwnStartup();
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    RemoveOwnStartup();
end;
