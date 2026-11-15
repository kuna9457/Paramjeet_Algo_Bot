; Nexus SMC Bot installer. Compiled by build.py:  ISCC /DAppVersion=1.0.0 installer.iss
; Per-user install (no admin rights needed):  %LOCALAPPDATA%\Programs\NexusBot\{python,app}
; Re-running a newer installer = update. The client's .env, logs, data_cache and config\settings.yaml are kept.

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

[Setup]
AppId={{7D3E5C1A-4B2F-4E8A-9C61-5A0F2B8D6E41}
AppName=Nexus SMC Bot
AppVersion={#AppVersion}
AppPublisher=Nexus SMC Bot
DefaultDirName={localappdata}\Programs\NexusBot
DefaultGroupName=Nexus SMC Bot
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=dist
OutputBaseFilename=NexusBot_Setup_{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName=Nexus SMC Bot
CloseApplications=no

[Tasks]
Name: "desktopicon"; Description: "Create a &Desktop shortcut"; Flags: checkedonce
Name: "startup"; Description: "Open the dashboard automatically when Windows starts"; Flags: unchecked

[Dirs]
Name: "{app}\app\logs"
Name: "{app}\app\data_cache"

[InstallDelete]
; code + runtime are fully replaced on every install; user data (.env, logs, data_cache, settings.yaml) is NOT listed here
Type: filesandordirs; Name: "{app}\python"
Type: filesandordirs; Name: "{app}\app\bot"
Type: filesandordirs; Name: "{app}\app\backtest"

[Files]
Source: "build\stage\python\*"; DestDir: "{app}\python"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "build\stage\app\*"; DestDir: "{app}\app"; Excludes: "config\settings.yaml"; Flags: recursesubdirs createallsubdirs ignoreversion
; default settings are only written on a fresh install - later updates never overwrite the client's settings
Source: "build\stage\app\config\settings.yaml"; DestDir: "{app}\app\config"; Flags: onlyifdoesntexist

[Icons]
Name: "{autodesktop}\Nexus SMC Bot"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\app\launcher.py"""; WorkingDir: "{app}\app"; Tasks: desktopicon
Name: "{group}\Nexus SMC Bot"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\app\launcher.py"""; WorkingDir: "{app}\app"
Name: "{group}\Stop Nexus dashboard"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\app\launcher.py"" stop"; WorkingDir: "{app}\app"
Name: "{group}\Open logs folder"; Filename: "{app}\app\logs"
Name: "{userstartup}\Nexus SMC Bot"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\app\launcher.py"""; WorkingDir: "{app}\app"; Tasks: startup

[Run]
; normal install: offer to open it. Silent (auto-update): reopen it automatically.
Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\app\launcher.py"""; WorkingDir: "{app}\app"; Description: "Open Nexus SMC Bot now"; Flags: nowait postinstall skipifsilent
Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\app\launcher.py"""; WorkingDir: "{app}\app"; Flags: nowait; Check: WizardSilent

[UninstallRun]
Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\app\launcher.py"" stop --silent"; WorkingDir: "{app}\app"; Flags: runhidden; RunOnceId: "StopDashboard"
