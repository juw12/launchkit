; LaunchKit for Windows - per-user installer (no admin rights). build.py compiles it with:
;   iscc /DAppVersion=1.0.0 /DSourceDir=<dist\LaunchKit> /DOutputDir=<dist> /DIconFile=<build\launchkit.ico> launchkit.iss
; Installs to %LOCALAPPDATA%\Programs\LaunchKit, adds LaunchKit to the Start menu (and optionally the desktop) and lists it
; in Settings > Apps. The window keeps its data (what you saved in it) in %LOCALAPPDATA%\LaunchKit, which
; uninstalling leaves in place so a reinstall finds it again.

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "dist\LaunchKit"
#endif
#ifndef OutputDir
  #define OutputDir "dist"
#endif
#ifndef IconFile
  #define IconFile "build\launchkit.ico"
#endif

[Setup]
AppId={{E81247BA-698B-569E-A6DE-E1E1C366EEFB}
AppName=LaunchKit
AppVersion={#AppVersion}
AppVerName=LaunchKit {#AppVersion}
AppPublisher=Juwen Lie
AppPublisherURL=https://juw12.github.io/launchkit/
AppSupportURL=https://github.com/juw12/launchkit
DefaultDirName={autopf}\LaunchKit
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir={#OutputDir}
OutputBaseFilename=LaunchKit-Windows-Setup
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\LaunchKit.exe
UninstallDisplayName=LaunchKit
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=no
SetupLogging=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\LaunchKit"; Filename: "{app}\LaunchKit.exe"; Comment: "Promote an app before it ships"
Name: "{userdesktop}\LaunchKit"; Filename: "{app}\LaunchKit.exe"; Comment: "Promote an app before it ships"; Tasks: desktopicon

[Run]
Filename: "{app}\LaunchKit.exe"; Description: "{cm:LaunchProgram,LaunchKit}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{app}\LaunchKit.exe"; Parameters: "--stop"; Flags: runhidden waituntilterminated; RunOnceId: "StopLaunchKit"

[Code]
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  Result := '';
  { Updating: close an open LaunchKit window first so its files can be replaced. }
  if FileExists(ExpandConstant('{app}\LaunchKit.exe')) then
    Exec(ExpandConstant('{app}\LaunchKit.exe'), '--stop', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;
