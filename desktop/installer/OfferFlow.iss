; ============================================================
; OfferFlow 求职工作台 —— Windows 安装程序脚本
; 工具：Inno Setup 6.7.3
; 产物：OfferFlow-Setup.exe（文件名固定，升级覆盖同一链接）
; 特性：中文向导 / 免管理员（装到用户目录）/ 桌面与开始菜单快捷方式
;       / 可选开机启动 / 升级自动识别旧目录（覆盖升级，不再让用户重选目录）
; ------------------------------------------------------------
; 【为什么升级会变成"重新选目录 + 提示文件夹已存在"】
;   Inno Setup 判断「本机装没装过」默认只看注册表卸载项
;   {7E8C1D3A-...}_is1。该项在真实环境里会丢：安全软件清理注册表、
;   非正常卸载、以及「卸载时程序还在运行 → 删文件被占用 → 卸载中断」
;   都会只留下残留目录而没有任何注册表记录。
;   此时 Inno 误判为全新安装 → 重新弹出「选择安装位置」页 →
;   用户选到旧目录 → 再弹「文件夹已存在」确认框。
;
; 【对策：七路探测，任意一路命中即按升级处理】
;   1) HKCU 卸载项 InstallLocation
;   2) HKLM 卸载项 InstallLocation
;   3) HKCU\Software\OfferFlow\InstallPath   （本安装程序写入）
;   4) %LOCALAPPDATA%\OfferFlow\install_path.txt
;      （本安装程序写一份 + 程序每次启动再写一份，互为兜底）
;   5) HKCU ...\App Paths\OfferFlow.exe      （本安装程序写入）
;   6) 常见默认位置（用户目录 / Program Files / Program Files (x86)）
;   7) 盘符扫描 C:~H:\ 下的常见安装目录（兜底，最后一道防线）
;   命中后：跳过目录页、目录框锁定只读、直接覆盖升级；数据与登录态保留。
;
;   1/3/5 由安装程序负责写入 → 只要装机走的是本安装程序，
;   下次升级必然命中，不再依赖任何单一注册表项。
;
; 【排查】安装向导加 /LOG="x.log" 运行，日志里能看到：
;   "检测到已有安装 -> <目录>（命中：N-...）" 以及 "跳过「选择安装位置」页"
; ============================================================

#define MyAppName "OfferFlow 求职工作台"
#define MyAppVersion "2.5.6"
#define MyAppPublisher "OfferFlow"
#define MyAppExeName "OfferFlow.exe"

[Setup]
; 固定 AppId：升级安装靠它识别为同一应用（覆盖安装、保留开始菜单项）
AppId={{7E8C1D3A-4B5F-4A2C-9D1E-3F6B8C0A2D4E}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} v{#MyAppVersion}
AppPublisher={#MyAppPublisher}

; 默认装到用户目录（VS Code / Chrome 同款），免管理员、双击即装
DefaultDirName={localappdata}\Programs\OfferFlow
DefaultGroupName=OfferFlow
; 不显示「开始菜单文件夹」选择页，用固定分组
DisableProgramGroupPage=yes

; 安装器与卸载图标
SetupIconFile=..\icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}

; 免管理员权限；用户若强行改到 Program Files 会弹出提权对话框
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

; 仅 64 位系统
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

; 压缩与分卷
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern

; 允许静默安装参数（/SILENT /VERYSILENT 等）——程序内「一键更新」用的就是它
OutputDir=..\release
OutputBaseFilename=OfferFlow-Setup

DisableWelcomePage=no
; auto = 检测到旧版本时自动隐藏目录页；再叠加 [Code] 里的 ShouldSkipPage 双保险
DisableDirPage=auto

[Languages]
; 临时规避：本机 Inno Setup (D:\tools\InnoSetup6) 缺 ChineseSimplified.isl，先出英文包；
; 补回中文需将 ChineseSimplified.isl 放入 D:\tools\InnoSetup6\Languages\ 后改回下一行
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
; 附加图标：桌面快捷方式（默认勾选）、开机启动（默认不勾）
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "autostart"; Description: "开机自动启动 OfferFlow"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; 打包 PyInstaller onedir 整个产物（exe + _internal），保持目录结构
Source: "..\dist\OfferFlow\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Registry]
; 自留一份安装位置登记（卸载项丢失时仍能识别旧版本）
Root: HKCU; Subkey: "Software\OfferFlow"; ValueType: string; ValueName: "InstallPath"; ValueData: "{app}"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\OfferFlow"; ValueType: string; ValueName: "Version"; ValueData: "{#MyAppVersion}"
; 注册到 Windows 标准「程序路径」表：既是又一路探测依据，
; 也让「运行」对话框里直接输入 OfferFlow.exe 能启动
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\App Paths\{#MyAppExeName}"; ValueType: string; ValueName: ""; ValueData: "{app}\{#MyAppExeName}"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\App Paths\{#MyAppExeName}"; ValueType: string; ValueName: "Path"; ValueData: "{app}"

[Icons]
; 开始菜单（总是创建）
Name: "{autoprograms}\OfferFlow\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
; 开始菜单卸载项
Name: "{autoprograms}\OfferFlow\卸载 OfferFlow"; Filename: "{uninstallexe}"
; 桌面快捷方式（可选）
Name: "{autodesktop}\OfferFlow"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
; 开机启动（可选，写入当前用户启动文件夹，无需管理员）
Name: "{userstartup}\OfferFlow"; Filename: "{app}\{#MyAppExeName}"; Tasks: autostart

[Run]
; 安装完成后可选「立即运行」。
; 不加 skipifsilent：程序内「一键更新」用静默方式升级完，靠这一条自动拉起新版。
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall

[InstallDelete]
; 升级时先把旧的 _internal 整体清掉再全量重铺。
; 原因：历史包结构差异极大（v2.0.x 未瘦身 468MB、v2.1.x 含 QtWebEngine 约 470MB，
; 现在的原生版只有 145MB），只做同名覆盖会残留几百 MB 早已不用的 Qt / 语言包文件。
; 删除前 Inno 会先按 CloseApplications 关闭正在运行的 OfferFlow，故此时文件不再被占用。
Type: filesandordirs; Name: "{app}\_internal"

[UninstallDelete]
; 卸载时清理可能残留的运行日志/崩溃日志（不含用户数据，用户数据在 %LOCALAPPDATA%\OfferFlow）
Type: files; Name: "{app}\crash.log"
Type: files; Name: "{app}\*.log"

[Code]
var
  PrevDir: String;       { 探测到的旧安装目录；空串 = 未装过 }
  UpgradeMode: Boolean;  { True = 升级模式：跳过目录页、锁定原目录 }
  DetectReason: String;  { 命中的是第几路，写进安装日志便于排查 }

{ 去掉结尾反斜杠，便于比较与拼接 }
function TrimSlash(D: String): String;
begin
  Result := Trim(D);
  while (Length(Result) > 1) and (Result[Length(Result)] = '\') do
    Delete(Result, Length(Result), 1);
end;

{ 该目录是否为一个可用的 OfferFlow 安装（以主程序存在为准） }
function IsOfferFlowDir(D: String): Boolean;
begin
  Result := False;
  if Trim(D) = '' then
    Exit;
  { 注意：这里不能写 {#MyAppExeName}，ISPP 在 [Code] 段会展开成带引号的形式 }
  Result := FileExists(TrimSlash(D) + '\OfferFlow.exe');
end;

{ 命中即锁定，并记下命中来源 }
function Hit(D, Reason: String): Boolean;
begin
  Result := False;
  if IsOfferFlowDir(D) then
  begin
    PrevDir := TrimSlash(D);
    DetectReason := Reason;
    Result := True;
  end;
end;

{ 第 7 路兜底：扫描 C:~\ 下的常见安装目录 }
function ScanDrives(): String;
var
  I, J: Integer;
  Root, Candidate: String;
  Subs: array[0..8] of String;
begin
  Result := '';
  Subs[0] := '\offer\OfferFlow';
  Subs[1] := '\OfferFlow';
  Subs[2] := '\Program Files\OfferFlow';
  Subs[3] := '\Program Files (x86)\OfferFlow';
  Subs[4] := '\apps\OfferFlow';
  Subs[5] := '\software\OfferFlow';
  Subs[6] := '\soft\OfferFlow';
  Subs[7] := '\tools\OfferFlow';
  Subs[8] := '\dev\OfferFlow';
  for I := Ord('C') to Ord('H') do
  begin
    Root := Chr(I) + ':';
    if not DirExists(Root + '\') then
      Continue;
    for J := 0 to 8 do
    begin
      Candidate := Root + Subs[J];
      if IsOfferFlowDir(Candidate) then
      begin
        Result := TrimSlash(Candidate);
        DetectReason := '7-盘符扫描命中 ' + Result;
        Exit;
      end;
    end;
  end;
end;

{ 七路探测旧安装目录，返回空串表示未安装过 }
function DetectPrevDir(): String;
var
  V: String;
  P: String;
  Lines: TArrayOfString;
begin
  Result := '';
  PrevDir := '';
  DetectReason := '';

  { 1) Inno 标准卸载项（HKCU，免管理员安装写在这里） }
  V := '';
  if RegQueryStringValue(HKCU,
       'Software\Microsoft\Windows\CurrentVersion\Uninstall\{7E8C1D3A-4B5F-4A2C-9D1E-3F6B8C0A2D4E}_is1',
       'InstallLocation', V) then
    if Hit(V, '1-卸载项 InstallLocation(HKCU)') then
    begin
      Result := PrevDir;
      Exit;
    end;

  { 2) Inno 标准卸载项（HKLM，管理员安装写在这里） }
  V := '';
  if RegQueryStringValue(HKLM,
       'Software\Microsoft\Windows\CurrentVersion\Uninstall\{7E8C1D3A-4B5F-4A2C-9D1E-3F6B8C0A2D4E}_is1',
       'InstallLocation', V) then
    if Hit(V, '2-卸载项 InstallLocation(HKLM)') then
    begin
      Result := PrevDir;
      Exit;
    end;

  { 3) 安装程序自留的登记值（[Registry] 段写入，不依赖卸载项） }
  V := '';
  if RegQueryStringValue(HKCU, 'Software\OfferFlow', 'InstallPath', V) then
    if Hit(V, '3-HKCU\Software\OfferFlow\InstallPath') then
    begin
      Result := PrevDir;
      Exit;
    end;

  { 4) 安装程序 / 程序自己写的安装路径自述文件 }
  P := ExpandConstant('{localappdata}\OfferFlow\install_path.txt');
  if FileExists(P) then
  begin
    V := '';
    if LoadStringsFromFile(P, Lines) and (GetArrayLength(Lines) > 0) then
      V := Lines[0];
    if Hit(V, '4-install_path.txt') then
    begin
      Result := PrevDir;
      Exit;
    end;
  end;

  { 5) Windows 标准「程序路径」表（[Registry] 段写入） }
  V := '';
  if RegQueryStringValue(HKCU,
       'Software\Microsoft\Windows\CurrentVersion\App Paths\OfferFlow.exe', '', V) then
    if Hit(ExtractFileDir(V), '5-App Paths\OfferFlow.exe') then
    begin
      Result := PrevDir;
      Exit;
    end;

  { 6) 常见默认位置 }
  V := ExpandConstant('{localappdata}\Programs\OfferFlow');
  if Hit(V, '6-默认位置 ' + V) then
  begin
    Result := PrevDir;
    Exit;
  end;
  V := ExpandConstant('{pf}\OfferFlow');
  if Hit(V, '6-默认位置 ' + V) then
  begin
    Result := PrevDir;
    Exit;
  end;
  V := ExpandConstant('{pf32}\OfferFlow');
  if Hit(V, '6-默认位置 ' + V) then
  begin
    Result := PrevDir;
    Exit;
  end;

  { 7) 盘符扫描兜底 }
  Result := ScanDrives();
end;

function InitializeSetup(): Boolean;
begin
  PrevDir := DetectPrevDir();
  UpgradeMode := PrevDir <> '';
  if UpgradeMode then
    Log('OfferFlow 安装程序：检测到已有安装 -> ' + PrevDir +
        '（命中第 ' + DetectReason + ' 路），按【升级安装】处理')
  else
    Log('OfferFlow 安装程序：七路探测均未命中，按【全新安装】处理');
  Result := True;
end;

procedure InitializeWizard();
begin
  if UpgradeMode then
  begin
    { 锁定为原安装目录：既不重复让用户选，也不会再弹「文件夹已存在」 }
    WizardForm.DirEdit.Text := PrevDir;
    WizardForm.DirEdit.ReadOnly := True;
    WizardForm.DirBrowseButton.Enabled := False;
  end;
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := False;
  if UpgradeMode and (PageID = wpSelectDir) then
  begin
    Result := True;
    Log('OfferFlow 安装程序：升级模式 -> 跳过「选择安装位置」页，直接覆盖 ' + PrevDir);
  end;
end;

{ 安装收尾：由安装程序自己登记一次安装路径。
  这样即使程序装完从未启动过，install_path.txt 也已存在，
  下次升级必定能命中。程序启动时还会再写一次，两处互为兜底。 }
procedure CurStepChanged(CurStep: TSetupStep);
var
  Folder, Target: String;
begin
  if CurStep = ssPostInstall then
  begin
    Folder := ExpandConstant('{localappdata}\OfferFlow');
    if ForceDirectories(Folder) then
    begin
      Target := Folder + '\install_path.txt';
      if SaveStringToFile(Target, ExpandConstant('{app}'), False) then
        Log('OfferFlow 安装程序：已登记安装目录 -> ' + Target)
      else
        Log('OfferFlow 安装程序：登记安装目录失败 -> ' + Target);
    end;
  end;
end;

function UpdateReadyMemo(Space, NewLine, MemoUserInfoInfo, MemoDirInfo, MemoTypeInfo,
  MemoComponentsInfo, MemoGroupInfo, MemoTasksInfo: String): String;
begin
  if UpgradeMode then
  begin
    Result := '升级安装：检测到本机已安装 OfferFlow，将直接覆盖升级，不需要重新选择文件夹。' + NewLine +
              Space + '安装目录：' + PrevDir + NewLine +
              Space + '账号登录状态与本地数据保留不变' + NewLine + NewLine;
    Result := Result + MemoTasksInfo;
  end
  else
    Result := MemoDirInfo + NewLine + NewLine + MemoTasksInfo;
end;
